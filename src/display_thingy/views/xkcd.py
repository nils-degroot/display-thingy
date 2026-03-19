"""xkcd comic view: fetches the latest xkcd comic and renders it as a
dithered 1-bit image with title and alt text, suitable for e-paper display."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

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
    render_error,
    truncate_text,
)
from display_thingy.views._render import (
    font as _font,
)

log = logging.getLogger(__name__)


# --- Data model ---


@dataclass
class Comic:
    num: int
    title: str
    alt: str
    image: Image.Image
    date_str: str


# --- API client ---

LATEST_COMIC_URL = "https://xkcd.com/info.0.json"

# Month abbreviations for the header context text. The API returns month
# as a string like "3", so we map to short names ourselves rather than
# pulling in calendar/datetime just for formatting.
_MONTH_ABBR = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def fetch_latest_comic() -> Comic:
    """Fetch the latest xkcd comic.

    Makes two HTTP requests: one for the JSON metadata, one to download
    the comic image.  Returns a Comic with the raw PIL Image.
    """
    log.info("Fetching latest xkcd comic metadata from %s", LATEST_COMIC_URL)

    resp = httpx.get(
        LATEST_COMIC_URL,
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()

    num = data["num"]
    title = data["title"]
    alt = data["alt"]
    img_url = data["img"]

    # Build a human-readable date string like "Mar 18" for the header.
    month_idx = int(data.get("month", "0"))
    day = data.get("day", "")
    month_name = _MONTH_ABBR[month_idx] if 1 <= month_idx <= 12 else "?"
    date_str = f"{month_name} {day}"

    log.info("Downloading comic #%d image from %s", num, img_url)

    img_resp = httpx.get(
        img_url,
        timeout=30,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    img_resp.raise_for_status()

    image = Image.open(io.BytesIO(img_resp.content))

    return Comic(num=num, title=title, alt=alt, image=image, date_str=date_str)


# --- Renderer ---

# Layout constants for the footer bar (title + alt text).
FOOTER_HEIGHT = 55
FOOTER_PADDING = 10
TITLE_LINE_HEIGHT = 20
ALT_LINE_HEIGHT = 18


def _scale_to_fit(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale an image to fit within the target dimensions, preserving aspect ratio.

    Unlike crop-to-fill, this never clips content — important for comics
    where meaningful details often sit at the edges.  The caller is
    responsible for centering the result on a white background.
    """
    src_w, src_h = image.size
    scale = min(target_w / src_w, target_h / src_h)

    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))

    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)


def render_comic(comic: Comic, width: int, height: int) -> Image.Image:
    """Render an xkcd comic into a 1-bit image for e-paper display.

    Layout (top to bottom):
    - Header bar (35px): "xkcd" + "#N · Mon DD"
    - Image area (fills remaining space): comic scaled to fit, centered
    - Footer bar (55px): title (Bold 14) + alt text (Regular 12, truncated)
    """
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    image_area_h = height - HEADER_HEIGHT - FOOTER_HEIGHT

    # ── Header ──

    context = f"#{comic.num} \u00b7 {comic.date_str}"
    draw_header(draw, width, "xkcd", context)

    # ── Image area ──
    #
    # Convert to greyscale, scale to fit within the image area, then
    # dither to 1-bit.  Line art comics will mostly threshold cleanly;
    # the occasional greyscale comic benefits from Floyd-Steinberg.

    greyscale = comic.image.convert("L")
    scaled = _scale_to_fit(greyscale, width, image_area_h)
    dithered = scaled.convert("1")

    # Center the scaled image within the image area.  Any leftover space
    # is white, which is invisible on the e-paper background.
    paste_x = (width - dithered.width) // 2
    paste_y = HEADER_HEIGHT + (image_area_h - dithered.height) // 2
    img.paste(dithered, (paste_x, paste_y))

    # ── Footer bar ──

    footer_top = HEADER_HEIGHT + image_area_h

    # Divider above footer.
    draw.line([(0, footer_top), (width, footer_top)], fill=BLACK, width=1)

    max_text_w = width - 2 * FOOTER_PADDING

    # Title line (Bold 14).
    title_font = _font("Bold", 14)
    title_text = truncate_text(draw, comic.title, title_font, max_text_w)
    title_y = footer_top + FOOTER_PADDING
    draw.text((FOOTER_PADDING, title_y), title_text, fill=BLACK, font=title_font)

    # Alt text line (Regular 12, truncated).
    alt_font = _font("Regular", 12)
    alt_text = truncate_text(draw, comic.alt, alt_font, max_text_w)
    alt_y = title_y + TITLE_LINE_HEIGHT
    draw.text((FOOTER_PADDING, alt_y), alt_text, fill=BLACK, font=alt_font)

    # ── Outer border ──
    draw_border(draw, width, height)

    return img


# --- View class ---


@registry.register
class XkcdView(BaseView):
    """xkcd latest comic display."""

    name = "xkcd"
    description = "xkcd latest comic"

    def render(self, width: int, height: int) -> Image.Image:
        try:
            log.info("Fetching latest xkcd comic")
            comic = fetch_latest_comic()
            log.info("Got comic #%d: %s", comic.num, comic.title)
            return render_comic(comic, width, height)
        except Exception:
            log.exception("Failed to fetch or render xkcd comic")
            return render_error(
                "xkcd",
                "Could not load comic",
                "Check network connection and try again.",
                width,
                height,
            )
