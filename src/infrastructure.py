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

from typing import Any, Protocol

from cross_cutting.observability import traced


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
