"""Hacker News view: fetches the current top stories from the Hacker News API
and renders a ranked list with scores, comment counts, and relative timestamps.

Uses the public Firebase-backed HN API (https://github.com/HackerNews/API).
No authentication or API key is required.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw, ImageFont

from display_thingy.config import FONTS_DIR
from display_thingy.views import BaseView, registry

log = logging.getLogger(__name__)


# ── Fonts ──

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(weight: str = "Regular", size: int = 16) -> ImageFont.FreeTypeFont:
    """Load an Inter font at the given size, with caching."""
    key = (weight, size)
    if key not in _font_cache:
        path = FONTS_DIR / f"Inter-{weight}.ttf"
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


# ── Constants ──

BLACK = 0
WHITE = 1

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
USER_AGENT = "display-thingy/0.1 (e-paper HN reader)"

# How many stories to fetch and display.  The top-stories endpoint returns
# up to 500 IDs, but we only need enough to fill the screen.  Each story
# occupies ~42px (title line + metadata line + gap), giving us room for
# about 10 stories in the 415px usable area.
MAX_STORIES = 10


# ── Data model ──


@dataclass
class Story:
    """A single Hacker News story."""

    id: int
    title: str
    url: str
    by: str
    score: int
    descendants: int  # total comment count
    time: int  # unix timestamp


# ── API client ──


def _fetch_item(client: httpx.Client, item_id: int) -> dict:
    """Fetch a single HN item by ID."""
    resp = client.get(f"{HN_API_BASE}/item/{item_id}.json")
    resp.raise_for_status()
    return resp.json()


def _parse_story(data: dict) -> Story:
    """Parse an HN API item response into a Story."""
    return Story(
        id=data["id"],
        title=data.get("title", "(untitled)"),
        url=data.get("url", ""),
        by=data.get("by", "unknown"),
        score=data.get("score", 0),
        descendants=data.get("descendants", 0),
        time=data.get("time", 0),
    )


def fetch_stories(count: int = MAX_STORIES) -> list[Story]:
    """Fetch the current top stories from the HN API.

    Makes 1 + count HTTP requests: one for the top-story ID list, then one
    per story to get its details.  Uses a shared httpx session for connection
    reuse.
    """
    with httpx.Client(
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        # 1. Get the ranked list of top-story IDs.
        resp = client.get(f"{HN_API_BASE}/topstories.json")
        resp.raise_for_status()
        story_ids: list[int] = resp.json()

        # 2. Fetch each story's details.  We only need `count` stories, but
        #    some items may be deleted or non-story types, so fetch a few
        #    extras as a buffer.
        stories: list[Story] = []
        for item_id in story_ids[: count + 5]:
            if len(stories) >= count:
                break
            try:
                data = _fetch_item(client, item_id)
            except httpx.HTTPError:
                log.warning("Failed to fetch item %d, skipping", item_id)
                continue

            # The top-stories list can occasionally contain non-story items
            # (e.g. job postings).  Skip anything that isn't a story.
            if data is None or data.get("type") != "story":
                continue
            if data.get("deleted") or data.get("dead"):
                continue

            stories.append(_parse_story(data))

    total_count = len(story_ids)
    log.info("Fetched %d stories (of %d total)", len(stories), total_count)
    return stories


# ── Relative time formatting ──


def _relative_time(unix_ts: int) -> str:
    """Format a unix timestamp as a human-readable relative time string.

    Examples: "2m ago", "3h ago", "1d ago", "2w ago".
    """
    now = time.time()
    delta = int(now - unix_ts)

    if delta < 0:
        return "just now"
    if delta < 60:
        return "just now"

    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"

    days = hours // 24
    if days < 14:
        return f"{days}d ago"

    weeks = days // 7
    return f"{weeks}w ago"


# ── Renderer ──

# Layout constants
HEADER_HEIGHT = 35
OVERFLOW_BAR_HEIGHT = 30
LEFT_PADDING = 12
RIGHT_PADDING = 12
ROW_HEIGHT = 42  # generous spacing for ~10 stories
RANK_WIDTH = 30  # width reserved for "1." .. "10."
META_INDENT = RANK_WIDTH  # metadata line aligns with title text


def render_hackernews(
    stories: list[Story],
    width: int,
    height: int,
) -> Image.Image:
    """Render a list of HN stories onto an 800x480 1-bit image."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    header_font = _font("Bold", 18)
    subtitle_font = _font("Regular", 16)

    draw.text((LEFT_PADDING, 8), "Hacker News", font=header_font, fill=BLACK)

    # Right-aligned subtitle showing the current time.
    now_str = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
    subtitle = f"Top Stories \u00b7 {now_str}"
    sub_w = draw.textbbox((0, 0), subtitle, font=subtitle_font)[2]
    draw.text((width - RIGHT_PADDING - sub_w, 10), subtitle, font=subtitle_font, fill=BLACK)

    # Divider line below header
    draw.line([(0, HEADER_HEIGHT), (width, HEADER_HEIGHT)], fill=BLACK, width=1)

    # ── Story rows ──

    usable_h = height - HEADER_HEIGHT - OVERFLOW_BAR_HEIGHT
    max_rows = usable_h // ROW_HEIGHT

    title_font = _font("Bold", 16)
    rank_font = _font("Bold", 16)
    meta_font = _font("Regular", 13)

    visible = stories[:max_rows]
    remaining = len(stories) - len(visible)

    y = HEADER_HEIGHT + 4  # small top padding

    for i, story in enumerate(visible):
        rank = i + 1

        # -- Title line --
        # Draw rank number right-aligned within the rank column.
        rank_text = f"{rank}."
        rank_w = draw.textbbox((0, 0), rank_text, font=rank_font)[2]
        rank_x = LEFT_PADDING + RANK_WIDTH - rank_w - 4
        draw.text((rank_x, y), rank_text, font=rank_font, fill=BLACK)

        # Draw title, truncated with ellipsis if it overflows.
        title_x = LEFT_PADDING + RANK_WIDTH
        max_title_w = width - title_x - RIGHT_PADDING
        title = story.title

        title_w = draw.textbbox((0, 0), title, font=title_font)[2]
        if title_w > max_title_w:
            while len(title) > 1:
                title = title[:-1]
                truncated = title.rstrip() + "\u2026"
                tw = draw.textbbox((0, 0), truncated, font=title_font)[2]
                if tw <= max_title_w:
                    title = truncated
                    break

        draw.text((title_x, y), title, font=title_font, fill=BLACK)

        # -- Metadata line --
        # Score, comment count, and relative time, separated by middle dots.
        comments_label = "comment" if story.descendants == 1 else "comments"
        meta_parts = [
            f"\u25b2 {story.score}",
            f"{story.descendants} {comments_label}",
            _relative_time(story.time),
        ]
        meta_text = "  \u00b7  ".join(meta_parts)
        meta_x = LEFT_PADDING + META_INDENT
        meta_y = y + 20  # below the title line
        draw.text((meta_x, meta_y), meta_text, font=meta_font, fill=BLACK)

        y += ROW_HEIGHT

        # Draw a subtle separator line between stories (not after the last one).
        if i < len(visible) - 1:
            sep_y = y - 3
            draw.line(
                [(LEFT_PADDING + RANK_WIDTH, sep_y), (width - RIGHT_PADDING, sep_y)],
                fill=BLACK,
                width=1,
            )

    # ── Overflow bar ──

    overflow_y = height - OVERFLOW_BAR_HEIGHT
    draw.line([(0, overflow_y), (width, overflow_y)], fill=BLACK, width=1)

    if remaining > 0:
        overflow_font = _font("Regular", 14)
        overflow_text = f"+ {remaining} more on news.ycombinator.com"
        ow = draw.textbbox((0, 0), overflow_text, font=overflow_font)[2]
        draw.text(
            (width - RIGHT_PADDING - ow, overflow_y + 8),
            overflow_text,
            font=overflow_font,
            fill=BLACK,
        )

    # ── Border ──

    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=BLACK, width=2)

    return img


