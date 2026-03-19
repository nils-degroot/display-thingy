"""Wiktionary Word of the Day view: fetches today's featured word from
Wiktionary and renders it as a dictionary-style page with the word,
part of speech, and numbered definitions.

Uses the MediaWiki API on en.wiktionary.org to fetch the WOTD template
for today's date.  No authentication or API key is required.

Top-level definitions (``#`` lines) are shown.  When a ``#`` line is
label-only (e.g. ``(transitive)``), its ``##`` sub-definitions are
promoted to top-level with the label prepended.  Deeper levels
(``###`` etc.) are omitted to keep the layout clean on a 480px-tall
display.
"""

from __future__ import annotations

import logging
import re
import textwrap
from dataclasses import dataclass
from datetime import date

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

WIKTIONARY_API = "https://en.wiktionary.org/w/api.php"
USER_AGENT = "display-thingy/0.1 (e-paper word display)"


# ── Data model ──


@dataclass
class WordOfTheDay:
    """A single word-of-the-day entry with its definitions."""

    word: str
    part_of_speech: str
    definitions: list[str]
    comment: str = ""


# ── Wiki markup cleanup ──

# Matches [[Target|display text]] or [[word]] wiki links.
_WIKI_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")

# Matches {{lb|en|label1|label2|...}} label templates.  The first param
# is always the language code ("en"), which we discard.  Remaining params
# are the human-readable labels joined with commas.
_LABEL_TMPL_RE = re.compile(r"\{\{lb\|en\|([^}]+)\}\}")

# Matches generic {{template|...}} calls we don't specifically handle.
# We remove these entirely rather than leaving raw braces in the output.
_GENERIC_TMPL_RE = re.compile(r"\{\{[^}]*\}\}")

# Matches ''italic'' wiki markup.
_ITALIC_RE = re.compile(r"''([^']+)''")

# Matches [...] continuation markers (e.g. "[...]" at end of definitions).
_CONTINUATION_RE = re.compile(r"\s*\[\.\.\.?\]")

# Matches <!-- HTML comments -->.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Matches <br /> and similar tags.
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _format_labels(match: re.Match) -> str:
    """Convert a ``{{lb|en|label1|label2}}`` template to ``(label1, label2)``."""
    raw_labels = match.group(1).split("|")

    # Filter out link modifiers like "_" or "and" that Wiktionary uses as
    # separators between label groups.
    skip = {"_", "and", "or"}
    labels = [lb for lb in raw_labels if lb not in skip]

    if not labels:
        return ""
    return "(" + ", ".join(labels) + ")"


def _strip_wiki_markup(raw: str) -> str:
    """Remove wiki markup from a WOTD definition line, returning plain text.

    Handles:
    - ``{{lb|en|humorous}}``   -> ``(humorous)``
    - ``{{other templates}}``  -> removed
    - ``[[Target|display]]``   -> ``display``
    - ``[[word]]``             -> ``word``
    - ``''italic''``           -> ``italic``
    - ``<<region>>``           -> ``region``
    - ``[...]``                -> removed
    - ``<!-- comments -->``    -> removed
    - ``<br />``               -> space
    - Consecutive whitespace   -> single space
    """
    text = _COMMENT_RE.sub("", raw)
    text = _BR_RE.sub(" ", text)
    text = _LABEL_TMPL_RE.sub(_format_labels, text)
    text = _GENERIC_TMPL_RE.sub("", text)
    text = _WIKI_LINK_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _CONTINUATION_RE.sub("", text)
    # Wiktionary uses <<region>> markers for geographic labels.
    text = re.sub(r"<<([^>]+)>>", r"\1", text)
    # Collapse whitespace and strip.
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Template parser ──


