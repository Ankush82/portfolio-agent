"""System Infrastructure (component 18) — the shared interface every
other component talks through. Never a specific store directly.

Design: Phase 0 Cross-Cutting Design, fig. 18.1
Decision: ADR-0019 — unified, managed stack (Postgres + Redis + object
storage + cloud secret manager), built to scale from day one, behind
this interface. No component may import a database driver, cache
client, or storage SDK directly; the interface is what stays stable
if Postgres-as-queue is later replaced (e.g. by Kafka/Redpanda).

No concrete implementation exists yet (Mem0-vs-Supermemory for Memory,
ADR-0010, is also unresolved and may bypass parts of this for component
06 specifically — see that component's file).
"""

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol

from cross_cutting.observability import traced

if TYPE_CHECKING:
    from cross_cutting.observability import AuditManager, AuditReader

AuditManagerLike = "AuditManager"
AuditReaderLike = "AuditReader"


class Infrastructure(Protocol):
    """Concrete implementation: src/infrastructure_postgres.py (not yet
    built). Every method below is a boundary crossing — see
    cross_cutting/security.py for the gate every call here should pass
    through first."""

    def store(self, table: str, record: dict) -> str:
        """Write a record. Returns its id."""
        ...

    def retrieve(self, table: str, id_: str) -> dict | None:
        """Read a record by id."""
        ...

    def query(self, table: str, filters: dict) -> list[dict]:
        """Read records matching filters."""
        ...

    def publish(self, topic: str, event: dict) -> None:
        """Publish an event onto the (Postgres-backed, for now) queue."""
        ...

    def subscribe(self, topic: str, handler: Any) -> None:
        """Register a handler for a topic."""
        ...

    def schedule(self, delay_seconds: float, task: dict) -> str:
        """Schedule deferred work. Returns a schedule id."""
        ...

    def cache_get(self, key: str) -> Any | None:
        ...

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        ...

    def get_secret(self, name: str) -> str:
        """Reads from the cloud provider's secret manager. Never read
        an environment variable or config file directly for anything
        credential-shaped (ADR-0019)."""
        ...

    def get_audit_manager(self) -> "AuditManagerLike":
        """Returns an AuditManager instance for recording audit events."""
        ...

    def get_audit_reader(self) -> "AuditReaderLike":
        """Returns an AuditReader instance for querying audit events.

        Obtain via: ``infrastructure.get_audit_reader()`` where ``infrastructure``
        is the dependency-injected Infrastructure instance passed to components
        via constructor injection.
        """
        ...

    def transaction(self) -> "AbstractContextManager[None]":
        """Real, atomic transaction boundary (STORY-SYNC-06). Any
        store/retrieve/query/delete or broker-specific write call made
        against THIS SAME Infrastructure instance while inside the
        `with` block commits together on clean exit, or rolls back
        together if the block raises -- callers that need "all these
        writes succeed or none do" (e.g. a portfolio sync touching many
        holdings/transactions) wrap them in one `with
        infrastructure.transaction():` block rather than trusting each
        individual write's own default auto-commit behavior."""
        ...

    def upsert_broker_transaction(
        self,
        user_id: str,
        broker_id: str,
        external_id: str,
        symbol: str,
        isin: str,
        trade_date: "date",
        side: str,
        quantity: "Decimal",
        price: "Decimal",
        amount: "Decimal",
        exchange: str,
        segment: str,
        raw: dict,
    ) -> bool:
        """Insert or update a broker transaction, keyed on (user_id, broker_id, external_id).

        Returns True if a new row was inserted, False if an existing row was
        updated (i.e. the external_id already existed). The UNIQUE constraint
        on (user_id, broker_id, external_id) enforces idempotency — re-running
        the import skips existing rows.
        """
        ...

    def touch_last_import(self, user_id: str, broker_id: str) -> None:
        """Update last_import_at to now on the broker connection row."""
        ...

    def get_audit_reader(self) -> "AuditReader":
        """Return an AuditReader bound to this infrastructure's store
        (the queue_events table, in the Postgres implementation). The
        reader exposes read-only access to the audit trail of every
        event ever published via publish() — so callers (tests,
        admin/debug tools) can assert what actually happened without
        needing direct DB access. Returns a fresh AuditReader per call;
        readers are stateless wrappers around the connection."""
        ...


