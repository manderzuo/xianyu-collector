#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: server-migration-import.sh --app-root PATH --bundle FILE [options]

Options:
  --app-root PATH       Target Compose application root (required)
  --bundle FILE         Encrypted .tar.gz.enc bundle (required)
  --force               Allow restore into a non-empty target
  --restore-nginx       Restore archived Nginx configuration
  --restore-registry    Restore archived registry data to its recorded path
  -h, --help            Show this help
EOF
}

die() { echo "[xianyu] ERROR: $*" >&2; exit 1; }
log() { echo "[xianyu] $*"; }

APP_ROOT="${XIANYU_APP_ROOT:-}"
BUNDLE=""
FORCE=0
RESTORE_NGINX=0
RESTORE_REGISTRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-root) APP_ROOT="${2:-}"; shift 2 ;;
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --restore-nginx) RESTORE_NGINX=1; shift ;;
    --restore-registry) RESTORE_REGISTRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$APP_ROOT" ]] || die '--app-root is required.'
[[ -n "$BUNDLE" ]] || die '--bundle is required.'
[[ -f "$BUNDLE" ]] || die "Migration bundle not found: $BUNDLE"
[[ "$EUID" -eq 0 ]] || die 'Run the import as root.'
command -v docker >/dev/null 2>&1 || die 'Docker CLI was not found.'
command -v openssl >/dev/null 2>&1 || die 'OpenSSL was not found.'
command -v sha256sum >/dev/null 2>&1 || die 'sha256sum was not found.'
command -v python3 >/dev/null 2>&1 || die 'Python 3 was not found.'

