"""Wikiquote Quote of the Day view: fetches today's featured quote from
Wikiquote and renders it as a poster-style display with large decorative
quotation marks, adaptive font sizing, and author attribution.

Uses the MediaWiki API on en.wikiquote.org to fetch the QOTD template
for today's date.  No authentication or API key is required.
"""

from __future__ import annotations

import logging
import re
import textwrap
from dataclasses import dataclass
from datetime import date

import httpx
from PIL import Image, ImageDraw, ImageFont

from display_thingy.views import BaseView, registry
from display_thingy.views._render import (
    BLACK,
    HEADER_HEIGHT,
    WHITE,
    draw_border,
    draw_header,
    render_error,
)
from display_thingy.views._render import (
    font as _font,
)
from display_thingy.views._wiki import strip_basic_wiki_markup

log = logging.getLogger(__name__)

WIKIQUOTE_API = "https://en.wikiquote.org/w/api.php"
USER_AGENT = "display-thingy/0.1 (e-paper quote display)"


# ── Data model ──


@dataclass
class Quote:
    """A single quote with its attribution."""

    text: str
    author: str


# ── API client ──


def _parse_template(wikitext: str) -> Quote:
    """Extract quote and author from the QOTD wiki template text.

    The template has the form::

        {{Wikiquote:Quote of the day/Template
        | quote = ...
        | author = ...
        }}

    We extract the named parameters with a simple regex rather than a
    full wikitext parser, since the template structure is very consistent.
    """
    # Extract the quote parameter.  The value may span multiple lines and
    # contains wiki markup that we need to strip.
    quote_match = re.search(
        r"\|\s*quote\s*=\s*(.*?)(?=\n\s*\||\n\s*\}\})", wikitext, re.DOTALL
    )
    if not quote_match:
        raise ValueError("Could not find 'quote' parameter in QOTD template")
    quote_text = strip_basic_wiki_markup(quote_match.group(1))

    # Extract the author parameter (single line).
    author_match = re.search(r"\|\s*author\s*=\s*(.+)", wikitext)
    if not author_match:
        raise ValueError("Could not find 'author' parameter in QOTD template")
    author = strip_basic_wiki_markup(author_match.group(1).strip())

    return Quote(text=quote_text, author=author)


