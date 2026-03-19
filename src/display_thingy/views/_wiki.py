"""Shared wiki markup stripping utilities for Wikimedia-based views.

Both the wikiquote and wiktionary views need to strip MediaWiki markup
from template fields.  They share a common base pipeline (comments,
``<br>``, wiki links, whitespace collapse) which this module provides
as ``strip_basic_wiki_markup``.

The wiktionary view adds extra steps on top (label templates, generic
templates, italics, continuation markers, region markers) in its own
``_strip_wiki_markup`` function, calling this base as the first stage.
"""

from __future__ import annotations

import re

# Matches [[Target|display text]] or [[word]] wiki links.
WIKI_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")

# Matches <!-- HTML comments -->, including multi-line.
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Matches <br />, <br/>, <br> tags.
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def strip_basic_wiki_markup(raw: str) -> str:
    """Remove common wiki markup, returning plain text.

    Handles the four-step pipeline shared by wikiquote and wiktionary:

    1. ``<!-- comments -->``  -> removed
    2. ``<br />``             -> space
    3. ``[[Target|display]]`` -> ``display``; ``[[word]]`` -> ``word``
    4. Consecutive whitespace -> single space, then strip
    """
    text = COMMENT_RE.sub("", raw)
    text = BR_RE.sub(" ", text)
    text = WIKI_LINK_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
