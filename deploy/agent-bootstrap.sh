#!/usr/bin/env bash
#
# Vidhive agent bootstrap — installs and starts a single agent on a bare Linux
# host. Idempotent; re-running upgrades the code and restarts the service.
#
# Configuration arrives via environment (the coordinator sets it over SSH):
#   VIDHIVE_COORDINATOR_URL  required — how the agent reaches the coordinator
#   VIDHIVE_WORKER_NAME      required — unique name of this agent
#   VIDHIVE_AGENT_URL        required — how the coordinator reaches this agent
#   VIDHIVE_THREADS          worker threads (default 8)
#   VIDHIVE_STORAGE_PATH     where videos are stored (default /var/lib/vidhive/videos)
#   VIDHIVE_AGENT_PORT       port the agent listens on (default 8100)
#   VIDHIVE_AGENT_TOKEN      shared secret (optional)
#   VIDHIVE_TARGET_TEMPLATE  target URL template (optional)
# Deployment knobs (optional):
#   REPO_URL     git repo (default https://github.com/Yda4nik/Vidhive)
#   INSTALL_DIR  checkout dir (default /opt/vidhive)
#   ENV_FILE     env file (default /etc/vidhive-agent.env)
#   SERVICE_NAME systemd unit name (default vidhive-agent)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Yda4nik/Vidhive}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vidhive}"
ENV_FILE="${ENV_FILE:-/etc/vidhive-agent.env}"
SERVICE_NAME="${SERVICE_NAME:-vidhive-agent}"
AGENT_PORT="${VIDHIVE_AGENT_PORT:-8100}"

: "${VIDHIVE_COORDINATOR_URL:?VIDHIVE_COORDINATOR_URL is required}"
: "${VIDHIVE_WORKER_NAME:?VIDHIVE_WORKER_NAME is required}"
: "${VIDHIVE_AGENT_URL:?VIDHIVE_AGENT_URL is required}"

step() { echo ">>> $*"; }

SUDO=""
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else
    echo "need root or sudo" >&2; exit 1
  fi
fi

step "Detecting package manager and installing python3, ffmpeg, git..."
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg git
elif command -v dnf >/dev/null 2>&1; then
  $SUDO dnf install -y python3 python3-pip ffmpeg git
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum install -y python3 python3-pip ffmpeg git
elif command -v zypper >/dev/null 2>&1; then
  $SUDO zypper --non-interactive install python3 python3-pip ffmpeg git
elif command -v pacman >/dev/null 2>&1; then
  $SUDO pacman -Sy --noconfirm python ffmpeg git
elif command -v apk >/dev/null 2>&1; then
  $SUDO apk add --no-cache python3 py3-pip ffmpeg git
else
  echo "no supported package manager found" >&2; exit 1
fi

step "Fetching agent code into ${INSTALL_DIR}..."
if [ -d "${INSTALL_DIR}/.git" ]; then
  $SUDO git -C "${INSTALL_DIR}" pull --ff-only
else
  $SUDO rm -rf "${INSTALL_DIR}"
  $SUDO git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
fi

step "Creating virtualenv and installing dependencies..."
$SUDO python3 -m venv "${INSTALL_DIR}/.venv"
$SUDO "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
$SUDO "${INSTALL_DIR}/.venv/bin/pip" install "${INSTALL_DIR}/common"
$SUDO "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/agent/requirements.txt"

step "Writing config to ${ENV_FILE}..."
$SUDO mkdir -p "${VIDHIVE_STORAGE_PATH:-/var/lib/vidhive/videos}"
$SUDO tee "${ENV_FILE}" >/dev/null <<EOF
VIDHIVE_COORDINATOR_URL=${VIDHIVE_COORDINATOR_URL}
VIDHIVE_WORKER_NAME=${VIDHIVE_WORKER_NAME}
VIDHIVE_AGENT_URL=${VIDHIVE_AGENT_URL}
VIDHIVE_AGENT_PORT=${AGENT_PORT}
VIDHIVE_THREADS=${VIDHIVE_THREADS:-8}
VIDHIVE_STORAGE_PATH=${VIDHIVE_STORAGE_PATH:-/var/lib/vidhive/videos}
VIDHIVE_AGENT_TOKEN=${VIDHIVE_AGENT_TOKEN:-}
VIDHIVE_TARGET_TEMPLATE=${VIDHIVE_TARGET_TEMPLATE:-http://kinescope.io/{id}}
EOF

START_CMD="${INSTALL_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${AGENT_PORT}"

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  step "Installing systemd service ${SERVICE_NAME}..."
  $SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Vidhive agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}/agent
EnvironmentFile=${ENV_FILE}
ExecStart=${START_CMD}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "${SERVICE_NAME}"
  $SUDO systemctl restart "${SERVICE_NAME}"
  step "Service started. Status:"
  $SUDO systemctl --no-pager --lines=0 status "${SERVICE_NAME}" || true
else
  step "systemd not found — starting with nohup..."
  $SUDO pkill -F "${INSTALL_DIR}/agent.pid" 2>/dev/null || true
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
  ( cd "${INSTALL_DIR}/agent" && $SUDO nohup ${START_CMD} >/var/log/vidhive-agent.log 2>&1 & echo $! | $SUDO tee "${INSTALL_DIR}/agent.pid" >/dev/null )
fi

step "Done. Agent '${VIDHIVE_WORKER_NAME}' should register with the coordinator shortly."
