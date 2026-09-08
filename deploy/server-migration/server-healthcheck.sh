#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${XIANYU_APP_ROOT:-}"
FRONTEND_URL="${XIANYU_FRONTEND_URL:-}"
AUTH_URL="${XIANYU_AUTH_URL:-https://www.gemstory.cn/api/xianyu/auth/health}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-root) APP_ROOT="${2:-}"; shift 2 ;;
    --frontend-url) FRONTEND_URL="${2:-}"; shift 2 ;;
    --auth-url) AUTH_URL="${2:-}"; shift 2 ;;
    -h|--help) echo 'Usage: server-healthcheck.sh --app-root PATH [--frontend-url URL] [--auth-url URL]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$APP_ROOT" ]] || { echo '[xianyu] ERROR: --app-root is required.' >&2; exit 1; }
APP_ROOT="$(cd "$APP_ROOT" && pwd -P)"
COMPOSE_FILE="$APP_ROOT/docker-compose.yml"
ENV_FILE="$APP_ROOT/.env"
[[ -f "$COMPOSE_FILE" && -f "$ENV_FILE" ]] || { echo '[xianyu] ERROR: Compose or .env is missing.' >&2; exit 1; }

compose() { docker compose --project-directory "$APP_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }
env_value() { local key="$1"; awk -v key="$key" 'index($0, key "=") == 1 { sub("^" key "=", ""); print; exit }' "$ENV_FILE"; }

echo '[xianyu] Compose status:'
compose ps
failed=0
for service in mysql redis backend websocket scheduler frontend; do
  id="$(compose ps -q "$service" | head -n 1)"
  if [[ -z "$id" ]]; then echo "[xianyu] FAIL $service: no container"; failed=1; continue; fi
  state="$(docker inspect -f '{{.State.Status}}' "$id")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$id")"
  if [[ "$state" != running || ( "$health" != healthy && "$health" != no-healthcheck ) ]]; then
    echo "[xianyu] FAIL $service: state=$state health=$health"
    failed=1
  else
    echo "[xianyu] OK   $service: state=$state health=$health"
  fi
done

if [[ -z "$FRONTEND_URL" ]]; then
  port="$(env_value FRONTEND_PORT || true)"
  [[ "$port" =~ ^[0-9]+$ ]] && FRONTEND_URL="http://127.0.0.1:$port"
fi
if [[ -n "$FRONTEND_URL" ]]; then
  if curl --fail --silent --show-error --max-time 15 "$FRONTEND_URL" >/dev/null; then echo "[xianyu] OK   frontend HTTP: $FRONTEND_URL"; else echo "[xianyu] FAIL frontend HTTP: $FRONTEND_URL"; failed=1; fi
fi
if [[ -n "$AUTH_URL" ]]; then
  if curl --fail --silent --show-error --max-time 15 "$AUTH_URL" >/dev/null; then echo "[xianyu] OK   cloud auth: $AUTH_URL"; else echo "[xianyu] WARN cloud auth unavailable: $AUTH_URL"; fi
fi

if [[ "$failed" -ne 0 ]]; then echo '[xianyu] Health check failed.' >&2; exit 1; fi
echo '[xianyu] Health check passed.'
