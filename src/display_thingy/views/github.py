"""GitHub activity view: fetches recent events for a GitHub user and renders
them as a ranked activity feed with event summaries and relative timestamps."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

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
    draw_overflow_bar,
    relative_time,
    render_error,
    truncate_text,
)
from display_thingy.views._render import (
    font as _font,
)

log = logging.getLogger(__name__)

# How many events to display.  Each event occupies ~42px (summary line
# + metadata line), giving us room for about 10 in the usable area.
MAX_EVENTS = 10

# How many events to request from the API.  We fetch more than we
# display because some event types are skipped or collapsed.
FETCH_COUNT = 30


# ── Data model ──


@dataclass
class GitHubEvent:
    """A single GitHub activity event, summarised for display."""

    summary: str  # e.g. "Pushed 3 commits to owner/repo"
    detail: str  # e.g. branch name, PR title (may be empty)
    created_at: int  # unix timestamp


# ── Event summarisation ──
#
# Each event type from the GitHub Events API has a different payload
# shape.  We extract a human-readable one-line summary from each
# supported type and skip the rest.


def _summarise_event(event: dict) -> GitHubEvent | None:
    """Convert a raw GitHub API event dict into a GitHubEvent.

    Returns None for event types we don't display (keeps the feed
    focused on meaningful activity).
    """
    event_type = event.get("type", "")
    repo_name = event.get("repo", {}).get("name", "unknown")
    payload = event.get("payload", {})
    created_at = _parse_iso_timestamp(event.get("created_at", ""))

    summary: str | None = None
    detail = ""

    if event_type == "PushEvent":
        # The payload `size` field is the true commit count.  The
        # `commits` array may be truncated or empty for unauthenticated
        # requests, so we prefer `size` over `len(commits)`.  Both can
        # be null/0 for large merges or force-pushes (common on repos
        # like torvalds/linux), so we fall back to a generic summary.
        count = payload.get("size") or len(payload.get("commits", []))
        if count > 0:
            noun = "commit" if count == 1 else "commits"
            summary = f"Pushed {count} {noun} to {repo_name}"
        else:
            summary = f"Pushed to {repo_name}"

        ref = payload.get("ref", "")
        branch = ref.removeprefix("refs/heads/")
        if branch:
            detail = branch

    elif event_type == "PullRequestEvent":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        number = payload.get("number", pr.get("number", "?"))

        # The API reports "closed" for both merges and plain closes.
        # The merged flag distinguishes them, but the minimal PR object
        # in the events API doesn't always include it — so we check both
        # the PR object and a top-level merged key.
        merged = pr.get("merged", payload.get("merged", False))
        if action == "closed" and merged:
            action = "merged"

        summary = f"{action.capitalize()} PR #{number} on {repo_name}"

    elif event_type == "IssuesEvent":
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        number = issue.get("number", "?")
        summary = f"{action.capitalize()} issue #{number} on {repo_name}"

    elif event_type == "IssueCommentEvent":
        issue = payload.get("issue", {})
        number = issue.get("number", "?")
        summary = f"Commented on #{number} in {repo_name}"

    elif event_type == "CreateEvent":
        ref_type = payload.get("ref_type", "repository")
        ref = payload.get("ref", "")
        if ref_type == "repository":
            summary = f"Created repository {repo_name}"
        else:
            summary = f"Created {ref_type} {ref} on {repo_name}"

    elif event_type == "DeleteEvent":
        ref_type = payload.get("ref_type", "branch")
        ref = payload.get("ref", "")
        summary = f"Deleted {ref_type} {ref} from {repo_name}"

    elif event_type == "ForkEvent":
        summary = f"Forked {repo_name}"

    elif event_type == "WatchEvent":
        # WatchEvent with action "started" means the user starred a repo.
        summary = f"Starred {repo_name}"

    elif event_type == "ReleaseEvent":
        action = payload.get("action", "")
        release = payload.get("release", {})
        tag = release.get("tag_name", "")
        if action == "published" and tag:
            summary = f"Released {tag} of {repo_name}"
        else:
            summary = f"{action.capitalize()} release on {repo_name}"

    elif event_type == "PullRequestReviewEvent":
        pr = payload.get("pull_request", {})
        number = pr.get("number", "?")
        summary = f"Reviewed PR #{number} on {repo_name}"

    # Intentionally omit noisy/low-value event types like
    # PublicEvent, GollumEvent (wiki edits), MemberEvent, etc.

    if summary is None:
        return None

    return GitHubEvent(summary=summary, detail=detail, created_at=created_at)


def _parse_iso_timestamp(iso_str: str) -> int:
    """Parse a GitHub ISO 8601 timestamp (e.g. '2026-03-19T12:34:56Z') to unix time."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return 0


