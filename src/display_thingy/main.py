"""Main entry point: load config, pick view, run the refresh loop."""

from __future__ import annotations

import logging
import signal
import sys
import time

from display_thingy.config import DISPLAY_HEIGHT, DISPLAY_WIDTH, load_settings
from display_thingy.display import create_display
from display_thingy.views import BaseView, discover_views, registry

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
        "Config loaded: views=%s, location=%s (%.4f, %.4f), interval=%ds",
        ", ".join(settings.display_views),
        settings.location_name,
        settings.latitude,
        settings.longitude,
        settings.refresh_interval,
    )

    # Discover and resolve the configured view rotation list. Invalid names
    # are skipped with a warning so that one typo doesn't prevent the other
    # views from working.
    discover_views()

    views: list[BaseView] = []
    for name in settings.display_views:
        view_cls = registry.get(name)
        if view_cls is None:
            log.warning(
                "View '%s' not found, skipping. Available: %s",
                name,
                ", ".join(registry.available()) or "(none)",
            )
            continue
        views.append(view_cls(settings))

    if not views:
        log.error(
            "No valid views configured. Available: %s",
            ", ".join(registry.available()) or "(none)",
        )
        sys.exit(1)

    if len(views) == 1:
        log.info("Using view: %s (%s)", views[0].name, views[0].description)
    else:
        rotation = " -> ".join(v.name for v in views)
        log.info(
            "View rotation: %s (%ds each)",
            rotation,
            settings.refresh_interval,
        )

    # Create display
    display = create_display(preview_mode=settings.preview_mode)

    # Main loop — rotate through the configured views, advancing to the
    # next view on each refresh cycle.
    view_index = 0
    while not _shutdown:
        view = views[view_index]
        try:
            log.info("Rendering view %d/%d: %s", view_index + 1, len(views), view.name)
            image = view.render(DISPLAY_WIDTH, DISPLAY_HEIGHT)
            display.update(image)
            log.info("Display updated, sleeping %ds", settings.refresh_interval)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Error during render/update cycle")

        view_index += 1
        if view_index >= len(views):
            view_index = 0

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
