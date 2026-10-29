#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="docker-compose.generated.yml"
UNIT="mihomo-proxy-bench.service"
UNIT_PATH="/etc/systemd/system/${UNIT}"
WANTS_PATH="/etc/systemd/system/multi-user.target.wants/${UNIT}"

err() {
  printf '%s\n' "error: $*" >&2
  exit 1
}

run_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

systemd_unit_installed() {
  if [[ -e "$UNIT_PATH" || -L "$UNIT_PATH" || -e "$WANTS_PATH" || -L "$WANTS_PATH" ]]; then
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1 && systemctl cat "$UNIT" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

remove_systemd_unit() {
  if ! systemd_unit_installed; then
    return 0
  fi
  command -v systemctl >/dev/null 2>&1 || err "systemd unit ${UNIT} is installed but systemctl is missing"
  if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    err "need root or sudo to remove ${UNIT}"
  fi
  echo "removing ${UNIT}..."
  if systemctl cat "$UNIT" >/dev/null 2>&1; then
    if ! run_sudo systemctl stop "$UNIT"; then
      printf '%s\n' "warning: systemctl stop ${UNIT} failed; removing the unit anyway" >&2
    fi
    if ! run_sudo systemctl disable "$UNIT"; then
      printf '%s\n' "warning: systemctl disable ${UNIT} failed; removing the unit file anyway" >&2
    fi
  fi
  if [[ -e "$UNIT_PATH" || -L "$UNIT_PATH" ]]; then
    run_sudo rm -f "$UNIT_PATH"
  fi
  if [[ -e "$WANTS_PATH" || -L "$WANTS_PATH" ]]; then
    run_sudo rm -f "$WANTS_PATH"
  fi
  run_sudo systemctl daemon-reload
  run_sudo systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
}

if [ ! -f "$COMPOSE_FILE" ]; then
  if [ -d configs ] && ls configs/*.yaml >/dev/null 2>&1; then
    python3 generate_compose.py --write "$COMPOSE_FILE"
  else
    err "no ${COMPOSE_FILE} and configs/ is empty — nothing to stop (volumes are not removed)"
  fi
fi

# Never pass -v: keep bind-mounted data/ and results/.
# Drop the unit so the next boot does not run compose up.
remove_systemd_unit
docker compose -f "$COMPOSE_FILE" down
echo "down (volumes kept)"
