#!/usr/bin/env bash
#
# Vidhive agent teardown — removes everything agent-bootstrap.sh created on a
# host: the service, the code, the config, and the video storage. Idempotent.
#
# Configuration via environment:
#   VIDHIVE_STORAGE_PATH  video storage to remove (default /var/lib/vidhive/videos)
#   INSTALL_DIR   checkout dir (default /opt/vidhive)
#   ENV_FILE      env file (default /etc/vidhive-agent.env)
#   SERVICE_NAME  systemd unit name (default vidhive-agent)
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/vidhive}"
ENV_FILE="${ENV_FILE:-/etc/vidhive-agent.env}"
SERVICE_NAME="${SERVICE_NAME:-vidhive-agent}"
STORAGE_PATH="${VIDHIVE_STORAGE_PATH:-/var/lib/vidhive/videos}"

step() { echo ">>> $*"; }

SUDO=""
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else
    echo "need root or sudo" >&2; exit 1
  fi
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  step "Stopping and disabling ${SERVICE_NAME}..."
  $SUDO systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  $SUDO systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  $SUDO rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  $SUDO systemctl daemon-reload 2>/dev/null || true
else
  step "Stopping nohup process (if any)..."
  $SUDO pkill -F "${INSTALL_DIR}/agent.pid" 2>/dev/null || true
fi

step "Removing code (${INSTALL_DIR}), config (${ENV_FILE}) and storage (${STORAGE_PATH})..."
$SUDO rm -rf "${INSTALL_DIR}"
$SUDO rm -f "${ENV_FILE}"
$SUDO rm -rf "${STORAGE_PATH}"
$SUDO rm -f /var/log/vidhive-agent.log 2>/dev/null || true

step "Done. The host is clean of the Vidhive agent (system packages left intact)."
