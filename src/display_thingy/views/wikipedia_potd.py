"""Wikipedia Picture of the Day view: fetches the daily featured image and renders it
as a dithered 1-bit image with a caption bar, suitable for e-paper display."""

from __future__ import annotations

import io
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
    USER_AGENT,
    WHITE,
    draw_border,
    draw_header,
)
from display_thingy.views._render import (
    font as _font,
)

log = logging.getLogger(__name__)


# --- Data model ---


@dataclass
class PictureOfTheDay:
    image: Image.Image
    description: str
    title: str


# --- API client ---

FEATURED_CONTENT_URL = "https://en.wikipedia.org/api/rest_v1/feed/featured"

# Width to request for the thumbnail. Wikimedia only serves thumbnails at
# specific "step" sizes (20, 40, 60, 120, 250, 330, 500, 960, 1280, ...);
# requesting arbitrary widths returns a 429. We use 960px -- the smallest
# step larger than our 800px display -- and let Pillow downscale the rest.
THUMBNAIL_WIDTH = 960

# The Wikimedia thumbnail URL contains a `{N}px-` segment that controls the
# rendered width. We rewrite it to request exactly the width we need.
_THUMB_WIDTH_RE = re.compile(r"/(\d+)px-")


def fetch_potd(today: date | None = None) -> PictureOfTheDay:
    """Fetch today's Wikipedia Picture of the Day.

    Calls the Wikimedia featured-content API for the given date (defaults to
    today), extracts the image metadata, downloads the thumbnail at display
    width, and returns a PictureOfTheDay with the raw PIL Image.
    """
    if today is None:
        today = date.today()

    url = f"{FEATURED_CONTENT_URL}/{today.year}/{today.month:02d}/{today.day:02d}"
    log.info("Fetching featured content from %s", url)

    response = httpx.get(
        url,
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    data = response.json()

    image_data = data.get("image")
    if image_data is None:
        raise ValueError(f"No picture of the day for {today}")

    # Extract metadata.
    description = image_data.get("description", {}).get("text", "")
    title = image_data.get("title", "")

    # Build a thumbnail URL at the width we actually need. The API returns a
    # thumbnail URL with an arbitrary default width; we swap in our own.
    thumb_url = image_data.get("thumbnail", {}).get("source", "")
    if not thumb_url:
        # Fall back to full-resolution image if no thumbnail is available.
        thumb_url = image_data.get("image", {}).get("source", "")
    else:
        thumb_url = _THUMB_WIDTH_RE.sub(f"/{THUMBNAIL_WIDTH}px-", thumb_url)

    if not thumb_url:
        raise ValueError(f"No image URL found for {today}")

    log.info("Downloading image from %s", thumb_url)
    img_response = httpx.get(
        thumb_url,
        timeout=30,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    img_response.raise_for_status()

    image = Image.open(io.BytesIO(img_response.content))

    return PictureOfTheDay(
        image=image,
        description=description,
        title=title,
    )


# --- Renderer ---

# Layout constants
CAPTION_HEIGHT = 75
CAPTION_PADDING = 10


def _crop_to_fill(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize and center-crop an image to exactly fill the target dimensions.

    Scales the image so the smaller dimension matches the target, then crops
    the center of the larger dimension. This avoids letterboxing and ensures
    the full area is covered.
    """
    src_w, src_h = image.size
    src_aspect = src_w / src_h
    target_aspect = target_w / target_h

    if src_aspect > target_aspect:
        # Source is wider than target: scale to match height, crop width.
        scale_h = target_h
        scale_w = int(src_w * (target_h / src_h))
    else:
        # Source is taller than target: scale to match width, crop height.
        scale_w = target_w
        scale_h = int(src_h * (target_w / src_w))

    resized = image.resize((scale_w, scale_h), Image.Resampling.LANCZOS)

    # Center-crop to exact target size.
    left = (scale_w - target_w) // 2
    top = (scale_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _wrap_description(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Word-wrap a description string to fit within max_width pixels.

    Returns the wrapped lines. Uses a character-width estimate to find an
    initial wrap width, then trims any lines that still overflow.
    """
    # Estimate characters per line from the average character width.
    avg_char_w = draw.textlength("x", font=font)
    chars_per_line = max(1, int(max_width / avg_char_w))

    # textwrap operates on character counts, which is a reasonable approximation
    # for a monospace-ish proportional font at small sizes.
    lines = textwrap.wrap(text, width=chars_per_line)
    return lines


def render_potd(potd: PictureOfTheDay, width: int, height: int) -> Image.Image:
    """Render the Picture of the Day into an 800x480 1-bit image.

    Layout (top to bottom):
    - Header bar (35px): "Picture of the Day" + date
    - Image area (variable, fills remaining space): dithered photograph
    - Caption bar (75px): description text, word-wrapped
    """
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    image_area_h = height - HEADER_HEIGHT - CAPTION_HEIGHT

    # ── Header ──
    today_str = date.today().strftime("%B %-d, %Y")
    draw_header(draw, width, "Picture of the Day", today_str)

    # ── Image area ──
    # Convert to grayscale, crop to fill the image area, then dither to 1-bit.
    photo = potd.image.convert("L")
    photo = _crop_to_fill(photo, width, image_area_h)

    # Pillow's default "1" conversion uses Floyd-Steinberg dithering, which
    # produces good results on e-paper displays.
    dithered = photo.convert("1")
    img.paste(dithered, (0, HEADER_HEIGHT))

    # ── Caption area ──
    caption_top = HEADER_HEIGHT + image_area_h

    # Caption background (white bar so text is legible over any image edge).
    draw.rectangle(
        [(0, caption_top), (width, height)],
        fill=WHITE,
    )

    # Divider above caption
    draw.line([(0, caption_top), (width, caption_top)], fill=BLACK, width=1)

    caption_font = _font("Regular", 14)
    max_text_w = width - 2 * CAPTION_PADDING
    lines = _wrap_description(draw, potd.description, caption_font, max_text_w)

    # Calculate how many lines fit in the caption area. Leave some vertical
    # padding above and below the text.
    line_height = 18
    usable_h = CAPTION_HEIGHT - 2 * CAPTION_PADDING
    max_lines = max(1, usable_h // line_height)

    # Truncate with ellipsis if the description is too long.
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        # Trim the last line and append an ellipsis.
        if len(last) > 3:
            lines[-1] = last[:-3].rstrip() + "..."

    for i, line in enumerate(lines):
        y = caption_top + CAPTION_PADDING + i * line_height
        draw.text((CAPTION_PADDING, y), line, fill=BLACK, font=caption_font)

    # ── Outer border ──
    draw_border(draw, width, height)

    return img


# --- View class ---


@registry.register
class WikipediaPotdView(BaseView):
    """Wikipedia Picture of the Day display."""

    name = "wikipedia"
    description = "Wikipedia Picture of the Day"

    def render(self, width: int, height: int) -> Image.Image:
        log.info("Fetching Wikipedia Picture of the Day")
        potd = fetch_potd()
        log.info("Got image: %s", potd.title)
        return render_potd(potd, width, height)
