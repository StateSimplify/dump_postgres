#!/usr/bin/env python3

import getpass
import graphlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
except ImportError:
    print(
        'Missing Psycopg. Install it with:\n'
        '  python -m pip install "psycopg[binary]"',
        file=sys.stderr,
    )
    sys.exit(2)


SSL_MODES = {
    "disable",
    "allow",
    "prefer",
    "require",
    "verify-ca",
    "verify-full",
}

# While connecting, retry in short slices so progress can be printed
# instead of sitting silently for the whole connection timeout.
CONNECT_ATTEMPT_SECONDS = 15


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def local_timestamp():
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def safe_filename(value):
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    result = result.strip("._")
    return result or "database"


def write_json(path, value):
    temporary = path.with_name(f".{path.name}.temporary")

    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    os.replace(temporary, path)


def server_major(version_number):
    if version_number >= 100000:
        return version_number // 10000

    return version_number // 100


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def ask_text(label, default=None, required=False):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        value = input(f"{label}{suffix}: ").strip()

        if not value and default is not None:
            value = str(default)

        if required and not value:
            print("A value is required.")
            continue

        return value


def ask_integer(label, default, minimum=None):
    while True:
        value = ask_text(label, str(default))

        try:
            number = int(value)
        except ValueError:
            print("Enter a valid integer.")
            continue

        if minimum is not None and number < minimum:
            print(f"Enter a value of {minimum} or more.")
            continue

        return number


def ask_yes_no(label, default=False):
    suffix = "Y/n" if default else "y/N"

    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()

        if not value:
            return default

        if value in {"y", "yes"}:
            return True

        if value in {"n", "no"}:
            return False

        print("Enter y or n.")


def ask_ssl_mode():
    while True:
        value = ask_text("SSL mode", "prefer").lower()

        if value in SSL_MODES:
            return value

        print("Choose one of:")
        print("  " + ", ".join(sorted(SSL_MODES)))


def read_executable_version(executable):
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{executable} did not respond to --version within "
            "30 seconds."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"Could not execute {executable}:\n{result.stderr}"
        )

    version_text = (result.stdout or result.stderr).strip()
    match = re.search(r"\b(\d+)(?:\.\d+)?\b", version_text)

    if not match:
        raise RuntimeError(
            f"Could not determine version from: {version_text}"
        )

    return int(match.group(1)), version_text


def ask_executable(label, default=None, minimum_major=None):
    while True:
        entered = ask_text(
            label,
            default=default,
            required=default is None,
        ).strip('"')

        executable = Path(entered).resolve()

        if not executable.is_file():
            print(f"File does not exist: {executable}")
            continue

        try:
            major, version_text = read_executable_version(executable)
        except (OSError, RuntimeError) as error:
            print(error)
            continue

        if minimum_major is not None and major < minimum_major:
            print(
                f"This script requires version {minimum_major} or newer; "
                f"selected executable is version {major}."
            )
            continue

        print(f"Using: {version_text}")
        return executable, major, version_text


def prompt_for_connection():
    print()
    print("PostgreSQL server-wide schema export")
    print("------------------------------------")
    print("No table rows, sequence values, or large-object contents")
    print("will be exported.")
    print()

    host = ask_text("Server host", "localhost", required=True)
    port = ask_integer("Server port", 5432, minimum=1)

    discovery_database = ask_text(
        "Database used to discover other databases",
        "postgres",
        required=True,
    )

    username = ask_text("Database username", required=True)

    password = getpass.getpass(
        "Database password (leave blank if none is required): "
    )

    sslmode = ask_ssl_mode()

    sslrootcert = ask_text(
        "SSL root certificate path, blank if not needed",
        "",
    )

    sslcert = ask_text(
        "SSL client certificate path, blank if not needed",
        "",
    )

    sslkey = ask_text(
        "SSL client key path, blank if not needed",
        "",
    )

    connect_timeout = ask_integer(
        "Connection timeout in seconds, 0 waits forever",
        30,
        minimum=0,
    )

    lock_timeout = ask_integer(
        "Table lock wait timeout in seconds, 0 waits forever",
        120,
        minimum=0,
    )

    role = ask_text(
        "Role to SET ROLE to, blank to use login user",
        "",
    ) or None

    return {
        "host": host,
        "port": port,
        "discovery_database": discovery_database,
        "username": username,
        "password": password,
        "sslmode": sslmode,
        "sslrootcert": sslrootcert,
        "sslcert": sslcert,
        "sslkey": sslkey,
        "connect_timeout": connect_timeout,
        "lock_timeout": lock_timeout,
        "role": role,
    }


def prompt_for_export_options(configuration, remote_major):
    # pg_dump needs --filter (new in 17) and must not be older than
    # the server; pg_dumpall only must not be older than the server.
    pg_dump_minimum = max(17, remote_major)

    print()
    pg_dump_path, pg_dump_major, pg_dump_version = ask_executable(
        "Full path to pg_dump",
        default=shutil.which("pg_dump"),
        minimum_major=pg_dump_minimum,
    )

    export_globals = ask_yes_no(
        "Try to export global roles and tablespaces?",
        default=True,
    )

    pg_dumpall_path = None
    pg_dumpall_major = None
    pg_dumpall_version = None

    if export_globals:
        executable_name = (
            "pg_dumpall.exe"
            if os.name == "nt"
            else "pg_dumpall"
        )

        default_pg_dumpall = str(
            pg_dump_path.parent / executable_name
        )

        (
            pg_dumpall_path,
            pg_dumpall_major,
            pg_dumpall_version,
        ) = ask_executable(
            "Full path to pg_dumpall",
            default=default_pg_dumpall,
            minimum_major=remote_major,
        )

    include_templates = ask_yes_no(
        "Include template databases?",
        default=False,
    )

    include_create_database = ask_yes_no(
        "Include CREATE DATABASE in each database dump?",
        default=True,
    )

    keep_ownership = ask_yes_no(
        "Keep ownership and GRANT/REVOKE statements?",
        default=True,
    )

    default_output = (
        f"postgres_server_schema_{local_timestamp()}"
    )

    output_directory = Path(
        ask_text(
            "Output directory",
            default_output,
            required=True,
        )
    ).resolve()

    configuration.update({
        "pg_dump_path": pg_dump_path,
        "pg_dump_major": pg_dump_major,
        "pg_dump_version": pg_dump_version,
        "export_globals": export_globals,
        "pg_dumpall_path": pg_dumpall_path,
        "pg_dumpall_major": pg_dumpall_major,
        "pg_dumpall_version": pg_dumpall_version,
        "include_templates": include_templates,
        "include_create_database": include_create_database,
        "keep_ownership": keep_ownership,
        "output_directory": output_directory,
    })