def _render_error(message: str, width: int, height: int) -> Image.Image:
    """Render a human-readable error image when data fetching fails."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    title_font = _font("Bold", 18)
    body_font = _font("Regular", 16)

    draw.text((LEFT_PADDING, 8), "Hacker News", font=title_font, fill=BLACK)
    draw.line([(0, HEADER_HEIGHT), (width, HEADER_HEIGHT)], fill=BLACK, width=1)

    # Center the error message vertically in the usable area.
    error_title = "Could not load stories"
    et_bbox = draw.textbbox((0, 0), error_title, font=title_font)
    et_w = et_bbox[2] - et_bbox[0]
    center_y = HEADER_HEIGHT + (height - HEADER_HEIGHT) // 2 - 30
    draw.text(((width - et_w) // 2, center_y), error_title, font=title_font, fill=BLACK)

    # Show the specific error below.
    msg_bbox = draw.textbbox((0, 0), message, font=body_font)
    msg_w = msg_bbox[2] - msg_bbox[0]
    draw.text(((width - msg_w) // 2, center_y + 30), message, font=body_font, fill=BLACK)

    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=BLACK, width=2)
    return img


# ── View class ──


@registry.register
class HackerNewsView(BaseView):
    """Hacker News top stories view."""

    name = "hackernews"
    description = "Hacker News top stories"

    def render(self, width: int, height: int) -> Image.Image:
        try:
            stories = fetch_stories()
        except Exception as exc:
            log.error("Hacker News view: %s", exc)
            return _render_error(str(exc), width, height)

        if not stories:
            return _render_error("No stories returned from API", width, height)

        return render_hackernews(stories, width, height)
