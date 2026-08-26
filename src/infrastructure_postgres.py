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
from typing import Any

import psycopg
import redis
from psycopg.types.json import Json

from cross_cutting.observability import traced

DEFAULT_POSTGRES_DSN = "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


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
                    (table, record_id, Json(record)),
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
                    (table, Json(filters)),
                )
                rows = cursor.fetchall()
            return [row[0] for row in rows]

    def publish(self, topic: str, event: dict) -> None:
        with traced("DefaultInfrastructure.publish"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    "INSERT INTO queue_events (topic, event) VALUES (%s, %s)",
                    (topic, Json(event)),
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
                    (delay_seconds, Json(task)),
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
