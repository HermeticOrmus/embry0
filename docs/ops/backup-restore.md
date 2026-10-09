# Postgres backup and restore

The backup schedule, how to restore a dump and roll a restore back, how to
rotate the database password, how to copy MinIO's volume before changing its
image ([MinIO's data volume](#minios-data-volume)), and the optional off-host
copy of the dumps and the MinIO buckets, with how to restore a new host from it
alone ([Off-host copy](#off-host-copy)).

embry0 runs a daily `pg_dump` via the `embry0-postgres-backup` compose service. Backups are stored in the `backup-data` named volume:

- `/backups/daily/` — 7 most recent daily dumps
- `/backups/weekly/` — 4 most recent Sunday dumps
- `/backups/monthly/` — 6 most recent month-end dumps
- `/backups/last/` — the most recent dump, as `<db>-<timestamp>.sql.gz` plus a stable `<db>-latest.sql.gz` alias

That volume is on the same disk as the database, so it does not survive the loss of the host. MinIO, which holds the QA artifacts, has no local backup at all. Turn on the [off-host copy](#off-host-copy) to cover both.

**The role and database names are deployment-specific — never hardcode them.** They come from `POSTGRES_USER` / `POSTGRES_DB` in `infra/.env` (compose default `embry0`; an older deployment may still use different names for both). Both the `embry0-postgres` and `embry0-postgres-backup` containers carry `POSTGRES_USER`, `POSTGRES_DB` and `POSTGRES_PASSWORD` in their environment, so every command below reads them there (single-quoted `sh -c '…'`, expanded inside the container) — no name or password is typed on the host. Open an ad-hoc shell the same way: `docker exec -it embry0-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`.

**Restore from the latest backup.** The dump is plain SQL without `--clean`, so it only loads into an EMPTY database — restore into a scratch database, verify, then swap. Run `psql` from the **backup** container: its client matches the `pg_dump` that wrote the file, and the older `psql` inside `embry0-postgres` rejects the dump's `\restrict` header (`invalid command \restrict`). Last drilled 2026-09-20: [`postgres-restore-drill.md`](postgres-restore-drill.md).
```bash
BK=embry0-postgres-backup

# 1. List available backups
docker exec "$BK" ls -lh /backups/last/

# 2. Create an empty scratch database "<db>_restore" (live database untouched)
docker exec "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" createdb -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -O "$POSTGRES_USER" "${POSTGRES_DB}_restore"'

# 3. Load the latest dump into it (to use an older one, swap the path for a file under /backups/daily|weekly|monthly)
docker exec "$BK" bash -c 'set -o pipefail; gunzip -c "/backups/last/${POSTGRES_DB}-latest.sql.gz" | PGPASSWORD="$POSTGRES_PASSWORD" psql -q -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "${POSTGRES_DB}_restore"'

# 4. Verify the scratch copy
docker exec "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "${POSTGRES_DB}_restore" -Atc "SELECT count(*), max(created_at) FROM jobs" -c "SELECT count(*) FROM issues" -c "SELECT count(*) FROM watcher_runs"'
```
Note these numbers — the post-swap check in the recovery block compares against them.

Stop here for a drill, and drop the scratch copy:
```bash
docker exec embry0-postgres-backup bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" dropdb -h "$POSTGRES_HOST" -U "$POSTGRES_USER" "${POSTGRES_DB}_restore"'
```

**Real recovery — swap the verified copy in.** Downtime: this stops the orchestrator and kills running jobs. The live database is kept as `<db>_pre_restore`. Do not run this around the 00:00 UTC daily backup — the `pg_terminate_backend` step would kill an in-flight `pg_dump`.

The block runs in a subshell with `set -e`, so any failure stops it BEFORE the orchestrator is started again (paste it as-is: chaining anything onto the subshell with `&&` or `||` silently disables `set -e` inside it) — a failed swap must never end in a green health check on the old data. It refuses to start unless exactly `<db>` and `<db>_restore` exist. If it reports a leftover `<db>_pre_restore` (from an earlier recovery), drop that first with the one-liner below the block; that is safe once the earlier recovery has been verified and nobody still needs the pre-incident rows for salvage or forensics (rows written after the dump exist ONLY there, and the daily backup does not cover it). Both renames happen in one transaction: either both apply or neither does, so a failure never leaves the deployment without a `<db>`. Run from the repo root.
```bash
(
set -euo pipefail
BK=embry0-postgres-backup
DB=$(docker exec "$BK" printenv POSTGRES_DB)

# Precondition: exactly <db> and <db>_restore, no leftover <db>_pre_restore
STATE=$(docker exec -i "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -At -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT string_agg(datname, ',' ORDER BY datname COLLATE "C") FROM pg_database
 WHERE datname IN (:'db', :'db' || '_restore', :'db' || '_pre_restore');
SQL
)
[ "$STATE" = "${DB},${DB}_restore" ] || { echo "ABORT, nothing changed: expected '${DB},${DB}_restore', found '${STATE}'" >&2; exit 1; }

docker compose -f infra/docker-compose.yml stop orchestrator
docker exec -i "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname IN (:'db', :'db' || '_restore') AND pid <> pg_backend_pid();
BEGIN;
SELECT format('ALTER DATABASE %I RENAME TO %I', :'db', :'db' || '_pre_restore') \gexec
SELECT format('ALTER DATABASE %I RENAME TO %I', :'db' || '_restore', :'db') \gexec
COMMIT;
SQL
echo "SWAP OK - now serving the restored copy:"
docker exec "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*), max(created_at) FROM jobs"'
docker compose -f infra/docker-compose.yml up -d orchestrator
curl -s http://localhost:8200/health
)
```
Success looks like: `BEGIN`, two `ALTER DATABASE` lines, `COMMIT`, then `SWAP OK` followed by a `jobs` count and latest `created_at` that EQUAL the step-4 numbers from the scratch copy (the old data has more rows and a later timestamp). An `ABORT`, or an `ERROR:` line with no `COMMIT` / `SWAP OK` after it, means the swap did not happen: the renames were rolled back, `<db>` is still the old data, and (unless it was the precondition `ABORT`, which fires before anything is touched) the orchestrator is still stopped. Fix the cause and re-run, or bring the old data back up with `docker compose -f infra/docker-compose.yml up -d orchestrator`. If `SWAP OK` is printed but the verification query then errors or shows the wrong numbers, the swap DID commit and the restored copy is bad — the orchestrator is left stopped on purpose; use the roll-back block below.

Drop a leftover `<db>_pre_restore` (see above for when that is safe):
```bash
docker exec embry0-postgres-backup bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" dropdb -h "$POSTGRES_HOST" -U "$POSTGRES_USER" "${POSTGRES_DB}_pre_restore"'
```

**Roll a swap back** (the restored copy turned out worse than what it replaced). Same guarantees: one transaction, stops on failure. Afterwards `<db>` is the pre-restore data again and the restored copy is back under `<db>_restore`.
```bash
(
set -euo pipefail
BK=embry0-postgres-backup
docker compose -f infra/docker-compose.yml stop orchestrator
docker exec -i "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname IN (:'db', :'db' || '_pre_restore') AND pid <> pg_backend_pid();
BEGIN;
SELECT format('ALTER DATABASE %I RENAME TO %I', :'db', :'db' || '_restore') \gexec
SELECT format('ALTER DATABASE %I RENAME TO %I', :'db' || '_pre_restore', :'db') \gexec
COMMIT;
SQL
echo "ROLLBACK OK - now serving the pre-restore data:"
docker exec "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*), max(created_at) FROM jobs"'
docker compose -f infra/docker-compose.yml up -d orchestrator
curl -s http://localhost:8200/health
)
```

**Upgrading POSTGRES_PASSWORD on a live deploy.** Compose splices the password into a URL — `DATABASE_URL: postgresql://<user>:${POSTGRES_PASSWORD}@postgres:5432/<db>` — so it must be URL-safe and compose-safe: any of `@ / : # % ?` breaks the URL and `$` is interpolated by compose. Postgres itself accepts such a password, so the rotation appears to succeed and then the orchestrator cannot connect. Use only `A-Z a-z 0-9 _ -`; generate one with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
```bash
# 1. Update the password inside Postgres (read silently; never lands in shell history or the process list)
read -rs NEWPW && export NEWPW
docker exec -e NEWPW -i embry0-postgres sh -c 'psql -v ON_ERROR_STOP=1 -v role="$POSTGRES_USER" -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\getenv newpw NEWPW
ALTER USER :"role" PASSWORD :'newpw';
SQL
unset NEWPW

# 2. Edit the env files (see the table below) - infra/.env POSTGRES_PASSWORD is the one that matters
# 3. Recreate orchestrator and backup containers (postgres keeps running)
cd infra && docker compose up -d --force-recreate orchestrator postgres-backup
curl -s http://localhost:8200/health
```

Which keys to change in step 2, and who actually reads them (compose `environment:` overrides `env_file:`):

| File | Key | Read by | Action |
|---|---|---|---|
| `infra/.env` | `POSTGRES_PASSWORD` | compose interpolation: the orchestrator's effective `DATABASE_URL` and the backup container's `POSTGRES_PASSWORD` | **Must change — the only one the running stack uses** |
| `infra/.env` | `DATABASE_URL` | nothing (compose never references `${DATABASE_URL}`) | Change only if it embeds a literal password instead of `${POSTGRES_PASSWORD}` |
| root `.env` | `POSTGRES_PASSWORD` | loaded into the orchestrator via `env_file` but unused there | Change, so the two files agree |
| root `.env` | `DATABASE_URL` | loaded via `env_file`, then overridden by the compose `environment:` value; used only when running embry0 outside compose | Change only if it embeds a literal password |

Note: The `backup-data` volume persists across redeployments. Rotate backup credentials by updating `POSTGRES_PASSWORD` (above) — the backup container reads it from the environment. The `embry0-postgres` container's own `POSTGRES_PASSWORD` env is only consulted at first init and goes stale after a rotation; that is harmless because the commands that exec into it connect over the local socket.

## MinIO's data volume

MinIO holds the QA artifacts (the `qa-artifacts` bucket) in the `embry0_minio-data` volume, which nothing backs up on a schedule. Copy it before you change the `minio` image, and keep the image that wrote it, so the change can be undone.

The stack runs Silo, the community fork of MinIO, since #573. Silo reads a MinIO volume as it is, so the switch from `quay.io/minio/minio:RELEASE.2025-05-24T17-08-30Z` needs no migration ([deploy.md](deploy.md#images-registry-pins-and-rebuilds)). That release could read the volume back after Silo had written to it, for the bucket, lifecycle rule and objects embry0 uses. Silo's release notes still say an older server may not read everything a newer one writes, so roll back by restoring the copy, not by changing the image alone.

**Copy the volume.** MinIO is stopped for the copy, so it is consistent; QA uploads fail while it is down. The `postgres` image only supplies `tar`; it is already on the host.
```bash
docker compose -f infra/docker-compose.yml stop minio
docker run --rm -v embry0_minio-data:/data:ro -v "$PWD":/backup postgres:16.6-alpine \
  tar czf "/backup/minio-data-$(date -u +%Y%m%dT%H%M%SZ).tgz" -C /data .
docker compose -f infra/docker-compose.yml up -d minio
```

**Keep the old image.** A host that pulled the old image still has it until a prune removes it. Save it to a file next to the copy:
```bash
docker save quay.io/minio/minio:RELEASE.2025-05-24T17-08-30Z | gzip > minio-RELEASE.2025-05-24T17-08-30Z.tar.gz
```

**Roll back.** Bring the old image back (`gunzip -c minio-RELEASE.2025-05-24T17-08-30Z.tar.gz | docker load`), set the `minio` service's `image:` in `infra/docker-compose.yml` back to it, then replace the volume's contents with the copy:
```bash
docker compose -f infra/docker-compose.yml stop minio
docker run --rm -v embry0_minio-data:/data -v "$PWD":/backup postgres:16.6-alpine \
  sh -c 'find /data -mindepth 1 -delete && tar xzf /backup/minio-data-<timestamp>.tgz -C /data'
docker compose -f infra/docker-compose.yml up -d minio
```
Artifacts written after the copy are lost. They expire after `QA_ARTIFACT_RETENTION_DAYS` anyway.

## Off-host copy

The optional `offhost-backup` compose service (#581) copies both data sets to an S3-compatible bucket you own, off this host:

| Data set | Source | In the bucket | Recorded as fresh when |
|---|---|---|---|
| `postgres` | each complete dump in `/backups/last/` (the `backup-data` volume, mounted read-only) | `<path>/postgres/<db>-<timestamp>.sql.gz` | the newest dump in the bucket is recent, by its modification time, which is when `pg_dump` wrote it |
| `minio` | every bucket of the stack's MinIO, read over its S3 API | `<path>/minio/<bucket>/<key>` | the last MinIO copy finished, by the marker `<path>/status/minio-last-copy` |

It runs a pass at start and then every `OFFHOST_BACKUP_INTERVAL_SECONDS` (default 3600). A dump `pg_dump` is still writing fails `gzip -t` and waits for the next pass. After each pass the service reads the bucket back and writes what it found to its status volume, which the orchestrator serves as `embry0_offhost_backup_last_success_timestamp_seconds{dataset}` ([metrics.md](metrics.md#off-host-backup)).

**It never deletes anything off-host**: it uses `rclone copy`, which adds and updates objects, never `sync`. A wiped or replaced `backup-data` volume therefore cannot empty the bucket. Two consequences:

- **Retention is the bucket's job.** Dumps accumulate, one a day. Set a lifecycle rule on `<path>/postgres/` (for example, expire after 90 days); MinIO objects that embry0 deletes locally (QA artifact retention, `QA_ARTIFACT_RETENTION_DAYS`) stay in the bucket until a lifecycle rule on `<path>/minio/` removes them.
- **An overwritten MinIO object is replaced in the bucket.** If you need the earlier version back, turn on versioning on the bucket.

The copy is not encrypted by embry0. The dumps hold the whole database, including Fernet-encrypted secrets whose key (`ENVIRONMENT_SECRET_KEY`) is not in the dump. Keep the bucket private, use the provider's encryption at rest, and keep `ENVIRONMENT_SECRET_KEY` somewhere other than this host and this bucket: without it a restored database cannot decrypt its credential and env var secrets.

**Turn it on.** In `infra/.env` (or the root `.env` with `embry0 start`):

```bash
COMPOSE_PROFILES=offhost-backup              # add to the list if you already run profiles
OFFHOST_BACKUP_S3_PATH=<bucket>/<prefix>
OFFHOST_BACKUP_S3_ENDPOINT=https://<s3-endpoint>    # empty for AWS
OFFHOST_BACKUP_S3_PROVIDER=Other            # rclone's provider name: AWS, Cloudflare, Wasabi, Minio, ...
OFFHOST_BACKUP_S3_REGION=                     # if your provider needs one
OFFHOST_BACKUP_S3_ACCESS_KEY_ID=<key id>
OFFHOST_BACKUP_S3_SECRET_ACCESS_KEY=<secret>
```

Then `docker compose -f infra/docker-compose.yml up -d offhost-backup orchestrator`. The orchestrator reads `OFFHOST_BACKUP_S3_PATH` to arm the alert, so it is recreated, which stops running jobs: do it while none runs, or let the next `infra/deploy.sh` pick it up. Create the bucket first: the service does not create it. Give the key list, read and write on `<bucket>/<prefix>`, and no delete; it needs no permission on any other bucket.

Set the profile and the path together. The path arms the alert: a path without the profile reads as "never copied" and fires it, which is the point. The profile without a path makes the service exit with `OFFHOST_BACKUP_S3_PATH is not set` and restart.

**Check it.** A pass logs one line per data set and `pass complete`:

```bash
docker logs --tail 20 embry0-offhost-backup
docker exec embry0-offhost-backup sh /scripts/offhost-backup.sh once    # run a pass now; exit 0 when both copied and read back
docker exec embry0-offhost-backup sh -c 'for d in postgres minio; do echo "$d $(date -u -d @"$(cat /status/$d)" +%FT%TZ)"; done'
```

**Alert.** `infra/prometheus/offhost-backup.rules.yml` holds `Embry0OffhostBackupStale`: the newest copy of either data set is more than 28 h old for 15 minutes, while `embry0_offhost_backup_enabled` is 1. Both series exist from the first scrape and read 0 until a copy is read back, so a copy that never once succeeded fires too. Load the file into the Prometheus that scrapes the orchestrator. Its `promtool` tests are `offhost-backup.rules.test.yml` ([verification.md](verification.md#gates)). If you lengthen the interval, raise the 28 h with it. The alert does not cover the orchestrator itself being down: then neither series exists, and your scrape-health alert (`up == 0`) is the one that fires.

### Restore a new host from the off-host copy

For a host that never held the original volumes. Every command runs from the repo root on the new host, whose `infra/.env` carries the old host's `POSTGRES_USER` and `POSTGRES_DB` and the same `OFFHOST_BACKUP_*` settings; the passwords and `MINIO_ROOT_*` may be new. The role name must match: the dump assigns ownership to it, and the load stops at the first `ALTER … OWNER TO` it cannot apply. `ob` (defined in each block) runs a one-off copy of the `offhost-backup` service, with the same rclone remotes, without starting anything else.

**Postgres.** This streams a dump from the bucket into an empty scratch database, then reuses the verify and swap steps above.

```bash
ob() { docker compose -f infra/docker-compose.yml run --rm --no-deps -T offhost-backup "$@"; }
BK=embry0-postgres-backup

# 1. Start the database and the backup container (it carries the matching psql)
docker compose -f infra/docker-compose.yml up -d postgres postgres-backup

# 2. List the dumps in the bucket, oldest first, and pick one (default: the newest)
ob -c 'rclone lsf --files-only "offhost:$OFFHOST_BACKUP_S3_PATH/postgres"' | sort
DUMP=$(ob -c 'rclone lsf --files-only "offhost:$OFFHOST_BACKUP_S3_PATH/postgres"' | sort | tail -n 1); echo "$DUMP"

# 3. Create the empty scratch database "<db>_restore"
docker exec "$BK" bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" createdb -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -O "$POSTGRES_USER" "${POSTGRES_DB}_restore"'

# 4. Stream the dump from the bucket into it
(
set -euo pipefail
ob -c 'rclone cat "offhost:$OFFHOST_BACKUP_S3_PATH/postgres/$1"' sh "$DUMP" \
  | docker exec -i "$BK" bash -c 'set -o pipefail; gunzip -c | PGPASSWORD="$POSTGRES_PASSWORD" psql -q -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "${POSTGRES_DB}_restore"'
echo "LOAD OK: $DUMP"
)
```

No `LOAD OK` means the load failed: drop the scratch database (the `dropdb` one-liner above) and fix the cause before trying again. Then run step 4 of the local restore ("Verify the scratch copy") and the swap block, unchanged. On a new host the live `<db>` is empty, or holds only what the orchestrator's migrations created, so the swap keeps nothing worth salvaging under `<db>_pre_restore`.

**MinIO.** Start MinIO, copy every bucket back, then check each object against the bucket:

```bash
ob() { docker compose -f infra/docker-compose.yml run --rm --no-deps -T offhost-backup "$@"; }
docker compose -f infra/docker-compose.yml up -d minio

# Copy back. --s3-no-check-bucket=false lets rclone create the buckets, which the copy off-host never does.
ob -c 'rclone copy --s3-no-check-bucket=false "offhost:$OFFHOST_BACKUP_S3_PATH/minio" embry0minio:'

# Every object in the bucket must now be in MinIO with the same size and checksum
ob -c 'rclone check --one-way "offhost:$OFFHOST_BACKUP_S3_PATH/minio" embry0minio:'
```

`rclone check` ends with `0 differences found` on success. Then start the rest of the stack (`infra/deploy.sh`, or `docker compose up -d`) and, once it is up, turn the off-host copy on again on this host. Its first pass adds this host's dumps next to the old host's.

Last drilled 2026-10-09: [`postgres-restore-drill.md`](postgres-restore-drill.md#2026-10-09--off-host-restore-onto-a-fresh-host-issue-581).
