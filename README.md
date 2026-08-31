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
- **Table lock wait timeout** (default 120 s) is passed to `pg_dump` as
  `--lock-wait-timeout`, so a table locked by another session makes that
  database fail with a clear error instead of blocking forever. Set
  either to `0` to wait forever.
