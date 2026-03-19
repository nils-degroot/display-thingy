# display-thingy

E-paper display manager for Raspberry Pi with pluggable views. Currently shows weather data from OpenWeatherMap on a Waveshare 7.5" V2 (800x480) display.

## Hardware

- Raspberry Pi 3B+ (or newer)
- Waveshare 7.5inch E-Paper V2 (800x480)
- The display connects via the SPI GPIO header (HAT connector or jumper wires)

## Raspberry Pi Setup

### 1. Enable SPI

```bash
sudo raspi-config
# Interface Options → SPI → Enable
sudo reboot
```

### 2. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3-dev git libopenjp2-7 libtiff6
```

### 3. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### 4. Clone and install the project

```bash
git clone <your-repo-url> ~/display-thingy
cd ~/display-thingy
uv sync --extra pi
```

### 5. Install the Waveshare e-Paper driver

```bash
cd /tmp
git clone https://github.com/waveshareteam/e-Paper.git
cd e-Paper/RaspberryPi_JetsonNano/python
uv pip install --python ~/display-thingy/.venv/bin/python .
cd ~/display-thingy
```

### 6. Configure

```bash
cp .envrc.example .envrc
```

Edit `.envrc` and fill in your values:

```bash
export OPENWEATHERMAP_KEY="your_api_key_here"  # https://openweathermap.org/api
export LATITUDE="52.3508"
export LONGITUDE="5.2647"
export LOCATION_NAME="Almere"
export UNITS="metric"       # metric / imperial
export REFRESH_INTERVAL="900"  # seconds (900 = 15 minutes)
```

### 7. Test it

```bash
source .envrc
uv run display-thingy
```

The display should update with current weather. Press `Ctrl+C` to stop.

### 8. Install as a system service

For the systemd service, create an environment file from your `.envrc`:

```bash
# Convert .envrc exports to a systemd-compatible env file
grep '^export ' .envrc | sed 's/^export //' > ~/.display-thingy.env
```

Install and start the service:

```bash
sudo cp deploy/display-thingy.service /etc/systemd/system/

# Edit the service file if your username or paths differ from the defaults
sudo systemctl edit display-thingy
```

Override the defaults if needed (the default assumes user `pi` and path `/home/pi/display-thingy`):

```ini
[Service]
User=your_username
WorkingDirectory=/home/your_username/display-thingy
EnvironmentFile=/home/your_username/.display-thingy.env
ExecStart=/home/your_username/display-thingy/.venv/bin/display-thingy
```

Then enable and start:

```bash
sudo systemctl enable display-thingy
sudo systemctl start display-thingy
```

Check status and logs:

```bash
sudo systemctl status display-thingy
journalctl -u display-thingy -f
```

## Development

On your dev machine (no e-paper hardware needed), the display output is saved as PNG files in the `preview/` directory.

```bash
# Install dependencies
uv sync

# Set up config
cp .envrc.example .envrc
# Edit .envrc with your API key and location

# Run (saves preview/latest.png)
source .envrc
uv run display-thingy
```

## Adding a New View

Views are self-contained modules that fetch data and render an 800x480 image.

1. Create a file in `src/display_thingy/views/`, e.g. `calendar.py`
2. Implement a view class:

```python
from PIL import Image, ImageDraw
from display_thingy.views import BaseView, registry

@registry.register
class CalendarView(BaseView):
    name = "calendar"
    description = "Upcoming calendar events"

    def render(self, width: int, height: int) -> Image.Image:
        img = Image.new("1", (width, height), 1)  # white background
        draw = ImageDraw.Draw(img)
        # ... your rendering logic ...
        return img
```

3. Set `DISPLAY_VIEW=calendar` in your `.envrc`

## Wiring

If using the Waveshare HAT, just plug it onto the Pi's GPIO header. For jumper wires:

| Display Pin | Pi GPIO Pin | Function |
|-------------|-------------|----------|
| VCC         | 3.3V (pin 1) | Power |
| GND         | GND (pin 6)  | Ground |
| DIN         | GPIO 10 / MOSI (pin 19) | SPI data |
| CLK         | GPIO 11 / SCLK (pin 23) | SPI clock |
| CS          | GPIO 8 / CE0 (pin 24) | Chip select |
| DC          | GPIO 25 (pin 22) | Data/command |
| RST         | GPIO 17 (pin 11) | Reset |
| BUSY        | GPIO 24 (pin 18) | Busy signal |
