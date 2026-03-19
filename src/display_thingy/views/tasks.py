"""Tasks view: fetches incomplete VTODO items from a CalDAV server and renders
a pending task list sorted by due date, with single-level subtask indentation.

Uses raw HTTP (httpx) with CalDAV PROPFIND/REPORT requests rather than a heavy
CalDAV client library. The iCalendar parsing is handled by the ``icalendar``
package. This approach works with any CalDAV server (Nextcloud, Radicale,
Baikal, etc.) — not just Nextcloud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import httpx
import icalendar
from PIL import Image, ImageDraw

from display_thingy.views import BaseView, registry
from display_thingy.views._caldav import discover_collections, parse_calendar_responses
from display_thingy.views._render import (
    BLACK,
    HEADER_HEIGHT,
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


# ── Data model ──

# iCalendar PRIORITY values: 1-4 = high, 5 = medium, 6-9 = low, 0 = undefined
# (RFC 5545 §3.8.1.9). Tasks.org maps its UI priorities to this scale.
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITY_NONE = "none"


def _classify_priority(ical_priority: int) -> str:
    """Map an iCalendar PRIORITY value (0-9) to a display tier."""
    if ical_priority == 0:
        return PRIORITY_NONE
    if ical_priority <= 4:
        return PRIORITY_HIGH
    if ical_priority == 5:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


# Sort key: lower number = shown first.
_PRIORITY_SORT_ORDER = {
    PRIORITY_HIGH: 0,
    PRIORITY_MEDIUM: 1,
    PRIORITY_LOW: 2,
    PRIORITY_NONE: 3,
}


@dataclass
class Task:
    """A single to-do item parsed from a VTODO component."""

    uid: str
    summary: str
    priority: str = PRIORITY_NONE
    due: date | None = None
    status: str = "NEEDS-ACTION"
    parent_uid: str | None = None
    children: list[Task] = field(default_factory=list)


# ── CalDAV client ──

# CalDAV REPORT body to fetch all VTODOs. We filter for VTODO components
# only and exclude completed tasks via the COMPLETED property not being
# defined. We intentionally avoid filtering on STATUS server-side because
# some CalDAV servers don't reliably support prop-filter on STATUS; instead
# we filter client-side after parsing, which is more portable.
_CALENDAR_QUERY_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VTODO">
        <C:prop-filter name="COMPLETED">
          <C:is-not-defined/>
        </C:prop-filter>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""


def _fetch_vtodos(
    client: httpx.Client,
    collection_url: str,
) -> list[Task]:
    """Fetch all incomplete VTODOs from a single calendar collection."""
    response = client.request(
        "REPORT",
        collection_url,
        content=_CALENDAR_QUERY_XML,
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": "1",
        },
    )
    response.raise_for_status()

    tasks: list[Task] = []
    for cal in parse_calendar_responses(response.text):
        for component in cal.walk("VTODO"):
            task = _parse_vtodo(component)
            if task is not None:
                tasks.append(task)

    return tasks


def _parse_vtodo(component: icalendar.cal.Component) -> Task | None:
    """Parse a VTODO iCalendar component into a Task.

    Returns None for completed/cancelled tasks (client-side filter for
    servers that don't fully support server-side STATUS filtering).
    """
    status = str(component.get("STATUS", "NEEDS-ACTION")).upper()
    if status in ("COMPLETED", "CANCELLED"):
        return None

    uid = str(component.get("UID", ""))
    summary = str(component.get("SUMMARY", ""))
    if not summary:
        return None

    # Parse priority (0-9 iCalendar scale).
    raw_priority = component.get("PRIORITY")
    ical_priority = int(raw_priority) if raw_priority is not None else 0
    priority = _classify_priority(ical_priority)

    # Parse due date. VTODO DUE can be either a date or a datetime.
    due_raw = component.get("DUE")
    due: date | None = None
    if due_raw is not None:
        due_val = due_raw.dt
        if isinstance(due_val, datetime):
            due = due_val.astimezone(timezone.utc).date()
        elif isinstance(due_val, date):
            due = due_val

    # Parse parent relationship for subtask linkage. The RELATED-TO property
    # with RELTYPE=PARENT (the default) points to the parent task's UID.
    parent_uid: str | None = None
    related = component.get("RELATED-TO")
    if related is not None:
        parent_uid = str(related)

    return Task(
        uid=uid,
        summary=summary,
        priority=priority,
        due=due,
        status=status,
        parent_uid=parent_uid,
    )


def _build_task_tree(tasks: list[Task]) -> list[Task]:
    """Organize tasks into a tree and return sorted top-level tasks.

    Children are attached to their parents via ``Task.children``. Only
    single-level indentation is applied: if a child's parent is not in the
    task list (e.g. the parent is already completed), the child is promoted
    to top-level. Grandchildren are similarly flattened to the child level.

    Both top-level tasks and children within each parent are sorted by
    priority (high first), then due date (earliest first, undated last),
    then alphabetically.
    """
    by_uid: dict[str, Task] = {t.uid: t for t in tasks}

    top_level: list[Task] = []
    for task in tasks:
        if task.parent_uid and task.parent_uid in by_uid:
            parent = by_uid[task.parent_uid]
            parent.children.append(task)
        else:
            top_level.append(task)

    def _sort_key(t: Task) -> tuple[int, date, str]:
        prio_order = _PRIORITY_SORT_ORDER.get(t.priority, 3)
        due_key = t.due if t.due is not None else date.max
        return (prio_order, due_key, t.summary.lower())

    top_level.sort(key=_sort_key)
    for task in top_level:
        task.children.sort(key=_sort_key)

    return top_level


def fetch_tasks(settings: Settings) -> list[Task]:
    """Fetch all incomplete tasks from the configured CalDAV server.

    Discovers task list collections, fetches VTODOs from each, builds a
    parent-child tree, and returns sorted top-level tasks with children
    attached.
    """
    if not settings.caldav_url or not settings.caldav_username:
        raise ValueError(
            "CalDAV not configured. Set CALDAV_URL, CALDAV_USERNAME, "
            "and CALDAV_PASSWORD environment variables."
        )

    base_url = settings.caldav_url.rstrip("/")

    with httpx.Client(
        auth=(settings.caldav_username, settings.caldav_password),
        timeout=20,
        headers={"User-Agent": "display-thingy/0.1 (e-paper task display)"},
        follow_redirects=True,
    ) as client:
        task_lists = discover_collections(
            client, base_url, settings.caldav_username,
            settings.caldav_task_lists, "VTODO",
        )

        if not task_lists:
            log.warning("No task lists found on %s", base_url)
            return []

        all_tasks: list[Task] = []
        for list_name, collection_url in task_lists:
            log.info("Fetching tasks from '%s'", list_name)
            tasks = _fetch_vtodos(client, collection_url)
            all_tasks.extend(tasks)
            log.info("  Got %d incomplete tasks", len(tasks))

    tree = _build_task_tree(all_tasks)
    total = sum(1 + len(t.children) for t in tree)
    log.info("Total: %d incomplete tasks (%d top-level)", total, len(tree))
    return tree


# ── Renderer ──

# Layout constants (view-specific; standard constants like BLACK, WHITE,
# HEADER_HEIGHT come from _render).
ROW_HEIGHT = 32
CHECKBOX_SIZE = 14
INDENT_WIDTH = 30
LEFT_PADDING = 15
RIGHT_PADDING = 15
OVERFLOW_HEIGHT = 30


def _flatten_for_display(tasks: list[Task]) -> list[tuple[Task, bool]]:
    """Flatten the task tree into render order: ``(task, is_subtask)`` pairs.

    Parents come first, followed immediately by their children.
    """
    result: list[tuple[Task, bool]] = []
    for task in tasks:
        result.append((task, False))
        for child in task.children:
            result.append((child, True))
    return result


def _format_due_date(due: date) -> str:
    """Format a due date for compact display, relative to today."""
    today = date.today()
    delta = (due - today).days

    if delta < -1:
        return f"{-delta}d overdue"
    if delta == -1:
        return "Yesterday"
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta < 7:
        return due.strftime("%a")  # e.g. "Wed"

    # Within the same year, omit the year.
    if due.year == today.year:
        return due.strftime("%b %-d")  # e.g. "Mar 20"
    return due.strftime("%b %-d, %Y")


def _draw_checkbox(
    draw: ImageDraw.ImageDraw, x: int, y: int, size: int, in_process: bool = False
) -> None:
    """Draw a task checkbox.

    Empty square for NEEDS-ACTION; small filled inner square for IN-PROCESS
    to indicate work has started.
    """
    draw.rectangle([(x, y), (x + size, y + size)], outline=BLACK, width=1)
    if in_process:
        inset = 3
        draw.rectangle(
            [(x + inset, y + inset), (x + size - inset, y + size - inset)],
            fill=BLACK,
        )


def _draw_priority_dot(
    draw: ImageDraw.ImageDraw, x: int, cy: int, priority: str
) -> None:
    """Draw a small priority indicator next to the task summary."""
    if priority == PRIORITY_HIGH:
        r = 4
        draw.ellipse([(x - r, cy - r), (x + r, cy + r)], fill=BLACK)
    elif priority == PRIORITY_MEDIUM:
        r = 3
        draw.ellipse([(x - r, cy - r), (x + r, cy + r)], outline=BLACK, width=1)


def render_tasks(tasks: list[Task], width: int, height: int) -> Image.Image:
    """Render the task list into an 800x480 1-bit image.

    Layout (top to bottom):
    - Header bar (35px): "Tasks" title + pending count
    - Task rows (32px each): checkbox, summary, due date
      - Subtasks are indented one level
    - Overflow bar (30px): "+ N more tasks" if list is truncated
    """
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    flat_tasks = _flatten_for_display(tasks)
    total_count = len(flat_tasks)

    # ── Header ──
    draw_header(draw, width, "Tasks", f"{total_count} pending")

    # ── Empty state ──
    if total_count == 0:
        empty_font = _font("Regular", 20)
        msg = "No pending tasks"
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

    # ── Task rows ──
    usable_h = height - HEADER_HEIGHT - OVERFLOW_HEIGHT
    max_rows = usable_h // ROW_HEIGHT
    visible_tasks = flat_tasks[:max_rows]
    overflow_count = total_count - len(visible_tasks)

    summary_font = _font("Regular", 15)
    summary_bold_font = _font("Bold", 15)
    due_font = _font("Regular", 13)

    for i, (task, is_subtask) in enumerate(visible_tasks):
        row_y = HEADER_HEIGHT + i * ROW_HEIGHT
        x_offset = LEFT_PADDING + (INDENT_WIDTH if is_subtask else 0)

        # Vertical center of this row.
        cy = row_y + ROW_HEIGHT // 2

        # Checkbox
        cb_y = cy - CHECKBOX_SIZE // 2
        _draw_checkbox(draw, x_offset, cb_y, CHECKBOX_SIZE, task.status == "IN-PROCESS")

        # Priority dot (between checkbox and summary).
        dot_x = x_offset + CHECKBOX_SIZE + 10
        if task.priority in (PRIORITY_HIGH, PRIORITY_MEDIUM):
            _draw_priority_dot(draw, dot_x, cy, task.priority)
            text_x = dot_x + 10
        else:
            text_x = x_offset + CHECKBOX_SIZE + 12

        # Due date (right-aligned — drawn first so we know how much width
        # the summary can occupy).
        due_str = ""
        due_w = 0
        if task.due is not None:
            due_str = _format_due_date(task.due)
            due_bbox = draw.textbbox((0, 0), due_str, font=due_font)
            due_w = int(due_bbox[2] - due_bbox[0])
            due_h = int(due_bbox[3] - due_bbox[1])
            draw.text(
                (width - RIGHT_PADDING - due_w, cy - due_h // 2),
                due_str,
                fill=BLACK,
                font=due_font,
            )

        # Summary text — bold for high-priority tasks, truncated with
        # ellipsis if it would overlap the due date.
        task_font = summary_bold_font if task.priority == PRIORITY_HIGH else summary_font
        due_gap = due_w + 15 if due_w else 0
        max_summary_w = width - text_x - RIGHT_PADDING - due_gap

        summary = truncate_text(draw, task.summary, task_font, max_summary_w)
        summary_bbox = draw.textbbox((0, 0), summary, font=task_font)
        summary_h = summary_bbox[3] - summary_bbox[1]

        draw.text((text_x, cy - summary_h // 2), summary, fill=BLACK, font=task_font)

        # Subtle row divider.
        if i < len(visible_tasks) - 1:
            div_y = row_y + ROW_HEIGHT - 1
            draw.line(
                [(LEFT_PADDING, div_y), (width - RIGHT_PADDING, div_y)],
                fill=BLACK,
                width=1,
            )

    # ── Overflow indicator ──
    draw_overflow_bar(draw, width, height, overflow_count, "task", bar_height=OVERFLOW_HEIGHT)

    # ── Outer border ──
    draw_border(draw, width, height)

    return img


# ── View class ──


@registry.register
class TasksView(BaseView):
    """CalDAV task list display."""

    name = "tasks"
    description = "Pending tasks from CalDAV"

    def render(self, width: int, height: int) -> Image.Image:
        log.info("Fetching tasks from CalDAV server")
        try:
            tasks = fetch_tasks(self.settings)
        except ValueError as e:
            # Configuration error -- render an error message rather than
            # crashing the entire view rotation.
            log.error("Tasks view: %s", e)
            return render_error("Tasks", "Tasks: configuration error", str(e), width, height)

        log.info("Got %d top-level tasks", len(tasks))
        return render_tasks(tasks, width, height)
