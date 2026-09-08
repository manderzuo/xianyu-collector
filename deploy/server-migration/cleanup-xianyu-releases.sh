#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: cleanup-xianyu-releases.sh --release-dir PATH [options]

Options:
  --release-dir PATH  Directory served as /release/xianyu (required)
  --keep COUNT        Number of complete versions to retain (default: 3)
  --apply             Delete archives outside the retention set
  -h, --help          Show this help
EOF
}

die() { echo "[xianyu] ERROR: $*" >&2; exit 1; }
log() { echo "[xianyu] $*"; }

RELEASE_DIR="${XIANYU_RELEASE_DIR:-}"
KEEP="${XIANYU_RELEASE_KEEP:-3}"
APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir) RELEASE_DIR="${2:-}"; shift 2 ;;
    --keep) KEEP="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$RELEASE_DIR" ]] || die '--release-dir is required.'
[[ -d "$RELEASE_DIR" ]] || die "Release directory does not exist: $RELEASE_DIR"
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die '--keep must be a positive integer.'
RELEASE_DIR="$(cd "$RELEASE_DIR" && pwd -P)"

declare -A versions=()
while IFS= read -r -d '' file; do
  base="$(basename "$file")"
  if [[ "$base" =~ ^xianyu-(backend|websocket|scheduler|frontend)-([0-9]+(\.[0-9]+){1,3})\.tar\.gz$ ]]; then
    versions["${BASH_REMATCH[2]}"]=1
  fi
done < <(find "$RELEASE_DIR" -maxdepth 1 -type f -name 'xianyu-*.tar.gz' -print0)

mapfile -t ordered < <(printf '%s\n' "${!versions[@]}" | sort -Vr)
keep_versions=()
for version in "${ordered[@]:0:$KEEP}"; do keep_versions+=("$version"); done

is_kept() {
  local candidate="$1"
  for version in "${keep_versions[@]}"; do
    [[ "$candidate" == "$version" ]] && return 0
  done
  return 1
}

removed=0
while IFS= read -r -d '' file; do
  base="$(basename "$file")"
  if [[ "$base" =~ ^xianyu-(backend|websocket|scheduler|frontend)-([0-9]+(\.[0-9]+){1,3})\.tar\.gz$ ]]; then
    version="${BASH_REMATCH[2]}"
    if ! is_kept "$version"; then
      if [[ "$APPLY" -eq 1 ]]; then
        rm -- "$file"
        log "Removed obsolete image archive: $base"
      else
        log "Would remove obsolete image archive: $base"
      fi
      removed=$((removed + 1))
    fi
  fi
done < <(find "$RELEASE_DIR" -maxdepth 1 -type f -name 'xianyu-*.tar.gz' -print0)

log "Retained versions: ${keep_versions[*]:-none}"
if [[ "$APPLY" -eq 0 && "$removed" -gt 0 ]]; then
  log 'Dry run only. Re-run with --apply to remove obsolete archives.'
fi