def _parse_wotd_template(wikitext: str) -> WordOfTheDay:
    """Parse the ``{{WOTD|word|pos|definitions...|Month|Day}}`` template.

    The WOTD template positional params are:
      1. word
      2. part of speech abbreviation (n, v, adj, adv, etc.)
      3. definitions (everything up to the trailing ``|Month|Day}}``)

    Named params like ``comment=...`` and ``audio=...`` may appear
    anywhere in the definition block.
    """
    # Strip the outer {{WOTD| and trailing }}.
    inner_match = re.search(r"\{\{WOTD\|(.+)\}\}", wikitext, re.DOTALL)
    if not inner_match:
        raise ValueError("Could not find {{WOTD|...}} template in page")

    inner = inner_match.group(1)

    # The first two pipe-delimited fields are word and POS.  We can't
    # just split on "|" because the definitions contain wiki links with
    # pipes inside [[ ]].  Instead, we find the first two unbracketed
    # pipes manually.
    word, rest = _split_first_param(inner)
    pos_abbrev, rest = _split_first_param(rest)

    # Expand the part-of-speech abbreviation to a full word.
    pos_map = {
        "n": "noun",
        "v": "verb",
        "adj": "adjective",
        "adv": "adverb",
        "prep": "preposition",
        "conj": "conjunction",
        "interj": "interjection",
        "pron": "pronoun",
        "det": "determiner",
        "num": "numeral",
        "part": "particle",
        "phrase": "phrase",
        "idiom": "idiom",
        "prefix": "prefix",
        "suffix": "suffix",
        "abbr": "abbreviation",
        "propn": "proper noun",
    }
    part_of_speech = pos_map.get(pos_abbrev.strip(), pos_abbrev.strip())

    # ── Named parameter extraction ──
    #
    # Named params like |comment=..., |audio=..., and |tl=... can appear
    # anywhere in the definition block.  Their values may contain wiki
    # links with pipes (e.g. [[w:Target|display]]), so we can't just
    # split on "|".  Instead, we use bracket-depth-aware extraction.
    #
    # Additionally, tl= params sometimes appear at the very start of
    # `rest` (without a leading "|"), e.g.:
    #   {{WOTD|lurgy|n|tl=humorous|tl2=slang|...}}
    # After extracting "lurgy" and "n", rest = "tl=humorous|tl2=slang|..."
    comment = ""
    tl_labels: list[str] = []
    named_param_spans: list[tuple[int, int]] = []

    # Match named params with or without a leading "|".
    for np_match in re.finditer(r"(?:^|\|)(comment|audio|tl\d*)=", rest):
        param_name = np_match.group(1)
        value_start = np_match.end()
        value_end = _find_unbracketed_pipe(rest, value_start)
        value = rest[value_start:value_end]

        # Include the leading "|" in the span if present, so it gets
        # removed along with the param.
        span_start = np_match.start()
        named_param_spans.append((span_start, value_end))

        if param_name == "comment":
            comment = _strip_wiki_markup(value.strip())
        elif param_name.startswith("tl"):
            label = value.strip()
            if label:
                tl_labels.append(label)

    # ── Definition extraction ──
    #
    # The definition block (everything after word|pos|) comes in two
    # flavours:
    #
    # 1. Single inline definition:  "{{lb|en|humorous}} Furore caused..."
    #    No "#" prefix.  May be followed by ##-prefixed sub-definitions
    #    on subsequent lines.
    #
    # 2. Multi-definition:  Lines prefixed with "#" (top-level) and "##"
    #    (sub-definitions).
    #
    # Some templates also embed a secondary word block (e.g.
    # '''bode''' ''n'') -- we stop parsing at that point.

    # Remove named params and trailing positional params from the rest
    # string to isolate the definition block.  We remove the spans
    # identified above (in reverse order to preserve indices).
    def_block = rest
    for span_start, span_end in sorted(named_param_spans, reverse=True):
        def_block = def_block[:span_start] + def_block[span_end:]

    # Remove trailing positional params: |Month|Day at the very end.
    def_block = re.sub(r"\|[A-Z][a-z]+\|\d+\s*$", "", def_block)

    # Stop at a secondary word block ('''word''' ''pos'').
    secondary_match = re.search(r"\n\s*'''", def_block)
    if secondary_match:
        def_block = def_block[:secondary_match.start()]

    # Strip any leading "|" left over after named-param removal (e.g.
    # when tl= params appeared at the very start of the definition
    # block, removing them leaves a stray leading pipe).
    def_block = def_block.lstrip("|").lstrip()

    definitions: list[str] = []
    lines = def_block.split("\n")

    # Track the label from a label-only `#` line so we can prepend it
    # to any `##` sub-definitions that follow.  When a `#` line is
    # label-only (e.g. "(transitive)"), the real definitions live at the
    # `##` level underneath.  We promote those sub-definitions to
    # top-level, prefixing them with the parent label.
    pending_label: str = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # A top-level definition: starts with exactly one "#" (not "##").
        if re.match(r"^#(?!#)\s", stripped):
            cleaned = _strip_wiki_markup(stripped.lstrip("#").strip())
            # Check for label-only lines that have no real definition
            # text.  After stripping, these are just "(transitive)" or
            # "(obsolete)" etc.  A real definition contains text beyond
            # just a parenthesized label.
            if cleaned and re.match(r"^\([^)]+\)\.?$", cleaned):
                # This is a label-only line — remember the label so we
                # can prepend it to any ## sub-definitions that follow.
                pending_label = cleaned.rstrip(".")
            elif cleaned:
                pending_label = ""
                definitions.append(cleaned)
            else:
                pending_label = ""

        # A sub-definition (##): promote to top-level if its parent #
        # was label-only.
        elif re.match(r"^##(?!#)\s", stripped):
            if pending_label:
                cleaned = _strip_wiki_markup(stripped.lstrip("#").strip())
                if cleaned and not re.match(r"^\([^)]+\)\.?$", cleaned):
                    definitions.append(f"{pending_label} {cleaned}")

        # An inline definition (no "#" prefix).  This is the first
        # definition for simple single-definition words.  We only
        # take it if we haven't found any "#"-prefixed definitions yet,
        # to avoid picking up stray non-definition text.
        elif not stripped.startswith("#") and not definitions:
            cleaned = _strip_wiki_markup(stripped)
            if cleaned and not re.match(r"^\([^)]+\)\.?$", cleaned):
                definitions.append(cleaned)

    # Prepend top-level labels to the first definition if present.
    if tl_labels and definitions:
        label_str = "(" + ", ".join(tl_labels) + ") "
        definitions[0] = label_str + definitions[0]

    return WordOfTheDay(
        word=word.strip(),
        part_of_speech=part_of_speech,
        definitions=definitions,
        comment=comment,
    )


