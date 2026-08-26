"""Memory (component 06) — what the system has learned, not what
merely exists to be retrieved.

Design: Memory Design, fig. 1 (write path) and fig. 2 (read path and
working set)
Decisions: ADR-0005 (active working-set management), ADR-0006 (linked
network structure), ADR-0007 (quarantine at write), ADR-0008
(structural partition, user vs. shared)
Technology: Mem0 (ADR-0010) — vendor chosen, and its fit against the
four decisions above now formally checked against the real `mem0ai`
2.0.19 package (see ADR-0010's amended Consequences). The short
version: Mem0 fits none of the four natively. It scopes by metadata
filter inside one collection, not physically separate stores
(ADR-0008); it has no pre-store trust gate (ADR-0007); its 2026 OSS
release ships no graph/relationship store at all, only read-time
similarity search (ADR-0006); and it is a passive store with no active
working-set concept (ADR-0005). Its one real differentiator — LLM-driven
fact extraction/dedup — needs a model provider this project has never
chosen for any component (ADR-0028, mirroring ADR-0021's identical gap
for Agent Runtime). All four `Default*` adapters below are therefore
implemented directly against `Infrastructure` (ADR-0019's interface,
concretely `DefaultInfrastructure`, `../infrastructure_postgres.py`)
rather than Mem0 — real, structural, threshold-based logic, no LLM
required, no external credential needed to run.
"""

import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from infrastructure import Infrastructure

# Structural partition (ADR-0008): two physically distinct table
# namespaces behind Infrastructure, not one table filtered by a scope
# column — the exact distinction ADR-0008 draws against Mem0's own
# metadata-filter scoping (see this module's docstring, ADR-0010).
USER_MEMORY_TABLE = "memory_user"
SHARED_MEMORY_TABLE = "memory_shared"

# Quarantine bookkeeping (ADR-0007) lives in its own table — a
# quarantined candidate is explicitly not a Memory yet, so it does not
# belong in either scoped store above.
QUARANTINE_TABLE = "memory_quarantine"


def _table_for_scope(scope: str) -> str:
    """Single source of truth for the scope -> table mapping, shared by
    DefaultScopeRouter (which decides it) and DefaultMemoryManager /
    DefaultMemoryConsolidator (which need to reach the same table a
    routed Memory already lives in). Raises rather than guessing at an
    unrecognized scope — ADR-0008's whole point is that a query has to
    say which store it means."""
    if scope == "user":
        return USER_MEMORY_TABLE
    if scope == "shared":
        return SHARED_MEMORY_TABLE
    raise ValueError(f"unknown memory scope {scope!r}; expected 'user' or 'shared'")


def _memory_to_record(memory: "Memory") -> dict:
    return {
        "id": memory.id,
        "content": memory.content,
        "scope": memory.scope,
        "links": memory.links,
        "confidence": memory.confidence,
        "quarantined": memory.quarantined,
        "last_touched_at": memory.last_touched_at,
    }


def _memory_from_record(record: dict) -> "Memory":
    return Memory(
        id=record["id"],
        content=record.get("content", {}),
        scope=record.get("scope", ""),
        links=list(record.get("links", [])),
        confidence=record.get("confidence", 0.0),
        quarantined=record.get("quarantined", False),
        last_touched_at=record.get("last_touched_at", 0.0),
    )


def _content_tokens(content: dict) -> set[str]:
    """Flattens a memory's content dict into a lowercase token set — a
    deterministic, structural relatedness signal. Naive by design: this
    is token overlap, not semantic similarity. True semantic linking
    needs an embedding model, which is exactly the capability ADR-0028
    defers pending an LLM/embedding provider decision; this is the
    honest substitute in the meantime, not a stand-in dressed up as
    equivalent to it."""
    text = " ".join(str(value) for value in content.values())
    return {token for token in text.lower().split() if token}


