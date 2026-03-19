#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="display-thingy"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_BIN="${PROJECT_DIR}/.venv/bin"
ENV_FILE="${HOME}/.display-thingy.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== display-thingy service installer ==="
echo ""
echo "  Project dir:  ${PROJECT_DIR}"
echo "  User:         ${USER}"
echo "  Env file:     ${ENV_FILE}"
echo "  Service file: ${SERVICE_FILE}"
echo ""

# Check we're on a system with systemd
if ! command -v systemctl &>/dev/null; then
    echo "Error: systemctl not found. This script requires systemd."
    exit 1
fi

# Check the venv exists
if [ ! -x "${VENV_BIN}/display-thingy" ]; then
    echo "Error: ${VENV_BIN}/display-thingy not found."
    echo "Run 'uv sync --extra pi' first."
    exit 1
fi

# Check .envrc exists
if [ ! -f "${PROJECT_DIR}/.envrc" ]; then
    echo "Error: ${PROJECT_DIR}/.envrc not found."
    echo "Copy .envrc.example to .envrc and fill in your values first."
    exit 1
fi

# Generate systemd-compatible env file from .envrc
echo "Creating environment file at ${ENV_FILE}..."
grep '^export ' "${PROJECT_DIR}/.envrc" | sed 's/^export //' > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"
echo "  Done (permissions set to 600)."

# Generate and install the service unit
echo "Installing systemd service..."
sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=E-Paper Display Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_BIN}/display-thingy
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "  Installed ${SERVICE_FILE}"

# Reload systemd, enable and start
echo "Enabling and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

echo ""
echo "=== Done ==="
echo ""
echo "Useful commands:"
echo "  sudo systemctl status ${SERVICE_NAME}    # check status"
echo "  journalctl -u ${SERVICE_NAME} -f         # follow logs"
echo "  sudo systemctl restart ${SERVICE_NAME}   # restart"
echo "  sudo systemctl stop ${SERVICE_NAME}      # stop"
echo ""
echo "To uninstall:"
echo "  sudo systemctl stop ${SERVICE_NAME}"
echo "  sudo systemctl disable ${SERVICE_NAME}"
echo "  sudo rm ${SERVICE_FILE}"
echo "  rm ${ENV_FILE}"
echo "  sudo systemctl daemon-reload"