# ---------------------------------------------------------------------------
# Password and environment handling
# ---------------------------------------------------------------------------

def escape_password_file_value(value):
    return value.replace("\\", "\\\\").replace(":", "\\:")


def create_temporary_password_file(password):
    if "\n" in password or "\r" in password:
        raise RuntimeError(
            "Passwords containing line breaks are not supported."
        )

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="postgres-server-dump-",
        suffix=".pgpass",
        delete=False,
        newline="\n",
    )

    path = Path(handle.name)

    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

        if password:
            escaped = escape_password_file_value(password)
            handle.write(f"*:*:*:*:{escaped}\n")

        handle.close()
        return path

    except Exception:
        handle.close()
        path.unlink(missing_ok=True)
        raise


@contextmanager
def without_pg_environment():
    removed = {}

    for key in list(os.environ):
        if key.upper().startswith("PG"):
            removed[key] = os.environ.pop(key)

    try:
        yield
    finally:
        os.environ.update(removed)


def clean_child_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PG")
    }


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def make_database_conninfo(configuration, database, password_file):
    parameters = {
        "host": configuration["host"],
        "port": configuration["port"],
        "dbname": database,
        "user": configuration["username"],
        "sslmode": configuration["sslmode"],
        "connect_timeout": configuration["connect_timeout"],
        "passfile": str(password_file),
        "application_name": "server_schema_dump",
    }

    if configuration["sslrootcert"]:
        parameters["sslrootcert"] = configuration["sslrootcert"]

    if configuration["sslcert"]:
        parameters["sslcert"] = configuration["sslcert"]

    if configuration["sslkey"]:
        parameters["sslkey"] = configuration["sslkey"]

    return make_conninfo(**parameters)


def resolved_addresses(host):
    if not host or host.startswith("/"):
        return []

    try:
        results = socket.getaddrinfo(host, None)
    except OSError:
        return []

    addresses = []

    for result in results:
        address = result[4][0]

        if address not in addresses:
            addresses.append(address)

    return addresses


def is_private_address(address):
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    return parsed.is_private and not parsed.is_loopback


def connection_timeout_error(configuration, database, waited_seconds):
    host = configuration["host"]

    lines = [
        f"connection to {host} port {configuration['port']} "
        f"(database \"{database}\", user "
        f"\"{configuration['username']}\") timed out after "
        f"{waited_seconds} seconds.",
        "Nothing answered on that host and port, which usually "
        "means the traffic is being dropped before it reaches "
        "PostgreSQL. Check that the host and port are correct, "
        "that PostgreSQL is running and listening on that "
        "address, and that a firewall, VPN, or cloud security "
        "group is not blocking the connection.",
    ]

    addresses = resolved_addresses(host)

    if addresses:
        lines.append(
            "The host resolves to: " + ", ".join(addresses) + "."
        )

    if any(is_private_address(address) for address in addresses):
        lines.append(
            "That is a private network address, so the server "
            "can only be reached from inside its network (a VPN, "
            "a bastion or jump host, or a machine in the same "
            "VPC or LAN)."
        )

    return psycopg.OperationalError("\n".join(lines))


def connect_to_database(configuration, database, password_file):
    conninfo = make_database_conninfo(
        configuration,
        database,
        password_file,
    )

    total_timeout = configuration["connect_timeout"]

    deadline = (
        time.monotonic() + total_timeout
        if total_timeout
        else None
    )

    waited = 0

    while True:
        if deadline is None:
            attempt_timeout = CONNECT_ATTEMPT_SECONDS
        else:
            remaining = deadline - time.monotonic()
            attempt_timeout = min(
                CONNECT_ATTEMPT_SECONDS,
                max(2, int(remaining + 0.999)),
            )

        attempt_conninfo = make_conninfo(
            conninfo,
            connect_timeout=attempt_timeout,
        )

        try:
            with without_pg_environment():
                connection = psycopg.connect(attempt_conninfo)

            return connection, conninfo

        except psycopg.OperationalError as error:
            # Anything other than a timeout (connection refused,
            # unknown host, failed authentication, ...) is final.
            if "timeout" not in str(error).lower():
                raise

            waited += attempt_timeout

            if (
                deadline is not None
                and time.monotonic() >= deadline
            ):
                raise connection_timeout_error(
                    configuration,
                    database,
                    total_timeout,
                ) from error

            print(
                f"  Still trying to reach {configuration['host']} "
                f"port {configuration['port']} "
                f"({waited}s elapsed, Ctrl-C to give up)...",
                flush=True,
            )


