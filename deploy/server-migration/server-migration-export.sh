#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: server-migration-export.sh --app-root PATH --output DIR [options]

Options:
  --app-root PATH          Compose application root (required)
  --output DIR             Output directory (required)
  --auth-root PATH         Cloud auth application directory
  --auth-data PATH         Cloud auth data directory
  --nginx-config PATH      Nginx site configuration to archive
  --include-images         Include all current Compose images
  --include-registry PATH  Include Docker Registry data from PATH
  -h, --help               Show this help
EOF
}

die() { echo "[xianyu] ERROR: $*" >&2; exit 1; }
log() { echo "[xianyu] $*"; }

APP_ROOT="${XIANYU_APP_ROOT:-}"
OUTPUT_DIR="${XIANYU_MIGRATION_OUTPUT:-}"
AUTH_ROOT="${XIANYU_AUTH_ROOT:-}"
AUTH_DATA="${XIANYU_AUTH_DATA:-}"
NGINX_CONFIG="${XIANYU_NGINX_CONFIG:-}"
REGISTRY_DATA=""
INCLUDE_IMAGES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-root) APP_ROOT="${2:-}"; shift 2 ;;
    --output) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --auth-root) AUTH_ROOT="${2:-}"; shift 2 ;;
    --auth-data) AUTH_DATA="${2:-}"; shift 2 ;;
    --nginx-config) NGINX_CONFIG="${2:-}"; shift 2 ;;
    --include-images) INCLUDE_IMAGES=1; shift ;;
    --include-registry) REGISTRY_DATA="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

if [[ -z "$REGISTRY_DATA" && -d /var/lib/gemstory/xianyu-registry ]]; then
  REGISTRY_DATA=/var/lib/gemstory/xianyu-registry
fi

