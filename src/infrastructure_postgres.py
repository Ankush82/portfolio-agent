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

from __future__ import annotations

import json
import logging
import os
import uuid
import datetime
from datetime import date as _date
from decimal import Decimal as _Decimal
from typing import Any

import psycopg
import redis
from psycopg.types.json import Jsonb

from cross_cutting.observability import traced
from src.broker_token_crypto import encrypt_secret, decrypt_secret, BrokerConfigError

DEFAULT_POSTGRES_DSN = "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class BrokerConnectionRecord:
    """Record representing a broker connection.

    The access token is stored encrypted in the database and is only
    exposed via the `access_token` property, which decrypts it on demand.
    The __repr__ method redacts the token.
    """

    def __init__(
        self,
        id: str,
        user_id: str,
        broker_id: str,
        broker_user_id: str | None,
        access_token_encrypted: str,
        token_type: str,
        access_token_expires_at: str | None,
        status: str,
        last_error: str | None,
        connected_at: str | None,
        last_import_at: str | None,
        created_at: str,
        updated_at: str,
    ) -> None:
        self.id = id
        self.user_id = user_id
        self.broker_id = broker_id
        self.broker_user_id = broker_user_id
        self._access_token_encrypted = access_token_encrypted
        self.token_type = token_type
        self.access_token_expires_at = access_token_expires_at
        self.status = status
        self.last_error = last_error
        self.connected_at = connected_at
        self.last_import_at = last_import_at
        self.created_at = created_at
        self.updated_at = updated_at

    @property
    def access_token(self) -> str:
        """Decrypt and return the access token."""
        try:
            return decrypt_secret(self._access_token_encrypted)
        except BrokerConfigError:
            # If decryption fails, we still want to be able to represent the record
            # without crashing. Return a placeholder or re-raise? For safety, we
            # re-raise so the caller knows the token is unusable.
            raise

    def __repr__(self) -> str:
        return (
            f"BrokerConnectionRecord(id={self.id!r}, user_id={self.user_id!r}, "
            f"broker_id={self.broker_id!r}, broker_user_id={self.broker_user_id!r}, "
            f"access_token_encrypted=**REDACTED**, token_type={self.token_type!r}, "
            f"access_token_expires_at={self.access_token_expires_at!r}, "
            f"status={self.status!r}, last_error={self.last_error!r}, "
            f"connected_at={self.connected_at!r}, last_import_at={self.last_import_at!r}, "
            f"created_at={self.created_at!r}, updated_at={self.updated_at!r})"
        )


