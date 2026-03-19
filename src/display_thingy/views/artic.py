"""Art Institute of Chicago view: fetches a random public domain artwork
and renders it as a dithered 1-bit image with a caption bar showing the
title, artist, and date.

Uses the Art Institute of Chicago's public API (https://api.artic.edu/docs/)
and their IIIF Image API for image delivery.  No authentication or API key
is required.  Only public domain artworks are selected.
"""

from __future__ import annotations

import io
import json
import logging
import random
import textwrap
from dataclasses import dataclass

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

API_BASE = "https://api.artic.edu/api/v1"

# The AIC docs recommend an additional ``AIC-User-Agent`` header as a
# courtesy identifier, since browser restrictions prevent modifying the
# standard User-Agent in some contexts.  We reuse the shared constant.
AIC_USER_AGENT = USER_AGENT

# The AIC docs recommend 843px as the standard cached image width.  This is
# slightly larger than our 800px display, so Pillow handles the final scale.
IIIF_IMAGE_WIDTH = 843


# ── Data model ──


@dataclass
class Artwork:
    """A single artwork with its downloaded image."""

    title: str
    artist_display: str
    date_display: str
    image: Image.Image


# ── API client ──


def _search_random_artwork(client: httpx.Client) -> dict:
    """Search for a random public domain artwork that has an image.

    The strategy: fetch a small batch of artworks from a random page
    and pick one at random.  This avoids the ES ``from`` parameter
    which triggers 403 errors at high offsets (the API caps pagination
    at 10,000 results).  With ``limit=10`` and up to 1,000 pages, we
    can reach all 10,000 accessible artworks in a single data request.
    """
    search_url = f"{API_BASE}/artworks/search"

    # Elasticsearch query: public domain artworks that have an image_id.
    query = {
        "bool": {
            "must": [
                {"term": {"is_public_domain": True}},
                {"exists": {"field": "image_id"}},
            ]
        }
    }
    fields = "id,title,artist_display,date_display,image_id"

    # Step 1: get the total count (no data transferred).
    # The AIC API accepts the full Elasticsearch payload as minified
    # JSON in a single ``params`` GET parameter.
    count_payload = json.dumps({"query": query, "limit": 0, "fields": fields})
    count_resp = client.get(search_url, params={"params": count_payload})
    count_resp.raise_for_status()
    total = count_resp.json().get("pagination", {}).get("total", 0)

    if total == 0:
        raise ValueError("No public domain artworks with images found")

    # Step 2: pick a random page and fetch a small batch.
    # The search API returns a Cloudflare 403 for pages above ~100
    # (with limit=10), so we cap at 100 pages — giving access to 1,000
    # distinct artworks.
    page_size = 10
    max_pages = min(total // page_size, 100)
    page = random.randint(1, max(1, max_pages))

    fetch_payload = json.dumps(
        {
            "query": query,
            "fields": fields,
            "limit": page_size,
            "page": page,
        }
    )
    resp = client.get(search_url, params={"params": fetch_payload})
    resp.raise_for_status()
    data = resp.json()

    results = data.get("data", [])
    if not results:
        raise ValueError(f"No artworks returned on page {page}")

    # Pick one artwork at random from the batch.
    artwork_data = random.choice(results)

    # The IIIF base URL comes from the API response config, so we
    # don't have to hardcode it.
    iiif_url = data.get("config", {}).get("iiif_url", "https://www.artic.edu/iiif/2")
    artwork_data["_iiif_url"] = iiif_url

    return artwork_data


def fetch_artwork() -> Artwork:
    """Fetch a random public domain artwork with its image.

    Makes 3 HTTP requests total: one to count available artworks, one
    to fetch metadata for a random artwork, and one to download the
    image via the IIIF Image API.
    """
    with httpx.Client(
        timeout=15,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "AIC-User-Agent": AIC_USER_AGENT,
            "Referer": "https://www.artic.edu/",
        },
    ) as client:
        artwork_data = _search_random_artwork(client)

        image_id = artwork_data.get("image_id")
        if not image_id:
            raise ValueError("Artwork has no image_id")

        iiif_url = artwork_data["_iiif_url"]
        image_url = f"{iiif_url}/{image_id}/full/{IIIF_IMAGE_WIDTH},/0/default.jpg"

        log.info("Downloading artwork image from %s", image_url)

        img_resp = client.get(image_url, timeout=30)
        img_resp.raise_for_status()

        image = Image.open(io.BytesIO(img_resp.content))

    title = artwork_data.get("title") or "Untitled"
    artist = artwork_data.get("artist_display") or "Unknown artist"
    date_display = artwork_data.get("date_display") or ""

    log.info("Selected artwork: %s by %s", title, artist)

    return Artwork(
        title=title,
        artist_display=artist,
        date_display=date_display,
        image=image,
    )


