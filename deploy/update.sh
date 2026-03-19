#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="display-thingy"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_BIN="${PROJECT_DIR}/.venv/bin"
ENV_FILE="${HOME}/.display-thingy.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== display-thingy updater ==="
echo ""
echo "  Project dir:  ${PROJECT_DIR}"
echo "  User:         ${USER}"
echo "  Env file:     ${ENV_FILE}"
echo "  Service file: ${SERVICE_FILE}"
echo ""

# --- Pre-flight checks ---

if ! command -v systemctl &>/dev/null; then
    echo "Error: systemctl not found. This script requires systemd."
    exit 1
fi

if [ ! -f "${SERVICE_FILE}" ]; then
    echo "Error: Service not installed (${SERVICE_FILE} not found)."
    echo "Run deploy/install.sh first."
    exit 1
fi

if ! command -v uv &>/dev/null; then
    echo "Error: uv not found. Install it first: https://docs.astral.sh/uv/"
    exit 1
fi

# Abort if there are uncommitted changes that could cause merge conflicts.
if ! git -C "${PROJECT_DIR}" diff --quiet || ! git -C "${PROJECT_DIR}" diff --cached --quiet; then
    echo "Error: Working tree has uncommitted changes."
    echo "Commit or stash them before updating."
    exit 1
fi

# --- Pull latest changes ---

echo "Pulling latest changes..."
git -C "${PROJECT_DIR}" pull
echo ""

# --- Sync dependencies ---

echo "Syncing dependencies..."
uv sync --extra pi --project "${PROJECT_DIR}"
echo ""

# --- Regenerate env file ---

if [ ! -f "${PROJECT_DIR}/.envrc" ]; then
    echo "Warning: ${PROJECT_DIR}/.envrc not found, skipping env file generation."
else
    echo "Updating environment file at ${ENV_FILE}..."
    grep '^export ' "${PROJECT_DIR}/.envrc" | sed 's/^export //' > "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "  Done (permissions set to 600)."
fi
echo ""

# --- Reinstall systemd unit ---

echo "Updating systemd service..."
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
sudo systemctl daemon-reload
echo ""

# --- Restart service ---

echo "Restarting service..."
sudo systemctl restart "${SERVICE_NAME}"
echo ""

# --- Show status ---

echo "=== Done ==="
echo ""
sudo systemctl status "${SERVICE_NAME}" --no-pager || true
