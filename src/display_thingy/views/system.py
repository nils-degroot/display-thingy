"""System stats view: displays CPU, memory, disk, network, and uptime
information in a dashboard-style 2x2 grid layout with progress bars."""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass

import psutil
from PIL import Image, ImageDraw

from display_thingy.views import BaseView, registry
from display_thingy.views._render import (
    BLACK,
    HEADER_HEIGHT,
    WHITE,
    draw_border,
    draw_header,
    render_error,
)
from display_thingy.views._render import (
    font as _font,
)

log = logging.getLogger(__name__)


# ── Data models ──


@dataclass
class CpuStats:
    usage_percent: float   # 0-100
    temperature: float | None  # degrees Celsius, None if unavailable


@dataclass
class MemoryStats:
    used_gb: float
    total_gb: float
    percent: float  # 0-100


@dataclass
class DiskStats:
    used_gb: float
    total_gb: float
    percent: float  # 0-100


@dataclass
class NetworkInterface:
    name: str       # e.g. "eth0", "wlan0"
    address: str    # IPv4 address


@dataclass
class SystemInfo:
    hostname: str
    cpu: CpuStats
    memory: MemoryStats
    disk: DiskStats
    networks: list[NetworkInterface]
    uptime_seconds: int


# ── Data collection ──
#
# All stats come from psutil and the standard library.  On non-Pi
# systems (dev machines), temperature sensors may not exist — we
# handle that gracefully by returning None.


def _get_cpu_temperature() -> float | None:
    """Read the SoC temperature, returning None if sensors are unavailable.

    On a Raspberry Pi the sensor is typically reported under the
    ``cpu_thermal`` or ``cpu-thermal`` key.  On other Linux systems it
    may appear under ``coretemp`` or ``k10temp``.  We try common keys
    in order and fall back to the first available sensor.
    """
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        # sensors_temperatures() is not available on all platforms
        # (e.g. macOS raises AttributeError).
        return None

    if not temps:
        return None

    # Preferred sensor names, ordered by likelihood on a Pi.
    preferred = ["cpu_thermal", "cpu-thermal", "coretemp", "k10temp"]
    for key in preferred:
        if key in temps and temps[key]:
            return temps[key][0].current

    # Fall back to whatever sensor is available first.
    for entries in temps.values():
        if entries:
            return entries[0].current

    return None


def collect_stats() -> SystemInfo:
    """Gather all system stats in one pass."""
    hostname = socket.gethostname()

    # CPU: use a short sampling interval so render() doesn't block
    # too long.  0.5s gives a reasonable reading without feeling sluggish.
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_temp = _get_cpu_temperature()

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    # Collect non-loopback IPv4 addresses.
    net_addrs = psutil.net_if_addrs()
    networks: list[NetworkInterface] = []
    for iface_name, addrs in sorted(net_addrs.items()):
        if iface_name == "lo":
            continue
        for addr in addrs:
            if addr.family == socket.AF_INET:
                networks.append(NetworkInterface(name=iface_name, address=addr.address))

    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    return SystemInfo(
        hostname=hostname,
        cpu=CpuStats(usage_percent=cpu_percent, temperature=cpu_temp),
        memory=MemoryStats(
            used_gb=mem.used / (1024**3),
            total_gb=mem.total / (1024**3),
            percent=mem.percent,
        ),
        disk=DiskStats(
            used_gb=disk.used / (1024**3),
            total_gb=disk.total / (1024**3),
            percent=disk.percent,
        ),
        networks=networks,
        uptime_seconds=uptime_seconds,
    )


# ── Rendering ──
#
# The layout is a dashboard-style 2x2 grid with a header and uptime
# footer bar:
#
#   ┌──────────────────────────────────────────────┐
#   │  System Stats                   hostname     │  35px header
#   ├───────────────────────┬──────────────────────┤
#   │  CPU                  │  Memory              │
#   │  ▓▓▓▓▓▓▓▓░░░░  62%   │  ▓▓▓▓▓▓▓▓░░░░  74%  │
#   │  Temperature: 48.5°C  │  2.8 / 3.8 GB        │
#   ├───────────────────────┼──────────────────────┤
#   │  Disk                 │  Network             │
#   │  ▓▓▓▓▓▓▓▓░░░░  45%   │  eth0: 192.168.1.42  │
#   │  13.2 / 29.1 GB      │  wlan0: 192.168.1.43 │
#   ├───────────────────────┴──────────────────────┤
#   │  Uptime: 14d 3h 22m                          │  40px footer
#   └──────────────────────────────────────────────┘

PADDING = 15
FOOTER_HEIGHT = 40
BAR_HEIGHT = 20
BAR_RADIUS = 4


def _format_uptime(seconds: int) -> str:
    """Format an uptime duration as a compact human-readable string.

    Examples: ``"3m"``, ``"2h 15m"``, ``"14d 3h 22m"``.
    """
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    # Always show minutes unless we already have days+hours, to avoid
    # cluttering the line.  For very long uptimes (>= 1 day) we still
    # include minutes for precision.
    parts.append(f"{minutes}m")

    return " ".join(parts)


def _draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    percent: float,
) -> None:
    """Draw a horizontal progress bar with a filled portion.

    The bar has a 1px black outline and a filled region proportional
    to *percent* (0-100).  We use rounded rectangles for a cleaner
    look on the e-paper display.
    """
    # Outer border (rounded rectangle).
    draw.rounded_rectangle(
        [(x, y), (x + width, y + BAR_HEIGHT)],
        radius=BAR_RADIUS,
        outline=BLACK,
        width=1,
    )

    # Filled region.  We need at least a few pixels to draw a visible
    # rounded rectangle, so skip the fill for very small percentages.
    fill_width = int((width - 2) * min(percent, 100) / 100)
    if fill_width > BAR_RADIUS * 2:
        draw.rounded_rectangle(
            [(x + 1, y + 1), (x + 1 + fill_width, y + BAR_HEIGHT - 1)],
            radius=max(BAR_RADIUS - 1, 1),
            fill=BLACK,
        )