[[ -n "$APP_ROOT" ]] || die '--app-root is required.'
[[ -n "$OUTPUT_DIR" ]] || die '--output is required.'
[[ -d "$APP_ROOT" ]] || die "Application root does not exist: $APP_ROOT"
APP_ROOT="$(cd "$APP_ROOT" && pwd -P)"
COMPOSE_FILE="$APP_ROOT/docker-compose.yml"
ENV_FILE="$APP_ROOT/.env"
[[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || die "Environment file not found: $ENV_FILE"
command -v docker >/dev/null 2>&1 || die 'Docker CLI was not found.'
command -v openssl >/dev/null 2>&1 || die 'OpenSSL was not found.'
command -v sha256sum >/dev/null 2>&1 || die 'sha256sum was not found.'

OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && cd "$OUTPUT_DIR" && pwd -P)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/xianyu-migration-export.XXXXXX")"
PLAIN="$OUTPUT_DIR/xianyu-migration-$STAMP.tar.gz"
ENCRYPTED="$PLAIN.enc"
trap 'rm -rf "$STAGE" "$PLAIN"' EXIT

compose() {
  docker compose --project-directory "$APP_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

env_value() {
  local key="$1"
  awk -v key="$key" 'index($0, key "=") == 1 { sub("^" key "=", ""); print; exit }' "$ENV_FILE"
}

backup_volume() {
  local service="$1" destination="$2" name="$3"
  local container volume
  container="$(compose ps -q "$service" | head -n 1)"
  [[ -n "$container" ]] || { log "Skipping volume $name: service $service is not running."; return 0; }
  volume="$(docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}" "$container")"
  [[ -n "$volume" ]] || { log "Skipping volume $name: mount $destination was not found."; return 0; }
  if ! docker image inspect alpine:3.20 >/dev/null 2>&1; then
    log 'Pulling the small migration helper image alpine:3.20.'
    docker pull alpine:3.20 >/dev/null
  fi
  docker run --rm \
    -v "$volume:/source:ro" \
    -v "$STAGE/data/volumes:/backup" \
    alpine:3.20 sh -c "tar czf /backup/$name.tar.gz -C /source ."
  printf '%s\n' "$volume" >> "$STAGE/volume-map.txt"
}

mkdir -p "$STAGE/config" "$STAGE/data/volumes" "$STAGE/app" "$STAGE/auth" "$STAGE/registry" "$STAGE/images"
cp -a "$COMPOSE_FILE" "$STAGE/config/docker-compose.yml"
cp -a "$ENV_FILE" "$STAGE/config/app.env"

log 'Checking Compose services.'
compose ps >/dev/null

DB_NAME="$(env_value MYSQL_DATABASE || true)"
ROOT_PASSWORD="$(env_value MYSQL_ROOT_PASSWORD || true)"
REDIS_PASSWORD="$(env_value REDIS_PASSWORD || true)"
[[ "$DB_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || die 'MYSQL_DATABASE is missing or contains unsupported characters.'
[[ -n "$ROOT_PASSWORD" ]] || die 'MYSQL_ROOT_PASSWORD is missing from .env.'

MYSQL_CONTAINER="$(compose ps -q mysql | head -n 1)"
[[ -n "$MYSQL_CONTAINER" ]] || die 'MySQL container is not running.'
mkdir -p "$STAGE/data"
log 'Creating a consistent MySQL logical backup.'
compose exec -T -e MYSQL_PWD="$ROOT_PASSWORD" mysql \
  mysqldump -uroot --single-transaction --routines --events --triggers \
  --hex-blob --default-character-set=utf8mb4 "$DB_NAME" > "$STAGE/data/mysql.sql"

REDIS_CONTAINER="$(compose ps -q redis | head -n 1 || true)"
if [[ -n "$REDIS_CONTAINER" ]]; then
  if [[ -z "$REDIS_PASSWORD" ]]; then
    log 'Redis password is empty; skipping Redis snapshot for safety.'
  else
    log 'Creating a Redis snapshot.'
    docker exec -e REDISCLI_AUTH="$REDIS_PASSWORD" "$REDIS_CONTAINER" redis-cli --rdb /tmp/xianyu-migration.rdb >/dev/null
    docker cp "$REDIS_CONTAINER:/tmp/xianyu-migration.rdb" "$STAGE/data/redis.rdb"
    docker exec "$REDIS_CONTAINER" rm -f /tmp/xianyu-migration.rdb >/dev/null
  fi
fi

backup_volume websocket /app/browser_data browser_data
backup_volume backend /app/static static_files
backup_volume backend /app/backups backup_files

# The deployment tree is small and makes a replacement server reproducible.
# Runtime data and .env are stored separately above.
tar -C "$APP_ROOT" \
  --exclude='./.env' --exclude='./logs' --exclude='./backups' \
  --exclude='./static' --exclude='./browser_data' --exclude='./.git' \
  -czf "$STAGE/app/application.tar.gz" .

if [[ -z "$AUTH_ROOT" && -d /opt/gemstory/xianyu-auth ]]; then AUTH_ROOT=/opt/gemstory/xianyu-auth; fi
if [[ -z "$AUTH_DATA" && -d /var/lib/gemstory/xianyu-auth ]]; then AUTH_DATA=/var/lib/gemstory/xianyu-auth; fi
if [[ -n "$AUTH_ROOT" && -d "$AUTH_ROOT" ]]; then
  tar -C "$AUTH_ROOT" -czf "$STAGE/auth/application.tar.gz" .
  printf '%s\n' "$AUTH_ROOT" > "$STAGE/auth/root.path"
fi
if [[ -n "$AUTH_DATA" && -d "$AUTH_DATA" ]]; then
  tar -C "$AUTH_DATA" -czf "$STAGE/auth/data.tar.gz" .
  printf '%s\n' "$AUTH_DATA" > "$STAGE/auth/data.path"
fi
if [[ -f /etc/systemd/system/xianyu-auth.service ]]; then
  cp -a /etc/systemd/system/xianyu-auth.service "$STAGE/auth/xianyu-auth.service"
fi
if [[ -z "$NGINX_CONFIG" && -f /etc/nginx/sites-available/filmcrew.conf ]]; then NGINX_CONFIG=/etc/nginx/sites-available/filmcrew.conf; fi
if [[ -n "$NGINX_CONFIG" && -f "$NGINX_CONFIG" ]]; then
  cp -a "$NGINX_CONFIG" "$STAGE/config/nginx.conf"
  printf '%s\n' "$NGINX_CONFIG" > "$STAGE/config/nginx.path"
fi

if [[ -n "$REGISTRY_DATA" ]]; then
  [[ -d "$REGISTRY_DATA" ]] || die "Registry data directory does not exist: $REGISTRY_DATA"
  tar -C "$REGISTRY_DATA" -czf "$STAGE/registry/data.tar.gz" .
  printf '%s\n' "$REGISTRY_DATA" > "$STAGE/registry/data.path"
fi

if [[ "$INCLUDE_IMAGES" -eq 1 ]]; then
  log 'Exporting current Compose images for offline restore.'
  mapfile -t images < <(compose config --images | sort -u)
  ((${#images[@]} > 0)) || die 'Compose did not report any images.'
  docker save --output "$STAGE/images/compose-images.tar" "${images[@]}"
  image_entries="$(tar -tf "$STAGE/images/compose-images.tar" 2>/dev/null | wc -l | tr -d ' ')"
  [[ "$image_entries" -gt 10 ]] || die 'Docker image export contains no usable layers; use the configured registry or export with Docker Engine.'
  printf '%s\n' "${images[@]}" > "$STAGE/images/list.txt"
fi

cat > "$STAGE/manifest.json" <<EOF
{
  "format_version": 1,
  "product": "xianyu-rewrite",
  "created_at": "$STAMP",
  "source_app_root": "$APP_ROOT",
  "includes_auth": $([[ -f "$STAGE/auth/data.tar.gz" ]] && echo true || echo false),
  "includes_nginx": $([[ -f "$STAGE/config/nginx.conf" ]] && echo true || echo false),
  "includes_registry": $([[ -f "$STAGE/registry/data.tar.gz" ]] && echo true || echo false),
  "includes_images": $([[ -f "$STAGE/images/compose-images.tar" ]] && echo true || echo false)
}
EOF

find "$STAGE" -type f ! -name checksums.sha256 -print0 | sort -z | xargs -0 sha256sum > "$STAGE/checksums.sha256"
tar -C "$STAGE" -czf "$PLAIN" .
read -r -s -p 'Migration passphrase: ' PASSPHRASE
echo
[[ -n "$PASSPHRASE" ]] || die 'Migration passphrase cannot be empty.'
printf '%s' "$PASSPHRASE" | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -in "$PLAIN" -out "$ENCRYPTED" -pass stdin >/dev/null
unset PASSPHRASE
sha256sum "$ENCRYPTED" > "$ENCRYPTED.sha256"

log "Encrypted migration bundle: $ENCRYPTED"
log "Bundle checksum: $ENCRYPTED.sha256"