def fetch_quote() -> Quote:
    """Fetch today's Quote of the Day from Wikiquote.

    Makes a single HTTP request to the MediaWiki parse API to get the
    wikitext of today's QOTD page, then extracts the quote and author.
    """
    today = date.today()
    # Page title format: "Wikiquote:Quote_of_the_day/March_19,_2026"
    page_title = (
        f"Wikiquote:Quote of the day/{today.strftime('%B')} {today.day}, {today.year}"
    )

    resp = httpx.get(
        WIKIQUOTE_API,
        params={
            "action": "parse",
            "page": page_title,
            "prop": "wikitext",
            "format": "json",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        error_info = data["error"].get("info", "Unknown error")
        raise ValueError(f"Wikiquote API error: {error_info}")

    wikitext = data["parse"]["wikitext"]["*"]
    return _parse_template(wikitext)


# ── Renderer ──

# Layout constants
LEFT_PADDING = 50
RIGHT_PADDING = 50
TOP_PADDING = 20
BOTTOM_PADDING = 30

# Adaptive font sizing: try each size in order until the text fits.
QUOTE_FONT_SIZES = [22, 18, 14]

# Large decorative quotation marks.
DECO_QUOTE_SIZE = 60


def _wrap_and_measure(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> tuple[list[str], int]:
    """Word-wrap text to fit within max_width and return lines + total height.

    Uses ``textwrap.wrap`` with a character-width estimate, then verifies
    each line fits by measuring with PIL.  Lines that still overflow are
    re-split.
    """
    # Estimate characters per line from average character width.
    avg_char_w = draw.textbbox((0, 0), "abcdefghijklm", font=font)[2] / 13
    chars_per_line = max(10, int(max_width / avg_char_w))

    raw_lines = textwrap.wrap(text, width=chars_per_line)

    # Verify each line actually fits; re-wrap if not.
    final_lines: list[str] = []
    for line in raw_lines:
        line_w = draw.textbbox((0, 0), line, font=font)[2]
        if line_w <= max_width:
            final_lines.append(line)
        else:
            # Re-wrap this line with fewer characters.
            narrower = textwrap.wrap(line, width=int(chars_per_line * 0.85))
            final_lines.extend(narrower)

    line_height = int(draw.textbbox((0, 0), "Ay", font=font)[3]) + 6
    total_height = len(final_lines) * line_height

    return final_lines, total_height


def render_quote(quote: Quote, width: int, height: int) -> Image.Image:
    """Render a quote onto an 800x480 1-bit image with poster-style layout."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    today = date.today()
    date_str = today.strftime("%B %-d, %Y")
    draw_header(draw, width, "Quote of the Day", date_str, left_pad=12, right_pad=12)

    # ── Usable area ──
    #
    # The quote body sits between the header and the bottom border, with
    # generous horizontal padding for a centered, poster-like feel.  We
    # reserve space for the decorative quote marks and the attribution line.

    deco_font = _font("Bold", DECO_QUOTE_SIZE)
    author_font = _font("Medium", 18)

    # Measure the attribution line so we can reserve space for it.
    attribution = f"\u2014 {quote.author}"
    attr_h = draw.textbbox((0, 0), attribution, font=author_font)[3] + 8

    # Space between the last quote line and the attribution.
    attr_gap = 20

    # Vertical space available for the decorative marks + quote text.
    usable_top = HEADER_HEIGHT + TOP_PADDING
    usable_bottom = height - BOTTOM_PADDING - attr_h - attr_gap
    usable_h = usable_bottom - usable_top

    # Horizontal space for the quote text (inset from the decorative marks).
    deco_inset = 15  # extra indent past the decorative open-quote
    text_left = LEFT_PADDING + deco_inset
    text_max_w = width - text_left - RIGHT_PADDING - deco_inset

    # ── Adaptive font sizing ──
    #
    # Try each candidate font size, picking the largest one where the
    # wrapped text fits within the available vertical space.

    chosen_font = _font("Regular", QUOTE_FONT_SIZES[-1])
    chosen_lines: list[str] = []
    chosen_line_h = 0
    chosen_total_h = 0

    for size in QUOTE_FONT_SIZES:
        candidate_font = _font("Regular", size)
        lines, total_h = _wrap_and_measure(quote.text, candidate_font, text_max_w, draw)
        line_h = draw.textbbox((0, 0), "Ay", font=candidate_font)[3] + 6

        if total_h <= usable_h:
            chosen_font = candidate_font
            chosen_lines = lines
            chosen_line_h = line_h
            chosen_total_h = total_h
            break
    else:
        # Even the smallest size overflows.  Use it anyway and truncate.
        smallest = _font("Regular", QUOTE_FONT_SIZES[-1])
        lines, _ = _wrap_and_measure(quote.text, smallest, text_max_w, draw)
        line_h = draw.textbbox((0, 0), "Ay", font=smallest)[3] + 6
        max_lines = usable_h // line_h

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            # Replace the end of the last line with an ellipsis.
            lines[-1] = lines[-1].rstrip()
            if len(lines[-1]) > 3:
                lines[-1] = lines[-1][:-3] + "\u2026"

        chosen_font = smallest
        chosen_lines = lines
        chosen_line_h = line_h
        chosen_total_h = len(lines) * line_h

    # ── Vertical centering ──
    #
    # Center the block of (decorative marks + quote text + attribution)
    # within the usable area.

    block_h = chosen_total_h + attr_gap + attr_h
    block_top = usable_top + (usable_h - block_h) // 2
    block_top = max(block_top, usable_top)  # clamp to top

    # ── Decorative open quote ──

    deco_open = "\u201c"
    draw.text((LEFT_PADDING - 5, block_top - 15), deco_open, font=deco_font, fill=BLACK)

    # ── Quote text ──

    text_y = block_top
    for line in chosen_lines:
        draw.text((text_left, text_y), line, font=chosen_font, fill=BLACK)
        text_y += chosen_line_h

    # ── Decorative close quote ──

    deco_close = "\u201d"
    deco_close_w = draw.textbbox((0, 0), deco_close, font=deco_font)[2]
    # Position after the last line of text, nudged right.
    draw.text(
        (width - RIGHT_PADDING - deco_close_w + 5, text_y - chosen_line_h - 10),
        deco_close,
        font=deco_font,
        fill=BLACK,
    )

    # ── Attribution ──

    attr_w = draw.textbbox((0, 0), attribution, font=author_font)[2]
    attr_x = width - RIGHT_PADDING - deco_inset - attr_w
    attr_y = text_y + attr_gap
    draw.text((attr_x, attr_y), attribution, font=author_font, fill=BLACK)

    # ── Border ──

    draw_border(draw, width, height)

    return img


@registry.register
class WikiquoteView(BaseView):
    """Wikiquote Quote of the Day view."""

    name = "wikiquote"
    description = "Wikiquote Quote of the Day"

    def render(self, width: int, height: int) -> Image.Image:
        try:
            quote = fetch_quote()
        except Exception as exc:
            log.error("Wikiquote view: %s", exc)
            return render_error(
                "Quote of the Day", "Could not load quote", str(exc), width, height,
            )

        if not quote.text:
            return render_error(
                "Quote of the Day", "Could not load quote",
                "Empty quote returned from API", width, height,
            )

        return render_quote(quote, width, height)