def _quarantine_id_for(candidate: "MemoryCandidate") -> str:
    """Deterministic id derived from the candidate's own content and
    source, so a caller can recompute the same id later (for
    release()/is_expired()) without QuarantineGate.quarantine() having
    to violate its Protocol's `-> None` signature just to hand one
    back."""
    digest_input = json.dumps(
        {"source": candidate.source, "content": candidate.content}, sort_keys=True, default=str
    )
    return hashlib.sha256(digest_input.encode()).hexdigest()


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
    # Freshness signal fig. 2's eviction policy needs ("recency and
    # relevance, not just age" — ADR-0005's Consequences) and
    # MemoryConsolidator's staleness check needs (ADR-0006's
    # Consequences: the network can go stale). Not part of the
    # original skeleton; added here because both real behaviors below
    # are load-bearing on it and neither `confidence` nor `id` alone
    # can answer "when was this last relevant." Defaulted, so existing
    # `Memory(...)` call sites (including every Stub*) are unaffected.
    last_touched_at: float = field(default_factory=time.time)


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


class DefaultMemoryEvaluator:
    """Real implementation of MemoryEvaluator: threshold-based on the
    same `confidence` signal Memory itself carries. An experience only
    becomes memory if it has real content and meets a minimum
    confidence bar — structural gating, not cognition; no LLM decides
    "is this worth remembering," a numeric threshold does."""

    def __init__(self, min_confidence: float = 0.3) -> None:
        self._min_confidence = min_confidence

    def should_become_memory(self, experience: dict) -> bool:
        with traced("DefaultMemoryEvaluator.should_become_memory"):
            content = experience.get("content")
            confidence = experience.get("confidence", 0.0)
            return bool(content) and confidence >= self._min_confidence


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


class DefaultEntityLinker:
    """Real implementation of EntityLinker (ADR-0006). Mem0's 2026 OSS
    release ships no graph/relationship store (see this module's
    docstring, ADR-0010's amended Consequences), so this computes
    relatedness directly: Jaccard token-overlap between the candidate's
    content and each existing memory's content, above
    `similarity_threshold`, at write time — explicit links created now,
    not discovered later at search time (the distinction ADR-0006
    actually turns on)."""

    def __init__(self, similarity_threshold: float = 0.3) -> None:
        self._similarity_threshold = similarity_threshold

    def link(self, candidate: MemoryCandidate, existing: list[Memory]) -> list[str]:
        with traced("DefaultEntityLinker.link"):
            candidate_tokens = _content_tokens(candidate.content)
            if not candidate_tokens:
                return []
            linked_ids = []
            for memory in existing:
                memory_tokens = _content_tokens(memory.content)
                if not memory_tokens:
                    continue
                union = candidate_tokens | memory_tokens
                similarity = len(candidate_tokens & memory_tokens) / len(union)
                if similarity >= self._similarity_threshold:
                    linked_ids.append(memory.id)
            return linked_ids


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