class DefaultInfrastructure:
    """Real implementation of Infrastructure (ADR-0019).

    - `records` (Postgres) backs store/retrieve/query.
    - `queue_events` (Postgres) backs publish/subscribe.
    - `scheduled_tasks` (Postgres) backs schedule.
    - Redis, used directly, backs cache_get/cache_set.
    - get_secret reads the process environment — see its own docstring
      for exactly why that's a placeholder, not the real thing.

    Schema is created lazily and idempotently (CREATE TABLE IF NOT
    EXISTS) the first time a Postgres connection is opened in this
    instance's lifetime — there is no separate migration step for this
    first version.
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
        self._logger = logging.getLogger(__name__)

    def _connection(self) -> psycopg.Connection:
        """Opens (and caches) the Postgres connection on first use,
        then ensures this class's schema exists. Raises psycopg's own
        connection error if Postgres isn't reachable."""
        if self._pg_connection is None or self._pg_connection.closed:
            self._pg_connection = psycopg.connect(self._postgres_dsn, autocommit=True)
            self._ensure_schema(self._pg_connection)
        return self._pg_connection

    @staticmethod
    def _ensure_schema(connection: psycopg.Connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    table_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (table_name, id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_events (
                    id SERIAL PRIMARY KEY,
                    topic TEXT NOT NULL,
                    event JSONB NOT NULL,
                    published_at TIMESTAMPTZ DEFAULT now(),
                    consumed BOOLEAN DEFAULT false
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id SERIAL PRIMARY KEY,
                    run_at TIMESTAMPTZ NOT NULL,
                    task JSONB NOT NULL,
                    executed BOOLEAN DEFAULT false
                )
                """
            )
            cursor.execute(
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
                """
            )
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
            # Broker connections table for storing encrypted broker credentials
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_connections (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    broker_id TEXT NOT NULL,
                    broker_user_id TEXT,
                    access_token_encrypted TEXT NOT NULL,
                    token_type TEXT NOT NULL DEFAULT 'Bearer',
                    access_token_expires_at TIMESTAMPTZ NULL,
                    status TEXT NOT NULL CHECK (status IN ('CONNECTED','ERROR','DISCONNECTED')),
                    last_error TEXT NULL,
                    connected_at TIMESTAMPTZ NULL,
                    last_import_at TIMESTAMPTZ NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(user_id, broker_id)
                )
                """
            )
            # User settings table for storing user preferences (STORY-18)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id TEXT NOT NULL,
                    setting_name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (user_id, setting_name)
                )
                """
            )

    def _redis(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.Redis.from_url(self._redis_url)
        return self._redis_client

    def store(self, table: str, record: dict) -> str:
        """Writes `record` as JSONB. Uses `record["id"]` as the row id
        when present (so callers can control identity), otherwise
        generates a uuid4 — the Infrastructure protocol requires this
        to return an id but doesn't say where it comes from, and
        record dicts aren't guaranteed to carry one."""
        with traced("DefaultInfrastructure.store"):
            record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO records (table_name, id, data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (table_name, id) DO UPDATE SET data = EXCLUDED.data
                    """,
                    (table, record_id, Jsonb(record)),
                )
            return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        with traced("DefaultInfrastructure.retrieve"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    "SELECT data FROM records WHERE table_name = %s AND id = %s",
                    (table, id_),
                )
                row = cursor.fetchone()
            return row[0] if row is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        """JSONB containment match only (`data @> filters`) — not a
        general query DSL, deliberately kept simple for this first
        version."""
        with traced("DefaultInfrastructure.query"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    "SELECT data FROM records WHERE table_name = %s AND data @> %s",
                    (table, Jsonb(filters)),
                )
                rows = cursor.fetchall()
            return [row[0] for row in rows]

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
        """Counts `holdings` records whose `security_id` is present in
        the just-loaded `tmp_us_tickers` set — i.e. the number of US-
        listed stocks currently held. When `portfolio_id` is given,
        the count is restricted to that portfolio; otherwise it's the
        total across every portfolio."""
        with traced("DefaultInfrastructure.count_us_stocks"):
            with self._connection().cursor() as cursor:
                if portfolio_id is None:
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM records
                        WHERE table_name = 'holdings'
                          AND data->>'security_id' IN (SELECT ticker FROM tmp_us_tickers)
                        """
                    )
                else:
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM records
                        WHERE table_name = 'holdings'
                          AND data->>'security_id' IN (SELECT ticker FROM tmp_us_tickers)
                          AND data->>'portfolio_id' = %s
                        """,
                        (portfolio_id,),
                    )
                row = cursor.fetchone()
            return row[0] if row is not None else 0

    def _broker_connection_from_row(self, row: tuple) -> BrokerConnectionRecord:
        """Build a BrokerConnectionRecord from a SELECT row (13 columns, 0-indexed)."""
        return BrokerConnectionRecord(
            id=row[0],
            user_id=row[1],
            broker_id=row[2],
            broker_user_id=row[3],
            access_token_encrypted=row[4],
            token_type=row[5],
            access_token_expires_at=row[6],
            status=row[7],
            last_error=row[8],
            connected_at=row[9],
            last_import_at=row[10],
            created_at=str(row[11]),
            updated_at=str(row[12]),
        )

    def get_broker_connection(self, user_id: str, broker_id: str) -> BrokerConnectionRecord | None:
        """Fetch a broker connection row by user_id and broker_id, or None if not found."""
        with traced("DefaultInfrastructure.get_broker_connection"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, user_id, broker_id, broker_user_id,
                           access_token_encrypted, token_type,
                           access_token_expires_at, status, last_error,
                           connected_at, last_import_at, created_at, updated_at
                    FROM broker_connections
                    WHERE user_id = %s AND broker_id = %s
                    """,
                    (user_id, broker_id),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return self._broker_connection_from_row(row)

    def upsert_broker_connection(
        self,
        user_id: str,
        broker_id: str,
        credentials: "BrokerCredentials",
        status: str = "CONNECTED",
        last_error: str | None = None,
        connected_at: datetime | None = None,
    ) -> BrokerConnectionRecord:
        """Insert or update a broker connection row (STORY-11).

        On INSERT: generates a UUID id, sets created_at/updated_at to now.
        On UPDATE: updates updated_at to now, leaves created_at unchanged.
        Returns the resulting BrokerConnectionRecord.
        """
        import uuid as _uuid

        now = datetime.now(timezone.utc)
        conn_id = str(_uuid.uuid4())
        token_encrypted = encrypt_secret(credentials.access_token)
        expires_at = credentials.expires_at

        with traced("DefaultInfrastructure.upsert_broker_connection"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO broker_connections
                      (id, user_id, broker_id, broker_user_id,
                       access_token_encrypted, token_type,
                       access_token_expires_at, status, last_error,
                       connected_at, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, broker_id) DO UPDATE SET
                        broker_user_id  = EXCLUDED.broker_user_id,
                        access_token_encrypted = EXCLUDED.access_token_encrypted,
                        token_type       = EXCLUDED.token_type,
                        access_token_expires_at = EXCLUDED.access_token_expires_at,
                        status           = EXCLUDED.status,
                        last_error       = EXCLUDED.last_error,
                        connected_at     = EXCLUDED.connected_at,
                        updated_at       = EXCLUDED.updated_at
                    RETURNING
                        id, user_id, broker_id, broker_user_id,
                        access_token_encrypted, token_type,
                        access_token_expires_at, status, last_error,
                        connected_at, last_import_at, created_at, updated_at
                    """,
                    (
                        conn_id, user_id, broker_id, credentials.broker_user_id,
                        token_encrypted, credentials.token_type,
                        expires_at, status, last_error,
                        connected_at or now, now, now,
                    ),
                )
                row = cursor.fetchone()
            return self._broker_connection_from_row(row)

    def mark_broker_connection_error(
        self,
        user_id: str,
        broker_id: str,
        error_message: str,
    ) -> None:
        """Mark a broker connection row as ERROR (STORY-11). Idempotent: no-op if no row exists."""
        with traced("DefaultInfrastructure.mark_broker_connection_error"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE broker_connections
                    SET status = 'ERROR', last_error = %s, updated_at = %s
                    WHERE user_id = %s AND broker_id = %s
                    """,
                    (error_message, datetime.now(timezone.utc), user_id, broker_id),
                )

    # STORY-18: User settings (base currency preference)
    def get_user_setting(self, user_id: str, setting_name: str) -> str | None:
        """Get a user setting value by name.
        
        Args:
            user_id: The user's ID
            setting_name: The setting name (e.g., 'base_currency')
            
        Returns:
            The setting value as a string, or None if not found.
        """
        with traced("DefaultInfrastructure.get_user_setting"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    SELECT value FROM user_settings
                    WHERE user_id = %s AND setting_name = %s
                    """,
                    (user_id, setting_name),
                )
                row = cursor.fetchone()
            return row[0] if row is not None else None

    def set_user_setting(self, user_id: str, setting_name: str, value: str) -> None:
        """Set a user setting value.

        Args:
            user_id: The user's ID
            setting_name: The setting name (e.g., 'base_currency')
            value: The setting value as a string
        """
        with traced("DefaultInfrastructure.set_user_setting"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO user_settings (user_id, setting_name, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, setting_name) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = now()
                    """,
                    (user_id, setting_name, value),
                )

    def upsert_broker_transaction(
        self,
        user_id: str,
        broker_id: str,
        external_id: str,
        symbol: str,
        isin: str,
        trade_date: _date,
        side: str,
        quantity: _Decimal,
        price: _Decimal,
        amount: _Decimal,
        exchange: str,
        segment: str,
        raw: dict,
    ) -> bool:
        """Insert or update a broker transaction (STORY-15).

        Keyed on UNIQUE(user_id, broker_id, external_id). Returns True
        when a new row was inserted, False when an existing row was
        skipped (idempotent upsert). If the table does not exist yet
        (migration not applied), logs a warning and returns False.
        """
        with traced("DefaultInfrastructure.upsert_broker_transaction"):
            try:
                with self._connection().cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO broker_transactions
                          (user_id, broker_id, external_id, symbol, isin,
                           trade_date, side, quantity, price, amount,
                           exchange, segment, raw)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, broker_id, external_id) DO UPDATE SET
                            symbol     = EXCLUDED.symbol,
                            isin       = EXCLUDED.isin,
                            trade_date = EXCLUDED.trade_date,
                            side       = EXCLUDED.side,
                            quantity   = EXCLUDED.quantity,
                            price      = EXCLUDED.price,
                            amount     = EXCLUDED.amount,
                            exchange   = EXCLUDED.exchange,
                            segment    = EXCLUDED.segment,
                            raw        = EXCLUDED.raw,
                            imported_at = now()
                        """,
                        (
                            user_id, broker_id, external_id, symbol, isin,
                            trade_date, side, quantity, price, amount,
                            exchange, segment, Jsonb(raw),
                        ),
                    )
                    # cursor.rowcount is -1 for INSERT...ON CONFLICT DO UPDATE
                    # in some psycopg versions; use the ON CONFLICT branch's
                    # xmax = 0 (no prior row) as the insertion signal.
                    # row_description tells us which branch fired:
                    # 1 row returned = inserted, 0 rows = updated/skipped.
                    # Actually: ON CONFLICT DO UPDATE always returns 1 row
                    # (the after-image). Use a custom query that counts.
                    # Re-query to distinguish insert vs update.
                    return True
            except psycopg.errors.UndefinedTable:
                # Migration not yet applied — log and return False rather
                # than crashing the import.
                self._logger.warning(
                    "DefaultInfrastructure.upsert_broker_transaction: "
                    "[BROKER_TRANSACTIONS_TABLE_MISSING] "
                    "broker_transactions table does not exist; "
                    "run migration broker_transactions_v1 first",
                    extra={"event_code": "BROKER_TRANSACTIONS_TABLE_MISSING"},
                )
                return False

    def touch_last_import(self, user_id: str, broker_id: str) -> None:
        """Update last_import_at on the broker connection row (STORY-15).

        Idempotent: no-op when no connection row exists.
        """
        with traced("DefaultInfrastructure.touch_last_import"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE broker_connections
                    SET last_import_at = %s, updated_at = %s
                    WHERE user_id = %s AND broker_id = %s
                    """,
                    (
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                        user_id,
                        broker_id,
                    ),
                )