class AuditReader(Protocol):
    """Read-only view of the infrastructure's event audit trail.

    Backed in the Postgres implementation by the `queue_events` table
    that publish() writes to — every record published onto a topic is
    one audit row, identified by (id, topic, event, published_at,
    consumed). This Protocol deliberately has no write methods: the
    audit trail is append-only from the Infrastructure side, and
    readers must never be able to mutate it."""

    def query(
        self,
        topic: str | None = None,
        since: "datetime | None" = None,
        until: "datetime | None" = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return audit events newest-first. Each returned dict has
        keys: id (int), topic (str), event (dict, the original
        payload), published_at (ISO-8601 string), consumed (bool).

        - `topic`: when set, only events on that topic; when None,
          events on every topic.
        - `since`/`until`: inclusive lower/upper bound on published_at;
          either or both may be None.
        - `limit`: cap on returned rows (default 100, hard cap 10000
          to keep a single call from pulling a giant window).
        """
        ...


class StubAuditReader:
    """Structural AuditReader: returns an empty list from every query.
    Same role for AuditReader that StubInfrastructure plays for
    Infrastructure — a no-op shape so test code can wire one in
    without standing up a real Postgres."""

    def query(
        self,
        topic: str | None = None,
        since: "datetime | None" = None,
        until: "datetime | None" = None,
        limit: int = 100,
    ) -> list[dict]:
        with traced("StubAuditReader.query"):
            return []


class StubInfrastructure:
    """Structural implementation of Infrastructure. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def store(self, table: str, record: dict) -> str:
        with traced("StubInfrastructure.store"):
            return ""

    def retrieve(self, table: str, id_: str) -> dict | None:
        with traced("StubInfrastructure.retrieve"):
            return None

    def query(self, table: str, filters: dict) -> list[dict]:
        with traced("StubInfrastructure.query"):
            return []

    def publish(self, topic: str, event: dict) -> None:
        with traced("StubInfrastructure.publish"):
            return None

    def subscribe(self, topic: str, handler: Any) -> None:
        with traced("StubInfrastructure.subscribe"):
            return None

    def schedule(self, delay_seconds: float, task: dict) -> str:
        with traced("StubInfrastructure.schedule"):
            return ""

    def cache_get(self, key: str) -> Any | None:
        with traced("StubInfrastructure.cache_get"):
            return None

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with traced("StubInfrastructure.cache_set"):
            return None

    def get_secret(self, name: str) -> str:
        with traced("StubInfrastructure.get_secret"):
            return ""

    def get_audit_manager(self) -> "AuditManagerLike":
        with traced("StubInfrastructure.get_audit_manager"):
            from cross_cutting.observability import StubAuditManager
            return StubAuditManager()

    def transaction(self) -> AbstractContextManager[None]:
        # A real, structural no-op -- the stub has nothing to commit or
        # roll back (every write above is itself a no-op), but must
        # still support `with infrastructure.transaction():` the same
        # way DefaultInfrastructure's real one does, so test code
        # written against the Protocol works against either.
        from contextlib import nullcontext

        return nullcontext()

    def get_audit_reader(self) -> AuditReader:
        # Real bug in the pre-merge audit-logging branch, found live:
        # this returned a real DefaultAuditReader (reads from an actual
        # Postgres connection this stub never opens), not a stub --
        # defeating the entire point of StubInfrastructure. Every other
        # method here is a real, structural no-op; this one now matches.
        with traced("StubInfrastructure.get_audit_reader"):
            return StubAuditReader()
