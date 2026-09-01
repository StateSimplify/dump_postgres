# dump_postgres

Interactive script that exports the **schema** of every database on a
PostgreSQL server that the login user is allowed to read — no table rows,
sequence values, or large-object contents are ever exported.

Because `pg_dump` fails outright when it cannot lock even one table, a
restricted (non-superuser) account normally cannot dump a whole database.
This script first inspects the catalogs to find every schema, table,
partition, and extension the user *cannot* access, writes a `pg_dump`
filter file that excludes exactly those objects (plus any descendants
that could not be restored without them), and then runs `pg_dump` so that
everything the user *does* have permission to export is included.

## Requirements

- Python 3.9 or newer
- `psycopg` 3 — `python -m pip install "psycopg[binary]"`
- `pg_dump` version 17 or newer (the script relies on `--filter`);
  `pg_dumpall` is optional and only needed for the roles/tablespaces
  export. The client tools may be newer than the server, but not older.

## Usage

```
python dump_postgres.py
```

The script prompts for the connection settings (host, port, user,
password, SSL options, timeouts, optional `SET ROLE`) and immediately
connects, so a wrong host or blocked port fails right away with a
diagnostic instead of after every prompt. Knowing the server version, it
then prompts for `pg_dump`/`pg_dumpall` paths (requiring a version new
enough for that server) and the output directory, and:

1. Lists every database, skipping templates (optional), databases that
   do not allow connections, and databases the user cannot `CONNECT` to.
2. Optionally exports cluster-wide roles (without password hashes) and
   tablespaces via `pg_dumpall --globals-only`.
3. For each remaining database: inspects permissions, writes a filter
   file, and runs `pg_dump --schema-only`.

## Output

```
<output directory>/
  00_globals.sql            roles and tablespaces (if requested)
  databases/<db>__oid_<n>.sql   one schema dump per database
  filters/<db>__oid_<n>.filter  the exclusions applied to that dump
  report.json               full machine-readable run report
```

`report.json` records, for every database, what was exported, what was
excluded and why (missing schema `USAGE`, no privilege that permits an
`ACCESS SHARE` lock, and so on), and the complete `pg_dump` output for
any failure. The script exits with `0` on success, `1` if any database
failed, and `2` on a fatal error.

## Timeouts

Two prompts keep the export from hanging indefinitely:

- **Connection timeout** (default 30 s) bounds every connection attempt.
  While connecting, the script retries in 15-second slices and prints a
  progress line each time, so a server that is not answering is visible
  immediately rather than after the full timeout. A server that is up
  and reachable answers in a second or two — if you see "still trying"
  lines at all, the problem is connectivity (wrong host or port, VPN not
  connected, firewall or cloud security group), and a larger timeout
  will not fix it. The failure message includes the addresses the host
  name resolves to and flags private addresses that need a VPN.
- **Table lock wait timeout** (default 120 s) is passed to `pg_dump` as
  `--lock-wait-timeout`, so a table locked by another session makes that
  database fail with a clear error instead of blocking forever. Set
  either to `0` to wait forever.

## Databases with very many tables

`pg_dump` locks every table it dumps inside one transaction, and the
server's lock table holds at most `max_locks_per_transaction ×
(max_connections + max_prepared_transactions)` locks across all
sessions. A database with more tables than that fails with
`out of shared memory ... increase max_locks_per_transaction`. The
script warns before attempting such a dump, and on this failure it
prints (and records in `report.json`) the exact
`max_locks_per_transaction` value to ask the administrator for
(changing it requires a server restart) — and then **automatically
falls back to a chunked export** that works within the existing limit:

- `<db>__part-000-prelude.sql` — everything that needs no table
  locks: the database, schemas, extensions, types, functions, and so
  on, with every relation excluded.
- `<db>__part-001.sql`, `part-002.sql`, … — groups of whole schemas,
  each group small enough for the server's lock table. Groups are
  ordered by cross-schema dependencies (foreign keys, views,
  partitions, owned sequences), and schemas in a dependency cycle
  stay in the same file, so the parts restore cleanly **in file name
  order** (`psql -f` each part in sequence; parts after the prelude
  must be run against the created database).

A single schema (or dependency cycle) with more tables than the lock
budget cannot be split further; such a chunk is attempted anyway with
a warning, since servers usually hold somewhat more locks than the
guaranteed minimum. Each chunk is a separate `pg_dump` snapshot, so
avoid running DDL on the server during a chunked export. Databases
whose dump fits in a single file are unaffected.
