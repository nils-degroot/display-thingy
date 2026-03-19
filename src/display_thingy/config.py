"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


ASSETS_DIR = Path(__file__).parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
PREVIEW_DIR = Path(__file__).parent.parent.parent / "preview"

# Display dimensions for Waveshare 7.5" V2
DISPLAY_WIDTH = 800
DISPLAY_HEIGHT = 480


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Compatible with direnv (.envrc) — all values come from env vars.
    """

    # Required
    openweathermap_key: str
    latitude: float = 52.3508
    longitude: float = 5.2647
    location_name: str = "Almere"

    # Display
    display_view: str = "weather"
    refresh_interval: int = 900  # seconds
    preview_mode: bool = False

    # Weather
    units: str = "metric"  # metric / imperial / standard
    lang: str = "en"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def load_settings() -> Settings:
    """Load and validate settings from environment."""
    return Settings()
