"""Main entry point: load config, pick view, run the refresh loop."""

from __future__ import annotations

import logging
import signal
import sys
import time

from display_thingy.config import DISPLAY_HEIGHT, DISPLAY_WIDTH, load_settings
from display_thingy.display import create_display
from display_thingy.views import discover_views, registry

log = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    if not _shutdown:
        log.info("Received signal %s, shutting down...", signal.Signals(signum).name)
    _shutdown = True


def main() -> None:
    """Application entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handle graceful shutdown
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Load config
    try:
        settings = load_settings()
    except Exception:
        log.exception("Failed to load settings")
        sys.exit(1)

    log.info(
        "Config loaded: view=%s, location=%s (%.4f, %.4f), interval=%ds",
        settings.display_view,
        settings.location_name,
        settings.latitude,
        settings.longitude,
        settings.refresh_interval,
    )

    # Discover and select view
    discover_views()
    view_cls = registry.get(settings.display_view)
    if view_cls is None:
        log.error(
            "View '%s' not found. Available views: %s",
            settings.display_view,
            ", ".join(registry.available()) or "(none)",
        )
        sys.exit(1)

    view = view_cls(settings)
    log.info("Using view: %s (%s)", view.name, view.description)

    # Create display
    display = create_display(preview_mode=settings.preview_mode)

    # Main loop
    while not _shutdown:
        try:
            log.info("Rendering view: %s", view.name)
            image = view.render(DISPLAY_WIDTH, DISPLAY_HEIGHT)
            display.update(image)
            log.info("Display updated, sleeping %ds", settings.refresh_interval)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Error during render/update cycle")

        # Sleep in short intervals so we can respond to shutdown signals
        elapsed = 0
        while elapsed < settings.refresh_interval and not _shutdown:
            time.sleep(1)
            elapsed += 1

    # Cleanup
    log.info("Shutting down...")
    try:
        display.clear()
        display.close()
    except Exception:
        log.exception("Error during cleanup")
    log.info("Goodbye.")


if __name__ == "__main__":
    main()
