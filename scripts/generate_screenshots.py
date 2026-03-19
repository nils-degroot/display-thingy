"""Generate screenshot PNGs for each view, for use in the README.

Usage:
    source .envrc
    uv run python scripts/generate_screenshots.py

Renders every registered view at 800x480 and saves the result to
docs/images/{view_name}.png.  Views listed in SKIP are excluded
(e.g. CalDAV views that require a server connection).
"""

from __future__ import annotations

import sys
from pathlib import Path

from display_thingy.config import DISPLAY_HEIGHT, DISPLAY_WIDTH, Settings
from display_thingy.views import discover_views, registry

# Views to skip — these require external services that may not be
# available in a dev environment.
SKIP = {"calendar", "tasks"}

OUTPUT_DIR = Path(__file__).parent.parent / "docs" / "images"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    discover_views()

    available = sorted(registry.available())
    to_render = [name for name in available if name not in SKIP]

    print(f"Rendering {len(to_render)} views (skipping {SKIP & set(available)})...")
    print()

    failed: list[tuple[str, str]] = []

    for name in to_render:
        view_cls = registry.get(name)
        if view_cls is None:
            print(f"  {name}: not found in registry, skipping")
            continue

        print(f"  {name}...", end=" ", flush=True)
        try:
            view = view_cls(settings)
            img = view.render(DISPLAY_WIDTH, DISPLAY_HEIGHT)
            out_path = OUTPUT_DIR / f"{name}.png"
            img.save(str(out_path))
            print(f"ok ({out_path})")
        except Exception as exc:
            print(f"FAILED: {exc}")
            failed.append((name, str(exc)))

    print()
    print(f"Done. {len(to_render) - len(failed)}/{len(to_render)} succeeded.")

    if failed:
        print()
        print("Failures:")
        for name, err in failed:
            print(f"  {name}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