def _split_first_param(text: str) -> tuple[str, str]:
    """Split text at the first pipe ``|`` that is not inside ``[[ ]]``.

    Returns ``(before_pipe, after_pipe)``.  If no unbracketed pipe is
    found, returns ``(text, "")``.
    """
    pos = _find_unbracketed_pipe(text, 0)
    if pos < len(text):
        return text[:pos], text[pos + 1:]
    return text, ""


def _find_unbracketed_pipe(text: str, start: int = 0) -> int:
    """Find the index of the next ``|`` that is not inside ``[[ ]]``.

    Returns the index of the pipe, or ``len(text)`` if no unbracketed
    pipe is found.  Also treats ``{{`` / ``}}`` as bracket depth changes,
    since template params can contain nested templates.
    """
    depth = 0
    i = start
    while i < len(text):
        two = text[i:i + 2]
        if two in ("[[", "{{"):
            depth += 1
            i += 2
        elif two in ("]]", "}}"):
            depth = max(0, depth - 1)
            i += 2
        elif text[i] == "|" and depth == 0:
            return i
        else:
            i += 1
    return len(text)


# ── API client ──


def fetch_word_of_the_day() -> WordOfTheDay:
    """Fetch today's Word of the Day from Wiktionary.

    Makes a single HTTP request to the MediaWiki parse API to get the
    wikitext of today's WOTD page, then extracts the word, part of
    speech, and definitions.
    """
    today = date.today()
    # Page title format: "Wiktionary:Word_of_the_day/2026/March_19"
    page_title = (
        f"Wiktionary:Word of the day/{today.year}/{today.strftime('%B')}_{today.day}"
    )

    resp = httpx.get(
        WIKTIONARY_API,
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
        raise ValueError(f"Wiktionary API error: {error_info}")

    wikitext = data["parse"]["wikitext"]["*"]
    return _parse_wotd_template(wikitext)


# ── Renderer ──

# Layout constants
HEADER_HEIGHT = 35
LEFT_PADDING = 30
RIGHT_PADDING = 30
TOP_CONTENT_PADDING = 20
COMMENT_BAR_HEIGHT = 30
DEF_NUMBER_WIDTH = 28  # width reserved for "1. " .. "9. "


def _wrap_definition(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    """Word-wrap a definition line to fit within max_width.

    Returns a list of wrapped lines.  The first line is indented by the
    definition number (handled by the caller), and continuation lines
    are also indented to align with the text after the number.
    """
    avg_char_w = draw.textbbox((0, 0), "abcdefghijklm", font=font)[2] / 13
    chars_per_line = max(10, int(max_width / avg_char_w))

    raw_lines = textwrap.wrap(text, width=chars_per_line)

    # Verify each line fits; re-wrap if not.
    final_lines: list[str] = []
    for line in raw_lines:
        line_w = draw.textbbox((0, 0), line, font=font)[2]
        if line_w <= max_width:
            final_lines.append(line)
        else:
            narrower = textwrap.wrap(line, width=int(chars_per_line * 0.85))
            final_lines.extend(narrower)

    return final_lines


def render_wotd(wotd: WordOfTheDay, width: int, height: int) -> Image.Image:
    """Render a word-of-the-day onto an 800x480 1-bit image with
    a dictionary-page layout."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    header_font = _font("Bold", 18)
    date_font = _font("Regular", 16)

    draw.text((12, 8), "Word of the Day", font=header_font, fill=BLACK)

    today = date.today()
    date_str = today.strftime("%B %-d, %Y")
    date_w = draw.textbbox((0, 0), date_str, font=date_font)[2]
    draw.text((width - 12 - date_w, 10), date_str, font=date_font, fill=BLACK)

    draw.line([(0, HEADER_HEIGHT), (width, HEADER_HEIGHT)], fill=BLACK, width=1)

    # ── Comment bar at the bottom (if present) ──
    #
    # Reserve space for the comment bar before laying out definitions,
    # so we know how much vertical space is available.

    has_comment = bool(wotd.comment)
    bottom_reserved = COMMENT_BAR_HEIGHT if has_comment else 0

    # ── Word and part of speech ──

    word_font = _font("Bold", 36)
    pos_font = _font("Regular", 16)

    content_y = HEADER_HEIGHT + TOP_CONTENT_PADDING

    # If the word is very long, use a smaller font.
    word_w = draw.textbbox((0, 0), wotd.word, font=word_font)[2]
    max_word_w = width - LEFT_PADDING - RIGHT_PADDING
    if word_w > max_word_w:
        word_font = _font("Bold", 28)

    draw.text((LEFT_PADDING, content_y), wotd.word, font=word_font, fill=BLACK)
    word_h = draw.textbbox((0, 0), wotd.word, font=word_font)[3]
    content_y += word_h + 6

    # Part of speech on the line below the word.
    draw.text((LEFT_PADDING, content_y), wotd.part_of_speech, font=pos_font, fill=BLACK)
    pos_h = draw.textbbox((0, 0), wotd.part_of_speech, font=pos_font)[3]
    content_y += pos_h + 16

    # ── Separator line between word header and definitions ──

    draw.line(
        [(LEFT_PADDING, content_y), (width - RIGHT_PADDING, content_y)],
        fill=BLACK,
        width=1,
    )
    content_y += 12

    # ── Definitions ──
    #
    # Available vertical space for definitions, after the word header
    # and before the bottom border / comment bar.

    usable_bottom = height - 4 - bottom_reserved  # 4px inner margin
    available_h = usable_bottom - content_y

    # Adaptive font sizing: try each size until definitions fit.
    def_font_sizes = [16, 14, 12]
    num_font_sizes = [16, 14, 12]

    # We'll render definitions as numbered items.  Each definition may
    # wrap to multiple lines.  We pre-compute the layout for each font
    # size candidate and pick the largest one that fits.
    best_layout: list[tuple[str, list[str]]] | None = None
    best_font_size = def_font_sizes[-1]
    best_line_h = 0

    for font_size_idx, def_size in enumerate(def_font_sizes):
        num_size = num_font_sizes[font_size_idx]
        candidate_font = _font("Regular", def_size)
        num_font = _font("Bold", num_size)

        # Maximum width for definition text (after the number prefix).
        num_w = draw.textbbox((0, 0), "8. ", font=num_font)[2]
        text_max_w = int(width - LEFT_PADDING - num_w - RIGHT_PADDING)

        line_h = draw.textbbox((0, 0), "Ay", font=candidate_font)[3] + 5

        # Build the layout: each definition becomes a (number_str, [wrapped_lines]) pair.
        layout: list[tuple[str, list[str]]] = []
        total_h = 0
        for i, defn in enumerate(wotd.definitions):
            number = f"{i + 1}."
            wrapped = _wrap_definition(defn, candidate_font, text_max_w, draw)
            layout.append((number, wrapped))
            # Height for this definition: number of wrapped lines * line_h,
            # plus a small gap between definitions.
            total_h += len(wrapped) * line_h + 6

        if total_h <= available_h:
            best_layout = layout
            best_font_size = def_size
            best_line_h = line_h
            break

    # If nothing fit (even the smallest), use the smallest and truncate.
    if best_layout is None:
        smallest_size = def_font_sizes[-1]
        candidate_font = _font("Regular", smallest_size)
        num_font = _font("Bold", num_font_sizes[-1])
        num_w = draw.textbbox((0, 0), "8. ", font=num_font)[2]
        text_max_w = int(width - LEFT_PADDING - num_w - RIGHT_PADDING)
        line_h = draw.textbbox((0, 0), "Ay", font=candidate_font)[3] + 5

        layout = []
        total_h = 0
        for i, defn in enumerate(wotd.definitions):
            number = f"{i + 1}."
            wrapped = _wrap_definition(defn, candidate_font, text_max_w, draw)
            entry_h = len(wrapped) * line_h + 6
            if total_h + entry_h > available_h:
                # Can we fit at least the first line of this definition?
                if total_h + line_h <= available_h:
                    layout.append((number, [wrapped[0] + "\u2026"] if wrapped else []))
                break
            layout.append((number, wrapped))
            total_h += entry_h

        best_layout = layout
        best_font_size = smallest_size
        best_line_h = line_h

    # ── Draw definitions ──

    def_font = _font("Regular", best_font_size)
    num_font = _font("Bold", best_font_size)
    num_w = draw.textbbox((0, 0), "8. ", font=num_font)[2]

    y = content_y
    for number, wrapped_lines in best_layout:
        # Draw the number.
        draw.text((LEFT_PADDING, y), number, font=num_font, fill=BLACK)

        # Draw wrapped text lines.
        text_x = LEFT_PADDING + num_w
        for j, line in enumerate(wrapped_lines):
            # First line sits next to the number; continuation lines
            # are indented to align with the text.
            line_x = text_x if j > 0 else LEFT_PADDING + num_w
            draw.text((line_x, y), line, font=def_font, fill=BLACK)
            y += best_line_h

        y += 6  # gap between definitions

    # ── Comment bar ──

    if has_comment:
        comment_y = height - COMMENT_BAR_HEIGHT
        draw.line([(0, comment_y), (width, comment_y)], fill=BLACK, width=1)

        comment_font = _font("Regular", 13)

        # Truncate the comment if it doesn't fit on one line.
        max_comment_w = width - LEFT_PADDING - RIGHT_PADDING
        comment_text = wotd.comment
        cw = draw.textbbox((0, 0), comment_text, font=comment_font)[2]
        if cw > max_comment_w:
            while len(comment_text) > 1:
                comment_text = comment_text[:-1]
                truncated = comment_text.rstrip() + "\u2026"
                tw = draw.textbbox((0, 0), truncated, font=comment_font)[2]
                if tw <= max_comment_w:
                    comment_text = truncated
                    break

        draw.text(
            (LEFT_PADDING, comment_y + 8),
            comment_text,
            font=comment_font,
            fill=BLACK,
        )

    # ── Border ──

    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=BLACK, width=2)

    return img


def _render_error(message: str, width: int, height: int) -> Image.Image:
    """Render a human-readable error image when fetching fails."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    title_font = _font("Bold", 18)
    body_font = _font("Regular", 16)

    draw.text((12, 8), "Word of the Day", font=title_font, fill=BLACK)
    draw.line([(0, HEADER_HEIGHT), (width, HEADER_HEIGHT)], fill=BLACK, width=1)

    error_title = "Could not load word"
    et_bbox = draw.textbbox((0, 0), error_title, font=title_font)
    et_w = et_bbox[2] - et_bbox[0]
    center_y = HEADER_HEIGHT + (height - HEADER_HEIGHT) // 2 - 30
    draw.text(((width - et_w) // 2, center_y), error_title, font=title_font, fill=BLACK)

    msg_bbox = draw.textbbox((0, 0), message, font=body_font)
    msg_w = msg_bbox[2] - msg_bbox[0]
    draw.text(((width - msg_w) // 2, center_y + 30), message, font=body_font, fill=BLACK)

    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=BLACK, width=2)
    return img


# ── View class ──


@registry.register
class WiktionaryView(BaseView):
    """Wiktionary Word of the Day view."""

    name = "wiktionary"
    description = "Wiktionary Word of the Day"

    def render(self, width: int, height: int) -> Image.Image:
        try:
            wotd = fetch_word_of_the_day()
        except Exception as exc:
            log.error("Wiktionary view: %s", exc)
            return _render_error(str(exc), width, height)

        if not wotd.definitions:
            return _render_error("No definitions found for today's word", width, height)

        return render_wotd(wotd, width, height)
