#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="docker-compose.generated.yml"
IMAGE="mihomo-proxy-bench"
BOT_IMAGE="mihomo-proxy-bench-bot"
PLACEHOLDERS='change-me|CHANGE-ME|changeme|replace-me|REPLACE_ME|replace_me'

err() {
  printf '%s\n' "error: $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "missing command: $1"
}

env_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 || true)"
  if [ -z "$line" ]; then
    printf '%s' ""
    return 0
  fi
  printf '%s' "${line#"${key}"=}" | tr -d '\r'
}

require_env() {
  local key="$1"
  local val
  val="$(env_value "$key")"
  [ -n "$val" ] || err "$key is empty or missing in .env"
}

need_cmd docker
need_cmd python3
docker compose version >/dev/null 2>&1 || err "docker compose plugin is required"

[ -f .env ] || err "no .env — copy .env.example and fill in values from prod plus a bench bot token"

require_env MIHOMO_API_SECRET
require_env SUB1_URL
require_env SUB2_URL
require_env SUB3_URL
require_env SUB4_URL
require_env SUB5_URL
require_env TELEGRAM_BOT_TOKEN
require_env ALLOWED_CHAT_ID

secret="$(env_value MIHOMO_API_SECRET)"
if printf '%s' "$secret" | grep -Eqx "$PLACEHOLDERS"; then
  err "MIHOMO_API_SECRET is a placeholder; generate one (openssl rand -hex 32) or copy prod"
fi

if ! docker network inspect telegram-proxy >/dev/null 2>&1; then
  err "Docker network telegram-proxy is missing — start prod mihomo-proxy first (do not create this network here)"
fi

mapfile -t IDS < <(python3 generate_compose.py --write "$COMPOSE_FILE" --print-ids)
[ "${#IDS[@]}" -gt 0 ] || err "generator returned no instance ids"

mkdir -p results
for id in "${IDS[@]}"; do
  mkdir -p "data/${id}"
  rm -rf "data/${id}/providers"
  mkdir -p "data/${id}/providers"
done

echo "building ${IMAGE} from git (pull + no-cache) and bench-bot..."
docker compose -f "$COMPOSE_FILE" build --pull --no-cache

docker image inspect "$IMAGE" >/dev/null 2>&1 || err "image ${IMAGE} was not built"
docker image inspect "$BOT_IMAGE" >/dev/null 2>&1 || err "image ${BOT_IMAGE} was not built"

echo "recreating ${#IDS[@]} mihomo instance(s) + bench-bot..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

echo "up: ${IDS[*]}"
echo "bot talks to Telegram via prod socks5h://proxy:11808 — prod must stay running"
