#!/usr/bin/env bash
# Stop the bench, wipe subscription cache, rebuild from git, and start it.
# Installs a systemd unit so the bench comes back after reboot.
# --once / -o starts it for this boot only and does not install the systemd unit.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="docker-compose.generated.yml"
IMAGE="mihomo-proxy-bench"
BOT_IMAGE="mihomo-proxy-bench-bot"
PLACEHOLDERS='change-me|CHANGE-ME|changeme|replace-me|REPLACE_ME|replace_me'
UNIT="mihomo-proxy-bench.service"
UNIT_PATH="/etc/systemd/system/${UNIT}"
WANTS_PATH="/etc/systemd/system/multi-user.target.wants/${UNIT}"
ONCE=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--once|-o]

Cold-cache bench deploy:

  stop bench containers (prod is left running)
  wipe data/<id> (providers + cache.db) and archive probes
  build ${IMAGE} from git and recreate containers

  deploy/${UNIT} -> ${UNIT_PATH}

Does not change .env. To update later: git pull --ff-only && ./deploy.sh

  --once, -o      this boot only. Do not install ${UNIT}.
                  Start containers with restart policy "no", so Docker
                  does not start them again after a reboot.
                  An already installed unit is left in place; ./down.sh removes it.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --once|-o) ONCE=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

err() {
  printf '%s\n' "error: $*" >&2
  exit 1
}

results_writable() {
  local path
  [ -w results ] || return 1
  while IFS= read -r path; do
    [ -w "$path" ] || return 1
  done < <(find results -mindepth 1 -print)
  return 0
}

chown_results_via_docker() {
  local image="" candidate
  for candidate in "$BOT_IMAGE" python:3.12-slim-bookworm python:3.12-slim; do
    if docker image inspect "$candidate" >/dev/null 2>&1; then
      image="$candidate"
      break
    fi
  done
  if [ -z "$image" ]; then
    printf '%s\n' "warning: no local image to chown results/" >&2
    return 1
  fi
  docker run --rm --user 0 \
    -v "$ROOT/results:/results" \
    --entrypoint python \
    "$image" -c '
import os, sys
uid_s, gid_s = sys.argv[1].split(":")
uid, gid = int(uid_s), int(gid_s)
for dirpath, dirnames, filenames in os.walk("/results"):
    os.chown(dirpath, uid, gid)
    for name in dirnames + filenames:
        os.chown(os.path.join(dirpath, name), uid, gid, follow_symlinks=False)
' "$(id -u):$(id -g)"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "missing command: $1"
}

run_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