APP_ROOT="$(mkdir -p "$APP_ROOT" && cd "$APP_ROOT" && pwd -P)"
if [[ "$FORCE" -ne 1 ]] && [[ -n "$(find "$APP_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  die "Target application root is not empty. Re-run with --force after taking a target backup: $APP_ROOT"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/xianyu-migration-import.XXXXXX")"
PLAIN="$WORK/bundle.tar.gz"
STAGE="$WORK/stage"
trap 'rm -rf "$WORK"' EXIT

read -r -s -p 'Migration passphrase: ' PASSPHRASE
echo
[[ -n "$PASSPHRASE" ]] || die 'Migration passphrase cannot be empty.'
printf '%s' "$PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in "$BUNDLE" -out "$PLAIN" -pass stdin >/dev/null || die 'Could not decrypt migration bundle.'
unset PASSPHRASE
mkdir -p "$STAGE"
tar -xzf "$PLAIN" -C "$STAGE"
[[ -f "$STAGE/manifest.json" ]] || die 'Migration manifest is missing.'
MIGRATION_FORMAT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["format_version"])' "$STAGE/manifest.json")"
[[ "$MIGRATION_FORMAT" == 1 ]] || die "Unsupported migration manifest version: $MIGRATION_FORMAT"
(cd "$STAGE" && sha256sum -c checksums.sha256 >/dev/null) || die 'Migration bundle checksum verification failed.'

if [[ -f "$STAGE/app/application.tar.gz" ]]; then
  log 'Restoring application deployment files.'
  tar -xzf "$STAGE/app/application.tar.gz" -C "$APP_ROOT"
fi
[[ -f "$APP_ROOT/docker-compose.yml" ]] || die 'Restored Compose file is missing.'
[[ -f "$STAGE/config/app.env" ]] || die 'Restored environment file is missing.'
cp -a "$STAGE/config/app.env" "$APP_ROOT/.env"
COMPOSE_FILE="$APP_ROOT/docker-compose.yml"
ENV_FILE="$APP_ROOT/.env"

compose() {
  docker compose --project-directory "$APP_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}
env_value() {
  local key="$1"
  awk -v key="$key" 'index($0, key "=") == 1 { sub("^" key "=", ""); print; exit }' "$ENV_FILE"
}

if [[ -f "$STAGE/images/compose-images.tar" ]]; then
  log 'Loading offline Compose images.'
  docker load --input "$STAGE/images/compose-images.tar" >/dev/null
fi

DB_NAME="$(env_value MYSQL_DATABASE || true)"
ROOT_PASSWORD="$(env_value MYSQL_ROOT_PASSWORD || true)"
[[ "$DB_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || die 'Restored MYSQL_DATABASE is invalid.'
[[ -n "$ROOT_PASSWORD" ]] || die 'Restored MYSQL_ROOT_PASSWORD is empty.'

log 'Starting the database services.'
compose up -d mysql redis >/dev/null
for attempt in $(seq 1 60); do
  MYSQL_ID="$(compose ps -q mysql | head -n 1)"
  if [[ -n "$MYSQL_ID" ]] && [[ "$(docker inspect -f '{{.State.Health.Status}}' "$MYSQL_ID" 2>/dev/null || true)" == healthy ]]; then break; fi
  [[ "$attempt" -lt 60 ]] || die 'MySQL did not become healthy.'
  sleep 2
done

if [[ -f "$STAGE/data/mysql.sql" ]]; then
  log 'Restoring MySQL data.'
  compose exec -T -e MYSQL_PWD="$ROOT_PASSWORD" mysql mysql -uroot \
    -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  compose exec -T -e MYSQL_PWD="$ROOT_PASSWORD" mysql mysql -uroot "$DB_NAME" < "$STAGE/data/mysql.sql"
fi

restore_volume() {
  local service="$1" destination="$2" archive="$3"
  local container volume
  [[ -f "$STAGE/data/volumes/$archive.tar.gz" ]] || return 0
  container="$(compose ps -aq "$service" | head -n 1)"
  [[ -n "$container" ]] || return 0
  volume="$(docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}" "$container")"
  [[ -n "$volume" ]] || return 0
  if ! docker image inspect alpine:3.20 >/dev/null 2>&1; then docker pull alpine:3.20 >/dev/null; fi
  log "Restoring volume $archive."
  docker run --rm -v "$volume:/target" -v "$STAGE/data/volumes:/backup:ro" alpine:3.20 sh -c \
    "find /target -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar xzf /backup/$archive.tar.gz -C /target"
}

restore_redis_snapshot() {
  local container volume
  [[ -f "$STAGE/data/redis.rdb" ]] || return 0
  container="$(compose ps -aq redis | head -n 1)"
  [[ -n "$container" ]] || { log 'Redis container is unavailable; skipping Redis snapshot restore.'; return 0; }
  volume="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$container")"
  [[ -n "$volume" ]] || { log 'Redis data volume is unavailable; skipping Redis snapshot restore.'; return 0; }
  if ! docker image inspect alpine:3.20 >/dev/null 2>&1; then docker pull alpine:3.20 >/dev/null; fi
  log 'Restoring Redis snapshot.'
  docker run --rm -v "$volume:/target" -v "$STAGE/data:/backup:ro" alpine:3.20 sh -c \
    'rm -f /target/dump.rdb; cp /backup/redis.rdb /target/dump.rdb'
}

compose create backend websocket scheduler frontend >/dev/null
compose stop backend websocket scheduler frontend redis >/dev/null 2>&1 || true
restore_volume backend /app/static static_files
restore_volume backend /app/backups backup_files
restore_volume websocket /app/browser_data browser_data
restore_redis_snapshot
compose start redis >/dev/null 2>&1 || true

if [[ -f "$STAGE/auth/data.tar.gz" || -f "$STAGE/auth/application.tar.gz" || -f "$STAGE/auth/xianyu-auth.service" ]]; then
  if ! id xianyu-auth >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin xianyu-auth
  fi
fi
if [[ -f "$STAGE/auth/data.tar.gz" && -f "$STAGE/auth/data.path" ]]; then
  AUTH_DATA="$(cat "$STAGE/auth/data.path")"
  mkdir -p "$AUTH_DATA"
  tar -xzf "$STAGE/auth/data.tar.gz" -C "$AUTH_DATA"
  chown -R xianyu-auth:xianyu-auth "$AUTH_DATA" 2>/dev/null || true
  chmod 700 "$AUTH_DATA" 2>/dev/null || true
fi
if [[ -f "$STAGE/auth/application.tar.gz" && -f "$STAGE/auth/root.path" ]]; then
  AUTH_ROOT="$(cat "$STAGE/auth/root.path")"
  mkdir -p "$AUTH_ROOT"
  tar -xzf "$STAGE/auth/application.tar.gz" -C "$AUTH_ROOT"
  chown -R xianyu-auth:xianyu-auth "$AUTH_ROOT" 2>/dev/null || true
fi
if [[ -f "$STAGE/auth/xianyu-auth.service" ]]; then
  cp -a "$STAGE/auth/xianyu-auth.service" /etc/systemd/system/xianyu-auth.service
fi

if [[ "$RESTORE_NGINX" -eq 1 && -f "$STAGE/config/nginx.conf" && -f "$STAGE/config/nginx.path" ]]; then
  NGINX_CONFIG="$(cat "$STAGE/config/nginx.path")"
  mkdir -p "$(dirname "$NGINX_CONFIG")"
  cp -a "$STAGE/config/nginx.conf" "$NGINX_CONFIG"
  nginx -t || die 'Restored Nginx configuration is invalid.'
fi

if [[ "$RESTORE_REGISTRY" -eq 1 && -f "$STAGE/registry/data.tar.gz" && -f "$STAGE/registry/data.path" ]]; then
  REGISTRY_DATA="$(cat "$STAGE/registry/data.path")"
  mkdir -p "$REGISTRY_DATA"
  tar -xzf "$STAGE/registry/data.tar.gz" -C "$REGISTRY_DATA"
  if id docker-registry >/dev/null 2>&1; then
    chown -R docker-registry:docker-registry "$REGISTRY_DATA"
  fi
  if systemctl list-unit-files docker-registry.service >/dev/null 2>&1; then
    systemctl restart docker-registry
  fi
fi

systemctl daemon-reload 2>/dev/null || true
if [[ -f /etc/systemd/system/xianyu-auth.service ]]; then
  systemctl enable xianyu-auth 2>/dev/null || true
  systemctl restart xianyu-auth 2>/dev/null || true
fi
nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
compose up -d >/dev/null
log 'Migration restore completed. Run server-healthcheck.sh before DNS cutover.'
