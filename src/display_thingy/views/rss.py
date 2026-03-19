"""RSS/Atom feed reader view: fetches one or more feeds, merges them into a
single timeline sorted by publication date, and renders a ranked list with
feed names and relative timestamps."""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass

import feedparser
import httpx
from PIL import Image, ImageDraw

from display_thingy.views import BaseView, registry
from display_thingy.views._render import (
    BLACK,
    HEADER_HEIGHT,
    WHITE,
    draw_border,
    draw_header,
    draw_overflow_bar,
    relative_time,
    render_error,
    truncate_text,
)
from display_thingy.views._render import (
    font as _font,
)

log = logging.getLogger(__name__)

USER_AGENT = "display-thingy/0.1 (e-paper feed reader)"

# How many items to display.  Each item occupies ~42px (title + metadata),
# giving us room for about 10 items in the usable area between the header
# and overflow bar.
MAX_ITEMS = 10


# ── Data model ──


@dataclass
class FeedItem:
    """A single entry from an RSS/Atom feed."""

    title: str
    feed_name: str
    published: int  # unix timestamp
    url: str


# ── Feed fetching ──


def _parse_published(entry: feedparser.FeedParserDict) -> int:
    """Extract a unix timestamp from a feed entry's date fields.

    Tries ``published_parsed``, then ``updated_parsed``.  Returns 0 if
    neither is available — items with unknown dates sort to the bottom.
    """
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct is None:
        return 0
    return int(calendar.timegm(time_struct))


def _feed_title(feed: feedparser.FeedParserDict) -> str:
    """Extract a human-readable title from the feed metadata.

    Falls back to the feed link's hostname, or ``"Unknown"`` if neither
    is available.
    """
    title = feed.get("feed", {}).get("title", "").strip()
    if title:
        return title

    # Fall back to the domain name from the feed link.
    link = feed.get("feed", {}).get("link", "")
    if link:
        # Rough extraction: strip protocol and path.
        domain = link.split("//", 1)[-1].split("/", 0)[0]
        if domain:
            return domain

    return "Unknown"


def fetch_feeds(urls: list[str]) -> list[FeedItem]:
    """Fetch and merge items from multiple RSS/Atom feed URLs.

    Downloads each feed's raw content with httpx (for timeout control and
    a custom User-Agent), then parses with feedparser.  Feeds that fail
    to download or parse are logged and skipped — partial results are
    still returned.

    Returns items sorted newest-first, limited to ``MAX_ITEMS``.
    """
    items: list[FeedItem] = []

    with httpx.Client(
        timeout=15,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        for url in urls:
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Failed to fetch feed %s: %s", url, exc)
                continue

            feed = feedparser.parse(resp.text)

            if feed.bozo and not feed.entries:
                log.warning(
                    "Feed %s could not be parsed: %s", url, feed.bozo_exception
                )
                continue

            name = _feed_title(feed)

            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title:
                    continue

                items.append(
                    FeedItem(
                        title=title,
                        feed_name=name,
                        published=_parse_published(entry),
                        url=entry.get("link", ""),
                    )
                )

    # Sort by publication date, newest first.  Items with unknown dates
    # (published == 0) sort to the bottom.
    items.sort(key=lambda it: it.published, reverse=True)

    log.info("Fetched %d items from %d feed(s)", len(items), len(urls))
    return items


# ── Renderer ──

# Layout constants — same spacing as the Hacker News view for visual
# consistency across list-style views.
OVERFLOW_BAR_HEIGHT = 30
LEFT_PADDING = 12
RIGHT_PADDING = 12
ROW_HEIGHT = 42
RANK_WIDTH = 30
META_INDENT = RANK_WIDTH


def render_feed(
    items: list[FeedItem],
    header_title: str,
    width: int,
    height: int,
) -> Image.Image:
    """Render a list of feed items onto an 800x480 1-bit image."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    count_text = f"{len(items)} article{'s' if len(items) != 1 else ''}"
    draw_header(
        draw, width, header_title, count_text,
        left_pad=LEFT_PADDING, right_pad=RIGHT_PADDING,
    )

    # ── Item rows ──

    usable_h = height - HEADER_HEIGHT - OVERFLOW_BAR_HEIGHT
    max_rows = usable_h // ROW_HEIGHT

    title_font = _font("Bold", 16)
    rank_font = _font("Bold", 16)
    meta_font = _font("Regular", 13)

    visible = items[:max_rows]
    remaining = len(items) - len(visible)

    y = HEADER_HEIGHT + 4  # small top padding

    for i, item in enumerate(visible):
        rank = i + 1

        # -- Title line --
        rank_text = f"{rank}."
        rank_w = draw.textbbox((0, 0), rank_text, font=rank_font)[2]
        rank_x = LEFT_PADDING + RANK_WIDTH - rank_w - 4
        draw.text((rank_x, y), rank_text, font=rank_font, fill=BLACK)

        title_x = LEFT_PADDING + RANK_WIDTH
        max_title_w = width - title_x - RIGHT_PADDING
        title = truncate_text(draw, item.title, title_font, max_title_w)
        draw.text((title_x, y), title, font=title_font, fill=BLACK)

        # -- Metadata line --
        # Feed name and relative time, separated by a middle dot.
        meta_parts = [item.feed_name]
        if item.published > 0:
            meta_parts.append(relative_time(item.published))
        meta_text = "  \u00b7  ".join(meta_parts)
        meta_x = LEFT_PADDING + META_INDENT
        meta_y = y + 20
        draw.text((meta_x, meta_y), meta_text, font=meta_font, fill=BLACK)

        y += ROW_HEIGHT

        # Separator line between items (not after the last one).
        if i < len(visible) - 1:
            sep_y = y - 3
            draw.line(
                [(LEFT_PADDING + RANK_WIDTH, sep_y), (width - RIGHT_PADDING, sep_y)],
                fill=BLACK,
                width=1,
            )

    # ── Overflow bar ──

    draw_overflow_bar(draw, width, height, remaining, "article")

    # ── Border ──

    draw_border(draw, width, height)

    return img


# ── View class ──


@registry.register
class RssView(BaseView):
    """RSS/Atom feed reader display."""

    name = "rss"
    description = "RSS/Atom feed reader"

    def render(self, width: int, height: int) -> Image.Image:
        urls = self.settings.rss_urls

        if not urls:
            return render_error(
                "RSS",
                "No feeds configured",
                "Set RSS_URLS in your environment.",
                width,
                height,
            )

        try:
            items = fetch_feeds(urls)
        except Exception as exc:
            log.exception("RSS view failed")
            return render_error(
                self.settings.rss_title,
                "Could not load feeds",
                str(exc),
                width,
                height,
            )

        if not items:
            return render_error(
                self.settings.rss_title,
                "No articles found",
                "Feeds returned no entries.",
                width,
                height,
            )

        return render_feed(items, self.settings.rss_title, width, height)
