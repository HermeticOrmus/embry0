#!/bin/sh
# Copy the Postgres dumps and the MinIO buckets to an off-host bucket (#581).
#
# Runs in the offhost-backup compose service (rclone image, profile
# offhost-backup). Every OFFHOST_BACKUP_INTERVAL_SECONDS it:
#
#   1. copies each complete dump in /backups/last to <dest>/postgres/;
#   2. copies every MinIO bucket to <dest>/minio/<bucket>/, then writes the
#      marker <dest>/status/minio-last-copy;
#   3. reads the bucket back and writes, per dataset, the Unix time of the
#      newest copy found there to /status/<dataset>. The orchestrator serves
#      those as embry0_offhost_backup_last_success_timestamp_seconds.
#
# It only ever adds or updates objects off-host, never deletes them: a wiped
# or replaced local volume must not be able to empty the off-host copy.
# Retention is the bucket's lifecycle rule (docs/ops/backup-restore.md).
#
# The two remotes come from the environment compose sets: `offhost` (the
# operator's S3-compatible bucket) and `embry0minio` (the stack's MinIO).
#
# `sh offhost-backup.sh once` runs one pass and exits with its status.

set -u

DUMPS_DIR="${OFFHOST_BACKUP_DUMPS_DIR:-/backups/last}"
STATUS_DIR="${OFFHOST_BACKUP_STATUS_DIR:-/status}"
INTERVAL="${OFFHOST_BACKUP_INTERVAL_SECONDS:-3600}"

# To stderr: stdout carries the dump list and the read-back values.
log() { printf '%s offhost-backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

dest_path=$(printf '%s' "${OFFHOST_BACKUP_S3_PATH:-}" | sed 's#^/*##; s#/*$##')
if [ -z "$dest_path" ]; then
  log "OFFHOST_BACKUP_S3_PATH is not set: set it to <bucket>/<prefix> in infra/.env, or drop the offhost-backup profile"
  exit 64
fi
case "$INTERVAL" in
  '' | *[!0-9]*) log "OFFHOST_BACKUP_INTERVAL_SECONDS must be a whole number of seconds, got '$INTERVAL'"; exit 64 ;;
esac
DEST="offhost:$dest_path"

# Every complete dump, by name. A dump pg_dump is still writing has no gzip
# trailer yet, so `gzip -t` refuses it and it waits for the next pass. The
# `-latest` alias is a symlink to a file already in the list.
complete_dumps() {
  for f in "$DUMPS_DIR"/*.sql.gz; do
    [ -f "$f" ] || continue
    case "$f" in *-latest.sql.gz) continue ;; esac
    if gzip -t "$f" 2>/dev/null; then
      basename "$f"
    else
      log "skipping $(basename "$f"): not a complete gzip file yet"
    fi
  done
}

copy_postgres() {
  list=$(mktemp) || return 1
  complete_dumps >"$list" || { rm -f "$list"; return 1; }
  rclone copy --files-from-raw "$list" --no-traverse "$DUMPS_DIR" "$DEST/postgres"
  rc=$?
  rm -f "$list"
  return $rc
}

copy_minio() {
  rclone copy embry0minio: "$DEST/minio" || return 1
  date -u +%s | rclone rcat "$DEST/status/minio-last-copy"
}

# The newest modification time under a remote path, as Unix seconds. Prints
# nothing when the path holds no file; fails when the bucket cannot be read.
newest() {
  out=$(rclone lsf --files-only --format t --time-format unix "$1") || return 1
  printf '%s\n' "$out" | sort -n | tail -n 1
}

# Write one dataset's status file atomically. Unchanged when the read-back
# failed or found nothing, so the gauge ages and the alert can fire.
record() {
  dataset=$1
  value=$2
  case "$value" in
    '' | *[!0-9]*) log "$dataset: no copy found off-host"; return 1 ;;
  esac
  printf '%s\n' "$value" >"$STATUS_DIR/.$dataset.tmp" && mv "$STATUS_DIR/.$dataset.tmp" "$STATUS_DIR/$dataset"
}

run_once() {
  ok=0
  if copy_postgres; then log "postgres: copied to $DEST/postgres"; else log "postgres: copy FAILED"; ok=1; fi
  if copy_minio; then log "minio: copied to $DEST/minio"; else log "minio: copy FAILED"; ok=1; fi

  if pg=$(newest "$DEST/postgres"); then record postgres "$pg" || ok=1; else log "postgres: read-back FAILED"; ok=1; fi
  if mn=$(newest "$DEST/status/minio-last-copy"); then record minio "$mn" || ok=1; else log "minio: read-back FAILED"; ok=1; fi
  return $ok
}

mkdir -p "$STATUS_DIR"
if [ "${1:-}" = once ]; then
  run_once
  exit $?
fi

log "copying to $DEST every ${INTERVAL}s"
while :; do
  if run_once; then log "pass complete"; else log "pass finished with errors"; fi
  sleep "$INTERVAL"
done