def validate_role(connection, role):
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(role))
            )
    except psycopg.Error as error:
        raise RuntimeError(
            f'Cannot SET ROLE to "{role}": {error}'
        ) from error

    connection.rollback()


# ---------------------------------------------------------------------------
# Database discovery
# ---------------------------------------------------------------------------

def discover_databases(connection):
    server_information = connection.execute(
        """
        SELECT
            current_setting('server_version_num')::integer,
            current_setting('server_version'),
            session_user::text
        """
    ).fetchone()

    database_rows = connection.execute(
        """
        SELECT
            d.oid::bigint,
            d.datname,
            d.datistemplate,
            d.datallowconn,
            pg_catalog.has_database_privilege(
                session_user,
                d.oid,
                'CONNECT'
            ) AS can_connect
        FROM pg_catalog.pg_database AS d
        ORDER BY d.datname
        """
    ).fetchall()

    databases = []

    for (
        oid,
        name,
        is_template,
        allows_connections,
        can_connect,
    ) in database_rows:
        databases.append(
            {
                "oid": oid,
                "name": name,
                "is_template": bool(is_template),
                "allows_connections": bool(allows_connections),
                "can_connect": bool(can_connect),
            }
        )

    return {
        "server_version_number": server_information[0],
        "server_version": server_information[1],
        "session_user": server_information[2],
        "databases": databases,
    }


# ---------------------------------------------------------------------------
# Per-database permission inspection
# ---------------------------------------------------------------------------

def inspect_database_permissions(connection, role):
    with connection.cursor() as cursor:
        if role:
            cursor.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(role))
            )

        (
            server_version_number,
            server_version,
            session_user,
            effective_user,
            max_locks_per_transaction,
            max_connections,
            max_prepared_transactions,
        ) = cursor.execute(
            """
            SELECT
                current_setting('server_version_num')::integer,
                current_setting('server_version'),
                session_user::text,
                current_user::text,
                current_setting('max_locks_per_transaction')::integer,
                current_setting('max_connections')::integer,
                current_setting('max_prepared_transactions')::integer
            """
        ).fetchone()

        schema_rows = cursor.execute(
            """
            SELECT
                n.oid::bigint,
                n.nspname,
                pg_catalog.has_schema_privilege(
                    current_user,
                    n.oid,
                    'USAGE'
                )
            FROM pg_catalog.pg_namespace AS n
            WHERE n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_'
            ORDER BY n.nspname
            """
        ).fetchall()

        lock_privileges = "SELECT,INSERT,UPDATE,DELETE,TRUNCATE"

        if server_version_number >= 170000:
            lock_privileges += ",MAINTAIN"

        relation_rows = cursor.execute(
            """
            SELECT
                c.oid::bigint,
                n.oid::bigint,
                n.nspname,
                c.relname,
                c.relkind::text,
                ext.extname,

                pg_catalog.has_schema_privilege(
                    current_user,
                    n.oid,
                    'USAGE'
                ) AS schema_access,

                pg_catalog.has_table_privilege(
                    current_user,
                    c.oid,
                    %s
                ) AS lock_access

            FROM pg_catalog.pg_class AS c

            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace

            LEFT JOIN pg_catalog.pg_depend AS dep
              ON dep.classid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
             AND dep.objid = c.oid
             AND dep.objsubid = 0
             AND dep.refclassid =
                    'pg_catalog.pg_extension'::pg_catalog.regclass
             AND dep.deptype = 'e'

            LEFT JOIN pg_catalog.pg_extension AS ext
              ON ext.oid = dep.refobjid

            WHERE c.relkind IN ('r', 'p')
              AND c.relpersistence <> 't'
              AND n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_'

            ORDER BY n.nspname, c.relname
            """,
            (lock_privileges,),
        ).fetchall()

        inheritance_rows = cursor.execute(
            """
            SELECT
                inhrelid::bigint,
                inhparent::bigint
            FROM pg_catalog.pg_inherits
            """
        ).fetchall()

        extension_rows = cursor.execute(
            """
            SELECT
                e.extname,
                e.extnamespace::bigint
            FROM pg_catalog.pg_extension AS e
            """
        ).fetchall()

        sequence_rows = cursor.execute(
            """
            SELECT
                seq.oid::bigint,
                n.nspname,
                seq.relname,
                dep.refobjid::bigint
            FROM pg_catalog.pg_class AS seq

            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = seq.relnamespace

            JOIN pg_catalog.pg_depend AS dep
              ON dep.classid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
             AND dep.objid = seq.oid
             AND dep.refclassid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
             AND dep.deptype IN ('a', 'i')

            WHERE seq.relkind = 'S'
              AND n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_'
            """
        ).fetchall()

    excluded_schema_oids = {
        oid
        for oid, name, allowed in schema_rows
        if not allowed
    }

    excluded_schemas = {
        name
        for oid, name, allowed in schema_rows
        if not allowed
    }

    relations = {}
    inaccessible_relations = []

    for row in relation_rows:
        (
            oid,
            schema_oid,
            schema_name,
            relation_name,
            relation_kind,
            extension_name,
            schema_access,
            lock_access,
        ) = row

        relation = {
            "oid": oid,
            "schema_oid": schema_oid,
            "schema": schema_name,
            "name": relation_name,
            "kind": relation_kind,
            "extension": extension_name,
            "schema_access": bool(schema_access),
            "lock_access": bool(lock_access),
        }

        relations[oid] = relation

        reasons = []

        if not schema_access:
            reasons.append("no USAGE privilege on schema")

        if not lock_access:
            reasons.append(
                "no privilege permitting an ACCESS SHARE lock"
            )

        if reasons:
            inaccessible_relations.append(
                {
                    **relation,
                    "reasons": reasons,
                }
            )

    children = defaultdict(set)

    for child_oid, parent_oid in inheritance_rows:
        children[parent_oid].add(child_oid)

    excluded_extensions = {
        extension_name
        for extension_name, schema_oid in extension_rows
        if schema_oid in excluded_schema_oids
    }

    # Exclude each inaccessible relation together with its own
    # descendants (partitions and inheritance children cannot be
    # restored without their parent), but keep accessible parents
    # and sibling partitions in the dump.
    excluded_tables = set()
    excluded_table_trees = set()

    for relation in inaccessible_relations:
        relation_oid = relation["oid"]

        if relation["extension"]:
            excluded_extensions.add(relation["extension"])

        if relation["kind"] == "p" or children.get(relation_oid):
            excluded_table_trees.add(relation_oid)
        else:
            excluded_tables.add(relation_oid)

    # Everything the dump will exclude: excluded tables, the
    # descendants of excluded trees, and tables in excluded schemas
    # or extensions.
    excluded_oids = set(excluded_tables)
    pending = list(excluded_table_trees)

    while pending:
        oid = pending.pop()

        if oid in excluded_oids:
            continue

        excluded_oids.add(oid)
        pending.extend(children.get(oid, ()))

    for oid, relation in relations.items():
        if (
            relation["schema"] in excluded_schemas
            or relation["extension"] in excluded_extensions
        ):
            excluded_oids.add(oid)

    # Sequences owned by excluded tables cannot be restored (their
    # OWNED BY clause references a missing table), so exclude them
    # alongside their table.
    excluded_sequences = sorted(
        (schema_name, sequence_name, sequence_oid)
        for sequence_oid, schema_name, sequence_name, owner_oid
        in sequence_rows
        if owner_oid in excluded_oids
    )

    included_table_count = len(relations) - len(excluded_oids)

    return {
        "server_version_number": server_version_number,
        "server_version": server_version,
        "session_user": session_user,
        "effective_user": effective_user,
        "schema_count": len(schema_rows),
        "relation_count": len(relation_rows),
        "included_table_count": included_table_count,
        "max_locks_per_transaction": max_locks_per_transaction,
        "max_connections": max_connections,
        "max_prepared_transactions": max_prepared_transactions,
        "relations": relations,
        "excluded_schemas": excluded_schemas,
        "excluded_extensions": excluded_extensions,
        "excluded_tables": excluded_tables,
        "excluded_table_trees": excluded_table_trees,
        "excluded_relation_oids": excluded_oids,
        "excluded_sequences": excluded_sequences,
        "inaccessible_relations": inaccessible_relations,
    }


