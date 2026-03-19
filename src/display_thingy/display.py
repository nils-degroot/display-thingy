"""Display abstraction: e-paper hardware on Pi, PNG preview on dev machines."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from PIL import Image

from display_thingy.config import DISPLAY_HEIGHT, DISPLAY_WIDTH, PREVIEW_DIR

log = logging.getLogger(__name__)


class Display(ABC):
    """Abstract display interface."""

    @abstractmethod
    def update(self, image: Image.Image) -> None:
        """Push an image to the display."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clear the display to white."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Clean up resources."""
        ...


class PreviewDisplay(Display):
    """Development display that saves images as PNGs."""

    def __init__(self, output_dir: Path = PREVIEW_DIR) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        log.info("Preview display: saving images to %s", self.output_dir)

    def update(self, image: Image.Image) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"display_{timestamp}.png"

        # Also save as 'latest.png' for easy viewing
        latest = self.output_dir / "latest.png"

        image.save(path)
        image.save(latest)
        log.info("Preview saved: %s", path)

    def clear(self) -> None:
        white = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 1)
        self.update(white)
        log.info("Display cleared")

    def close(self) -> None:
        log.info("Preview display closed")


class EpaperDisplay(Display):
    """Waveshare 7.5" V2 e-paper display."""

    def __init__(self) -> None:
        try:
            from waveshare_epd import epd7in5_V2  # type: ignore[import-untyped]
        except ImportError as e:
            raise RuntimeError(
                "waveshare_epd not installed. Install it from: "
                "https://github.com/waveshareteam/e-Paper "
                "(RaspberryPi_JetsonNano/python/)"
            ) from e

        self.epd = epd7in5_V2.EPD()
        self.epd.init()
        self.epd.Clear()
        log.info("E-paper display initialized (%dx%d)", self.epd.width, self.epd.height)

    def update(self, image: Image.Image) -> None:
        self.epd.init()
        self.epd.display(self.epd.getbuffer(image))
        self.epd.sleep()
        log.info("E-paper display updated")

    def clear(self) -> None:
        self.epd.init()
        self.epd.Clear()
        self.epd.sleep()
        log.info("E-paper display cleared")

    def close(self) -> None:
        try:
            self.epd.sleep()
            from waveshare_epd import epd7in5_V2  # type: ignore[import-untyped]

            epd7in5_V2.epdconfig.module_exit(cleanup=True)
        except Exception:
            log.exception("Error closing e-paper display")
        log.info("E-paper display closed")


def _is_raspberry_pi() -> bool:
    """Detect if we're running on a Raspberry Pi."""
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().lower()
            return "raspberry pi" in model
    except (FileNotFoundError, PermissionError):
        return False


def create_display(preview_mode: bool = False) -> Display:
    """Create the appropriate display backend.

    Args:
        preview_mode: If True, always use preview display regardless of platform.

    Returns:
        An EpaperDisplay on Raspberry Pi, PreviewDisplay otherwise.
    """
    if preview_mode:
        log.info("Preview mode forced by config")
        return PreviewDisplay()

    if _is_raspberry_pi():
        try:
            return EpaperDisplay()
        except RuntimeError:
            log.warning("E-paper init failed, falling back to preview display")
            return PreviewDisplay()

    log.info("Not running on Raspberry Pi, using preview display")
    return PreviewDisplay()
