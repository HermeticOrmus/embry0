# Postgres restore drill

Evidence that the restore procedure in [backup-restore.md](backup-restore.md)
actually works. Re-run the drill after any change to the backup service, the
Postgres major version, or the documented commands, and append a new entry
under [Drill log](#drill-log). Record metadata only — never data contents,
row counts or sizes.

Until #529 the procedure lived in the repo's `CLAUDE.md`, under "Postgres
Backup / Restore", which is why the runs below name that file. It moved
verbatim, so they still describe the same blocks.

## Pinned versions

Both images are pinned in `infra/docker-compose.yml`. Bump them together and
re-run the drill; update this table in the same change (a unit test holds the
table equal to the compose file).

| Service | Image | Tools |
|---|---|---|
| `postgres` (server) | `postgres:16.6-alpine` | server + `psql` 16.6 |
| `postgres-backup` (dump + restore client) | `prodrigestivill/postgres-backup-local:16@sha256:a0103fbce0c48ccc7693ddbb2a2bfa733048db845354f635efef9e4566de185d` | `pg_dump` / `psql` 16.10 (Debian 16.10-1.pgdg13+1) |
| `offhost-backup` (off-host copy, #581) | `rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5` | rclone 1.75.1 |

The client is deliberately allowed to be newer than the server (`pg_dump`
supports older servers), never older. Because the 16.10 dump starts with a
`\restrict` header that the server image's 16.6 `psql` rejects, restores run
from the backup container — see finding 3 below. The digest is the one that
was running in production on 2026-09-20 (#324), so pinning it changed no
bits; before the pin the tag floated.

## How to run a drill without touching the live database

The documented restore creates a scratch database on the live cluster. A
drill goes one step further and uses throwaway containers, so production is
only ever read (one `docker cp` of a dump file).

```bash
SCRATCH=$(mktemp -d) && chmod 700 "$SCRATCH" && mkdir "$SCRATCH/last"
NET=embry0-restore-drill
DB=$(docker exec embry0-postgres printenv POSTGRES_DB)
ROLE=$(docker exec embry0-postgres printenv POSTGRES_USER)
IMG=$(docker inspect -f '{{.Image}}' embry0-postgres-backup)   # same client version as prod

# 1. Copy the latest dump out (read-only on the backup container)
docker cp -L "embry0-postgres-backup:/backups/last/${DB}-latest.sql.gz" "$SCRATCH/last/"

# 2. Throwaway Postgres + a stand-in for the backup container, on a private network.
#    Same env key names as compose, a throwaway password, the dump mounted read-only.
docker network create "$NET"
docker run -d --name "$NET-pg" --network "$NET" --network-alias postgres \
  -e POSTGRES_USER="$ROLE" -e POSTGRES_DB="$DB" -e POSTGRES_PASSWORD=drill_only \
  postgres:16.6-alpine
docker run -d --name "$NET-bk" --network "$NET" --entrypoint sleep \
  -v "$SCRATCH/last:/backups/last:ro" \
  -e POSTGRES_HOST=postgres -e POSTGRES_USER="$ROLE" -e POSTGRES_DB="$DB" \
  -e POSTGRES_PASSWORD=drill_only "$IMG" 3600

# 3. Follow backup-restore.md verbatim, with the container name in each block changed to "$NET-bk".
#    There is no orchestrator here, so shadow `docker compose` and `curl` with logging stubs
#    instead of editing the blocks - the stubs also prove WHICH of those lines were reached:
docker() { if [ "$1" = compose ]; then echo "[stub] docker $*"; else command docker "$@"; fi; }
curl() { echo "[stub] curl $*"; }

# 4. Clean up everything, including the dump copy
docker rm -f -v "$NET-bk" "$NET-pg" && docker network rm "$NET" && rm -rf "$SCRATCH"
```

The throwaway Postgres must use the same role name as production: the dump
contains `ALTER … OWNER TO <role>` statements and fails under
`ON_ERROR_STOP=1` if that role does not exist.

## How to run the off-host drill

The off-host restore ([backup-restore.md, "Restore a new host from the
off-host copy"](backup-restore.md#restore-a-new-host-from-the-off-host-copy))
must work from the bucket alone, so its drill uses two compose projects that
never share a volume, and a scratch bucket in place of the real one:

1. Start a throwaway S3 server as the bucket (any S3-compatible server; it
   must be reached by URL, not over the stack's network), and create a bucket.
2. **Host A:** a compose project (`-p <drill>-a`) with an `infra/.env` copy
   whose `OFFHOST_BACKUP_*` point at that bucket and whose
   `COMPOSE_PROFILES` holds `offhost-backup`. Start `postgres`,
   `postgres-backup`, `minio`; write marker rows and objects; run
   `/backup.sh` in the backup container; start `offhost-backup`. Record a
   fingerprint of the data (row counts plus an `md5` over a table, a
   `sha256` per object).
3. `docker compose -p <drill>-a down -v`: host A's volumes are gone.
4. **Host B:** a second project (`-p <drill>-b`), same env file. Run the
   runbook's blocks verbatim, with `docker` shadowed to add the project and
   env file to every `docker compose` call and to stub the calls that name
   the orchestrator (there is no orchestrator image in a drill), and `curl`
   stubbed, as in the drill above.
5. Compare the fingerprints, then `down -v` host B and remove the bucket.

Never point a drill at the deployment's real bucket: the copy only adds,
so drill objects would stay there until its lifecycle rule removed them.

## Drill log

`<db>` and `<role>` below were that deployment's `POSTGRES_DB` and
`POSTGRES_USER`. The host is not named. The migrations table on that
deployment was named from those same values.

### 2026-09-20 (run 1) — issue #271

| | |
|---|---|
| Source dump | `/backups/last/<db>-latest.sql.gz`, written 2026-09-20 00:00 by the daily job |
| Dump format | plain SQL, `pg_dump` 16.10, no `--clean`, no `CREATE DATABASE`; 38 `OWNER TO` statements |
| Server restored into | `postgres:16.6-alpine` (same image tag as compose), role `<role>` and database `<db>` |
| Client | `psql` 16.10 from the production backup image (by image ID) |
| Procedure | the `CLAUDE.md` bash blocks, extracted from the file by script and executed unmodified except: `BK` pointed at the stand-in container; the two `docker compose` lines of the swap block removed |
| Duration | 4.3 s wall clock for steps 1–4 (list, `createdb`, load, verify); the load itself ≈ 2.7 s |
| Result | exit 0 with `ON_ERROR_STOP=1`, no errors; 34 tables in `public` |

Row counts of `jobs`, `issues`, `watcher_runs` and the migrations table in the
restored copy were compared with the live database by a read-only `SELECT`.
The live copy was about 3.7 h newer than the dump, so equal or slightly higher
live counts were the expected shape, and that is what the comparison showed.

Also exercised in the throwaway containers: the (then non-transactional,
since replaced - see run 2) rename swap block (scratch
copy became the primary database, the previous one kept as
`<db>_pre_restore`, `jobs` count unchanged afterwards), the `dropdb` one-liner,
and the password-rotation block (old password rejected, new one accepted;
the new password was supplied via an exported variable instead of the
interactive `read -rs`).

Findings that changed the documentation:

1. The old snippets hardcoded role/db `embry0`; this deployment's
   `POSTGRES_USER` and `POSTGRES_DB` were different names for both. The
   old rotation command additionally omitted `-d`, so `psql` would have
   looked for a database named after the role.
2. The old restore piped the dump straight into the live database. The dump
   has no `--clean`, so against a populated database that produces a stream
   of "already exists" errors and duplicate-key `COPY` failures rather than
   a restore. The procedure now loads into an empty scratch database and
   swaps by rename.
3. A first attempt using the `psql` inside `embry0-postgres` (16.6) failed
   immediately with `invalid command \restrict`: the backup image's
   `pg_dump` 16.10 writes a `\restrict` header that older clients do not
   know. The restore must run from the backup container, whose client
   matches the dump.

Not covered: the `docker compose stop/up orchestrator` lines and the
orchestrator's behaviour against a restored database (would require
touching production), and restoring from the `weekly/` or `monthly/` tiers
(same file format, different path).

Cleanup: both throwaway containers, their network, and the dump copy were
removed; nothing was written to the live database or the backup volume.

### 2026-09-20 (run 2) — swap, roll-back and failure paths after PR review

Review of the first version found the swap block was not safe enough for an
incident runbook (non-atomic renames, no second-restore path, failure fell
through to a green health check). The blocks were rewritten and re-drilled
the same way: same dump, same throwaway
`postgres:16.6-alpine` + production backup image, every `CLAUDE.md` bash
block extracted by script and run unmodified apart from the container name,
with `docker compose` / `curl` shadowed by logging stubs. The stand-in
"live" database held a one-row `jobs` table plus a marker table so old and
restored data were distinguishable.

| # | Scenario | Result |
|---|---|---|
| A1 | Restore steps 1–4 into `<db>_restore` | exit 0, 6.3 s; `jobs` count as in run 1 |
| A2 | Swap block, happy path | `BEGIN`, 2× `ALTER DATABASE`, `COMMIT`, `SWAP OK`; 0.4 s; `<db>` now restored data (`jobs` count as in A1, marker gone), old data under `<db>_pre_restore`; stop → up → health all reached |
| C | Roll-back block | `ROLLBACK OK`; `<db>` is the old data again (marker present, `jobs` 1), restored copy back under `<db>_restore` |
| A3 | Swap again after a roll-back | succeeds as A2 |
| B1–B2 | Second restore with a leftover `<db>_pre_restore` | load succeeds; swap prints `ABORT, nothing changed: expected … found …`, exit 1, **no** compose call at all (orchestrator never stopped) |
| B3 | `dropdb …_pre_restore` one-liner, then swap | succeeds |
| D1 | Second rename fails (scratch database removed after the precondition passed, via the stop-stub) | `BEGIN`, `ALTER DATABASE`, `ERROR`; psql exit 3; first rename rolled back - `<db>` still present with its data; no `SWAP OK`; `up -d` and health check **not** reached |
| D2 | First rename fails (`<db>_pre_restore` created after the precondition passed) | `ERROR`, exit 3, nothing renamed, `up -d` / health not reached |
| D3 | Roll-back block with no `<db>_pre_restore` | `ERROR`, nothing renamed, `<db>` intact, `up -d` / health not reached |
| E | `dropdb …_restore` one-liner | succeeds |
| F | Swap where the scratch copy is an empty database | renames commit, `SWAP OK`, verification query errors (no `jobs` table), exit 1, orchestrator left stopped - the documented cue to use the roll-back block |

Finding from this run: the first rewrite ended the subshell with
`( set -e; … ) && curl …/health`. Bash ignores `set -e` inside the left
operand of `&&`, so in D1–D3 the block printed `SWAP OK` and "restarted" the
orchestrator after a failed rename - exactly the fall-through the rewrite
was meant to remove. The renames themselves were still atomic. The health
check now lives inside the subshell, the doc warns against chaining onto
it, and the table above is from the run after that fix.

Still not drilled: real `docker compose stop/up orchestrator` and the
orchestrator's behaviour on a restored database; `pg_terminate_backend`
against a live client or an in-flight `pg_dump` (no clients existed in the
drill - every run terminated 0 backends); the password-rotation URL-safety
constraint (documented from the compose `DATABASE_URL` template, not
reproduced); the interactive `read -rs` prompt.

Cleanup: throwaway containers, network and dump copy removed; nothing was
written to the live database or the backup volume.

### 2026-10-09 — off-host restore onto a fresh host (issue #581)

Run as described in [How to run the off-host drill](#how-to-run-the-off-host-drill),
on a workstation, against a scratch bucket on a throwaway MinIO reached by
URL. No production data or bucket was involved.

| | |
|---|---|
| Compose | `infra/docker-compose.yml` from the #581 branch, plus a drill-only override of the `minio` image (below). The service still had `depends_on: minio` then, since dropped so a down MinIO cannot hold back the Postgres copy; the runbook's calls pass `--no-deps` either way |
| Host A data | three tables (1,000 / 200 / 50 rows); 33 objects in two buckets, nested keys, one of 12 MB |
| Off-host copy | the `offhost-backup` service, started by its profile; first pass copied the dump and all 33 objects, wrote the marker and both status files; a second pass (`once`) exited 0 and left the bucket at the same 35 objects |
| Host A removed | `down -v`; no `offhostdrill-a_*` volume left |
| Host B | new project, new volumes; the runbook's Postgres block, the local verify step, the swap block and the MinIO block, extracted from `backup-restore.md` by script and run unmodified under a shadowed `docker` / `curl` |
| Result | `LOAD OK`, the swap's `BEGIN` … `COMMIT`, `SWAP OK`; `rclone check`: `0 differences found`, `33 matching files`. Postgres fingerprint (counts and `md5` over `jobs`) and every object's `sha256` equal to host A's |
| Afterwards | `offhost-backup` started on host B; its first pass added host B's own dump beside host A's, as the runbook says |

Also shown: with the bucket's server stopped, a pass logged `copy FAILED`
and `read-back FAILED` for both data sets, exited 1 and left both status
files unchanged, so the gauge keeps ageing. That run shortened rclone's
retries and timeouts through its environment (`RCLONE_RETRIES=1`,
`RCLONE_LOW_LEVEL_RETRIES=1`, `RCLONE_CONTIMEOUT=5s`, `RCLONE_TIMEOUT=10s`) to
finish in a second; with the defaults the service runs, the same pass was
still retrying after ten minutes. A pass that slow only delays the next one,
and the alert fires on the status it leaves untouched.

The alert, against live scrapes of the orchestrator's real `/metrics` code
(`create_app` with a test lifespan) by Prometheus 3.14.0, three targets: copies
fresh from this drill; the same with the `minio` status file removed (never
copied); and the second with `OFFHOST_BACKUP_S3_PATH` unset. The rule file
unmodified, and a copy with only its threshold relaxed to `> 0` (and renamed,
to load beside it). After the 15-minute `for`: the unmodified rule fired for
the never-copied `minio` only; the relaxed copy fired for all four `dataset`
series of the two enabled targets; nothing fired for the disabled target.

Finding: the pinned `quay.io/minio/minio` image can no longer be pulled
(#573), so both the stack's `minio` service and the scratch bucket ran
`bitnamilegacy/minio:2025.5.24`, the same MinIO release, through a drill-only
compose override (`user: "0"`, `entrypoint: ["minio"]`). A new host restoring
from the off-host copy hits #573 before it reaches the MinIO step.

Not drilled: a cloud provider's S3 (the scratch bucket was MinIO), a bucket
lifecycle rule, and the orchestrator's behaviour on the restored database.