# ---------------------------------------------------------------------------
# pg_dump filter generation
# ---------------------------------------------------------------------------

def quote_filter_identifier(name):
    if "\r" in name:
        raise RuntimeError(
            "Identifiers containing carriage returns are unsupported."
        )

    escaped = name.replace("\\", "\\\\")
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace('"', '""')

    return f'"{escaped}"'


def qualified_filter_pattern(schema_name, object_name):
    return (
        f"{quote_filter_identifier(schema_name)}."
        f"{quote_filter_identifier(object_name)}"
    )


def relation_sort_key(oid, relations):
    relation = relations[oid]
    return relation["schema"], relation["name"]


def exclusion_filter_entries(inspection):
    entries = []
    relations = inspection["relations"]

    for extension in sorted(inspection["excluded_extensions"]):
        entries.append(
            f"exclude extension {quote_filter_identifier(extension)}"
        )

    for schema_name in sorted(inspection["excluded_schemas"]):
        entries.append(
            f"exclude schema {quote_filter_identifier(schema_name)}"
        )

    for oid in sorted(
        inspection["excluded_table_trees"],
        key=lambda value: relation_sort_key(value, relations),
    ):
        relation = relations[oid]

        entries.append(
            "exclude table_and_children "
            + qualified_filter_pattern(
                relation["schema"],
                relation["name"],
            )
        )

    for oid in sorted(
        inspection["excluded_tables"],
        key=lambda value: relation_sort_key(value, relations),
    ):
        relation = relations[oid]

        entries.append(
            "exclude table "
            + qualified_filter_pattern(
                relation["schema"],
                relation["name"],
            )
        )

    for schema_name, sequence_name, _ in inspection[
        "excluded_sequences"
    ]:
        entries.append(
            "exclude table "
            + qualified_filter_pattern(schema_name, sequence_name)
        )

    return entries


def write_filter_file(path, entries):
    contents = [
        "# Automatically generated pg_dump filter",
        f"# Generated at {utc_now()}",
        "",
        *entries,
        "",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(contents), encoding="utf-8")


def create_filter_file(path, inspection):
    entries = exclusion_filter_entries(inspection)
    write_filter_file(path, entries)
    return entries


# ---------------------------------------------------------------------------
# Native tool execution
# ---------------------------------------------------------------------------

def run_tool_to_file(executable, arguments, final_output):
    final_output.parent.mkdir(parents=True, exist_ok=True)

    temporary_output = final_output.with_name(
        f".{final_output.name}.partial-{os.getpid()}"
    )

    temporary_output.unlink(missing_ok=True)

    command = [
        str(executable),
        *arguments,
        f"--file={temporary_output}",
    ]

    print(
        f"Running {Path(executable).name}, "
        f"writing {final_output.name}...",
        flush=True,
    )

    process = subprocess.Popen(
        command,
        env=clean_child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )

    captured = []

    try:
        with process.stdout:
            for line in process.stdout:
                captured.append(line)
                print(line, end="", file=sys.stderr, flush=True)

        return_code = process.wait()

    except BaseException:
        process.kill()
        process.wait()
        temporary_output.unlink(missing_ok=True)
        raise

    if return_code == 0 and temporary_output.exists():
        os.replace(temporary_output, final_output)
        succeeded = True
    else:
        temporary_output.unlink(missing_ok=True)
        succeeded = False

    return {
        "succeeded": succeeded,
        "return_code": return_code,
        "messages": "".join(captured),
        "output": str(final_output) if succeeded else None,
    }