class DefaultQuarantineGate:
    """Real implementation of QuarantineGate (ADR-0007). Mem0 has no
    native pre-store trust gate (ADR-0010's amended Consequences), so
    this is `Infrastructure`-backed directly: `quarantine()` writes a
    real pending/released/expired record to QUARANTINE_TABLE (real
    lifecycle bookkeeping, not a traced no-op), and `release()` /
    `is_expired()` — not part of the QuarantineGate Protocol, the same
    "extra real-behavior accessor" pattern DefaultTaskManager.status()
    uses in c10_agent_runtime.py — give that lifecycle somewhere to go
    beyond the two Protocol methods.

    Every quarantine decision is recorded via AuditManager — named
    explicitly as an audit-relevant event in
    cross_cutting/observability.py's own docstring — and the quarantined
    content is tagged UNTRUSTED via BoundaryGate.tag_provenance before
    it's stored, the same tagging document content and delegated
    sub-agent output get (ADR-0003, ADR-0018): a memory sitting in
    quarantine is, by definition, not yet trusted.
    """

    def __init__(
        self,
        infrastructure: Infrastructure,
        boundary_gate: BoundaryGate | None = None,
        audit_manager: AuditManager | None = None,
        ttl_seconds: float = 7 * 24 * 60 * 60,
    ) -> None:
        self._infrastructure = infrastructure
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._ttl_seconds = ttl_seconds

    def check_provenance(self, candidate: MemoryCandidate) -> bool:
        """The candidate's own `provenance_verified` flag is exactly
        what fig. 1's "provenance verified?" gate is asking about — real
        check, not a placeholder, because the answer is already a fact
        the caller determined upstream and attached to the candidate."""
        with traced("DefaultQuarantineGate.check_provenance"):
            return candidate.provenance_verified

    def quarantine(self, candidate: MemoryCandidate) -> None:
        with traced("DefaultQuarantineGate.quarantine"):
            quarantine_id = _quarantine_id_for(candidate)
            now = time.time()
            tagged_content = self._boundary_gate.tag_provenance(candidate.content, source=candidate.source)
            self._infrastructure.store(
                QUARANTINE_TABLE,
                {
                    "id": quarantine_id,
                    "content": tagged_content,
                    "source": candidate.source,
                    "status": "pending",
                    "quarantined_at": now,
                    "expires_at": now + self._ttl_seconds,
                },
            )
            self._audit_manager.record(
                "quarantine_decision",
                {"quarantine_id": quarantine_id, "source": candidate.source, "status": "pending"},
            )

    def release(self, quarantine_id: str) -> dict:
        """Not part of the QuarantineGate Protocol — real
        corroboration-release bookkeeping ADR-0007's Consequences call
        for ("released once corroborated"). Raises KeyError for an
        unknown id rather than pretending release succeeded."""
        with traced("DefaultQuarantineGate.release"):
            record = self._infrastructure.retrieve(QUARANTINE_TABLE, quarantine_id)
            if record is None:
                raise KeyError(f"DefaultQuarantineGate: unknown quarantine id {quarantine_id!r}")
            record["status"] = "released"
            self._infrastructure.store(QUARANTINE_TABLE, record)
            self._audit_manager.record(
                "quarantine_decision", {"quarantine_id": quarantine_id, "status": "released"}
            )
            return record

    def is_expired(self, quarantine_id: str) -> bool:
        """The other half of ADR-0007's lifecycle ("expires if never
        corroborated"): still pending, and past its TTL."""
        with traced("DefaultQuarantineGate.is_expired"):
            record = self._infrastructure.retrieve(QUARANTINE_TABLE, quarantine_id)
            if record is None:
                raise KeyError(f"DefaultQuarantineGate: unknown quarantine id {quarantine_id!r}")
            return record["status"] == "pending" and time.time() > record["expires_at"]


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


class DefaultScopeRouter:
    """Real implementation of ScopeRouter (ADR-0008). Mem0's own
    scoping is a metadata filter inside one shared collection —
    precisely the "single store, scope as metadata" alternative
    ADR-0008 rejected by name (ADR-0010's amended Consequences). This
    implements the physically-separate-store version directly:
    USER_MEMORY_TABLE and SHARED_MEMORY_TABLE are two distinct table
    namespaces behind `Infrastructure`, not one table with a scope
    column."""

    def route(self, memory: Memory) -> str:
        with traced("DefaultScopeRouter.route"):
            return _table_for_scope(memory.scope)


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