# ── API client ──


def fetch_events(username: str, token: str = "") -> list[GitHubEvent]:
    """Fetch recent GitHub events for a user.

    Uses the ``GET /users/{username}/events`` endpoint.  If a personal
    access token is provided, it's sent as a Bearer token to include
    private-repo events and get a higher rate limit (5000 vs 60 req/hr).
    """
    url = f"https://api.github.com/users/{username}/events"
    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    log.info("Fetching GitHub events for %s", username)

    resp = httpx.get(
        url,
        params={"per_page": FETCH_COUNT},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    raw_events: list[dict] = resp.json()

    events: list[GitHubEvent] = []
    for raw in raw_events:
        event = _summarise_event(raw)
        if event is not None:
            events.append(event)

    log.info("Parsed %d displayable events (of %d raw)", len(events), len(raw_events))
    return events


# ── Renderer ──

# Layout constants — same spacing as hackernews/rss for visual
# consistency across list-style views.
OVERFLOW_BAR_HEIGHT = 30
LEFT_PADDING = 12
RIGHT_PADDING = 12
ROW_HEIGHT = 42
RANK_WIDTH = 30
META_INDENT = RANK_WIDTH


def render_github(
    events: list[GitHubEvent],
    username: str,
    width: int,
    height: int,
) -> Image.Image:
    """Render a list of GitHub events onto an 800x480 1-bit image."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    draw_header(
        draw,
        width,
        "GitHub",
        f"@{username}",
        left_pad=LEFT_PADDING,
        right_pad=RIGHT_PADDING,
    )

    # ── Event rows ──

    usable_h = height - HEADER_HEIGHT - OVERFLOW_BAR_HEIGHT
    max_rows = usable_h // ROW_HEIGHT

    title_font = _font("Bold", 16)
    rank_font = _font("Bold", 16)
    meta_font = _font("Regular", 13)

    visible = events[:max_rows]
    remaining = len(events) - len(visible)

    y = HEADER_HEIGHT + 4  # small top padding

    for i, event in enumerate(visible):
        rank = i + 1

        # -- Summary line --
        rank_text = f"{rank}."
        rank_w = draw.textbbox((0, 0), rank_text, font=rank_font)[2]
        rank_x = LEFT_PADDING + RANK_WIDTH - rank_w - 4
        draw.text((rank_x, y), rank_text, font=rank_font, fill=BLACK)

        title_x = LEFT_PADDING + RANK_WIDTH
        max_title_w = width - title_x - RIGHT_PADDING
        summary = truncate_text(draw, event.summary, title_font, max_title_w)
        draw.text((title_x, y), summary, font=title_font, fill=BLACK)

        # -- Metadata line --
        # Optional detail (branch name, etc.) and relative time.
        meta_parts: list[str] = []
        if event.detail:
            meta_parts.append(event.detail)
        if event.created_at > 0:
            meta_parts.append(relative_time(event.created_at))
        meta_text = "  \u00b7  ".join(meta_parts) if meta_parts else ""

        if meta_text:
            meta_x = LEFT_PADDING + META_INDENT
            meta_y = y + 20
            draw.text((meta_x, meta_y), meta_text, font=meta_font, fill=BLACK)

        y += ROW_HEIGHT

        # Separator line between events (not after the last one).
        if i < len(visible) - 1:
            sep_y = y - 3
            draw.line(
                [(LEFT_PADDING + RANK_WIDTH, sep_y), (width - RIGHT_PADDING, sep_y)],
                fill=BLACK,
                width=1,
            )

    # ── Overflow bar ──

    draw_overflow_bar(draw, width, height, remaining, "event")

    # ── Border ──

    draw_border(draw, width, height)

    return img


# ── View class ──


@registry.register
class GitHubView(BaseView):
    """GitHub personal activity feed display."""

    name = "github"
    description = "GitHub activity feed"

    def render(self, width: int, height: int) -> Image.Image:
        username = self.settings.github_username

        if not username:
            return render_error(
                "GitHub",
                "No username configured",
                "Set GITHUB_USERNAME in your environment.",
                width,
                height,
            )

        try:
            events = fetch_events(username, self.settings.github_token)
        except Exception as exc:
            log.exception("GitHub view failed")
            return render_error(
                "GitHub",
                "Could not load activity",
                str(exc),
                width,
                height,
            )

        if not events:
            return render_error(
                "GitHub",
                "No recent activity",
                f"No events found for @{username}.",
                width,
                height,
            )

        return render_github(events, username, width, height)