def common_pg_dumpall_arguments(configuration, conninfo):
    arguments = [
        f"--dbname={conninfo}",
        "--no-password",
    ]

    if configuration["role"]:
        arguments.append(f"--role={configuration['role']}")

    if not configuration["keep_ownership"]:
        arguments.extend([
            "--no-owner",
            "--no-privileges",
        ])

    return arguments


def export_globals(configuration, password_file, output_directory):
    conninfo = make_database_conninfo(
        configuration,
        configuration["discovery_database"],
        password_file,
    )

    base_arguments = common_pg_dumpall_arguments(
        configuration,
        conninfo,
    )

    print()
    print("Exporting cluster-wide roles and tablespaces...")

    result = run_tool_to_file(
        configuration["pg_dumpall_path"],
        [
            *base_arguments,
            "--globals-only",
            "--no-role-passwords",
        ],
        output_directory / "00_globals.sql",
    )

    if result["succeeded"]:
        return {
            "status": "completed",
            "combined": result,
        }

    print()
    print(
        "Combined global export failed. Trying roles and "
        "tablespaces separately..."
    )

    roles_result = run_tool_to_file(
        configuration["pg_dumpall_path"],
        [
            *base_arguments,
            "--roles-only",
            "--no-role-passwords",
        ],
        output_directory / "00_roles.sql",
    )

    tablespaces_result = run_tool_to_file(
        configuration["pg_dumpall_path"],
        [
            *base_arguments,
            "--tablespaces-only",
        ],
        output_directory / "01_tablespaces.sql",
    )

    return {
        "status": "partial",
        "combined_attempt": result,
        "roles": roles_result,
        "tablespaces": tablespaces_result,
    }


# ---------------------------------------------------------------------------
# Individual database export
# ---------------------------------------------------------------------------

def lock_capacity(inspection):
    return (
        inspection["max_locks_per_transaction"]
        * (
            inspection["max_connections"]
            + inspection["max_prepared_transactions"]
        )
    )


def lock_exhaustion_advice(inspection):
    included = inspection["included_table_count"]
    sessions = (
        inspection["max_connections"]
        + inspection["max_prepared_transactions"]
    )

    # 25% headroom over the exact requirement, since the lock table is
    # shared with every other running session.
    suggested = max(
        128,
        math.ceil(included * 1.25 / sessions),
    )

    return (
        f"This database has about {included} dumpable tables, while "
        "the server is only guaranteed to hold "
        f"{lock_capacity(inspection)} table locks across all "
        "sessions (max_locks_per_transaction="
        f"{inspection['max_locks_per_transaction']} x "
        f"(max_connections={inspection['max_connections']} + "
        "max_prepared_transactions="
        f"{inspection['max_prepared_transactions']})), and pg_dump "
        "must lock every table it dumps inside a single "
        'transaction. If the dump fails with "out of shared '
        'memory", no client-side option can work around it: ask the '
        "server administrator to raise max_locks_per_transaction to "
        f"at least {suggested} (for example: ALTER SYSTEM SET "
        f"max_locks_per_transaction = {suggested};), restart "
        "PostgreSQL, and run this export again."
    )


# ---------------------------------------------------------------------------
# Chunked export for databases with more tables than the lock table
# ---------------------------------------------------------------------------

def collect_chunk_metadata(connection, role):
    with connection.cursor() as cursor:
        if role:
            cursor.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(role))
            )

        relation_rows = cursor.execute(
            """
            SELECT
                n.nspname,
                c.oid::bigint,
                c.relkind::text
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND c.relpersistence <> 't'
              AND n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_'
            """
        ).fetchall()

        edge_rows = cursor.execute(
            """
            -- foreign keys into another schema
            SELECT DISTINCT nd.nspname, nr.nspname
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS cd ON cd.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS nd
              ON nd.oid = cd.relnamespace
            JOIN pg_catalog.pg_class AS cr ON cr.oid = con.confrelid
            JOIN pg_catalog.pg_namespace AS nr
              ON nr.oid = cr.relnamespace
            WHERE con.contype = 'f'
              AND nd.oid <> nr.oid

            UNION

            -- inheritance children and partitions of another schema
            SELECT DISTINCT nd.nspname, nr.nspname
            FROM pg_catalog.pg_inherits AS i
            JOIN pg_catalog.pg_class AS cd ON cd.oid = i.inhrelid
            JOIN pg_catalog.pg_namespace AS nd
              ON nd.oid = cd.relnamespace
            JOIN pg_catalog.pg_class AS cr ON cr.oid = i.inhparent
            JOIN pg_catalog.pg_namespace AS nr
              ON nr.oid = cr.relnamespace
            WHERE nd.oid <> nr.oid

            UNION

            -- views over another schema's relations
            SELECT DISTINCT nd.nspname, nr.nspname
            FROM pg_catalog.pg_depend AS d
            JOIN pg_catalog.pg_rewrite AS rw ON rw.oid = d.objid
            JOIN pg_catalog.pg_class AS cd ON cd.oid = rw.ev_class
            JOIN pg_catalog.pg_namespace AS nd
              ON nd.oid = cd.relnamespace
            JOIN pg_catalog.pg_class AS cr ON cr.oid = d.refobjid
            JOIN pg_catalog.pg_namespace AS nr
              ON nr.oid = cr.relnamespace
            WHERE d.classid =
                    'pg_catalog.pg_rewrite'::pg_catalog.regclass
              AND d.refclassid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
              AND nd.oid <> nr.oid

            UNION

            -- sequences owned by another schema's table
            SELECT DISTINCT ns.nspname, nt.nspname
            FROM pg_catalog.pg_depend AS d
            JOIN pg_catalog.pg_class AS s
              ON s.oid = d.objid AND s.relkind = 'S'
            JOIN pg_catalog.pg_namespace AS ns
              ON ns.oid = s.relnamespace
            JOIN pg_catalog.pg_class AS t ON t.oid = d.refobjid
            JOIN pg_catalog.pg_namespace AS nt
              ON nt.oid = t.relnamespace
            WHERE d.classid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
              AND d.refclassid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
              AND d.deptype IN ('a', 'i')
              AND ns.oid <> nt.oid

            UNION

            -- column defaults using another schema's relations
            SELECT DISTINCT nd.nspname, nr.nspname
            FROM pg_catalog.pg_depend AS d
            JOIN pg_catalog.pg_attrdef AS ad ON ad.oid = d.objid
            JOIN pg_catalog.pg_class AS cd ON cd.oid = ad.adrelid
            JOIN pg_catalog.pg_namespace AS nd
              ON nd.oid = cd.relnamespace
            JOIN pg_catalog.pg_class AS cr ON cr.oid = d.refobjid
            JOIN pg_catalog.pg_namespace AS nr
              ON nr.oid = cr.relnamespace
            WHERE d.classid =
                    'pg_catalog.pg_attrdef'::pg_catalog.regclass
              AND d.refclassid =
                    'pg_catalog.pg_class'::pg_catalog.regclass
              AND nd.oid <> nr.oid
            """
        ).fetchall()

    return relation_rows, edge_rows


