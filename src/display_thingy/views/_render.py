"""Shared rendering utilities for e-paper display views.

Provides common constants, font loading, and drawing helpers that are
used across multiple view modules.  Centralising these eliminates ~250
lines of duplication.

This module is prefixed with ``_`` to signal it is internal to the views
package.  It will be found by ``pkgutil.iter_modules`` during
auto-discovery but harmlessly ignored since it contains no
``@registry.register``-decorated classes.
"""

from __future__ import annotations

import time

from PIL import Image, ImageDraw, ImageFont

from display_thingy.config import FONTS_DIR

# ── HTTP constants ──

USER_AGENT = "display-thingy/0.1 (e-paper display)"

# ── Colour constants ──
#
# Mode "1" images: 0 = black, 1 = white.

BLACK = 0
WHITE = 1

# ── Standard layout ──

HEADER_HEIGHT = 35


# ── Font loading ──

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(weight: str = "Regular", size: int = 16) -> ImageFont.FreeTypeFont:
    """Load an Inter font at the given weight and size, with caching.

    Available weights: ``Regular``, ``Medium``, ``Bold``.
    """
    key = (weight, size)
    if key not in _font_cache:
        path = FONTS_DIR / f"Inter-{weight}.ttf"
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


# ── Drawing helpers ──


def draw_border(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    """Draw the standard 2px outer border around the entire image."""
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=BLACK, width=2)


def relative_time(unix_ts: int) -> str:
    """Format a unix timestamp as a human-readable relative time string.

    Examples: ``"2m ago"``, ``"3h ago"``, ``"1d ago"``, ``"2w ago"``.
    """
    now = time.time()
    delta = int(now - unix_ts)

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


def draw_header(
    draw: ImageDraw.ImageDraw,
    width: int,
    title: str,
    context_text: str,
    *,
    left_pad: int = 15,
    right_pad: int = 15,
) -> None:
    """Draw the standard 35px header bar with title and right-aligned context.

    Uses Bold 18 for the title and Regular 16 for the context text,
    separated by a 1px divider at ``HEADER_HEIGHT``.
    """
    title_font = font("Bold", 18)
    context_font = font("Regular", 16)

    draw.text((left_pad, 8), title, font=title_font, fill=BLACK)

    ctx_w = draw.textbbox((0, 0), context_text, font=context_font)[2]
    draw.text(
        (width - right_pad - ctx_w, 10),
        context_text,
        font=context_font,
        fill=BLACK,
    )

    draw.line([(0, HEADER_HEIGHT), (width, HEADER_HEIGHT)], fill=BLACK, width=1)


def truncate_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    text_font: ImageFont.FreeTypeFont,
    max_width: int,
    ellipsis: str = "\u2026",
) -> str:
    """Truncate *text* with an ellipsis so it fits within *max_width* pixels.

    Returns the original string unchanged if it already fits.
    """
    text_w = draw.textbbox((0, 0), text, font=text_font)[2]
    if text_w <= max_width:
        return text

    while len(text) > 1:
        text = text[:-1]
        truncated = text.rstrip() + ellipsis
        tw = draw.textbbox((0, 0), truncated, font=text_font)[2]
        if tw <= max_width:
            return truncated

    return ellipsis


def render_error(
    view_title: str,
    error_heading: str,
    detail: str,
    width: int,
    height: int,
) -> Image.Image:
    """Render a standardised centered error screen.

    Layout:
    - Standard header bar with *view_title*
    - Centered *error_heading* (Bold 18)
    - Centered *detail* message (Regular 16) below
    - 2px outer border
    """
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    title_font = font("Bold", 18)
    body_font = font("Regular", 16)

    # Header (title only, no context text — just left-aligned name).
    draw.text((15, 8), view_title, font=title_font, fill=BLACK)
    draw.line([(0, HEADER_HEIGHT), (width, HEADER_HEIGHT)], fill=BLACK, width=1)

    # Centered error heading.
    et_w = draw.textbbox((0, 0), error_heading, font=title_font)[2]
    center_y = HEADER_HEIGHT + (height - HEADER_HEIGHT) // 2 - 30
    draw.text(((width - et_w) // 2, center_y), error_heading, font=title_font, fill=BLACK)

    # Centered detail message.
    msg_w = draw.textbbox((0, 0), detail, font=body_font)[2]
    draw.text(((width - msg_w) // 2, center_y + 30), detail, font=body_font, fill=BLACK)

    draw_border(draw, width, height)
    return img


def draw_overflow_bar(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    count: int,
    noun: str,
    *,
    right_pad: int = 15,
    bar_height: int = 30,
) -> None:
    """Draw a bottom overflow bar showing how many items didn't fit.

    Renders ``"+ N more <noun>s"`` right-aligned with a 1px divider
    above.  Does nothing (besides drawing the divider) if *count* is 0.
    """
    overflow_y = height - bar_height
    draw.line([(0, overflow_y), (width, overflow_y)], fill=BLACK, width=1)

    if count > 0:
        overflow_font = font("Medium", 14)
        plural = noun if count == 1 else f"{noun}s"
        overflow_text = f"+ {count} more {plural}"
        ow = draw.textbbox((0, 0), overflow_text, font=overflow_font)[2]
        draw.text(
            (width - right_pad - ow, overflow_y + 8),
            overflow_text,
            font=overflow_font,
            fill=BLACK,
        )
