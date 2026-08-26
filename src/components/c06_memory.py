"""Memory (component 06) — what the system has learned, not what
merely exists to be retrieved.

Design: Memory Design, fig. 1 (write path) and fig. 2 (read path and
working set)
Decisions: ADR-0005 (active working-set management), ADR-0006 (linked
network structure), ADR-0007 (quarantine at write), ADR-0008
(structural partition, user vs. shared)
Technology: Mem0 or Supermemory — still open (ADR-0010); the split
between User Memory Store and Shared Memory Store below assumes
whichever product is chosen supports it structurally.
"""

from dataclasses import dataclass, field
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class MemoryCandidate:
    content: dict
    source: str
    provenance_verified: bool


@dataclass
class Memory:
    id: str
    content: dict
    scope: str  # "user" | "shared"
    links: list[str] = field(default_factory=list)
    confidence: float = 0.0
    quarantined: bool = False


class MemoryEvaluator(Protocol):
    def should_become_memory(self, experience: dict) -> bool:
        """Fig. 1's first gate."""
        ...


class StubMemoryEvaluator:
    """Structural implementation of MemoryEvaluator. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def should_become_memory(self, experience: dict) -> bool:
        with traced("StubMemoryEvaluator.should_become_memory"):
            return True


class EntityLinker(Protocol):
    def link(self, candidate: MemoryCandidate, existing: list[Memory]) -> list[str]:
        """A-MEM-style: find related memories, create links, update
        the network (ADR-0006). Runs before the quarantine check."""
        ...


class StubEntityLinker:
    """Structural implementation of EntityLinker. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def link(self, candidate: MemoryCandidate, existing: list[Memory]) -> list[str]:
        with traced("StubEntityLinker.link"):
            return []


class QuarantineGate(Protocol):
    def check_provenance(self, candidate: MemoryCandidate) -> bool:
        """Fig. 1's 'provenance verified?' gate (ADR-0007)."""
        ...

    def quarantine(self, candidate: MemoryCandidate) -> None:
        """Released once corroborated, expires if never corroborated —
        not stored as usable memory either way, in the meantime."""
        ...


class StubQuarantineGate:
    """Structural implementation of QuarantineGate. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def check_provenance(self, candidate: MemoryCandidate) -> bool:
        with traced("StubQuarantineGate.check_provenance"):
            return True

    def quarantine(self, candidate: MemoryCandidate) -> None:
        with traced("StubQuarantineGate.quarantine"):
            return None


class ScopeRouter(Protocol):
    def route(self, memory: Memory) -> str:
        """Fig. 1's 'scope?' branch → User Memory Store or Shared
        Memory Store, physically separate (ADR-0008)."""
        ...


class StubScopeRouter:
    """Structural implementation of ScopeRouter. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def route(self, memory: Memory) -> str:
        with traced("StubScopeRouter.route"):
            return ""


class MemoryManager(Protocol):
    """The only component that actively decides what it's holding onto
    (ADR-0005) — everything else here revises what's in the working set,
    nothing else does."""

    def is_in_working_set(self, query: dict) -> Memory | None:
        ...

    def retrieve(self, query: dict, scope: str) -> list[Memory]:
        """From User Memory Store and/or Shared Memory Store, per
        scope. Fig. 2's slow path."""
        ...

    def admit(self, memory: Memory) -> None:
        ...

    def evict(self) -> Memory:
        """Least relevant / stalest, per active-management policy —
        not just age (fig. 2)."""
        ...


class StubMemoryManager:
    """Structural implementation of MemoryManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def is_in_working_set(self, query: dict) -> Memory | None:
        with traced("StubMemoryManager.is_in_working_set"):
            return None

    def retrieve(self, query: dict, scope: str) -> list[Memory]:
        with traced("StubMemoryManager.retrieve"):
            return []

    def admit(self, memory: Memory) -> None:
        with traced("StubMemoryManager.admit"):
            return None

    def evict(self) -> Memory:
        with traced("StubMemoryManager.evict"):
            return Memory(id="stub-id", content={}, scope="stub")


class MemoryConsolidator(Protocol):
    def check_staleness(self, memory: Memory) -> bool:
        ...

    def update_or_invalidate(self, memory: Memory) -> None:
        """Runs periodically, independent of any single query. Feeds
        corrections back into EntityLinker.link() (fig. 2 → fig. 1)."""
        ...


class StubMemoryConsolidator:
    """Structural implementation of MemoryConsolidator. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def check_staleness(self, memory: Memory) -> bool:
        with traced("StubMemoryConsolidator.check_staleness"):
            return True

    def update_or_invalidate(self, memory: Memory) -> None:
        with traced("StubMemoryConsolidator.update_or_invalidate"):
            return None
