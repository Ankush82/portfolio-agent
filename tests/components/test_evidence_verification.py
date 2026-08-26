"""Tests for the `Default*` adapters in
src/components/c09_evidence_verification.py (ADR-0013, ADR-0014,
ADR-0029, ADR-0030, ADR-0031).

`DefaultEvidenceLinker` is tested against real `DefaultMemoryManager`/
`DefaultScopeRouter` (c06_memory.py) backed by `_FakeInfrastructure`, a
minimal in-memory double of the `Infrastructure` Protocol (same shape
and semantics as tests/components/test_memory.py's own double) — per
the task's own instruction to use the real Memory interfaces rather
than a stub. `DefaultMandatoryEvidenceGate` is tested with a spy
`AuditManager` so the "logged, not forwarded" behavior (ADR-0013) is
asserted directly, not just trusted to have happened.
"""

import time
import uuid

import pytest

from components.c05_retrieval_context import ContextPack
from components.c06_memory import DefaultMemoryManager, DefaultScopeRouter, Memory
from components.c09_evidence_verification import (
    _FRESHNESS_HALF_LIFE_SECONDS,
    Claim,
    DefaultClaimVerifier,
    DefaultContradictionResolver,
    DefaultEvidenceLinker,
    DefaultMandatoryEvidenceGate,
    Evidence,
)


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol
    (src/infrastructure.py): store/retrieve/query only, since that's
    all DefaultMemoryManager calls. Same semantics as
    DefaultInfrastructure: store() keys off record["id"] when present,
    query() does containment matching."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}

    def store(self, table: str, record: dict) -> str:
        record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
        record = dict(record)
        record["id"] = record_id
        self._records[(table, record_id)] = record
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
    """Records every call so blocked-claim audit events can be
    asserted on directly, rather than only trusting DefaultAuditManager
    wrote a log line somewhere on disk."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


def _memory_manager() -> DefaultMemoryManager:
    return DefaultMemoryManager(infrastructure=_FakeInfrastructure(), scope_router=DefaultScopeRouter())


def _memory(memory_id: str, content: dict, scope: str = "user", **kwargs) -> Memory:
    return Memory(id=memory_id, content=content, scope=scope, **kwargs)


def _claim(text: str) -> Claim:
    return Claim(text=text, source_component="Analysis & Reasoning")


def _evidence(content: dict, source: str = "s1", reliability: float = 0.5, freshness: float = 1.0) -> Evidence:
    return Evidence(content=content, source=source, reliability=reliability, freshness=freshness)


# --- DefaultEvidenceLinker --------------------------------------------------


def test_link_finds_related_memory_and_scores_reliability_from_confidence():
    manager = _memory_manager()
    manager.admit(_memory("m1", {"text": "Acme Corp missed earnings this quarter"}, confidence=0.7))
    manager.admit(_memory("m2", {"text": "unrelated sector weather report"}, confidence=0.9))
    linker = DefaultEvidenceLinker(memory_manager=manager, similarity_threshold=0.3)

    evidence = linker.link(_claim("Acme Corp missed earnings this quarter"))

    assert len(evidence) == 1
    assert evidence[0].source == "memory:user:m1"
    assert evidence[0].reliability == pytest.approx(0.7)


def test_link_excludes_quarantined_memory_even_when_content_matches():
    manager = _memory_manager()
    manager.admit(_memory("m1", {"text": "Acme Corp missed earnings"}, confidence=0.9, quarantined=True))
    linker = DefaultEvidenceLinker(memory_manager=manager, similarity_threshold=0.2)

    evidence = linker.link(_claim("Acme Corp missed earnings"))

    assert evidence == []


def test_link_searches_both_user_and_shared_scopes():
    manager = _memory_manager()
    manager.admit(_memory("m1", {"text": "Acme Corp missed earnings"}, scope="user", confidence=0.5))
    manager.admit(_memory("m2", {"text": "Acme Corp missed earnings guidance"}, scope="shared", confidence=0.5))
    linker = DefaultEvidenceLinker(memory_manager=manager, similarity_threshold=0.3)

    evidence = linker.link(_claim("Acme Corp missed earnings"))

    sources = {e.source for e in evidence}
    assert "memory:user:m1" in sources
    assert "memory:shared:m2" in sources


def test_link_returns_empty_for_empty_claim_text():
    manager = _memory_manager()
    manager.admit(_memory("m1", {"text": "Acme Corp missed earnings"}, confidence=0.9))
    linker = DefaultEvidenceLinker(memory_manager=manager, similarity_threshold=0.0)

    assert linker.link(_claim("")) == []


class _FixedRetrieveMemoryManager:
    """Minimal MemoryManager double used only for this freshness test:
    DefaultMemoryManager's real admit()/retrieve() both "touch" a
    Memory (reset last_touched_at to now, correctly, since that's the
    LRU working-set behavior ADR-0005 requires) — which would defeat a
    test that needs a Memory with a specific, aged last_touched_at to
    reach DefaultEvidenceLinker at all. This double just hands back a
    fixed list untouched, isolating the freshness-decay computation
    from DefaultMemoryManager's own (correct) touch-on-access
    behavior."""

    def __init__(self, memories: list[Memory]) -> None:
        self._memories = memories

    def retrieve(self, query: dict, scope: str) -> list[Memory]:
        return [m for m in self._memories if m.scope == scope]


def test_link_memory_freshness_decays_with_age_by_configured_half_life():
    now = time.time()
    fresh = _memory("fresh", {"text": "Acme Corp missed earnings"}, confidence=0.5, last_touched_at=now)
    stale = _memory(
        "stale",
        {"text": "Acme Corp missed earnings"},
        confidence=0.5,
        last_touched_at=now - _FRESHNESS_HALF_LIFE_SECONDS,
    )
    manager = _FixedRetrieveMemoryManager([fresh, stale])
    linker = DefaultEvidenceLinker(memory_manager=manager, similarity_threshold=0.3)

    evidence = {e.source: e for e in linker.link(_claim("Acme Corp missed earnings"))}

    assert evidence["memory:user:fresh"].freshness == pytest.approx(1.0, abs=0.01)
    assert evidence["memory:user:stale"].freshness == pytest.approx(0.5, rel=0.01)


def test_link_with_context_searches_context_pack_documents_too():
    manager = _memory_manager()
    linker = DefaultEvidenceLinker(memory_manager=manager, similarity_threshold=0.3)
    context_pack = ContextPack(
        documents=[
            {"text": "Acme Corp missed earnings", "source": "news:reuters", "reliability": 0.8},
            {"text": "totally unrelated weather report"},
        ],
        sufficient=True,
    )

    evidence = linker.link_with_context(_claim("Acme Corp missed earnings"), context_pack)

    assert len(evidence) == 1
    assert evidence[0].source == "news:reuters"
    assert evidence[0].reliability == pytest.approx(0.8)
    assert evidence[0].freshness == pytest.approx(1.0)  # no timestamp on the document -> default "just retrieved"


def test_link_uses_context_pack_bound_at_construction():
    manager = _memory_manager()
    context_pack = ContextPack(
        documents=[{"text": "Acme Corp missed earnings", "source_id": "doc-1"}], sufficient=True
    )
    linker = DefaultEvidenceLinker(memory_manager=manager, context_pack=context_pack, similarity_threshold=0.3)

    evidence = linker.link(_claim("Acme Corp missed earnings"))

    assert [e.source for e in evidence] == ["doc-1"]


def test_link_context_pack_document_falls_back_to_neutral_defaults():
    manager = _memory_manager()
    context_pack = ContextPack(documents=[{"text": "Acme Corp missed earnings"}], sufficient=True)
    linker = DefaultEvidenceLinker(memory_manager=manager, context_pack=context_pack, similarity_threshold=0.3)

    evidence = linker.link(_claim("Acme Corp missed earnings"))

    assert evidence[0].source == "context_pack"
    assert evidence[0].reliability == pytest.approx(0.5)
    assert evidence[0].freshness == pytest.approx(1.0)


# --- DefaultMandatoryEvidenceGate -------------------------------------------


def test_has_evidence_true_for_nonempty_list():
    gate = DefaultMandatoryEvidenceGate()

    assert gate.has_evidence([_evidence({"result": "beat"})]) is True


def test_has_evidence_false_for_empty_list():
    gate = DefaultMandatoryEvidenceGate()

    assert gate.has_evidence([]) is False


def test_block_records_audit_event_and_does_not_raise():
    audit = _SpyAuditManager()
    gate = DefaultMandatoryEvidenceGate(audit_manager=audit)
    claim = _claim("Acme Corp beat earnings")

    result = gate.block(claim)

    assert result is None
    assert audit.events == [
        ("claim_blocked", {"claim_text": "Acme Corp beat earnings", "source_component": "Analysis & Reasoning"})
    ]


# --- DefaultContradictionResolver -------------------------------------------


def test_sources_agree_true_for_fewer_than_two_evidence():
    resolver = DefaultContradictionResolver()

    assert resolver.sources_agree([]) is True
    assert resolver.sources_agree([_evidence({"result": "beat"})]) is True


def test_sources_agree_false_when_topically_related_and_shared_key_conflicts():
    resolver = DefaultContradictionResolver()
    beat = _evidence({"company": "Acme Corp", "period": "Q3 2026", "result": "beat"}, source="filing:acme")
    missed = _evidence({"company": "Acme Corp", "period": "Q3 2026", "result": "missed"}, source="news:xyz")

    assert resolver.sources_agree([beat, missed]) is False


def test_sources_agree_true_when_conflicting_pair_is_not_topically_related():
    resolver = DefaultContradictionResolver()
    unrelated_a = _evidence({"company": "Acme Corp", "result": "beat"}, source="a")
    unrelated_b = _evidence({"company": "Zeta Inc", "result": "missed"}, source="b")

    # Same "result" key conflicts, but the two entries share no other
    # vocabulary -> below the topic-overlap gate, so not compared.
    assert resolver.sources_agree([unrelated_a, unrelated_b]) is True


def test_sources_agree_true_when_related_but_no_shared_keys():
    resolver = DefaultContradictionResolver()
    a = _evidence({"headline": "Acme Corp quarterly results"}, source="a")
    b = _evidence({"summary": "Acme Corp quarterly results"}, source="b")

    assert resolver.sources_agree([a, b]) is True


def test_resolve_picks_highest_reliability_times_freshness():
    resolver = DefaultContradictionResolver()
    low = _evidence({"result": "missed"}, source="low", reliability=0.6, freshness=0.8)
    high = _evidence({"result": "beat"}, source="high", reliability=0.9, freshness=1.0)
    mid = _evidence({"result": "flat"}, source="mid", reliability=0.5, freshness=0.5)

    assert resolver.resolve([low, high, mid]) is high


def test_resolve_breaks_ties_toward_higher_reliability():
    resolver = DefaultContradictionResolver()
    # Equal composite (0.8 * 0.5 == 0.5 * 0.8), higher reliability wins.
    lower_reliability = _evidence({}, source="a", reliability=0.5, freshness=0.8)
    higher_reliability = _evidence({}, source="b", reliability=0.8, freshness=0.5)

    assert resolver.resolve([lower_reliability, higher_reliability]) is higher_reliability


def test_resolve_raises_on_empty_evidence():
    resolver = DefaultContradictionResolver()

    with pytest.raises(ValueError):
        resolver.resolve([])


# --- DefaultClaimVerifier ----------------------------------------------------


def test_verify_returns_zero_confidence_for_empty_evidence():
    verifier = DefaultClaimVerifier(contradiction_resolver=DefaultContradictionResolver())

    verified = verifier.verify(_claim("Acme Corp beat earnings"), [])

    assert verified.evidence == []
    assert verified.confidence == 0.0
    assert verified.was_contradictory is False


def test_verify_agreeing_evidence_computes_mean_quality_times_diversity():
    verifier = DefaultClaimVerifier(contradiction_resolver=DefaultContradictionResolver())
    evidence = [
        _evidence({"result": "beat"}, source="s1", reliability=0.8, freshness=1.0),
        _evidence({"result": "beat"}, source="s2", reliability=0.6, freshness=0.5),
    ]

    verified = verifier.verify(_claim("Acme Corp beat earnings"), evidence)

    assert verified.was_contradictory is False
    # base_quality = mean(0.8*1.0, 0.6*0.5) = 0.55; 2 independent sources -> diversity 1.0; no penalty.
    assert verified.confidence == pytest.approx(0.55)


def test_verify_contradictory_evidence_applies_penalty_and_uses_resolved_winner():
    verifier = DefaultClaimVerifier(contradiction_resolver=DefaultContradictionResolver())
    beat = _evidence(
        {"company": "Acme Corp", "period": "Q3 2026", "result": "beat"},
        source="filing:acme",
        reliability=0.9,
        freshness=1.0,
    )
    missed = _evidence(
        {"company": "Acme Corp", "period": "Q3 2026", "result": "missed"},
        source="news:xyz",
        reliability=0.6,
        freshness=0.8,
    )

    verified = verifier.verify(_claim("Acme Corp Q3 2026 earnings"), [beat, missed])

    assert verified.was_contradictory is True
    # resolved winner is `beat` (0.9 > 0.48); base_quality = 0.9; diversity 1.0; penalty 0.85.
    assert verified.confidence == pytest.approx(0.9 * 0.85)


def test_score_confidence_scales_down_for_a_single_source():
    verifier = DefaultClaimVerifier(contradiction_resolver=DefaultContradictionResolver())
    single = [_evidence({"result": "beat"}, source="s1", reliability=1.0, freshness=1.0)]

    verified = verifier.verify(_claim("Acme Corp beat earnings"), single)

    # base_quality 1.0 * diversity (1 source / target 2 = 0.5) * no penalty = 0.5.
    assert verified.confidence == pytest.approx(0.5)


def test_score_confidence_caps_at_one_for_two_perfect_independent_sources():
    verifier = DefaultClaimVerifier(contradiction_resolver=DefaultContradictionResolver())
    evidence = [
        _evidence({"result": "beat"}, source="s1", reliability=1.0, freshness=1.0),
        _evidence({"result": "beat"}, source="s2", reliability=1.0, freshness=1.0),
    ]

    verified = verifier.verify(_claim("Acme Corp beat earnings"), evidence)

    assert verified.confidence == pytest.approx(1.0)


def test_score_confidence_target_independent_sources_is_configurable():
    verifier = DefaultClaimVerifier(
        contradiction_resolver=DefaultContradictionResolver(), target_independent_sources=1
    )
    single = [_evidence({"result": "beat"}, source="s1", reliability=1.0, freshness=1.0)]

    verified = verifier.verify(_claim("Acme Corp beat earnings"), single)

    # With target_independent_sources=1, one source is already "fully cited".
    assert verified.confidence == pytest.approx(1.0)