def order_schema_units(schema_names, edges):
    """Return schemas grouped into units, ordered so that a unit only
    depends on earlier units. Schemas in a dependency cycle share a
    unit."""

    owner = {name: name for name in schema_names}
    members = {name: [name] for name in schema_names}

    while True:
        graph = {unit: set() for unit in members}

        for dependent, required in edges:
            dependent_unit = owner.get(dependent)
            required_unit = owner.get(required)

            if (
                dependent_unit is None
                or required_unit is None
                or dependent_unit == required_unit
            ):
                continue

            graph[dependent_unit].add(required_unit)

        try:
            ordered = list(
                graphlib.TopologicalSorter(graph).static_order()
            )
        except graphlib.CycleError as error:
            cycle = list(dict.fromkeys(error.args[1]))
            target = cycle[0]

            for unit in cycle[1:]:
                for name in members[unit]:
                    owner[name] = target

                members[target].extend(members.pop(unit))

            continue

        return [sorted(members[unit]) for unit in ordered]


def pack_schema_groups(ordered_units, lockable_counts, budget):
    groups = []
    current = []
    current_size = 0

    for unit in ordered_units:
        unit_size = sum(
            lockable_counts.get(name, 0) for name in unit
        )

        if current and current_size + unit_size > budget:
            groups.append((current, current_size))
            current, current_size = [], 0

        current = current + unit
        current_size += unit_size

        if current_size > budget:
            groups.append((current, current_size))
            current, current_size = [], 0

    if current:
        groups.append((current, current_size))

    return groups


