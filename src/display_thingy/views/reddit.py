"""Reddit view: fetches top posts from configured subreddits and renders
a ranked list with scores, comment counts, subreddit tags, and relative
timestamps.

Uses Reddit's public JSON API (appending ``.json`` to any listing URL).
No authentication is required for public subreddits.  Reddit does require
a descriptive User-Agent header — requests with generic agents are
throttled or blocked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw

from display_thingy.views import BaseView, registry
from display_thingy.views._render import (
    BLACK,
    HEADER_HEIGHT,
    USER_AGENT,
    WHITE,
    draw_border,
    draw_header,
    relative_time,
    render_error,
    truncate_text,
)
from display_thingy.views._render import (
    font as _font,
)

log = logging.getLogger(__name__)

# How many posts to display.  Each post occupies ~42px (title line +
# metadata line + gap), giving room for about 10 posts in the 415px
# usable area (800x480 minus header and overflow bar).
MAX_POSTS = 10

# Valid sort modes for Reddit listing endpoints.
VALID_SORTS = {"hot", "top", "new", "rising"}


# ── Data model ──


@dataclass
class Post:
    """A single Reddit post."""

    title: str
    score: int
    num_comments: int
    subreddit: str
    author: str
    created_utc: int  # unix timestamp
    permalink: str


# ── API client ──


def _fetch_subreddit(
    client: httpx.Client,
    subreddit: str,
    sort: str,
    limit: int,
) -> list[Post]:
    """Fetch posts from a single subreddit.

    Returns an empty list (rather than raising) if the subreddit is
    private, banned, or otherwise inaccessible.
    """
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    resp = client.get(url, params={"limit": limit, "raw_json": 1})
    resp.raise_for_status()

    data = resp.json()
    children = data.get("data", {}).get("children", [])

    posts: list[Post] = []
    for child in children:
        if child.get("kind") != "t3":
            continue
        d = child["data"]

        # Skip stickied mod posts — they dominate the top of every
        # subreddit but rarely contain interesting content.
        if d.get("stickied"):
            continue

        posts.append(
            Post(
                title=d.get("title", "(untitled)"),
                score=d.get("score", 0),
                num_comments=d.get("num_comments", 0),
                subreddit=d.get("subreddit", subreddit),
                author=d.get("author", "[deleted]"),
                created_utc=int(d.get("created_utc", 0)),
                permalink=d.get("permalink", ""),
            )
        )

    return posts


def fetch_posts(
    subreddits: list[str],
    sort: str = "hot",
    count: int = MAX_POSTS,
) -> list[Post]:
    """Fetch and merge posts from one or more subreddits.

    Posts are deduplicated by permalink (cross-posts can appear in
    multiple subreddits) and sorted by score descending.  Returns at
    most ``count`` posts.
    """
    with httpx.Client(
        timeout=15,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        all_posts: list[Post] = []
        for sub in subreddits:
            try:
                posts = _fetch_subreddit(client, sub, sort, limit=count + 5)
                all_posts.extend(posts)
            except httpx.HTTPError as exc:
                log.warning("Failed to fetch r/%s: %s", sub, exc)
                continue

    # Deduplicate by permalink (cross-posts show up in multiple subs).
    seen: set[str] = set()
    unique: list[Post] = []
    for post in all_posts:
        if post.permalink not in seen:
            seen.add(post.permalink)
            unique.append(post)

    unique.sort(key=lambda p: p.score, reverse=True)

    log.info(
        "Fetched %d unique posts from %d subreddit(s)",
        len(unique),
        len(subreddits),
    )
    return unique[:count]


# ── Renderer ──

# Layout constants — identical to hackernews.py for visual consistency.
OVERFLOW_BAR_HEIGHT = 30
LEFT_PADDING = 12
RIGHT_PADDING = 12
ROW_HEIGHT = 42
RANK_WIDTH = 30
META_INDENT = RANK_WIDTH


def render_reddit(
    posts: list[Post],
    subreddits: list[str],
    width: int,
    height: int,
) -> Image.Image:
    """Render a list of Reddit posts onto an 800x480 1-bit image."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    now_str = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
    subs_label = ", ".join(f"r/{s}" for s in subreddits[:3])
    if len(subreddits) > 3:
        subs_label += f" +{len(subreddits) - 3}"
    subtitle = f"{subs_label} \u00b7 {now_str}"
    draw_header(
        draw,
        width,
        "Reddit",
        subtitle,
        left_pad=LEFT_PADDING,
        right_pad=RIGHT_PADDING,
    )

    # ── Post rows ──

    usable_h = height - HEADER_HEIGHT - OVERFLOW_BAR_HEIGHT
    max_rows = usable_h // ROW_HEIGHT

    title_font = _font("Bold", 16)
    rank_font = _font("Bold", 16)
    meta_font = _font("Regular", 13)

    visible = posts[:max_rows]
    remaining = len(posts) - len(visible)

    y = HEADER_HEIGHT + 4  # small top padding

    # Only show the subreddit tag per post when fetching from multiple
    # subs — it's redundant noise in single-subreddit mode.
    show_subreddit = len(subreddits) > 1

    for i, post in enumerate(visible):
        rank = i + 1

        # -- Title line --
        rank_text = f"{rank}."
        rank_w = draw.textbbox((0, 0), rank_text, font=rank_font)[2]
        rank_x = LEFT_PADDING + RANK_WIDTH - rank_w - 4
        draw.text((rank_x, y), rank_text, font=rank_font, fill=BLACK)

        title_x = LEFT_PADDING + RANK_WIDTH
        max_title_w = width - title_x - RIGHT_PADDING
        title = truncate_text(draw, post.title, title_font, max_title_w)
        draw.text((title_x, y), title, font=title_font, fill=BLACK)

        # -- Metadata line --
        comments_label = "comment" if post.num_comments == 1 else "comments"
        meta_parts = [
            f"\u25b2 {post.score}",
            f"{post.num_comments} {comments_label}",
        ]
        if show_subreddit:
            meta_parts.append(f"r/{post.subreddit}")
        meta_parts.append(relative_time(post.created_utc))

        meta_text = "  \u00b7  ".join(meta_parts)
        meta_x = LEFT_PADDING + META_INDENT
        meta_y = y + 20
        draw.text((meta_x, meta_y), meta_text, font=meta_font, fill=BLACK)

        y += ROW_HEIGHT

        # Separator line between posts (not after the last one).
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
        noun = "story" if remaining == 1 else "stories"
        overflow_text = f"+ {remaining} more {noun}"
        ow = draw.textbbox((0, 0), overflow_text, font=overflow_font)[2]
        draw.text(
            (width - RIGHT_PADDING - ow, overflow_y + 8),
            overflow_text,
            font=overflow_font,
            fill=BLACK,
        )

    # ── Border ──

    draw_border(draw, width, height)

    return img


@registry.register
class RedditView(BaseView):
    """Reddit top posts view."""

    name = "reddit"
    description = "Reddit top posts from configured subreddits"

    def render(self, width: int, height: int) -> Image.Image:
        subreddits = self.settings.reddit_subreddits
        sort = self.settings.reddit_sort

        if sort not in VALID_SORTS:
            log.warning("Invalid REDDIT_SORT %r, falling back to 'hot'", sort)
            sort = "hot"

        if not subreddits:
            return render_error(
                "Reddit",
                "No subreddits configured",
                "Set REDDIT_SUBREDDITS in your .envrc",
                width,
                height,
            )

        try:
            posts = fetch_posts(subreddits, sort=sort)
        except Exception as exc:
            log.error("Reddit view: %s", exc)
            return render_error(
                "Reddit",
                "Could not load posts",
                str(exc),
                width,
                height,
            )

        if not posts:
            subs = ", ".join(f"r/{s}" for s in subreddits)
            return render_error(
                "Reddit",
                "No posts found",
                f"No posts returned from {subs}",
                width,
                height,
            )

        return render_reddit(posts, subreddits, width, height)
