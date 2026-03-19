"""Shared CalDAV utilities for the calendar and tasks views.

Provides PROPFIND-based collection discovery and response parsing that
are shared between ``tasks.py`` (VTODO) and ``calendar.py`` (VEVENT).
Parameterising on the component type (``"VTODO"`` vs ``"VEVENT"``)
eliminates ~100 lines of near-identical discovery code.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx
import icalendar

log = logging.getLogger(__name__)

# XML namespaces used in CalDAV requests and responses.
NS_DAV = "DAV:"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"

# PROPFIND body to discover calendars.  Works for both VEVENT and VTODO
# discovery — the component type filtering happens client-side.
PROPFIND_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:resourcetype/>
    <D:displayname/>
    <C:supported-calendar-component-set/>
  </D:prop>
</D:propfind>"""


def discover_collections(
    client: httpx.Client,
    base_url: str,
    username: str,
    filter_names: list[str],
    component_type: str,
) -> list[tuple[str, str]]:
    """Discover calendar collections that support a given component type.

    Parameters
    ----------
    client:
        An authenticated ``httpx.Client``.
    base_url:
        The CalDAV server base URL (no trailing slash).
    username:
        The CalDAV username, used to construct the calendar-home path.
    filter_names:
        When non-empty, only include collections whose display name
        matches one of these entries.
    component_type:
        The iCalendar component to filter on, e.g. ``"VEVENT"`` or
        ``"VTODO"``.

    Returns
    -------
    list[tuple[str, str]]
        A list of ``(display_name, collection_url)`` tuples.
    """
    calendar_home = f"{base_url}/remote.php/dav/calendars/{username}/"
    log.info("Discovering %s collections at %s", component_type, calendar_home)

    response = client.request(
        "PROPFIND",
        calendar_home,
        content=PROPFIND_XML,
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": "1",
        },
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    collections: list[tuple[str, str]] = []

    for resp_elem in root.findall(f"{{{NS_DAV}}}response"):
        href = resp_elem.findtext(f"{{{NS_DAV}}}href", "")
        prop = resp_elem.find(f"{{{NS_DAV}}}propstat/{{{NS_DAV}}}prop")
        if prop is None:
            continue

        # Must be a calendar collection (has both <D:collection/> and
        # <C:calendar/> in resourcetype).
        restype = prop.find(f"{{{NS_DAV}}}resourcetype")
        if restype is None:
            continue
        is_calendar = (
            restype.find(f"{{{NS_DAV}}}collection") is not None
            and restype.find(f"{{{NS_CALDAV}}}calendar") is not None
        )
        if not is_calendar:
            continue

        # Must support the requested component type.
        comp_set = prop.find(f"{{{NS_CALDAV}}}supported-calendar-component-set")
        supports_type = False
        if comp_set is not None:
            for comp in comp_set.findall(f"{{{NS_CALDAV}}}comp"):
                if comp.get("name") == component_type:
                    supports_type = True
                    break
        if not supports_type:
            continue

        display_name = prop.findtext(f"{{{NS_DAV}}}displayname", "")

        # Optionally filter by display name.
        if filter_names and display_name not in filter_names:
            log.debug("Skipping collection '%s' (not in filter)", display_name)
            continue

        collection_url = f"{base_url}{href}" if href.startswith("/") else href
        collections.append((display_name, collection_url))
        log.info("  Found collection: '%s' -> %s", display_name, collection_url)

    return collections


def parse_calendar_responses(response_text: str) -> list[icalendar.Calendar]:
    """Parse a CalDAV REPORT response into a list of iCalendar objects.

    Each ``<D:response>`` element that contains ``<C:calendar-data>`` is
    parsed into an ``icalendar.Calendar``.  Responses that fail to parse
    are logged and skipped.
    """
    root = ET.fromstring(response_text)
    calendars: list[icalendar.Calendar] = []

    for resp_elem in root.findall(f"{{{NS_DAV}}}response"):
        cal_data_elem = resp_elem.find(
            f"{{{NS_DAV}}}propstat/{{{NS_DAV}}}prop/{{{NS_CALDAV}}}calendar-data"
        )
        if cal_data_elem is None or not cal_data_elem.text:
            continue

        try:
            cal = icalendar.Calendar.from_ical(cal_data_elem.text)
            calendars.append(cal)
        except Exception:
            log.warning("Failed to parse iCalendar data, skipping")

    return calendars