def chunked_export(
    configuration,
    password_file,
    output_directory,
    database,
    inspection,
    base_arguments,
    file_stem,
):
    print()
    print(
        "Falling back to a chunked export: first a file with "
        "everything except relations, then dependency-ordered "
        "groups of schemas that each fit in the server's lock "
        "table. Restore the resulting files in file name order.",
        flush=True,
    )

    try:
        connection, _ = connect_to_database(
            configuration,
            database["name"],
            password_file,
        )

        try:
            relation_rows, edge_rows = collect_chunk_metadata(
                connection,
                configuration["role"],
            )

            connection.rollback()
        finally:
            connection.close()

    except psycopg.Error as error:
        print(
            f"Could not analyze schemas for chunking: {error}",
            file=sys.stderr,
        )

        return {
            "succeeded": False,
            "error": str(error),
            "parts": [],
        }

    excluded_oids = inspection["excluded_relation_oids"]

    excluded_sequence_oids = {
        oid for _, _, oid in inspection["excluded_sequences"]
    }

    survivor_counts = defaultdict(int)
    lockable_counts = defaultdict(int)

    for schema_name, oid, relkind in relation_rows:
        if schema_name in inspection["excluded_schemas"]:
            continue

        if oid in excluded_oids or oid in excluded_sequence_oids:
            continue

        survivor_counts[schema_name] += 1

        if relkind in ("r", "p"):
            lockable_counts[schema_name] += 1

    edges = [
        (dependent, required)
        for dependent, required in edge_rows
        if dependent in survivor_counts and required in survivor_counts
    ]

    budget = max(64, lock_capacity(inspection) // 2)

    groups = pack_schema_groups(
        order_schema_units(sorted(survivor_counts), edges),
        lockable_counts,
        budget,
    )

    exclusions = exclusion_filter_entries(inspection)
    parts = []
    succeeded = True

    def run_part(part_name, filter_entries, include_create):
        filter_path = (
            output_directory
            / "filters"
            / f"{file_stem}__{part_name}.filter"
        )

        write_filter_file(filter_path, filter_entries)

        arguments = [*base_arguments, f"--filter={filter_path}"]

        if include_create:
            arguments.append("--create")

        return run_tool_to_file(
            configuration["pg_dump_path"],
            arguments,
            output_directory
            / "databases"
            / f"{file_stem}__{part_name}.sql",
        )

    prelude_entries = [
        f"exclude extension {quote_filter_identifier(name)}"
        for name in sorted(inspection["excluded_extensions"])
    ] + [
        f"exclude schema {quote_filter_identifier(name)}"
        for name in sorted(inspection["excluded_schemas"])
    ] + [
        "exclude table *.*",
    ]

    result = run_part(
        "part-000-prelude",
        prelude_entries,
        configuration["include_create_database"],
    )

    succeeded = result["succeeded"]

    parts.append({
        "part": "part-000-prelude",
        "schemas": None,
        "table_count": 0,
        "succeeded": result["succeeded"],
        "return_code": result["return_code"],
        "output": result["output"],
        "messages": None if result["succeeded"] else result["messages"],
    })

    for index, (schemas, table_count) in enumerate(groups, start=1):
        part_name = f"part-{index:03d}"

        if table_count > budget:
            print(
                f"Warning: {part_name} needs {table_count} table "
                f"locks, more than the {budget}-lock budget, "
                "because a single schema or dependency cycle "
                "cannot be split further. Attempting it anyway...",
                flush=True,
            )

        includes = [
            f"include table {quote_filter_identifier(name)}.*"
            for name in schemas
        ]

        result = run_part(
            part_name,
            [*includes, *exclusions],
            False,
        )

        succeeded = succeeded and result["succeeded"]

        parts.append({
            "part": part_name,
            "schemas": schemas,
            "table_count": table_count,
            "succeeded": result["succeeded"],
            "return_code": result["return_code"],
            "output": result["output"],
            "messages": (
                None if result["succeeded"] else result["messages"]
            ),
        })

    return {
        "succeeded": succeeded,
        "lock_budget": budget,
        "parts": parts,
    }


def database_file_stem(database):
    return (
        f"{safe_filename(database['name'])}"
        f"__oid_{database['oid']}"
    )


def inspection_report(inspection):
    relations = inspection["relations"]

    return {
        "effective_user": inspection["effective_user"],
        "discovered_schema_count": inspection["schema_count"],
        "discovered_table_count": inspection["relation_count"],
        "dumpable_table_count": inspection["included_table_count"],
        "excluded_schemas": sorted(
            inspection["excluded_schemas"]
        ),
        "excluded_extensions": sorted(
            inspection["excluded_extensions"]
        ),
        "excluded_table_trees": [
            {
                "schema": relations[oid]["schema"],
                "name": relations[oid]["name"],
            }
            for oid in sorted(
                inspection["excluded_table_trees"],
                key=lambda value: relation_sort_key(
                    value,
                    relations,
                ),
            )
        ],
        "excluded_tables": [
            {
                "schema": relations[oid]["schema"],
                "name": relations[oid]["name"],
            }
            for oid in sorted(
                inspection["excluded_tables"],
                key=lambda value: relation_sort_key(
                    value,
                    relations,
                ),
            )
        ],
        "excluded_sequences": [
            {"schema": schema_name, "name": sequence_name}
            for schema_name, sequence_name, _ in inspection[
                "excluded_sequences"
            ]
        ],
        "permission_failures": inspection[
            "inaccessible_relations"
        ],
    }


def export_database(
    configuration,
    password_file,
    output_directory,
    database,
):
    database_name = database["name"]
    file_stem = database_file_stem(database)

    print()
    print("=" * 70)
    print(f"Processing database: {database_name}")
    print("=" * 70)
    print("Inspecting permissions...", flush=True)

    try:
        connection, conninfo = connect_to_database(
            configuration,
            database_name,
            password_file,
        )
    except psycopg.Error as error:
        print(f"Could not connect: {error}", file=sys.stderr)

        return {
            **database,
            "status": "connection_failed",
            "error": str(error),
        }

    try:
        inspection = inspect_database_permissions(
            connection,
            configuration["role"],
        )

        connection.rollback()

    except psycopg.Error as error:
        print(
            f"Permission inspection failed: {error}",
            file=sys.stderr,
        )

        return {
            **database,
            "status": "inspection_failed",
            "error": str(error),
        }

    finally:
        connection.close()

    filter_path = (
        output_directory
        / "filters"
        / f"{file_stem}.filter"
    )

    filter_path.parent.mkdir(parents=True, exist_ok=True)

    filter_entries = create_filter_file(
        filter_path,
        inspection,
    )

    output_path = (
        output_directory
        / "databases"
        / f"{file_stem}.sql"
    )

    base_arguments = [
        f"--dbname={conninfo}",
        "--no-password",
        "--format=plain",
        "--schema-only",
    ]

    if configuration["lock_timeout"]:
        base_arguments.append(
            f"--lock-wait-timeout={configuration['lock_timeout'] * 1000}"
        )

    if configuration["role"]:
        base_arguments.append(f"--role={configuration['role']}")

    if not configuration["keep_ownership"]:
        base_arguments.extend([
            "--no-owner",
            "--no-privileges",
        ])

    arguments = list(base_arguments)

    if filter_entries:
        arguments.append(f"--filter={filter_path}")

    if configuration["include_create_database"]:
        arguments.append("--create")

    if inspection["included_table_count"] > lock_capacity(inspection):
        print()
        print(
            "Warning: this dump may exhaust the server's lock "
            "table. " + lock_exhaustion_advice(inspection),
            flush=True,
        )
        print("Attempting the dump anyway...", flush=True)
        print()

    result = run_tool_to_file(
        configuration["pg_dump_path"],
        arguments,
        output_path,
    )

    status = "completed" if result["succeeded"] else "dump_failed"

    messages = result["messages"] or ""

    lock_exhaustion = (
        not result["succeeded"]
        and "out of shared memory" in messages
        and "max_locks_per_transaction" in messages
    )

    failure_hint = None
    chunk_report = None

    if lock_exhaustion:
        failure_hint = lock_exhaustion_advice(inspection)
        print()
        print(failure_hint, flush=True)

        chunk_report = chunked_export(
            configuration,
            password_file,
            output_directory,
            database,
            inspection,
            base_arguments,
            file_stem,
        )

        if chunk_report["succeeded"]:
            status = "completed_chunked"

    print(f"Database status: {status}")

    return {
        **database,
        "status": status,
        "output": result["output"],
        "filter_file": str(filter_path),
        "pg_dump_return_code": result["return_code"],
        "pg_dump_messages": result["messages"],
        "failure_hint": failure_hint,
        "chunked": chunk_report,
        "inspection": inspection_report(inspection),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    password_file = None

    try:
        configuration = prompt_for_connection()

        password_file = create_temporary_password_file(
            configuration["password"]
        )

        addresses = resolved_addresses(configuration["host"])

        address_note = (
            f" ({', '.join(addresses)})" if addresses else ""
        )

        print()
        print(
            f"Connecting to {configuration['host']} port "
            f"{configuration['port']}{address_note} "
            "and discovering databases...",
            flush=True,
        )

        discovery_connection, _ = connect_to_database(
            configuration,
            configuration["discovery_database"],
            password_file,
        )

        try:
            if configuration["role"]:
                validate_role(
                    discovery_connection,
                    configuration["role"],
                )

            discovery = discover_databases(discovery_connection)
            discovery_connection.rollback()
        finally:
            discovery_connection.close()

        remote_major = server_major(
            discovery["server_version_number"]
        )

        print(
            f"Connected. Server version: {discovery['server_version']}",
            flush=True,
        )

        prompt_for_export_options(configuration, remote_major)
        output_directory = configuration["output_directory"]

        if output_directory.exists() and any(output_directory.iterdir()):
            if not ask_yes_no(
                f"{output_directory} is not empty. Continue?",
                default=False,
            ):
                print("Cancelled.")
                return 0

        output_directory.mkdir(parents=True, exist_ok=True)

        all_databases = discovery["databases"]
        eligible_databases = []
        initially_skipped = []

        for database in all_databases:
            reason = None

            if (
                database["is_template"]
                and not configuration["include_templates"]
            ):
                reason = "template database excluded by user"

            elif not database["allows_connections"]:
                reason = "database does not allow connections"

            elif not database["can_connect"]:
                reason = "login user has no CONNECT privilege"

            if reason:
                initially_skipped.append(
                    {
                        **database,
                        "status": "skipped",
                        "reason": reason,
                    }
                )
            else:
                eligible_databases.append(database)

        print()
        print(f"Server version:          {discovery['server_version']}")
        print(f"Databases discovered:    {len(all_databases)}")
        print(f"Databases to dump:       {len(eligible_databases)}")
        print(f"Databases initially skipped: {len(initially_skipped)}")
        print(f"Output directory:        {output_directory}")

        if not ask_yes_no("Start the server-wide export?", True):
            print("Cancelled.")
            return 0

        report_path = output_directory / "report.json"

        report = {
            "started_at": utc_now(),
            "status": "running",
            "server": {
                "host": configuration["host"],
                "port": configuration["port"],
                "version": discovery["server_version"],
                "login_user": configuration["username"],
                "discovery_database": configuration[
                    "discovery_database"
                ],
            },
            "pg_dump_version": configuration["pg_dump_version"],
            "pg_dumpall_version": configuration[
                "pg_dumpall_version"
            ],
            "globals": {
                "status": "not_requested"
            },
            "databases": list(initially_skipped),
        }

        write_json(report_path, report)

        if configuration["export_globals"]:
            report["globals"] = export_globals(
                configuration,
                password_file,
                output_directory,
            )

            write_json(report_path, report)

        for database in eligible_databases:
            database_result = export_database(
                configuration,
                password_file,
                output_directory,
                database,
            )

            report["databases"].append(database_result)
            write_json(report_path, report)

        completed = sum(
            1
            for database in report["databases"]
            if database["status"] in {
                "completed",
                "completed_chunked",
            }
        )

        skipped = sum(
            1
            for database in report["databases"]
            if database["status"] == "skipped"
        )

        failed = sum(
            1
            for database in report["databases"]
            if database["status"] in {
                "connection_failed",
                "inspection_failed",
                "dump_failed",
            }
        )

        report["completed_at"] = utc_now()
        report["summary"] = {
            "databases_completed": completed,
            "databases_skipped": skipped,
            "databases_failed": failed,
        }

        if failed:
            report["status"] = "completed_with_errors"
        else:
            report["status"] = "completed"

        write_json(report_path, report)

        print()
        print("=" * 70)
        print("Server-wide export finished")
        print("=" * 70)
        print(f"Databases completed: {completed}")
        print(f"Databases skipped:   {skipped}")
        print(f"Databases failed:    {failed}")
        print(f"Output directory:    {output_directory}")
        print(f"Detailed report:     {report_path}")

        if failed:
            return 1

        return 0

    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    except EOFError:
        print("\nInput ended unexpectedly.", file=sys.stderr)
        return 2

    except (OSError, RuntimeError, psycopg.Error) as error:
        print(f"\nError: {error}", file=sys.stderr)
        return 2

    finally:
        if password_file:
            try:
                password_file.unlink(missing_ok=True)
            except OSError:
                print(
                    "Warning: could not delete temporary password file: "
                    f"{password_file}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main())
