"""Calendar agenda view: fetches upcoming events from a CalDAV server and
renders a 7-day agenda grouped by day, with times, titles, and optional
location.

Uses the same CalDAV credentials as the tasks view (``CALDAV_URL``,
``CALDAV_USERNAME``, ``CALDAV_PASSWORD``).  An optional ``CALDAV_CALENDARS``
env var (comma-separated) filters which calendars to include; by default
all calendars that support VEVENT are shown.

Recurring events (RRULE, RDATE) are expanded using the
``recurring-ical-events`` package so weekly meetings, birthdays, etc.
appear correctly in the 7-day window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import icalendar
import recurring_ical_events
from PIL import Image, ImageDraw

from display_thingy.views import BaseView, registry
from display_thingy.views._caldav import discover_collections, parse_calendar_responses
from display_thingy.views._render import (
    BLACK,
    HEADER_HEIGHT,
    USER_AGENT,
    WHITE,
    draw_border,
    draw_header,
    draw_overflow_bar,
    render_error,
    truncate_text,
)
from display_thingy.views._render import (
    font as _font,
)

if TYPE_CHECKING:
    from display_thingy.config import Settings

log = logging.getLogger(__name__)


# ── Constants ──

LOOKAHEAD_DAYS = 7


# ── Data model ──


@dataclass
class Event:
    """A single calendar event parsed from a VEVENT component."""

    summary: str
    start: datetime | date
    end: datetime | date | None
    location: str = ""
    all_day: bool = False


# ── CalDAV client ──


def _make_calendar_query_xml(start: date, end: date) -> str:
    """Build a CalDAV REPORT body that fetches VEVENTs within a time range.

    The time-range filter tells the server to only return events that
    overlap with [start, end).  This avoids fetching the entire calendar
    history -- we only need the next 7 days.

    The start/end are formatted as UTC timestamps in iCalendar format
    (``YYYYMMDDTHHMMSSZ``).
    """
    # Convert date boundaries to UTC datetime strings for the CalDAV
    # time-range filter.  We use midnight-to-midnight in UTC, which is
    # slightly broader than the local-time day boundaries but ensures we
    # don't miss events near day boundaries in any timezone.
    start_str = start.strftime("%Y%m%dT000000Z")
    end_str = end.strftime("%Y%m%dT000000Z")
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start_str}" end="{end_str}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""


def _fetch_vevents(
    client: httpx.Client,
    collection_url: str,
    start: date,
    end: date,
) -> list[icalendar.Calendar]:
    """Fetch raw iCalendar data from a single calendar collection.

    Returns a list of parsed Calendar objects (each typically containing
    one VEVENT).  Recurrence expansion happens in the caller so that we
    have access to the full RRULE context.
    """
    query_xml = _make_calendar_query_xml(start, end)
    response = client.request(
        "REPORT",
        collection_url,
        content=query_xml,
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": "1",
        },
    )
    response.raise_for_status()

    return parse_calendar_responses(response.text)


def _parse_vevent(component: icalendar.cal.Component) -> Event | None:
    """Parse a VEVENT component into an Event.

    Returns None for cancelled events.
    """
    status = str(component.get("STATUS", "")).upper()
    if status == "CANCELLED":
        return None

    summary = str(component.get("SUMMARY", "")).strip()
    if not summary:
        return None

    # Parse start time.  DTSTART can be a date (all-day) or datetime.
    dt_start_raw = component.get("DTSTART")
    if dt_start_raw is None:
        return None
    dt_start = dt_start_raw.dt

    all_day = isinstance(dt_start, date) and not isinstance(dt_start, datetime)

    # Parse end time.  May be absent for all-day single-day events.
    dt_end: datetime | date | None = None
    dt_end_raw = component.get("DTEND")
    if dt_end_raw is not None:
        dt_end = dt_end_raw.dt

    location = str(component.get("LOCATION", "")).strip()

    return Event(
        summary=summary,
        start=dt_start,
        end=dt_end,
        location=location,
        all_day=all_day,
    )


def _event_sort_key(event: Event) -> tuple[int, datetime, str]:
    """Sort events: all-day first, then by start time, then alphabetically."""
    if event.all_day:
        # All-day events sort before timed events on the same day.
        assert isinstance(event.start, date)
        dt = datetime(event.start.year, event.start.month, event.start.day, tzinfo=timezone.utc)
        return (0, dt, event.summary.lower())
    else:
        start = event.start
        if isinstance(start, datetime):
            # Normalise to UTC for consistent sorting.
            if start.tzinfo is not None:
                start = start.astimezone(timezone.utc)
            else:
                start = start.replace(tzinfo=timezone.utc)
        else:
            start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        return (1, start, event.summary.lower())


def _event_date_key(event: Event) -> date:
    """Extract the local date of an event for day-grouping."""
    start = event.start
    if isinstance(start, datetime):
        # Convert to local time for grouping.  We use the system
        # timezone via astimezone() with no argument.
        return start.astimezone().date()
    return start


def fetch_events(settings: Settings) -> dict[date, list[Event]]:
    """Fetch upcoming events from the configured CalDAV server.

    Returns a dict mapping each date (that has events) to a sorted list
    of events on that date.  The window covers today through today + 6
    days (7 days total).
    """
    if not settings.caldav_url or not settings.caldav_username:
        raise ValueError(
            "CalDAV not configured. Set CALDAV_URL, CALDAV_USERNAME, "
            "and CALDAV_PASSWORD environment variables."
        )

    base_url = settings.caldav_url.rstrip("/")
    today = date.today()
    end = today + timedelta(days=LOOKAHEAD_DAYS)

    with httpx.Client(
        auth=(settings.caldav_username, settings.caldav_password),
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        calendars = discover_collections(
            client,
            base_url,
            settings.caldav_username,
            settings.caldav_calendars,
            "VEVENT",
        )

        if not calendars:
            log.warning("No calendars found on %s", base_url)
            return {}

        all_events: list[Event] = []
        for cal_name, collection_url in calendars:
            log.info("Fetching events from '%s'", cal_name)
            raw_cals = _fetch_vevents(client, collection_url, today, end)

            # Expand recurring events within our time window.  The
            # recurring-ical-events library handles RRULE, RDATE, and
            # EXDATE correctly.  We process each raw calendar object
            # individually (each contains one event series from the
            # server).
            for raw_cal in raw_cals:
                try:
                    expanded = recurring_ical_events.of(raw_cal).between(today, end)
                except Exception:
                    log.warning("Failed to expand recurrence, skipping", exc_info=True)
                    continue

                for component in expanded:
                    event = _parse_vevent(component)
                    if event is not None:
                        all_events.append(event)

            log.info("  Got %d events in window", len(all_events))

    # Group events by date.
    by_date: dict[date, list[Event]] = {}
    for event in all_events:
        day = _event_date_key(event)
        # Only include events within our window (recurrence expansion
        # may sometimes produce events just outside the range).
        if today <= day < end:
            by_date.setdefault(day, []).append(event)

    # Sort events within each day.
    for day_events in by_date.values():
        day_events.sort(key=_event_sort_key)

    total = sum(len(evts) for evts in by_date.values())
    log.info("Total: %d events across %d days", total, len(by_date))
    return by_date


# ── Renderer ──

# Layout constants (view-specific; standard constants like BLACK, WHITE,
# HEADER_HEIGHT come from _render).
LEFT_PADDING = 15
RIGHT_PADDING = 15
OVERFLOW_HEIGHT = 30
DAY_HEADER_HEIGHT = 28
EVENT_ROW_HEIGHT = 26
TIME_COLUMN_WIDTH = 70  # width reserved for "09:00" / "All day"


def _format_day_header(day: date) -> str:
    """Format a date as a day group header."""
    today = date.today()
    delta = (day - today).days

    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"

    # Show day name and date, e.g. "Fri, Mar 21"
    return day.strftime("%a, %b %-d")


def _format_event_time(event: Event) -> str:
    """Format the time portion of an event for display."""
    if event.all_day:
        return "All day"

    start = event.start
    if isinstance(start, datetime):
        # Convert to local time for display.
        local = start.astimezone()
        return local.strftime("%-H:%M")

    return ""


def render_agenda(events_by_date: dict[date, list[Event]], width: int, height: int) -> Image.Image:
    """Render the agenda into an 800x480 1-bit image.

    Layout (top to bottom):
    - Header bar (35px): "Agenda" title + date range
    - Day groups: bold date header + event rows beneath
      - Each event row: time (left column) + title + optional location
    - Overflow bar (30px): "+ N more events" if truncated
    - 2px outer border
    """
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    today = date.today()
    end = today + timedelta(days=LOOKAHEAD_DAYS - 1)

    # Date range label, e.g. "Mar 19 - 25" or "Mar 28 - Apr 3".
    if today.month == end.month:
        range_str = f"{today.strftime('%b %-d')} \u2013 {end.day}"
    else:
        range_str = f"{today.strftime('%b %-d')} \u2013 {end.strftime('%b %-d')}"

    draw_header(draw, width, "Agenda", range_str)

    # ── Empty state ──

    total_events = sum(len(evts) for evts in events_by_date.values())

    if total_events == 0:
        empty_font = _font("Regular", 20)
        msg = "No upcoming events"
        msg_bbox = draw.textbbox((0, 0), msg, font=empty_font)
        msg_w = msg_bbox[2] - msg_bbox[0]
        msg_h = msg_bbox[3] - msg_bbox[1]
        usable_h = height - HEADER_HEIGHT - OVERFLOW_HEIGHT
        draw.text(
            ((width - msg_w) // 2, HEADER_HEIGHT + (usable_h - msg_h) // 2),
            msg,
            fill=BLACK,
            font=empty_font,
        )
        draw_border(draw, width, height)
        return img

    # ── Compute layout ──
    #
    # We need to figure out how many events fit in the available space.
    # First pass: measure what we need, then decide on font size and
    # whether to truncate.

    usable_bottom = height - 4 - OVERFLOW_HEIGHT  # 4px inner margin
    available_h = usable_bottom - HEADER_HEIGHT

    # Sort the days chronologically.
    sorted_days = sorted(events_by_date.keys())

    # Try font sizes from largest to smallest.  For each size, compute
    # the total height needed and check if it fits.
    font_sizes = [16, 14, 12]
    best_font_size = font_sizes[-1]
    best_day_h = DAY_HEADER_HEIGHT
    best_row_h = EVENT_ROW_HEIGHT

    for font_size in font_sizes:
        test_font = _font("Regular", font_size)
        row_h = draw.textbbox((0, 0), "Ay", font=test_font)[3] + 8
        day_h = draw.textbbox((0, 0), "Ay", font=_font("Bold", font_size))[3] + 10

        total_h = 0
        for day in sorted_days:
            total_h += day_h  # day header
            total_h += len(events_by_date[day]) * row_h  # event rows

        if total_h <= available_h:
            best_font_size = font_size
            best_day_h = day_h
            best_row_h = row_h
            break

    # ── Draw day groups ──

    event_font = _font("Regular", best_font_size)
    event_bold_font = _font("Bold", best_font_size)
    time_font = _font("Regular", best_font_size - 2)
    location_font = _font("Regular", best_font_size - 3)
    day_header_font = _font("Bold", best_font_size)

    y = HEADER_HEIGHT + 4
    overflow_count = 0
    hit_bottom = False

    for day in sorted_days:
        if hit_bottom:
            overflow_count += len(events_by_date[day])
            continue

        # Check if the day header fits.
        if y + best_day_h > usable_bottom:
            overflow_count += len(events_by_date[day])
            hit_bottom = True
            continue

        # Draw day header.
        day_label = _format_day_header(day)
        draw.text((LEFT_PADDING, y + 2), day_label, font=day_header_font, fill=BLACK)

        # Underline the day header.
        underline_y = y + best_day_h - 4
        draw.line(
            [(LEFT_PADDING, underline_y), (width - RIGHT_PADDING, underline_y)],
            fill=BLACK,
            width=1,
        )
        y += best_day_h

        # Draw events for this day.
        for event in events_by_date[day]:
            if y + best_row_h > usable_bottom:
                overflow_count += 1
                hit_bottom = True
                continue

            # Time column.
            time_str = _format_event_time(event)
            draw.text(
                (LEFT_PADDING + 4, y + 2),
                time_str,
                font=time_font,
                fill=BLACK,
            )

            # Event title.
            text_x = LEFT_PADDING + TIME_COLUMN_WIDTH
            max_title_w = width - text_x - RIGHT_PADDING

            # If there's a location, reserve space for it after the
            # title (separated by " · ").
            location_suffix = ""
            location_w = 0
            if event.location:
                location_suffix = f" · {event.location}"
                location_w = draw.textbbox((0, 0), location_suffix, font=location_font)[2]
                # Only show location if it doesn't take more than 40%
                # of the available width.
                if location_w > max_title_w * 0.4:
                    location_suffix = ""
                    location_w = 0

            title_max_w = int(max_title_w - location_w)
            title = event.summary

            # Use bold for all-day events to make them stand out.
            title_font = event_bold_font if event.all_day else event_font

            title = truncate_text(draw, title, title_font, title_max_w)

            draw.text((text_x, y + 2), title, font=title_font, fill=BLACK)

            # Location suffix (smaller, after title).
            if location_suffix:
                actual_title_w = draw.textbbox((0, 0), title, font=title_font)[2]
                loc_x = text_x + actual_title_w
                # Vertically align the smaller location text with the
                # title baseline.
                loc_y_offset = best_font_size - (best_font_size - 3)
                draw.text(
                    (loc_x, y + 2 + loc_y_offset),
                    location_suffix,
                    font=location_font,
                    fill=BLACK,
                )

            y += best_row_h

    # ── Overflow indicator ──
    draw_overflow_bar(draw, width, height, overflow_count, "event", bar_height=OVERFLOW_HEIGHT)

    # ── Outer border ──
    draw_border(draw, width, height)

    return img


@registry.register
class CalendarView(BaseView):
    """CalDAV calendar agenda display."""

    name = "calendar"
    description = "Upcoming events from CalDAV"

    def render(self, width: int, height: int) -> Image.Image:
        log.info("Fetching calendar events from CalDAV server")
        try:
            events_by_date = fetch_events(self.settings)
        except ValueError as exc:
            log.error("Calendar view: %s", exc)
            return render_error(
                "Agenda",
                "Could not load calendar",
                str(exc),
                width,
                height,
            )
        except Exception as exc:
            log.error("Calendar view: %s", exc, exc_info=True)
            return render_error(
                "Agenda",
                "Could not load calendar",
                str(exc),
                width,
                height,
            )

        return render_agenda(events_by_date, width, height)