# ── Renderer ──

# Layout constants
CAPTION_HEIGHT = 75
CAPTION_PADDING = 10


def _crop_to_fill(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize and center-crop an image to exactly fill the target dimensions.

    Scales the image so the smaller dimension matches the target, then
    crops the center of the larger dimension.  This avoids letterboxing
    and ensures the full area is covered.
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


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, text_font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Word-wrap a text string to fit within max_width pixels."""
    avg_char_w = draw.textlength("x", font=text_font)
    chars_per_line = max(1, int(max_width / avg_char_w))
    return textwrap.wrap(text, width=chars_per_line)


def render_artwork(artwork: Artwork, width: int, height: int) -> Image.Image:
    """Render an artwork onto an 800x480 1-bit image.

    Layout (top to bottom):
    - Header bar (35px): "Art of the Day" + "Art Institute of Chicago"
    - Image area (variable, fills remaining space): dithered photograph
    - Caption bar (75px): title (bold) and artist/date (regular)
    """
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    image_area_h = height - HEADER_HEIGHT - CAPTION_HEIGHT

    # ── Header ──

    draw_header(
        draw,
        width,
        "Art of the Day",
        "Art Institute of Chicago",
        left_pad=CAPTION_PADDING,
        right_pad=CAPTION_PADDING,
    )

    # ── Image area ──
    # Convert to grayscale, crop to fill, then dither to 1-bit.
    # Pillow's default mode "1" conversion uses Floyd-Steinberg dithering,
    # which produces good results on e-paper displays.
    photo = artwork.image.convert("L")
    photo = _crop_to_fill(photo, width, image_area_h)
    dithered = photo.convert("1")
    img.paste(dithered, (0, HEADER_HEIGHT))

    # ── Caption area ──

    caption_top = HEADER_HEIGHT + image_area_h

    # White background so text is legible over any image edge.
    draw.rectangle([(0, caption_top), (width, height)], fill=WHITE)
    draw.line([(0, caption_top), (width, caption_top)], fill=BLACK, width=1)

    title_font = _font("Bold", 15)
    detail_font = _font("Regular", 13)
    max_text_w = width - 2 * CAPTION_PADDING
    line_height = 18

    # Title line(s) — bold, may wrap to a second line.
    title_lines = _wrap_text(draw, artwork.title, title_font, max_text_w)

    # Detail line — artist and date combined.
    detail = artwork.artist_display
    if artwork.date_display:
        detail = f"{detail}, {artwork.date_display}"
    # Take only the first line of artist_display (it often contains
    # multi-line nationality/dates info from the API).
    detail_first_line = detail.split("\n")[0]
    detail_lines = _wrap_text(draw, detail_first_line, detail_font, max_text_w)

    # Fit as many lines as the caption area allows.
    usable_h = CAPTION_HEIGHT - 2 * CAPTION_PADDING
    max_lines = max(1, usable_h // line_height)

    # Allocate lines: title gets priority, detail fills the rest.
    max_title_lines = min(len(title_lines), max(1, max_lines - 1))
    remaining_lines = max_lines - max_title_lines
    max_detail_lines = min(len(detail_lines), remaining_lines)

    y = caption_top + CAPTION_PADDING

    for line in title_lines[:max_title_lines]:
        draw.text((CAPTION_PADDING, y), line, fill=BLACK, font=title_font)
        y += line_height

    for line in detail_lines[:max_detail_lines]:
        draw.text((CAPTION_PADDING, y), line, fill=BLACK, font=detail_font)
        y += line_height

    # ── Outer border ──

    draw_border(draw, width, height)

    return img


# ── View class ──


@registry.register
class ArticView(BaseView):
    """Art Institute of Chicago random artwork view."""

    name = "artic"
    description = "Random artwork from the Art Institute of Chicago"

    def render(self, width: int, height: int) -> Image.Image:
        try:
            artwork = fetch_artwork()
        except Exception as exc:
            log.error("Art Institute view: %s", exc)
            from display_thingy.views._render import render_error

            return render_error(
                "Art of the Day",
                "Could not load artwork",
                str(exc),
                width,
                height,
            )

        return render_artwork(artwork, width, height)
