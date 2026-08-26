"""Tests for the `Default*` adapters in src/components/c06_memory.py
(ADR-0005 through ADR-0008, ADR-0010's amended Consequences, ADR-0028).

All tests run against `_FakeInfrastructure`, a minimal in-memory test
double of the `Infrastructure` Protocol (same shape and semantics as
tests/components/test_data_sources.py's own double), so this
component's real logic — scope routing, quarantine bookkeeping, entity
linking, working-set curation and eviction, staleness/consolidation —
is exercised fast and without a live Postgres. `DefaultBoundaryGate`
and `DefaultAuditManager` (cross_cutting/) have no external dependency,
so they're used for real rather than stubbed.
"""

import time
import uuid

import pytest

from components.c06_memory import (
    QUARANTINE_TABLE,
    SHARED_MEMORY_TABLE,
    USER_MEMORY_TABLE,
    DefaultEntityLinker,
    DefaultMemoryConsolidator,
    DefaultMemoryEvaluator,
    DefaultMemoryManager,
    DefaultQuarantineGate,
    DefaultScopeRouter,
    Mem0EntityLinker,
    Memory,
    MemoryCandidate,
    _table_for_scope,
)
from cross_cutting.security import Provenance


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol
    (src/infrastructure.py): store/retrieve/query only, since that's
    all component 06's Default* adapters call. Same semantics as
    DefaultInfrastructure: store() keys off record["id"] when present,
    query() does containment matching (every filter key/value must
    equal the stored record's)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}
        self.store_calls: list[tuple[str, dict]] = []

    def store(self, table: str, record: dict) -> str:
        record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
        record = dict(record)
        record["id"] = record_id
        self._records[(table, record_id)] = record
        self.store_calls.append((table, record))
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        record = self._records.get((table, id_))
        return dict(record) if record is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        matches = []
        for (record_table, _), record in self._records.items():
            if record_table != table:
                continue
            if all(record.get(key) == value for key, value in filters.items()):
                matches.append(dict(record))
        return matches


class _SpyAuditManager:
    """Records every call so quarantine-decision audit events can be
    asserted on directly, rather than only trusting DefaultAuditManager
    wrote a log line somewhere on disk."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


def _memory(memory_id: str = "m1", scope: str = "user", **kwargs) -> Memory:
    return Memory(id=memory_id, content=kwargs.pop("content", {"text": "earnings beat"}), scope=scope, **kwargs)


# --- _table_for_scope / DefaultScopeRouter -----------------------------


def test_table_for_scope_maps_user_and_shared_to_distinct_tables():
    assert _table_for_scope("user") == USER_MEMORY_TABLE
    assert _table_for_scope("shared") == SHARED_MEMORY_TABLE
    assert USER_MEMORY_TABLE != SHARED_MEMORY_TABLE


def test_table_for_scope_rejects_unknown_scope():
    with pytest.raises(ValueError):
        _table_for_scope("something-else")


def test_default_scope_router_routes_by_memory_scope():
    router = DefaultScopeRouter()

    assert router.route(_memory(scope="user")) == USER_MEMORY_TABLE
    assert router.route(_memory(scope="shared")) == SHARED_MEMORY_TABLE


def test_default_scope_router_rejects_unknown_scope():
    router = DefaultScopeRouter()

    with pytest.raises(ValueError):
        router.route(_memory(scope="global"))


# --- DefaultMemoryEvaluator ---------------------------------------------


def test_should_become_memory_true_above_confidence_threshold():
    evaluator = DefaultMemoryEvaluator(min_confidence=0.3)

    assert evaluator.should_become_memory({"content": {"text": "x"}, "confidence": 0.5}) is True


def test_should_become_memory_false_below_confidence_threshold():
    evaluator = DefaultMemoryEvaluator(min_confidence=0.3)

    assert evaluator.should_become_memory({"content": {"text": "x"}, "confidence": 0.1}) is False


def test_should_become_memory_false_when_content_missing():
    evaluator = DefaultMemoryEvaluator(min_confidence=0.0)

    assert evaluator.should_become_memory({"content": {}, "confidence": 1.0}) is False
    assert evaluator.should_become_memory({"confidence": 1.0}) is False


# --- DefaultEntityLinker --------------------------------------------------


def test_link_finds_related_memory_above_similarity_threshold():
    linker = DefaultEntityLinker(similarity_threshold=0.3)
    candidate = MemoryCandidate(
        content={"text": "Acme Corp missed earnings this quarter"}, source="filing", provenance_verified=True
    )
    existing = [
        _memory("related", content={"text": "Acme Corp earnings history this quarter"}),
        _memory("unrelated", content={"text": "unrelated sector weather report"}),
    ]

    links = linker.link(candidate, existing)

    assert "related" in links
    assert "unrelated" not in links


def test_link_returns_empty_list_when_candidate_content_empty():
    linker = DefaultEntityLinker()
    candidate = MemoryCandidate(content={}, source="filing", provenance_verified=True)

    assert linker.link(candidate, [_memory("m1")]) == []


def test_link_returns_empty_list_when_nothing_existing():
    linker = DefaultEntityLinker()
    candidate = MemoryCandidate(content={"text": "Acme Corp earnings"}, source="filing", provenance_verified=True)

    assert linker.link(candidate, []) == []


# --- Mem0EntityLinker (ADR-0028, resolved by ADR-0045) --------------------


def test_mem0_link_finds_semantically_related_memory_with_no_shared_tokens():
    linker = Mem0EntityLinker()
    candidate = MemoryCandidate(
        content={"text": "AAPL stock price dropped 5 percent after earnings miss"},
        source="filing",
        provenance_verified=True,
    )
    existing = [
        _memory("related", content={"text": "Apple shares fell following disappointing quarterly earnings"}),
        _memory("unrelated", content={"text": "The weather in Paris is sunny today"}),
    ]

    links = linker.link(candidate, existing)

    assert "related" in links
    assert "unrelated" not in links


def test_mem0_link_returns_empty_list_when_candidate_content_empty():
    linker = Mem0EntityLinker()
    candidate = MemoryCandidate(content={}, source="filing", provenance_verified=True)

    assert linker.link(candidate, [_memory("m1", content={"text": "something"})]) == []


def test_mem0_link_returns_empty_list_when_nothing_existing():
    linker = Mem0EntityLinker()
    candidate = MemoryCandidate(content={"text": "Acme Corp earnings"}, source="filing", provenance_verified=True)

    assert linker.link(candidate, []) == []


def test_mem0_link_skips_existing_memory_with_empty_content():
    linker = Mem0EntityLinker()
    candidate = MemoryCandidate(content={"text": "Acme Corp earnings"}, source="filing", provenance_verified=True)
    existing = [_memory("empty", content={})]

    assert linker.link(candidate, existing) == []


def test_mem0_link_respects_a_custom_similarity_threshold():
    candidate = MemoryCandidate(
        content={"text": "AAPL stock price dropped 5 percent after earnings miss"},
        source="filing",
        provenance_verified=True,
    )
    existing = [_memory("m1", content={"text": "Apple shares fell following disappointing quarterly earnings"})]

    lenient_linker = Mem0EntityLinker(similarity_threshold=0.0)
    strict_linker = Mem0EntityLinker(similarity_threshold=0.999)

    assert lenient_linker.link(candidate, existing) == ["m1"]
    assert strict_linker.link(candidate, existing) == []


# --- DefaultQuarantineGate -----------------------------------------------


def test_check_provenance_mirrors_candidate_flag():
    gate = DefaultQuarantineGate(infrastructure=_FakeInfrastructure())

    verified = MemoryCandidate(content={}, source="filing", provenance_verified=True)
    unverified = MemoryCandidate(content={}, source="rumor", provenance_verified=False)

    assert gate.check_provenance(verified) is True
    assert gate.check_provenance(unverified) is False


def test_quarantine_stores_pending_record_tagged_untrusted_and_audited():
    infra = _FakeInfrastructure()
    audit = _SpyAuditManager()
    gate = DefaultQuarantineGate(infrastructure=infra, audit_manager=audit, ttl_seconds=3600)
    candidate = MemoryCandidate(content={"text": "unverified rumor"}, source="social_media", provenance_verified=False)

    gate.quarantine(candidate)

    stored = [record for table, record in infra.store_calls if table == QUARANTINE_TABLE]
    assert len(stored) == 1
    record = stored[0]
    assert record["status"] == "pending"
    assert record["content"]["provenance"] == Provenance.UNTRUSTED.name
    assert record["expires_at"] > record["quarantined_at"]
    assert audit.events == [
        ("quarantine_decision", {"quarantine_id": record["id"], "source": "social_media", "status": "pending"})
    ]


def test_release_marks_record_released_and_raises_for_unknown_id():
    infra = _FakeInfrastructure()
    gate = DefaultQuarantineGate(infrastructure=infra)
    candidate = MemoryCandidate(content={"text": "later corroborated"}, source="filing", provenance_verified=False)
    gate.quarantine(candidate)
    quarantine_id = infra.store_calls[0][1]["id"]

    released = gate.release(quarantine_id)

    assert released["status"] == "released"
    assert infra.retrieve(QUARANTINE_TABLE, quarantine_id)["status"] == "released"
    with pytest.raises(KeyError):
        gate.release("no-such-id")


def test_is_expired_true_only_once_ttl_has_passed_and_still_pending():
    infra = _FakeInfrastructure()
    already_expired_gate = DefaultQuarantineGate(infrastructure=infra, ttl_seconds=-1)
    still_fresh_gate = DefaultQuarantineGate(infrastructure=infra, ttl_seconds=3600)

    expired_candidate = MemoryCandidate(content={"text": "old"}, source="filing", provenance_verified=False)
    already_expired_gate.quarantine(expired_candidate)
    expired_id = infra.store_calls[-1][1]["id"]

    fresh_candidate = MemoryCandidate(content={"text": "new"}, source="filing", provenance_verified=False)
    still_fresh_gate.quarantine(fresh_candidate)
    fresh_id = infra.store_calls[-1][1]["id"]

    assert already_expired_gate.is_expired(expired_id) is True
    assert still_fresh_gate.is_expired(fresh_id) is False

    # A released record never counts as expired, even past its TTL.
    already_expired_gate.release(expired_id)
    assert already_expired_gate.is_expired(expired_id) is False

    with pytest.raises(KeyError):
        already_expired_gate.is_expired("no-such-id")


# --- DefaultMemoryManager -------------------------------------------------


def test_admit_persists_to_the_scope_routed_table_and_enters_working_set():
    infra = _FakeInfrastructure()
    manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    memory = _memory("m1", scope="user")

    manager.admit(memory)

    assert infra.retrieve(USER_MEMORY_TABLE, "m1") is not None
    assert manager.is_in_working_set({"id": "m1"}) is memory


def test_admit_routes_shared_scope_to_shared_table():
    infra = _FakeInfrastructure()
    manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    memory = _memory("m1", scope="shared")

    manager.admit(memory)

    assert infra.retrieve(SHARED_MEMORY_TABLE, "m1") is not None
    assert infra.retrieve(USER_MEMORY_TABLE, "m1") is None


def test_is_in_working_set_returns_none_for_unknown_id():
    manager = DefaultMemoryManager(infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter())

    assert manager.is_in_working_set({"id": "unknown"}) is None
    assert manager.is_in_working_set({}) is None


def test_evict_raises_lookup_error_when_working_set_empty():
    manager = DefaultMemoryManager(infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter())

    with pytest.raises(LookupError):
        manager.evict()


def test_admit_evicts_least_recently_touched_when_working_set_full():
    manager = DefaultMemoryManager(
        infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter(), max_working_set_size=2
    )
    manager.admit(_memory("m1"))
    manager.admit(_memory("m2"))
    manager.admit(_memory("m3"))  # should evict m1, the least recently touched

    assert manager.is_in_working_set({"id": "m1"}) is None
    assert manager.is_in_working_set({"id": "m2"}) is not None
    assert manager.is_in_working_set({"id": "m3"}) is not None


def test_touching_via_is_in_working_set_protects_from_eviction():
    manager = DefaultMemoryManager(
        infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter(), max_working_set_size=2
    )
    manager.admit(_memory("m1"))
    manager.admit(_memory("m2"))
    manager.is_in_working_set({"id": "m1"})  # touch m1 so m2 is now the LRU entry
    manager.admit(_memory("m3"))  # should evict m2, not m1

    assert manager.is_in_working_set({"id": "m1"}) is not None
    assert manager.is_in_working_set({"id": "m2"}) is None


def test_retrieve_pulls_matching_scoped_records_into_working_set_without_reperisting():
    infra = _FakeInfrastructure()
    manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    manager.admit(_memory("m1", scope="user", content={"text": "a"}))
    store_calls_after_admit = len(infra.store_calls)

    results = manager.retrieve({}, scope="user")

    assert len(results) == 1
    assert results[0].id == "m1"
    assert manager.is_in_working_set({"id": "m1"}) is not None
    # retrieve() must not re-persist what it just read back out.
    assert len(infra.store_calls) == store_calls_after_admit


def test_retrieve_only_looks_at_the_requested_scope():
    infra = _FakeInfrastructure()
    manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    manager.admit(_memory("user-mem", scope="user"))
    manager.admit(_memory("shared-mem", scope="shared"))

    user_results = manager.retrieve({}, scope="user")
    shared_results = manager.retrieve({}, scope="shared")

    assert [m.id for m in user_results] == ["user-mem"]
    assert [m.id for m in shared_results] == ["shared-mem"]


# --- DefaultMemoryConsolidator --------------------------------------------


def test_check_staleness_true_when_last_touched_long_ago():
    consolidator = DefaultMemoryConsolidator(
        infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter(), staleness_threshold_seconds=60
    )
    old_memory = _memory(confidence=0.9, last_touched_at=time.time() - 3600)

    assert consolidator.check_staleness(old_memory) is True


def test_check_staleness_true_when_confidence_below_floor():
    consolidator = DefaultMemoryConsolidator(
        infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter(), min_confidence=0.2
    )
    low_confidence_memory = _memory(confidence=0.05, last_touched_at=time.time())

    assert consolidator.check_staleness(low_confidence_memory) is True


def test_check_staleness_false_for_fresh_confident_memory():
    consolidator = DefaultMemoryConsolidator(
        infrastructure=_FakeInfrastructure(),
        scope_router=DefaultScopeRouter(),
        staleness_threshold_seconds=3600,
        min_confidence=0.2,
    )
    fresh_memory = _memory(confidence=0.9, last_touched_at=time.time())

    assert consolidator.check_staleness(fresh_memory) is False


def test_update_or_invalidate_decays_confidence_and_persists():
    infra = _FakeInfrastructure()
    consolidator = DefaultMemoryConsolidator(infrastructure=infra, scope_router=DefaultScopeRouter())
    memory = _memory("m1", scope="user", confidence=0.8)

    consolidator.update_or_invalidate(memory)

    assert memory.confidence == pytest.approx(0.4)
    assert memory.quarantined is False
    stored = infra.retrieve(USER_MEMORY_TABLE, "m1")
    assert stored["confidence"] == pytest.approx(0.4)


def test_update_or_invalidate_requarantines_once_confidence_floor_crossed():
    infra = _FakeInfrastructure()
    consolidator = DefaultMemoryConsolidator(infrastructure=infra, scope_router=DefaultScopeRouter())
    memory = _memory("m1", scope="shared", confidence=0.15)  # decays to 0.075, below the 0.1 floor

    consolidator.update_or_invalidate(memory)

    assert memory.quarantined is True
    stored = infra.retrieve(SHARED_MEMORY_TABLE, "m1")
    assert stored["quarantined"] is True


# --- Memory dataclass ------------------------------------------------------


def test_memory_last_touched_at_defaults_to_now():
    before = time.time()
    memory = _memory()
    after = time.time()

    assert before <= memory.last_touched_at <= after
