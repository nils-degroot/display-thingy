"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

ASSETS_DIR = Path(__file__).parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
PREVIEW_DIR = Path(__file__).parent.parent.parent / "preview"

# Display dimensions for Waveshare 7.5" V2
DISPLAY_WIDTH = 800
DISPLAY_HEIGHT = 480


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated string into a list, stripping whitespace."""
    return [v.strip() for v in raw.split(",") if v.strip()]


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Compatible with direnv (.envrc) — all values come from env vars.

    Comma-separated fields (``display_views_csv``, ``caldav_task_lists_csv``)
    are kept as plain ``str`` to avoid pydantic-settings trying to JSON-decode
    values like ``"weather,wikipedia"``.  Use the corresponding properties
    (``display_views``, ``caldav_task_lists``) to get the parsed ``list[str]``.
    """

    # Required
    openweathermap_key: str
    latitude: float = 52.3508
    longitude: float = 5.2647
    location_name: str = "Almere"

    # Display — comma-separated list of view names to rotate through.
    # Each view is shown for one refresh interval before advancing to the next.
    display_views_csv: str = Field("weather", validation_alias="DISPLAY_VIEWS")
    refresh_interval: int = 900  # seconds
    preview_mode: bool = False

    # Weather
    units: str = "metric"  # metric / imperial / standard
    lang: str = "en"

    # CalDAV (required only when the "tasks" view is enabled).
    # Works with any CalDAV server (Nextcloud, Radicale, Baikal, etc.).
    caldav_url: str = ""          # e.g. "https://cloud.example.com"
    caldav_username: str = ""
    caldav_password: str = ""     # app password recommended
    caldav_task_lists_csv: str = Field(
        "", validation_alias="CALDAV_TASK_LISTS"
    )  # comma-separated list names; empty = all

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # -- Parsed accessors for comma-separated fields --

    _display_views: list[str] = []
    _caldav_task_lists: list[str] = []

    @model_validator(mode="after")
    def _parse_comma_separated_fields(self) -> Settings:
        """Pre-parse CSV fields so the properties return cached lists."""
        object.__setattr__(self, "_display_views", _split_csv(self.display_views_csv))
        object.__setattr__(
            self, "_caldav_task_lists", _split_csv(self.caldav_task_lists_csv)
        )
        return self

    @property
    def display_views(self) -> list[str]:
        """View names to rotate through, parsed from ``DISPLAY_VIEWS``."""
        return self._display_views

    @property
    def caldav_task_lists(self) -> list[str]:
        """CalDAV list names to display, parsed from ``CALDAV_TASK_LISTS``."""
        return self._caldav_task_lists


def load_settings() -> Settings:
    """Load and validate settings from environment."""
    return Settings()
