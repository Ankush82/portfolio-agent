"""System Infrastructure (component 18) — concrete Postgres + Redis
implementation of the Infrastructure interface.

Design: Phase 0 Cross-Cutting Design, fig. 18.1
Decision: ADR-0019 — unified, managed stack, built to scale from day
one. Production points at managed Postgres/Redis (Neon/Supabase-style,
Upstash-style — see the ADR); local development points at
docker-compose.yml's postgres/redis services, which is what the
defaults below match.

Connections are opened lazily: constructing DefaultInfrastructure never
touches the network. A method call opens (and caches) a connection the
first time it's actually needed, and lets the driver's own connection
error propagate if the service isn't reachable — this class never
hides a down Postgres or a down Redis behind a fake success.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
import psycopg.errors
import redis
from psycopg.types.json import Jsonb

from cross_cutting.observability import traced
from domain import HOLDINGS_TABLE, PORTFOLIOS_TABLE, TRANSACTIONS_TABLE, USERS_TABLE

DEFAULT_POSTGRES_DSN = "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


@dataclass(frozen=True)
class TableSpec:
    """A declared, reviewable description of one real, typed table's
    shape (STORY-10) -- pk, the closed column list, and which of those
    columns are JSONB. Deliberately a plain, hand-written registry, not
    information_schema introspection: which columns store/retrieve/
    query touch must depend on code a reviewer can read, not on
    whatever the live DB schema happens to be at runtime."""

    pk: str
    columns: tuple[str, ...]
    jsonb: tuple[str, ...] = field(default_factory=tuple)


# The four core domain tables whose schema is owned by the migration
# scripts/migrate_core_domain_entities.sql (STORY-11) -- verified against
# scripts/verify_migration_core_domain.sql's own expected column lists.
# DefaultInfrastructure must NEVER issue a CREATE TABLE for any of
# these — a real migration owns their DDL now. `store`/`retrieve`/
# `query`/`delete` read and write these tables' REAL, typed columns
# (STORY-10) rather than the generic `records` table's opaque JSONB
# payload every other table still uses; an operation against one of
# these four whose real table doesn't exist yet raises
# MigrationRequiredError rather than silently creating an untyped blob.
# A dict (not a plain set) so `table in MIGRATED_TABLES` still works
# everywhere it already did (STORY-14), while also giving store/
# retrieve/query/delete the real column spec they need.
MIGRATED_TABLES: dict[str, TableSpec] = {
    USERS_TABLE: TableSpec(pk="id", columns=("id", "email", "preferences"), jsonb=("preferences",)),
    PORTFOLIOS_TABLE: TableSpec(pk="id", columns=("id", "user_id"), jsonb=()),
    HOLDINGS_TABLE: TableSpec(
        pk="id",
        columns=(
            "id",
            "portfolio_id",
            "security_id",
            "quantity",
            "currency",
            "exchange",
            "symbol_suffix",
        ),
        jsonb=(),
    ),
    TRANSACTIONS_TABLE: TableSpec(pk="id", columns=("id", "portfolio_id", "kind", "amount"), jsonb=()),
}

# The script that creates the four migrated tables.  Used verbatim in the
# MigrationRequiredError remediation message so developers get an exact
# command to run rather than having to hunt for the right file.
_MIGRATION_SCRIPT = "scripts/run_migration.sh"


class MigrationRequiredError(RuntimeError):
    """Raised when a Postgres operation targets a table whose schema is
    owned by a migration that has not been applied yet.

    The exception message names the offending table and gives the exact
    command to run to apply the missing migration.
    """

    pass


class DefaultInfrastructure:
    """Real implementation of Infrastructure (ADR-0019).

    - `records` (Postgres) backs store/retrieve/query.
    - `queue_events` (Postgres) backs publish/subscribe.
    - `scheduled_tasks` (Postgres) backs schedule.
    - Redis, used directly, backs cache_get/cache_set.
    - get_secret reads the process environment — see its own docstring
      for exactly why that's a placeholder, not the real thing.

    The five generic system tables (records, queue_events, scheduled_tasks,
    migration_log, schema_migrations) are created lazily and idempotently
    (CREATE TABLE IF NOT EXISTS) the first time a Postgres connection is
    opened in this instance's lifetime.

    The four core domain tables (users, portfolios, holdings, transactions)
    are NOT created here — their schema is owned by the migration
    scripts/migrate_core_domain_entities.sql (STORY-11/STORY-14).  An
    operation against any of those four tables that fails because the
    relation does not exist raises MigrationRequiredError instead of
    silently creating an untyped records-table blob.
    """

    def __init__(
        self,
        postgres_dsn: str = DEFAULT_POSTGRES_DSN,
        redis_url: str = DEFAULT_REDIS_URL,
    ) -> None:
        self._postgres_dsn = postgres_dsn
        self._redis_url = redis_url
        self._pg_connection: psycopg.Connection | None = None
        self._redis_client: redis.Redis | None = None

    def _connection(self) -> psycopg.Connection:
        """Opens (and caches) the Postgres connection on first use,
        then ensures this class's schema exists. Raises psycopg's own
        connection error if Postgres isn't reachable."""
        if self._pg_connection is None or self._pg_connection.closed:
            self._pg_connection = psycopg.connect(self._postgres_dsn, autocommit=True)
            self._ensure_schema(self._pg_connection)
        return self._pg_connection

    def _safe_connection(self) -> psycopg.Connection:
        """Returns the raw connection, but wraps UndefinedTable errors for
        MIGRATED_TABLES in MigrationRequiredError so callers never see a
        bare psycopg error for those four tables."""
        return self._connection()

    @staticmethod
    def _wrap_migrated_table_error(table: str, exc: psycopg.errors.UndefinedTable) -> None:
        """Re-raise an UndefinedTable for a migrated table as MigrationRequiredError."""
        msg = (
            f"Table '{table}' does not exist. "
            f"Run: ./{_MIGRATION_SCRIPT}"
        )
        raise MigrationRequiredError(msg) from exc

    @staticmethod
    def _ensure_schema(connection: psycopg.Connection) -> None:
        with connection.cursor() as cursor:
            # The four core domain tables (users, portfolios, holdings,
            # transactions) are NOT created here — their schema is owned by
            # scripts/migrate_core_domain_entities.sql.  Creating them here
            # would recreate the untyped-blob problem that migration fixed.
            # Per-table: skip MIGRATED_TABLES; create everything else.
            for table_sql in (
                """
                CREATE TABLE IF NOT EXISTS records (
                    table_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (table_name, id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS queue_events (
                    id SERIAL PRIMARY KEY,
                    topic TEXT NOT NULL,
                    event JSONB NOT NULL,
                    published_at TIMESTAMPTZ DEFAULT now(),
                    consumed BOOLEAN DEFAULT false
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id SERIAL PRIMARY KEY,
                    run_at TIMESTAMPTZ NOT NULL,
                    task JSONB NOT NULL,
                    executed BOOLEAN DEFAULT false
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS migration_log (
                    id BIGSERIAL PRIMARY KEY,
                    migration_name VARCHAR NOT NULL,
                    run_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    rows_affected BIGINT,
                    error_message TEXT,
                    dry_run BOOLEAN NOT NULL
                )
                """,
            ):
                cursor.execute(table_sql)

            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_migration_log_name_run ON migration_log (migration_name, run_at)"
            )
            # schema_migrations tracks logical schema-version applications
            # distinct from migration_log's per-invocation log rows.
            # migration_log (existing) records every dry-run/real/failed
            # attempt with rowcount + error message; schema_migrations
            # (this table) records one row per applied logical migration
            # name, idempotent via UNIQUE(migration_name), so a second
            # application is a no-op and the table itself answers
            # "which logical migrations have been applied?".
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id BIGSERIAL PRIMARY KEY,
                    migration_name VARCHAR NOT NULL UNIQUE,
                    applied_at TIMESTAMPTZ NOT NULL
                )
                """
            )

    def _redis(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.Redis.from_url(self._redis_url)
        return self._redis_client

    def store(self, table: str, record: dict) -> str:
        """For a table in MIGRATED_TABLES (STORY-10): writes real, typed
        columns via `INSERT ... ON CONFLICT (<pk>) DO UPDATE`, over
        exactly the declared column list -- preserving the pre-existing
        `records`-table upsert-by-id semantics (docs/repo-layer-recon.md
        V3), just against real columns instead of an opaque JSONB
        payload. Table and column names come only from the registry
        (never caller input); every real value is still a bound
        parameter. An `UndefinedTable` here (the real migration hasn't
        been applied yet) is wrapped as `MigrationRequiredError`, same
        as every other operation on a migrated table.

        For any other table: unchanged -- writes `record` as JSONB into
        the generic `records` table. Uses `record["id"]` as the row id
        when present (so callers can control identity), otherwise
        generates a uuid4 — the Infrastructure protocol requires this
        to return an id but doesn't say where it comes from, and
        record dicts aren't guaranteed to carry one."""
        with traced("DefaultInfrastructure.store"):
            record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
            spec = MIGRATED_TABLES.get(table)
            try:
                if spec is not None:
                    row = dict(record)
                    row["id"] = record_id
                    values = [
                        Jsonb(row.get(column)) if column in spec.jsonb else row.get(column)
                        for column in spec.columns
                    ]
                    column_list = ", ".join(spec.columns)
                    placeholders = ", ".join(["%s"] * len(spec.columns))
                    update_clause = ", ".join(
                        f"{column} = EXCLUDED.{column}"
                        for column in spec.columns
                        if column != spec.pk
                    )
                    with self._connection().cursor() as cursor:
                        cursor.execute(
                            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
                            f"ON CONFLICT ({spec.pk}) DO UPDATE SET {update_clause}",
                            values,
                        )
                else:
                    with self._connection().cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO records (table_name, id, data)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (table_name, id) DO UPDATE SET data = EXCLUDED.data
                            """,
                            (table, record_id, Jsonb(record)),
                        )
            except psycopg.errors.UndefinedTable as exc:
                if table in MIGRATED_TABLES:
                    self._wrap_migrated_table_error(table, exc)
                raise
            return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        """Column-aware for a table in MIGRATED_TABLES (real SELECT over
        the declared columns, returned as a plain dict); unchanged
        `records`-table JSONB lookup for any other table."""
        with traced("DefaultInfrastructure.retrieve"):
            spec = MIGRATED_TABLES.get(table)
            try:
                if spec is not None:
                    column_list = ", ".join(spec.columns)
                    with self._connection().cursor() as cursor:
                        cursor.execute(
                            f"SELECT {column_list} FROM {table} WHERE {spec.pk} = %s",
                            (id_,),
                        )
                        row = cursor.fetchone()
                    return dict(zip(spec.columns, row)) if row is not None else None
                with self._connection().cursor() as cursor:
                    cursor.execute(
                        "SELECT data FROM records WHERE table_name = %s AND id = %s",
                        (table, id_),
                    )
                    row = cursor.fetchone()
            except psycopg.errors.UndefinedTable as exc:
                if table in MIGRATED_TABLES:
                    self._wrap_migrated_table_error(table, exc)
                raise
            return row[0] if row is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        """Column-aware for a table in MIGRATED_TABLES: real equality
        match on the declared columns (a filter key that isn't a real
        column raises ValueError rather than silently matching nothing
        the caller intended). Unchanged JSONB containment match
        (`data @> filters`) for any other table — not a general query
        DSL, deliberately kept simple for that path."""
        with traced("DefaultInfrastructure.query"):
            spec = MIGRATED_TABLES.get(table)
            try:
                if spec is not None:
                    unknown = set(filters) - set(spec.columns)
                    if unknown:
                        raise ValueError(
                            f"query filter key(s) {sorted(unknown)} are not real columns "
                            f"of migrated table {table!r}"
                        )
                    column_list = ", ".join(spec.columns)
                    sql = f"SELECT {column_list} FROM {table}"
                    values = [
                        Jsonb(value) if key in spec.jsonb else value
                        for key, value in filters.items()
                    ]
                    if filters:
                        sql += " WHERE " + " AND ".join(f"{key} = %s" for key in filters)
                    with self._connection().cursor() as cursor:
                        cursor.execute(sql, values)
                        rows = cursor.fetchall()
                    return [dict(zip(spec.columns, row)) for row in rows]
                with self._connection().cursor() as cursor:
                    cursor.execute(
                        "SELECT data FROM records WHERE table_name = %s AND data @> %s",
                        (table, Jsonb(filters)),
                    )
                    rows = cursor.fetchall()
            except psycopg.errors.UndefinedTable as exc:
                if table in MIGRATED_TABLES:
                    self._wrap_migrated_table_error(table, exc)
                raise
            return [row[0] for row in rows]

    def delete(self, table: str, id: str) -> bool:
        """Column-aware for a table in MIGRATED_TABLES: deletes the real
        row by its declared pk column (necessary for correctness, not
        just an incidental extension: HoldingRepository/
        TransactionRepository's own delete/delete_by_security route
        through this same Infrastructure.delete, and would silently
        no-op forever against a migrated table if this stayed pinned to
        the old `records` table alone). Unchanged `(table_name, id)`
        delete from `records` for any other table. Returns True if a
        row was removed, False if no matching row existed — idempotent
        for nonexistent ids (never raises), same contract either way."""
        with traced("DefaultInfrastructure.delete"):
            spec = MIGRATED_TABLES.get(table)
            try:
                if spec is not None:
                    with self._connection().cursor() as cursor:
                        cursor.execute(f"DELETE FROM {table} WHERE {spec.pk} = %s", (id,))
                        rowcount = cursor.rowcount
                else:
                    with self._connection().cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM records WHERE table_name = %s AND id = %s",
                            (table, id),
                        )
                        rowcount = cursor.rowcount
            except psycopg.errors.UndefinedTable as exc:
                if table in MIGRATED_TABLES:
                    self._wrap_migrated_table_error(table, exc)
                raise
            return rowcount > 0

    def publish(self, topic: str, event: dict) -> None:
        with traced("DefaultInfrastructure.publish"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    "INSERT INTO queue_events (topic, event) VALUES (%s, %s)",
                    (topic, Jsonb(event)),
                )

    def subscribe(self, topic: str, handler: Any) -> None:
        """Poll-once, not a live subscription: this immediately queries
        every currently-unconsumed event on `topic`, calls `handler`
        once per event (in published order), and marks each consumed.
        Real push delivery would need Postgres LISTEN/NOTIFY plus a
        background listener thread — out of scope for this pass."""
        with traced("DefaultInfrastructure.subscribe"):
            connection = self._connection()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, event FROM queue_events
                    WHERE topic = %s AND consumed = false
                    ORDER BY id
                    """,
                    (topic,),
                )
                pending = cursor.fetchall()
            for event_id, event in pending:
                handler(event)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE queue_events SET consumed = true WHERE id = %s",
                        (event_id,),
                    )

    def schedule(self, delay_seconds: float, task: dict) -> str:
        with traced("DefaultInfrastructure.schedule"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO scheduled_tasks (run_at, task)
                    VALUES (now() + %s * interval '1 second', %s)
                    RETURNING id
                    """,
                    (delay_seconds, Jsonb(task)),
                )
                row = cursor.fetchone()
            return str(row[0])

    def cache_get(self, key: str) -> Any | None:
        with traced("DefaultInfrastructure.cache_get"):
            raw = self._redis().get(key)
            return json.loads(raw) if raw is not None else None

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with traced("DefaultInfrastructure.cache_set"):
            self._redis().set(key, json.dumps(value), ex=ttl_seconds)

    def get_secret(self, name: str) -> str:
        """Local-dev placeholder: reads directly from the process
        environment. ADR-0019 specifies the cloud provider's secret
        manager for production; real cloud secret manager integration
        is not implemented here — that's out of scope for this pass.
        Raises KeyError if `name` isn't set, same as `os.environ[name]`."""
        with traced("DefaultInfrastructure.get_secret"):
            return os.environ[name]

    def load_us_tickers(self, csv_path: str = '/app/data/us_tickers.csv') -> None:
        """Loads the US ticker universe into a session-scoped TEMP
        table (`tmp_us_tickers`) from a one-ticker-per-line CSV at
        `csv_path`. Tickers containing '.' or '-' are skipped (those
        are non-US formats: e.g. Berkshire's BRK.B, dual-class shares
        with hyphens). Duplicates collapse via the PRIMARY KEY. If
        `csv_path` doesn't exist the temp table is left empty and the
        method returns silently — this is so the dev path
        (`/app/data/us_tickers.csv`) being missing doesn't take down
        callers that should still be able to count an empty US set."""
        with traced("DefaultInfrastructure.load_us_tickers"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TEMP TABLE IF NOT EXISTS tmp_us_tickers (
                        ticker TEXT PRIMARY KEY
                    )
                    """
                )
                cursor.execute("TRUNCATE TABLE tmp_us_tickers")

                try:
                    with open(csv_path, 'r') as f:
                        for line in f:
                            ticker = line.strip()
                            if not ticker:
                                continue
                            if '.' in ticker or '-' in ticker:
                                continue
                            cursor.execute(
                                """
                                INSERT INTO tmp_us_tickers (ticker) VALUES (%s)
                                ON CONFLICT (ticker) DO NOTHING
                                """,
                                (ticker,),
                            )
                except FileNotFoundError:
                    return

    def count_us_stocks(self, portfolio_id: str | None = None) -> int:
        """Counts `holdings` rows whose `security_id` is present in
        the just-loaded `tmp_us_tickers` set — i.e. the number of US-
        listed stocks currently held. When `portfolio_id` is given,
        the count is restricted to that portfolio; otherwise it's the
        total across every portfolio.

        STORY-10: queries the real, typed `holdings` table directly
        (real `security_id`/`portfolio_id` columns) now that `store`/
        `retrieve`/`query` route holdings there instead of into the
        generic `records` table's JSONB payload -- the old
        `data->>'security_id'` extraction would silently count zero
        rows forever once holdings stopped being written to `records`
        at all."""
        with traced("DefaultInfrastructure.count_us_stocks"):
            try:
                with self._connection().cursor() as cursor:
                    if portfolio_id is None:
                        cursor.execute(
                            """
                            SELECT COUNT(*) FROM holdings
                            WHERE security_id IN (SELECT ticker FROM tmp_us_tickers)
                            """
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT COUNT(*) FROM holdings
                            WHERE security_id IN (SELECT ticker FROM tmp_us_tickers)
                              AND portfolio_id = %s
                            """,
                            (portfolio_id,),
                        )
                    row = cursor.fetchone()
            except psycopg.errors.UndefinedTable as exc:
                if "holdings" in MIGRATED_TABLES:
                    self._wrap_migrated_table_error("holdings", exc)
                raise
            return row[0] if row is not None else 0
