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


class MemoryEvaluator:
    def should_become_memory(self, experience: dict) -> bool:
        """Fig. 1's first gate."""
        raise NotImplementedError


class EntityLinker:
    def link(self, candidate: MemoryCandidate, existing: list[Memory]) -> list[str]:
        """A-MEM-style: find related memories, create links, update
        the network (ADR-0006). Runs before the quarantine check."""
        raise NotImplementedError


class QuarantineGate:
    def check_provenance(self, candidate: MemoryCandidate) -> bool:
        """Fig. 1's 'provenance verified?' gate (ADR-0007)."""
        raise NotImplementedError

    def quarantine(self, candidate: MemoryCandidate) -> None:
        """Released once corroborated, expires if never corroborated —
        not stored as usable memory either way, in the meantime."""
        raise NotImplementedError


class ScopeRouter:
    def route(self, memory: Memory) -> str:
        """Fig. 1's 'scope?' branch → User Memory Store or Shared
        Memory Store, physically separate (ADR-0008)."""
        raise NotImplementedError


class MemoryManager:
    """The only component that actively decides what it's holding onto
    (ADR-0005) — everything else here revises what's in the working set,
    nothing else does."""

    def is_in_working_set(self, query: dict) -> Memory | None:
        raise NotImplementedError

    def retrieve(self, query: dict, scope: str) -> list[Memory]:
        """From User Memory Store and/or Shared Memory Store, per
        scope. Fig. 2's slow path."""
        raise NotImplementedError

    def admit(self, memory: Memory) -> None:
        raise NotImplementedError

    def evict(self) -> Memory:
        """Least relevant / stalest, per active-management policy —
        not just age (fig. 2)."""
        raise NotImplementedError


class MemoryConsolidator:
    def check_staleness(self, memory: Memory) -> bool:
        raise NotImplementedError

    def update_or_invalidate(self, memory: Memory) -> None:
        """Runs periodically, independent of any single query. Feeds
        corrections back into EntityLinker.link() (fig. 2 → fig. 1)."""
        raise NotImplementedError