need_root() {
  if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    err "$1"
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

install_systemd_unit() {
  echo "==> installing $UNIT (WorkingDirectory=$ROOT)"
  local docker_bin unit_tmp
  docker_bin="$(command -v docker)"
  unit_tmp="$(mktemp)"
  awk -v wd="$ROOT" -v docker="$docker_bin" '
    /^WorkingDirectory=/ { print "WorkingDirectory=" wd; next }
    /^ExecStart=/ { print "ExecStart=" docker " compose -f docker-compose.generated.yml up -d"; next }
    /^ExecStop=/ { print "ExecStop=" docker " compose -f docker-compose.generated.yml stop"; next }
    { print }
  ' "$ROOT/deploy/$UNIT" > "$unit_tmp"
  run_sudo install -m 644 "$unit_tmp" "$UNIT_PATH"
  rm -f "$unit_tmp"

  echo "==> systemd daemon-reload"
  run_sudo systemctl daemon-reload
  run_sudo systemctl enable "$UNIT"
}

force_restart_no() {
  local name policy
  for name in "$@"; do
    policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name")"
    if [[ "$policy" != "no" ]]; then
      echo "==> restart policy of ${name} is ${policy:-empty}, forcing no"
      docker update --restart=no "$name" >/dev/null
    fi
  done
}

load_git_version() {
  if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_COMMIT="$(git -C "$ROOT" rev-parse --short HEAD)"
    # Prefer the last real change, not "Merge branch 'main' of https://..."
    GIT_COMMIT_TITLE="$(git -C "$ROOT" log -1 --first-parent --no-merges --pretty=%s)"
    if [ -z "$GIT_COMMIT_TITLE" ]; then
      GIT_COMMIT_TITLE="$(git -C "$ROOT" log -1 --pretty=%s)"
    fi
  else
    GIT_COMMIT="unknown"
    GIT_COMMIT_TITLE="unknown"
  fi
  export GIT_COMMIT GIT_COMMIT_TITLE
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
if [[ "$ONCE" -eq 0 ]]; then
  need_cmd systemctl
  need_root "need root or sudo to install the systemd unit"
fi

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

RESTART_POLICY="unless-stopped"
if [[ "$ONCE" -eq 1 ]]; then
  RESTART_POLICY="no"
fi
mapfile -t IDS < <(python3 generate_compose.py --write "$COMPOSE_FILE" --print-ids --restart "$RESTART_POLICY")
[ "${#IDS[@]}" -gt 0 ] || err "generator returned no instance ids"

if [[ "$ONCE" -eq 1 ]] && systemd_unit_installed; then
  echo "warning: ${UNIT} is already installed; ./down.sh removes it. Until then a reboot starts the bench." >&2
fi

# Stop running bench first. If we wipe providers while mihomo is still up,
# the long --no-cache build lets it refill cache.db / providers before recreate.
echo "stopping previous bench containers (prod is left running)..."
docker compose -f "$COMPOSE_FILE" down
# leftover names if a yaml id was renamed since the last compose file
leftovers="$(
  { docker ps -aq --filter name=mihomo-bench-; docker ps -aq --filter name=bench-bot; } | sort -u
)"
if [ -n "$leftovers" ]; then
  # shellcheck disable=SC2086
  docker rm -f $leftovers
fi

mkdir -p results
if ! results_writable; then
  echo "results/ is not writable by $(id -un); taking ownership via docker..."
  chown_results_via_docker || printf '%s\n' "warning: chown failed; archive will try a read-only copy" >&2
fi
python3 archive_probes.py
echo "cold subscription cache for: ${IDS[*]}"
for id in "${IDS[@]}"; do
  rm -rf "data/${id}"
  mkdir -p "data/${id}/providers"
done

echo "git commit for bench-bot image"
load_git_version
echo "  ${GIT_COMMIT} ${GIT_COMMIT_TITLE}"

echo "building ${IMAGE} from git (pull + no-cache) and bench-bot..."
docker compose -f "$COMPOSE_FILE" build --pull --no-cache

docker image inspect "$IMAGE" >/dev/null 2>&1 || err "image ${IMAGE} was not built"
docker image inspect "$BOT_IMAGE" >/dev/null 2>&1 || err "image ${BOT_IMAGE} was not built"

echo "recreating ${#IDS[@]} mihomo instance(s) + bench-bot (restart: ${RESTART_POLICY})..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

names=()
for id in "${IDS[@]}"; do
  names+=("mihomo-bench-${id}")
done
names+=(bench-bot)
if [[ "$ONCE" -eq 1 ]]; then
  force_restart_no "${names[@]}"
else
  install_systemd_unit
  run_sudo systemctl start "$UNIT"
fi

echo "deploy: ${IDS[*]}"
if [[ "$ONCE" -eq 1 ]]; then
  if systemd_unit_installed; then
    echo "boot: Docker restart policy is no; ${UNIT} is still installed and will start the bench after reboot"
  else
    echo "boot: stays down after reboot (restart policy no, no systemd unit)"
  fi
else
  echo "boot: ${UNIT} starts the bench after reboot"
fi
echo "bot talks to Telegram via prod socks5h://proxy:11808 — prod must stay running"