class DefaultMemoryManager:
    """Real implementation of MemoryManager (ADR-0005). Mem0 is
    architecturally passive — add/search/get_all/update/delete, all
    callee-initiated, nothing that curates a bounded active set on its
    own (ADR-0010's amended Consequences) — so this is real, from
    scratch: a bounded, `OrderedDict`-backed working set with real LRU
    eviction (least-recently-touched entry evicted first when full).

    LRU is a real, defensible eviction policy — it directly covers the
    "recency" half of fig. 2's "weighing recency and relevance, not
    just age." It does not cover "relevance" (a similarity/importance
    score independent of access pattern); that would need a real
    scoring model this pass doesn't build, and is named here rather
    than silently folded into "recency" as if they were the same thing
    — this is this component's known-unknown per ADR-0005's own
    Consequences.

    `admit()` is both this class's working-set curation *and* the
    write path's terminal "scoped store" step (fig. 1): nothing else
    among Memory's six Protocols persists a Memory permanently, and
    MemoryManager is explicitly "the only component that actively
    decides what it's holding onto" — deciding to persist a memory is
    part of deciding to hold onto it. `retrieve()`'s slow-path results
    are pulled into the working set too, but via a private helper that
    skips the redundant re-persist `admit()` would otherwise do for
    something that was just read back out of its own store.
    """

    def __init__(
        self,
        infrastructure: Infrastructure,
        scope_router: ScopeRouter,
        max_working_set_size: int = 50,
    ) -> None:
        self._infrastructure = infrastructure
        self._scope_router = scope_router
        self._max_working_set_size = max_working_set_size
        self._working_set: "OrderedDict[str, Memory]" = OrderedDict()

    def is_in_working_set(self, query: dict) -> Memory | None:
        with traced("DefaultMemoryManager.is_in_working_set"):
            memory_id = query.get("id")
            if memory_id is None:
                return None
            memory = self._working_set.get(memory_id)
            if memory is not None:
                memory.last_touched_at = time.time()
                self._working_set.move_to_end(memory_id)
            return memory

    def retrieve(self, query: dict, scope: str) -> list[Memory]:
        with traced("DefaultMemoryManager.retrieve"):
            table = _table_for_scope(scope)
            records = self._infrastructure.query(table, query)
            memories = [_memory_from_record(record) for record in records]
            for memory in memories:
                self._admit_to_working_set(memory)
            return memories

    def admit(self, memory: Memory) -> None:
        with traced("DefaultMemoryManager.admit"):
            table = self._scope_router.route(memory)
            self._infrastructure.store(table, _memory_to_record(memory))
            self._admit_to_working_set(memory)

    def evict(self) -> Memory:
        with traced("DefaultMemoryManager.evict"):
            if not self._working_set:
                raise LookupError("DefaultMemoryManager: cannot evict, working set is empty")
            _, memory = self._working_set.popitem(last=False)
            return memory

    def _admit_to_working_set(self, memory: Memory) -> None:
        memory.last_touched_at = time.time()
        if memory.id in self._working_set:
            self._working_set.move_to_end(memory.id)
            self._working_set[memory.id] = memory
            return
        if len(self._working_set) >= self._max_working_set_size:
            self.evict()
        self._working_set[memory.id] = memory


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


class DefaultMemoryConsolidator:
    """Real implementation of MemoryConsolidator: structural,
    threshold-based staleness on the two real freshness signals a
    Memory carries — `confidence` (already on the dataclass) and
    `last_touched_at` (added in this pass — see Memory's own
    docstring). A memory is stale if it hasn't been touched recently
    enough, or if its confidence has already decayed below the floor.

    `update_or_invalidate()` applies real confidence decay and, once
    confidence drops far enough, re-quarantines the memory rather than
    silently deleting it — consistent with ADR-0007's own lifecycle
    language ("released once corroborated, expires if never
    corroborated") applying just as much to a memory that's gone stale
    as to one that was never verified. The change is persisted back to
    the memory's own scoped store via the same ScopeRouter every other
    write in this file goes through.

    Re-linking (fig. 2 → fig. 1, "feeds corrections back into
    EntityLinker.link()") is intentionally not done here: EntityLinker.
    link() needs a candidate list of existing memories to compare
    against, which requires a retrieval call this Protocol's
    single-memory `update_or_invalidate(memory)` signature has no way
    to request, and Memory has no fig.-1-orchestrator-equivalent class
    (unlike Agent Runtime's DefaultAgentCoordinator) to source that list
    from. Named here as a real, deliberate scope limit for this pass,
    not silently dropped.
    """

    _CONFIDENCE_DECAY_ON_STALENESS = 0.5
    _REQUARANTINE_FLOOR_CONFIDENCE = 0.1

    def __init__(
        self,
        infrastructure: Infrastructure,
        scope_router: ScopeRouter,
        staleness_threshold_seconds: float = 30 * 24 * 60 * 60,
        min_confidence: float = 0.2,
    ) -> None:
        self._infrastructure = infrastructure
        self._scope_router = scope_router
        self._staleness_threshold_seconds = staleness_threshold_seconds
        self._min_confidence = min_confidence

    def check_staleness(self, memory: Memory) -> bool:
        with traced("DefaultMemoryConsolidator.check_staleness"):
            age_seconds = time.time() - memory.last_touched_at
            return age_seconds > self._staleness_threshold_seconds or memory.confidence < self._min_confidence

    def update_or_invalidate(self, memory: Memory) -> None:
        with traced("DefaultMemoryConsolidator.update_or_invalidate"):
            memory.confidence *= self._CONFIDENCE_DECAY_ON_STALENESS
            if memory.confidence < self._REQUARANTINE_FLOOR_CONFIDENCE:
                memory.quarantined = True
            table = self._scope_router.route(memory)
            self._infrastructure.store(table, _memory_to_record(memory))
