"""Weather view: fetches data from OpenWeatherMap and renders a forecast display."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw, ImageFont

from display_thingy.config import FONTS_DIR, Settings
from display_thingy.views import BaseView, registry

log = logging.getLogger(__name__)

# --- Fonts ---

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(weight: str = "Regular", size: int = 16) -> ImageFont.FreeTypeFont:
    """Load an Inter font at the given size, with caching."""
    key = (weight, size)
    if key not in _font_cache:
        path = FONTS_DIR / f"Inter-{weight}.ttf"
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


# --- Data models ---


@dataclass
class CurrentWeather:
    temp: float
    feels_like: float
    humidity: int
    pressure: int
    wind_speed: float
    wind_deg: int
    description: str
    icon_code: str
    rain_1h: float = 0.0
    snow_1h: float = 0.0


@dataclass
class DailyForecast:
    date: datetime
    temp_min: float
    temp_max: float
    description: str
    icon_code: str
    pop: float  # probability of precipitation (0-1)
    rain: float = 0.0
    snow: float = 0.0


@dataclass
class WeatherData:
    current: CurrentWeather
    daily: list[DailyForecast] = field(default_factory=list)
    timezone_offset: int = 0


# --- API client ---

OWM_ONECALL_URL = "https://api.openweathermap.org/data/3.0/onecall"


def fetch_weather(settings: Settings) -> WeatherData:
    """Fetch weather data from OpenWeatherMap One Call API 3.0."""
    params = {
        "lat": settings.latitude,
        "lon": settings.longitude,
        "appid": settings.openweathermap_key,
        "units": settings.units,
        "lang": settings.lang,
        "exclude": "minutely,hourly,alerts",
    }

    response = httpx.get(OWM_ONECALL_URL, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    current_data = data["current"]
    current = CurrentWeather(
        temp=current_data["temp"],
        feels_like=current_data["feels_like"],
        humidity=current_data["humidity"],
        pressure=current_data["pressure"],
        wind_speed=current_data["wind_speed"],
        wind_deg=current_data["wind_deg"],
        description=current_data["weather"][0]["description"].title(),
        icon_code=current_data["weather"][0]["icon"],
        rain_1h=current_data.get("rain", {}).get("1h", 0.0),
        snow_1h=current_data.get("snow", {}).get("1h", 0.0),
    )

    daily = []
    for day in data.get("daily", [])[:7]:
        daily.append(
            DailyForecast(
                date=datetime.fromtimestamp(day["dt"], tz=timezone.utc),
                temp_min=day["temp"]["min"],
                temp_max=day["temp"]["max"],
                description=day["weather"][0]["description"],
                icon_code=day["weather"][0]["icon"],
                pop=day.get("pop", 0),
                rain=day.get("rain", 0),
                snow=day.get("snow", 0),
            )
        )

    return WeatherData(
        current=current,
        daily=daily,
        timezone_offset=data.get("timezone_offset", 0),
    )


# --- Weather icon drawing ---
# All icons are drawn with PIL primitives into a square canvas.
# Black = fill, White = background. Mode '1'.

BLACK = 0
WHITE = 1


def _draw_sun(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """Draw a sun: filled circle with rays."""
    # Main circle
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)
    # Rays
    ray_len = r * 0.6
    ray_start = r * 1.3
    for angle_deg in range(0, 360, 45):
        angle = math.radians(angle_deg)
        x1 = cx + ray_start * math.cos(angle)
        y1 = cy + ray_start * math.sin(angle)
        x2 = cx + (ray_start + ray_len) * math.cos(angle)
        y2 = cy + (ray_start + ray_len) * math.sin(angle)
        draw.line([(x1, y1), (x2, y2)], fill=BLACK, width=3)


def _draw_cloud(draw: ImageDraw.ImageDraw, cx: float, cy: float, w: float, h: float) -> None:
    """Draw a cloud shape using overlapping ellipses."""
    # Main body
    draw.ellipse([cx - w * 0.5, cy - h * 0.1, cx + w * 0.5, cy + h * 0.5], fill=BLACK)
    # Left bump
    draw.ellipse([cx - w * 0.55, cy - h * 0.2, cx - w * 0.05, cy + h * 0.3], fill=BLACK)
    # Right bump
    draw.ellipse([cx + w * 0.05, cy - h * 0.15, cx + w * 0.5, cy + h * 0.25], fill=BLACK)
    # Top bump
    draw.ellipse([cx - w * 0.25, cy - h * 0.5, cx + w * 0.2, cy + h * 0.1], fill=BLACK)


def _draw_rain_drops(draw: ImageDraw.ImageDraw, cx: float, cy: float, w: float) -> None:
    """Draw rain drops below a cloud."""
    drops = [(-0.3, 0), (0, 0.15), (0.3, 0.05)]
    for dx, dy in drops:
        x = cx + w * dx
        y = cy + w * (0.3 + dy)
        draw.line([(x, y), (x - 3, y + 12)], fill=BLACK, width=2)


def _draw_snow_dots(draw: ImageDraw.ImageDraw, cx: float, cy: float, w: float) -> None:
    """Draw snowflake dots below a cloud."""
    dots = [(-0.3, 0.3), (0, 0.45), (0.3, 0.35), (-0.15, 0.55), (0.15, 0.5)]
    for dx, dy in dots:
        x = cx + w * dx
        y = cy + w * dy
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=BLACK)


def _draw_lightning(draw: ImageDraw.ImageDraw, cx: float, cy: float, w: float) -> None:
    """Draw a lightning bolt below a cloud."""
    points = [
        (cx - 2, cy + w * 0.2),
        (cx + 6, cy + w * 0.35),
        (cx, cy + w * 0.35),
        (cx + 8, cy + w * 0.55),
    ]
    draw.line(points, fill=BLACK, width=3)


def draw_weather_icon(
    draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, icon_code: str
) -> None:
    """Draw a weather icon based on OWM icon code.

    Icon codes: https://openweathermap.org/weather-conditions
    01d/01n = clear, 02d/02n = few clouds, 03d/03n = scattered clouds,
    04d/04n = broken clouds, 09d/09n = shower rain, 10d/10n = rain,
    11d/11n = thunderstorm, 13d/13n = snow, 50d/50n = mist
    """
    r = size // 2
    code = icon_code[:2]  # strip day/night suffix

    if code == "01":  # Clear sky
        _draw_sun(draw, cx, cy, r * 0.4)
    elif code == "02":  # Few clouds (sun + cloud)
        _draw_sun(draw, cx - r * 0.2, cy - r * 0.2, r * 0.25)
        _draw_cloud(draw, cx + r * 0.1, cy + r * 0.1, r * 0.7, r * 0.6)
    elif code in ("03", "04"):  # Clouds
        _draw_cloud(draw, cx, cy, r * 0.8, r * 0.7)
    elif code == "09":  # Shower rain
        _draw_cloud(draw, cx, cy - r * 0.15, r * 0.7, r * 0.6)
        _draw_rain_drops(draw, cx, cy, r)
    elif code == "10":  # Rain
        _draw_sun(draw, cx - r * 0.25, cy - r * 0.3, r * 0.15)
        _draw_cloud(draw, cx + r * 0.05, cy - r * 0.05, r * 0.7, r * 0.6)
        _draw_rain_drops(draw, cx, cy, r)
    elif code == "11":  # Thunderstorm
        _draw_cloud(draw, cx, cy - r * 0.15, r * 0.7, r * 0.6)
        _draw_lightning(draw, cx, cy, r)
    elif code == "13":  # Snow
        _draw_cloud(draw, cx, cy - r * 0.15, r * 0.7, r * 0.6)
        _draw_snow_dots(draw, cx, cy, r)
    elif code == "50":  # Mist/fog
        for i in range(4):
            y = cy - r * 0.3 + i * r * 0.2
            x_off = r * 0.1 if i % 2 else 0
            draw.line(
                [(cx - r * 0.4 + x_off, y), (cx + r * 0.4 + x_off, y)],
                fill=BLACK,
                width=2,
            )
    else:
        # Fallback: question mark
        draw.text((cx - 8, cy - 10), "?", fill=BLACK, font=_font("Bold", 24))


def _wind_direction(degrees: int) -> str:
    """Convert wind degrees to compass direction."""
    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = round(degrees / 45) % 8
    return directions[idx]


def _unit_labels(units: str) -> dict[str, str]:
    """Get unit labels based on the units setting."""
    if units == "imperial":
        return {"temp": "F", "speed": "mph", "precip": "in"}
    else:  # metric or standard
        return {"temp": "C", "speed": "m/s", "precip": "mm"}


# --- Renderer ---


def render_weather(
    weather: WeatherData, settings: Settings, width: int, height: int
) -> Image.Image:
    """Render weather data into an 800x480 1-bit image."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)
    units = _unit_labels(settings.units)

    # Layout constants
    header_h = 40
    divider_y = header_h
    main_bottom = height - 140
    forecast_top = main_bottom + 5
    left_panel_w = width // 2

    # ── Header ──
    draw.text((15, 8), settings.location_name, fill=BLACK, font=_font("Bold", 24))

    now = datetime.now()
    time_str = f"Updated {now.strftime('%H:%M')}"
    time_bbox = draw.textbbox((0, 0), time_str, font=_font("Regular", 16))
    time_w = time_bbox[2] - time_bbox[0]
    draw.text((width - time_w - 15, 12), time_str, fill=BLACK, font=_font("Regular", 16))

    # Header divider
    draw.line([(10, divider_y), (width - 10, divider_y)], fill=BLACK, width=1)

    # ── Left panel: current weather ──
    panel_cy = divider_y + (main_bottom - divider_y) // 2

    # Weather icon
    icon_size = 100
    icon_cx = left_panel_w // 2
    icon_cy = panel_cy - 30
    draw_weather_icon(draw, icon_cx, icon_cy, icon_size, weather.current.icon_code)

    # Temperature
    temp_str = f"{weather.current.temp:.0f}\u00b0{units['temp']}"
    temp_font = _font("Bold", 48)
    temp_bbox = draw.textbbox((0, 0), temp_str, font=temp_font)
    temp_w = temp_bbox[2] - temp_bbox[0]
    draw.text(
        (icon_cx - temp_w // 2, icon_cy + icon_size // 2 + 10),
        temp_str,
        fill=BLACK,
        font=temp_font,
    )

    # Description
    desc_font = _font("Regular", 18)
    desc_bbox = draw.textbbox((0, 0), weather.current.description, font=desc_font)
    desc_w = desc_bbox[2] - desc_bbox[0]
    draw.text(
        (icon_cx - desc_w // 2, icon_cy + icon_size // 2 + 65),
        weather.current.description,
        fill=BLACK,
        font=desc_font,
    )

    # Vertical divider
    draw.line(
        [(left_panel_w, divider_y + 10), (left_panel_w, main_bottom - 10)],
        fill=BLACK,
        width=1,
    )

    # ── Right panel: details ──
    detail_x = left_panel_w + 30
    detail_y_start = divider_y + 25
    detail_spacing = 38
    label_font = _font("Regular", 18)
    value_font = _font("Medium", 18)
    value_x = detail_x + 175

    details = [
        ("Humidity", f"{weather.current.humidity}%"),
        (
            "Wind",
            f"{weather.current.wind_speed:.0f} {units['speed']}"
            f" {_wind_direction(weather.current.wind_deg)}",
        ),
        ("Pressure", f"{weather.current.pressure} hPa"),
    ]

    # Precipitation (rain or snow)
    precip = weather.current.rain_1h + weather.current.snow_1h
    details.append(("Precipitation", f"{precip:.1f} {units['precip']}/h"))

    # Pop from today's daily forecast
    if weather.daily:
        pop_pct = weather.daily[0].pop * 100
        details.append(("Chance of rain", f"{pop_pct:.0f}%"))

        details.append((
            "High / Low",
            f"{weather.daily[0].temp_max:.0f}\u00b0 / {weather.daily[0].temp_min:.0f}\u00b0",
        ))

    for i, (label, value) in enumerate(details):
        y = detail_y_start + i * detail_spacing
        draw.text((detail_x, y), label, fill=BLACK, font=label_font)
        draw.text((value_x, y), value, fill=BLACK, font=value_font)

    # ── Bottom divider ──
    draw.line([(10, main_bottom), (width - 10, main_bottom)], fill=BLACK, width=1)

    # ── 7-day forecast ──
    if weather.daily:
        days = weather.daily[:7]
        col_w = (width - 20) // len(days)
        day_font = _font("Medium", 16)
        temp_sm_font = _font("Regular", 14)
        pop_font = _font("Regular", 12)

        for i, day in enumerate(days):
            col_cx = 10 + i * col_w + col_w // 2

            # Day name
            day_name = day.date.strftime("%a")
            day_bbox = draw.textbbox((0, 0), day_name, font=day_font)
            day_w = day_bbox[2] - day_bbox[0]
            draw.text(
                (col_cx - day_w // 2, forecast_top + 5),
                day_name,
                fill=BLACK,
                font=day_font,
            )

            # Small weather icon
            draw_weather_icon(draw, col_cx, forecast_top + 50, 40, day.icon_code)

            # High/low
            temp_str = f"{day.temp_max:.0f}/{day.temp_min:.0f}"
            t_bbox = draw.textbbox((0, 0), temp_str, font=temp_sm_font)
            t_w = t_bbox[2] - t_bbox[0]
            draw.text(
                (col_cx - t_w // 2, forecast_top + 78),
                temp_str,
                fill=BLACK,
                font=temp_sm_font,
            )

            # Precipitation chance
            pop_str = f"{day.pop * 100:.0f}%"
            p_bbox = draw.textbbox((0, 0), pop_str, font=pop_font)
            p_w = p_bbox[2] - p_bbox[0]
            draw.text(
                (col_cx - p_w // 2, forecast_top + 98),
                pop_str,
                fill=BLACK,
                font=pop_font,
            )

    # ── Outer border ──
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=BLACK, width=2)

    return img


# --- View class ---


@registry.register
class WeatherView(BaseView):
    """Weather forecast display using OpenWeatherMap."""

    name = "weather"
    description = "Current weather and 7-day forecast"

    def render(self, width: int, height: int) -> Image.Image:
        log.info(
            "Fetching weather for %s (%.4f, %.4f)",
            self.settings.location_name,
            self.settings.latitude,
            self.settings.longitude,
        )
        weather = fetch_weather(self.settings)
        log.info(
            "Weather: %.1f°, %s",
            weather.current.temp,
            weather.current.description,
        )
        return render_weather(weather, self.settings, width, height)
