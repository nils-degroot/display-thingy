# display-thingy

E-paper display manager for Raspberry Pi (Waveshare 7.5" V2, 800x480,
1-bit black/white) with a pluggable view system.

## Project layout

```
src/display_thingy/
├── main.py              # Entry point: config loading, view rotation loop
├── config.py            # pydantic-settings config from env vars
├── display.py           # Display abstraction (EpaperDisplay / PreviewDisplay)
├── assets/fonts/        # Inter font family (Regular, Medium, Bold)
└── views/
    ├── __init__.py      # BaseView, ViewRegistry, auto-discovery
    ├── _render.py       # Shared rendering: font, constants, header/border/overflow/truncation/error
    ├── _caldav.py       # Shared CalDAV: collection discovery, response parsing
    ├── _wiki.py         # Shared wiki markup stripping
    ├── calendar.py      # CalDAV agenda (7-day upcoming events)
    ├── weather.py       # OpenWeatherMap current + 7-day forecast
    ├── wikipedia_potd.py # Wikimedia Picture of the Day
    ├── tasks.py         # CalDAV VTODO task list
    ├── hackernews.py    # Hacker News top stories
    ├── github.py        # GitHub personal activity feed
    ├── rss.py           # RSS/Atom feed reader
    ├── wikiquote.py     # Wikiquote Quote of the Day
    ├── wiktionary.py    # Wiktionary Word of the Day
    ├── xkcd.py          # xkcd latest comic
    └── system.py        # Local system stats dashboard (CPU, memory, disk, network, uptime)

deploy/
├── install.sh           # First-time systemd service installation
└── update.sh            # Pull + sync + restart (for deployed Pi)
```

## Tooling

- **Package manager**: `uv` (not pip). Use `uv sync`, `uv run`,
  `uv add`, etc.
- **Linter**: `ruff` via `uv run ruff check src/`. Config is in
  `pyproject.toml` (line-length 100, Python 3.12, rules E/F/I/W).
- **Build backend**: hatchling.
- **Testing**: `uv run pytest` (the `dev` extra includes pytest).
- **No type checker** is configured in CI, but pyright/mypy may be used
  in the editor. Keep type annotations honest.

## Key conventions

### View plugin system

Views are auto-discovered at startup. To add a new view:

1. Create a `.py` file in `src/display_thingy/views/`.
2. Subclass `BaseView`, set `name` and `description` class attributes.
3. Decorate with `@registry.register`.
4. Implement `render(self, width: int, height: int) -> Image.Image`
   returning a mode `"1"` (1-bit) PIL image.

The view receives `self.settings` (a `Settings` instance) in its
constructor. All data fetching happens inside `render()`.

### Configuration (pydantic-settings)

All config comes from environment variables (or `.envrc` via direnv).
The `Settings` class lives in `config.py`.

**Comma-separated list fields** require special handling because
pydantic-settings tries to JSON-decode `list[str]` fields before any
validator runs, rejecting values like `"weather,wikipedia"`. The
workaround is:

1. Declare the field as `str` with a `validation_alias` matching the
   desired env var name (e.g. `Field("weather",
   validation_alias="DISPLAY_VIEWS")`).
2. Name the field with a `_csv` suffix (e.g. `display_views_csv`).
3. Add a `@property` that returns `list[str]` (e.g. `display_views`).
4. The `model_validator(mode="after")` parses the CSV string into the
   private `_display_views` list that the property returns.

When adding a new comma-separated config field, follow this existing
pattern exactly.

### Waveshare e-paper driver

The `waveshare_epd` package is installed manually from a git clone
(`uv pip install .`) and is **not** in `pyproject.toml`. Therefore
`uv sync` must always use `--inexact` to avoid removing it. The
`deploy/update.sh` script already handles this.

### Display constraints

- Resolution: 800x480 pixels, 1-bit (black and white only).
- All images returned from `render()` must be mode `"1"`.
- For photographs or greyscale content, use Floyd-Steinberg dithering
  (`image.convert("1")` does this by default in Pillow).
- Font: Inter (Regular, Medium, Bold) in `assets/fonts/`. Each view
  caches loaded fonts in a module-level `_font_cache` dict.

### Wikimedia API

When fetching thumbnails from Wikimedia Commons, you must use one of the
allowed "step" widths: 20, 40, 60, 120, 250, 330, 500, 960, 1280,
1920, 3840. Arbitrary widths return HTTP 429.

### CalDAV / tasks view

- Uses raw HTTP (httpx) with PROPFIND/REPORT XML requests, not a
  CalDAV client library.
- The `icalendar` package parses VTODO components.
- Subtasks are shown with single-level indentation only (no deep
  nesting).
- Tasks are sorted by RFC 5545 priority (1-4 high, 5 medium, 6-9 low,
  0 undefined) then by due date.
- If the server is unreachable, the view renders an error screen instead
  of crashing.

### Error handling in views

Views should catch their own errors and render a human-readable error
image (white background with error text) rather than raising exceptions
that would crash the main loop. The main loop also has a catch-all, but
view-level error handling gives better user feedback on the display.

## Deploy scripts

Shell scripts in `deploy/` are written for bash and should pass
`shellcheck`. They use `set -euo pipefail`.

- `install.sh`: First-time setup. Generates `~/.display-thingy.env`
  from `.envrc`, installs a systemd service, enables and starts it.
- `update.sh`: Pulls latest code, syncs deps with `--inexact`,
  regenerates the env file, reinstalls the systemd unit, and restarts
  the service. Refuses to run if the working tree has uncommitted
  changes.

## Development workflow

On a dev machine (no e-paper hardware), the display automatically falls
back to `PreviewDisplay`, which saves PNGs to `preview/latest.png`.
Set `PREVIEW_MODE=true` to force this even on a Pi.

```bash
uv sync
cp .envrc.example .envrc  # fill in at least OPENWEATHERMAP_KEY
source .envrc
uv run display-thingy
```

## Documentation

When adding or modifying views, configuration fields, or user-facing
features, always update `README.md` to reflect the changes. This
includes:

- Adding/removing/renaming a view section under **Views**.
- Updating the `DISPLAY_VIEWS` example line to list all available views.
- Documenting new env vars in the relevant view's config table.
- Updating setup instructions if dependencies or install steps change.