def _draw_section_title(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    title: str,
) -> int:
    """Draw a section title and return the y position below it."""
    title_font = _font("Bold", 18)
    draw.text((x, y), title, font=title_font, fill=BLACK)
    return y + 28


def render_system(info: SystemInfo, width: int, height: int) -> Image.Image:
    """Render the system stats dashboard onto a 1-bit image."""
    img = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ── Header ──

    draw_header(
        draw, width, "System Stats", info.hostname,
        left_pad=PADDING, right_pad=PADDING,
    )

    # ── Grid layout ──
    #
    # The usable area between the header and footer is divided into a
    # 2x2 grid.  Each cell gets a section title, a progress bar (for
    # CPU/memory/disk), and detail text.

    grid_top = HEADER_HEIGHT + 1
    grid_bottom = height - FOOTER_HEIGHT
    grid_height = grid_bottom - grid_top

    col_width = width // 2
    row_height = grid_height // 2

    # Vertical divider.
    draw.line(
        [(col_width, grid_top), (col_width, grid_bottom)],
        fill=BLACK, width=1,
    )

    # Horizontal divider between the two rows.
    mid_y = grid_top + row_height
    draw.line(
        [(0, mid_y), (width, mid_y)],
        fill=BLACK, width=1,
    )

    label_font = _font("Regular", 16)
    value_font = _font("Medium", 16)
    detail_font = _font("Regular", 15)

    bar_width = col_width - PADDING * 2 - 70  # leave room for percentage text

    # ── Top-left: CPU ──

    cell_x = PADDING
    cell_y = grid_top + 12
    cell_y = _draw_section_title(draw, cell_x, cell_y, "CPU")

    _draw_progress_bar(draw, cell_x, cell_y, bar_width, info.cpu.usage_percent)
    pct_text = f"{info.cpu.usage_percent:.0f}%"
    draw.text((cell_x + bar_width + 10, cell_y + 1), pct_text, font=value_font, fill=BLACK)
    cell_y += BAR_HEIGHT + 14

    if info.cpu.temperature is not None:
        temp_text = f"Temperature: {info.cpu.temperature:.1f}\u00b0C"
    else:
        temp_text = "Temperature: N/A"
    draw.text((cell_x, cell_y), temp_text, font=detail_font, fill=BLACK)

    # ── Top-right: Memory ──

    cell_x = col_width + PADDING
    cell_y = grid_top + 12
    cell_y = _draw_section_title(draw, cell_x, cell_y, "Memory")

    _draw_progress_bar(draw, cell_x, cell_y, bar_width, info.memory.percent)
    pct_text = f"{info.memory.percent:.0f}%"
    draw.text((cell_x + bar_width + 10, cell_y + 1), pct_text, font=value_font, fill=BLACK)
    cell_y += BAR_HEIGHT + 14

    mem_detail = f"{info.memory.used_gb:.1f} / {info.memory.total_gb:.1f} GB"
    draw.text((cell_x, cell_y), mem_detail, font=detail_font, fill=BLACK)

    # ── Bottom-left: Disk ──

    cell_x = PADDING
    cell_y = mid_y + 12
    cell_y = _draw_section_title(draw, cell_x, cell_y, "Disk")

    _draw_progress_bar(draw, cell_x, cell_y, bar_width, info.disk.percent)
    pct_text = f"{info.disk.percent:.0f}%"
    draw.text((cell_x + bar_width + 10, cell_y + 1), pct_text, font=value_font, fill=BLACK)
    cell_y += BAR_HEIGHT + 14

    disk_detail = f"{info.disk.used_gb:.1f} / {info.disk.total_gb:.1f} GB"
    draw.text((cell_x, cell_y), disk_detail, font=detail_font, fill=BLACK)

    # ── Bottom-right: Network ──

    cell_x = col_width + PADDING
    cell_y = mid_y + 12
    cell_y = _draw_section_title(draw, cell_x, cell_y, "Network")

    if info.networks:
        for iface in info.networks:
            iface_text = f"{iface.name}: {iface.address}"
            draw.text((cell_x, cell_y), iface_text, font=detail_font, fill=BLACK)
            cell_y += 24
    else:
        draw.text((cell_x, cell_y), "No network", font=detail_font, fill=BLACK)

    # ── Footer: Uptime ──

    footer_y = height - FOOTER_HEIGHT
    draw.line([(0, footer_y), (width, footer_y)], fill=BLACK, width=1)

    uptime_label = "Uptime:"
    uptime_value = _format_uptime(info.uptime_seconds)

    draw.text((PADDING, footer_y + 12), uptime_label, font=label_font, fill=BLACK)
    label_w = draw.textbbox((0, 0), uptime_label, font=label_font)[2]
    draw.text(
        (PADDING + label_w + 8, footer_y + 12),
        uptime_value, font=value_font, fill=BLACK,
    )

    # ── Border ──

    draw_border(draw, width, height)

    return img


# ── View class ──


@registry.register
class SystemView(BaseView):
    """Local system stats dashboard."""

    name = "system"
    description = "System stats dashboard"

    def render(self, width: int, height: int) -> Image.Image:
        try:
            info = collect_stats()
        except Exception as exc:
            log.exception("System stats view failed")
            return render_error(
                "System Stats",
                "Could not read system stats",
                str(exc),
                width,
                height,
            )

        return render_system(info, width, height)
