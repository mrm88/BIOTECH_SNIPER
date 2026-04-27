#!/usr/bin/env bash
# backup_db.sh — Daily SQLite backup with 30-day rotation.
#
# Produces:  /root/alpha_sniper/backups/db_YYYY-MM-DD.gz
# Source:    /root/alpha_sniper/repo/data/alpha_sniper.db
#
# Behavior:
#   - Uses sqlite3 ".backup" to take a consistent online snapshot (WAL-safe),
#     then gzips it to the dated filename.
#   - Idempotent: same-UTC-day re-runs overwrite the existing dated archive.
#   - Rotation: prunes db_*.gz files older than 30 days (mtime). Hard cap
#     additionally keeps at most the 30 most-recent archives.
#
# Wired into alpha-sniper.service via ExecStartPost. Can also be run manually:
#     bash /root/alpha_sniper/repo/deploy/scripts/backup_db.sh
#
# Exits 0 on success, non-zero on failure.

set -euo pipefail

# Paths (override via env for local testing)
DB_PATH="${ALPHA_SNIPER_DB_PATH:-/root/alpha_sniper/repo/data/alpha_sniper.db}"
BACKUP_DIR="${ALPHA_SNIPER_BACKUP_DIR:-/root/alpha_sniper/backups}"
RETENTION_DAYS="${ALPHA_SNIPER_BACKUP_RETENTION_DAYS:-30}"
MAX_BACKUPS="${ALPHA_SNIPER_BACKUP_MAX_COUNT:-30}"

DATE_UTC="$(date -u +%F)"
TARGET="${BACKUP_DIR}/db_${DATE_UTC}.gz"

log() {
  # Single-line JSON log — picked up by systemd-journald.
  printf '{"ts":"%sZ","level":"INFO","event":"%s","module":"backup_db","message":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S)" "$1" "${2:-}"
}

err() {
  printf '{"ts":"%sZ","level":"ERROR","event":"%s","module":"backup_db","message":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S)" "$1" "${2:-}" >&2
}

if [[ ! -f "${DB_PATH}" ]]; then
  err "backup_db_missing_source" "DB not found at ${DB_PATH}"
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  err "backup_db_missing_sqlite3" "sqlite3 not on PATH"
  exit 1
fi

# Ensure backup directory exists with restrictive perms (owner-only by default).
mkdir -p "${BACKUP_DIR}"
chmod 750 "${BACKUP_DIR}" 2>/dev/null || true

# Take a consistent SQLite snapshot via .backup (handles WAL safely).
# Use a tempfile in the same directory so the rename/gzip is atomic-ish.
TMP_SNAPSHOT="$(mktemp "${BACKUP_DIR}/.backup_db.XXXXXX.tmp")"
trap 'rm -f "${TMP_SNAPSHOT}" "${TMP_SNAPSHOT}.gz"' EXIT

sqlite3 "${DB_PATH}" ".backup '${TMP_SNAPSHOT}'"

# Compress (-f overwrites in case of leftover .gz). Same-UTC-day idempotency:
# we always overwrite ${TARGET} via mv -f below.
gzip -f -9 "${TMP_SNAPSHOT}"
mv -f "${TMP_SNAPSHOT}.gz" "${TARGET}"
chmod 640 "${TARGET}" 2>/dev/null || true
# Clear the temp pattern so the trap doesn't try to delete the moved file.
TMP_SNAPSHOT=""

log "backup_db_written" "wrote ${TARGET}"

# --- Rotation ---------------------------------------------------------------
# 1) Delete archives older than RETENTION_DAYS by mtime.
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'db_*.gz' -mtime +"${RETENTION_DAYS}" -print -delete \
  | while read -r removed; do
      log "backup_db_rotated_age" "removed ${removed}"
    done || true

# 2) Hard cap on count: keep at most MAX_BACKUPS most-recent archives.
#    (Guards against multi-run-per-day pathology even though we only write 1/day.)
#    Portable to bash 3.2 — no `mapfile`/`readarray`.
SKIP=$((MAX_BACKUPS + 1))
ls -1t "${BACKUP_DIR}"/db_*.gz 2>/dev/null | tail -n +"${SKIP}" | while IFS= read -r over; do
  [[ -n "${over}" ]] || continue
  rm -f -- "${over}"
  log "backup_db_rotated_count" "removed ${over}"
done

REMAINING="$(ls -1 "${BACKUP_DIR}"/db_*.gz 2>/dev/null | wc -l | tr -d ' ')"
log "backup_db_done" "archives_retained=${REMAINING}"
