#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="docker-compose.generated.yml"

err() {
  printf '%s\n' "error: $*" >&2
  exit 1
}

if [ ! -f "$COMPOSE_FILE" ]; then
  if [ -d configs ] && ls configs/*.yaml >/dev/null 2>&1; then
    python3 generate_compose.py --write "$COMPOSE_FILE"
  else
    err "no ${COMPOSE_FILE} and configs/ is empty — nothing to stop (volumes are not removed)"
  fi
fi

# Never pass -v: keep bind-mounted data/ and results/.
docker compose -f "$COMPOSE_FILE" down
echo "down (volumes kept)"
