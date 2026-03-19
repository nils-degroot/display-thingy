#!/usr/bin/env bash
set -euo pipefail

REPO_URL_DEFAULT="https://github.com/nils-degroot/display-thingy.git"
INSTALL_DIR_DEFAULT="${HOME}/display-thingy"
WAVESHARE_REPO_DEFAULT="https://github.com/waveshareteam/e-Paper.git"

REPO_URL="${REPO_URL:-${REPO_URL_DEFAULT}}"
INSTALL_DIR="${INSTALL_DIR:-${INSTALL_DIR_DEFAULT}}"
WAVESHARE_REPO="${WAVESHARE_REPO:-${WAVESHARE_REPO_DEFAULT}}"


info() {
    echo "[display-thingy] $*"
}


warn() {
    echo "[display-thingy] Warning: $*" >&2
}


die() {
    echo "[display-thingy] Error: $*" >&2
    exit 1
}


have_cmd() {
    command -v "$1" >/dev/null 2>&1
}


is_raspberry_pi() {
    # Prefer the device tree model if available (Raspberry Pi OS).
    if [ -r /sys/firmware/devicetree/base/model ]; then
        if grep -q "Raspberry Pi" /sys/firmware/devicetree/base/model; then
            return 0
        fi
    fi

    # Fallback: /proc/cpuinfo often includes a Raspberry Pi model string.
    if [ -r /proc/cpuinfo ]; then
        if grep -q "Raspberry Pi" /proc/cpuinfo; then
            return 0
        fi
    fi

    return 1
}


ensure_uv() {
    if have_cmd uv; then
        return 0
    fi

    info "Installing uv..."
    if ! have_cmd curl; then
        die "curl not found (required to install uv). Install it and re-run."
    fi

    curl -LsSf https://astral.sh/uv/install.sh | sh

    # uv installs to ~/.local/bin by default.
    export PATH="${HOME}/.local/bin:${PATH}"
    if ! have_cmd uv; then
        die "uv install finished but 'uv' is not on PATH. Log out/in or add ~/.local/bin to PATH."
    fi
}


ensure_repo() {
    if [ -d "${INSTALL_DIR}" ]; then
        if [ ! -d "${INSTALL_DIR}/.git" ]; then
            die "${INSTALL_DIR} exists but is not a git repo. Move it aside or set INSTALL_DIR."
        fi
        info "Updating existing repo at ${INSTALL_DIR}..."
        git -C "${INSTALL_DIR}" pull
        return 0
    fi

    info "Cloning ${REPO_URL} to ${INSTALL_DIR}..."
    git clone "${REPO_URL}" "${INSTALL_DIR}"
}


install_waveshare_driver() {
    local tmpdir
    tmpdir="$(mktemp -d)"

    cleanup() {
        rm -rf "${tmpdir}" || true
    }
    trap cleanup EXIT

    info "Installing Waveshare e-paper driver..."
    git clone "${WAVESHARE_REPO}" "${tmpdir}/e-Paper"

    local driver_dir
    driver_dir="${tmpdir}/e-Paper/RaspberryPi_JetsonNano/python"
    if [ ! -d "${driver_dir}" ]; then
        die "Waveshare repo layout changed (missing ${driver_dir})."
    fi

    uv pip install --python "${INSTALL_DIR}/.venv/bin/python" "${driver_dir}"
}


maybe_setup_pi_runtime() {
    if ! is_raspberry_pi; then
        warn "Not a Raspberry Pi; skipping SPI and GPIO group setup."
        return 1
    fi

    if ! have_cmd raspi-config; then
        warn "raspi-config not found; skipping SPI enable."
    else
        info "Enabling SPI (raspi-config)..."
        sudo raspi-config nonint do_spi 0 || warn "Failed to enable SPI via raspi-config"
    fi

    info "Adding ${USER} to gpio and spi groups..."
    sudo usermod -aG gpio,spi "${USER}" || warn "Failed to add user to gpio/spi groups"

    return 0
}


main() {
    info "Starting bootstrap install"

    if ! have_cmd sudo; then
        die "sudo not found. This script expects a sudo-capable user."
    fi

    if ! have_cmd git; then
        info "Installing git..."
        sudo apt-get update
        sudo apt-get install -y git
    fi

    info "Installing system dependencies..."
    sudo apt-get update
    sudo apt-get install -y python3-dev python3-lgpio libopenjp2-7 libtiff6 curl

    local needs_reboot=0
    if maybe_setup_pi_runtime; then
        needs_reboot=1
    fi

    ensure_uv
    ensure_repo

    info "Syncing Python dependencies (uv)..."
    uv sync --extra pi --project "${INSTALL_DIR}"

    install_waveshare_driver

    if [ ! -f "${INSTALL_DIR}/.envrc" ]; then
        info "Creating default config (.envrc)..."
        cp "${INSTALL_DIR}/.envrc.example" "${INSTALL_DIR}/.envrc"
        info "Default view is 'system' (no API keys required)."
    else
        info "Keeping existing ${INSTALL_DIR}/.envrc"
    fi

    info "Installing systemd service..."
    "${INSTALL_DIR}/deploy/install.sh"

    info "Install complete."
    info "Edit ${INSTALL_DIR}/.envrc to enable more views, then run: ${INSTALL_DIR}/deploy/update.sh"

    if [ "${needs_reboot}" -eq 1 ]; then
        info "A reboot is recommended for SPI and group changes to take effect."
        read -r -p "Reboot now? [Y/n] " answer
        answer="${answer:-Y}"
        case "${answer}" in
            [Nn]*)
                info "Skipping reboot. Log out/in (or reboot) before running without sudo."
                ;;
            *)
                info "Rebooting..."
                sudo reboot
                ;;
        esac
    fi
}


main "$@"
