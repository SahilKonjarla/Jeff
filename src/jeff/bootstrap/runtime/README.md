# Runtime composition

## PostgreSQL startup

PostgreSQL is the runtime default: database `jeff`, role `jeff_app`. A single-link
mode-`0600` `postgresql.json` can explicitly name that database or select an
isolated `jeff_cli_proof_<suffix>` database with a same-named role. The schema
must already exist. Normal startup refuses old SQLite state or sidecars; it
never rewrites them or falls back to SQLite. SQLite remains only for offline
imports and explicitly selected legacy test fixtures.

Psycopg and its binary wheel are pinned in `pyproject.toml` and the development lockfile.
Jeff connects to
`127.0.0.1:55432`; the operator supplies
`JEFF_POSTGRES_PASSWORD` for `jeff start`; the native launcher passes it through a
bounded inherited pipe, not child arguments or environment. Do not put passwords
in the marker, source, or shell history. If startup cannot connect, Jeff does not
open SQLite instead. Without guarded execution settings, the proof advertises
project operations and `execution.inspect.v1`: historical job and pytest results,
retained logs, and watch inbox registration/read/ACK.

A valid installation-private `settings.yaml` with a settings4 guarded development
backend connects this writer to the existing guarded controller and advertises
`execution.v1` and cancellation. An absent or unavailable guarded backend does not
enable launches. The isolated native CLI proof covered prelaunch cancellation
and one real pytest job through live logs, report persistence and notification
ACK. Automatic retention runs on the existing PostgreSQL writer; it defers on
suspect wall/monotonic clocks and does not retry an uncertain write.

### Startup and backup

Run PostgreSQL independently of Jeff. From a zsh terminal, read the password
without echoing it or putting it in shell history:

```zsh
read -rs 'JEFF_POSTGRES_PASSWORD?PostgreSQL password: '; printf '\n'
JEFF_POSTGRES_PASSWORD="$JEFF_POSTGRES_PASSWORD" jeff start --json
unset JEFF_POSTGRES_PASSWORD
```

Use `jeff stop` to stop the native daemon. A Docker volume is not a backup. When
explicitly taking a backup, use PostgreSQL’s `pg_dump`, not a copy of live files
from the volume. For example, choose a new file outside the repository in a
private directory and write a custom-format dump:

```bash
(umask 077; set -C; docker exec --user postgres jeff-postgres \
  pg_dump --format=custom --dbname=jeff > "$JEFF_BACKUP_FILE")
```

Keep dumps private; they can contain project history and retained output. Check
that `pg_dump` succeeded, then rehearse `pg_restore --exit-on-error
--single-transaction --no-owner --role=jeff_app` into a **new database** with the
role provisioned. Compare the restored project and job facts before adopting the
database. Never restore over the running `jeff` database. This doc is not evidence
of a completed restore for any particular backup.

`clients.py` supplies lazy client/control factories; `child.py` and `serving.py` establish authenticated daemon ownership/storage/handlers; `execution.py` assembles optional execution ports. Entry points alone select concrete adapters; help/syntax performs no runtime discovery or connection.

Guarded launches require explicit private settings4 development profiles, matching pinned CLI/config/fresh managed HOMEs and installation-owned guard verification. Default settings2 has no backend; settings3 remains readable but cannot authorize a new guarded launch. Current M04 uses wire14/DB12; use matching client and daemon builds. Bootstrap injects pytest report capture, live log tail attachment and the watch store into the existing execution coordinator. No image acquisition or job launch occurs during startup.

Initialization uses existing bounded workers and actual readiness/drain, without Docker queries, pulls or jobs at startup. Expected backend faults disable only optional execution; owner/storage faults remain fatal. No new executor, host fallback or automatic resource adoption.

See [composition ownership](../README.md), [fresh coordinator](../../application/execution_coordinator/README.md) and [bootstrap/guard checks](../../../../tests/integration/test_execution_composition/README.md).
