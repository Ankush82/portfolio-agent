"""Tests for DefaultAnalysisReasoning and placeholder_reason_fn
(src/components/c08_analysis_reasoning.py, ADR-0037).

DefaultAnalysisReasoning is exercised against real, non-stub
implementations of the components it converges — `DefaultEventObservation`
(07), `DefaultMemoryManager` (06), `DefaultContextBuilder` (05),
`DefaultUserPortfolio` (01), and the real Evidence & Verification (09)
pipeline (`DefaultEvidenceLinker`/`DefaultClaimVerifier`/
`DefaultMandatoryEvidenceGate`) — all backed by one shared
`_FakeInfrastructure`, the same minimal in-memory double of the
`Infrastructure` Protocol every other component's tests already use.
The point of this file is to prove the real orchestration (`analyze`'s
fan-in, `compare`'s structural diff, `test_hypotheses`'s hand-off to
Evidence & Verification, `estimate_impact`'s real exposure half) is
genuinely wired to real logic, not mocked away — and that the injected
`reason_fn` seam is exactly that: swappable, and honestly a
non-cognitive placeholder by default.
"""

import time
import uuid

import pytest

from components.c01_user_portfolio import Holding, PortfolioSnapshot, Position
from components.c05_retrieval_context import ContextPack
from components.c06_memory import DefaultMemoryManager, DefaultScopeRouter, Memory
from components.c07_event_observation import _EVENTS_TABLE, DefaultEventObservation
from components.c08_analysis_reasoning import (
    Analysis,
    DefaultAnalysisReasoning,
    Hypothesis,
    placeholder_reason_fn,
)
from components.c09_evidence_verification import DefaultMandatoryEvidenceGate


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol
    (src/infrastructure.py): store/retrieve/query only, the same
    semantics every other component's own tests already rely on —
    store() upserts by record["id"] (or a generated id), query() does
    containment matching (an empty filters dict matches every row in
    the table)."""

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
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


def _service(infra=None, reason_fn=None, mandatory_evidence_gate=None) -> DefaultAnalysisReasoning:
    infra = infra or _FakeInfrastructure()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    kwargs = {"reason_fn": reason_fn} if reason_fn is not None else {}
    if mandatory_evidence_gate is not None:
        kwargs["mandatory_evidence_gate"] = mandatory_evidence_gate
    return DefaultAnalysisReasoning(infrastructure=infra, memory_manager=memory_manager, **kwargs)


# --- placeholder_reason_fn ---------------------------------------------------


def test_placeholder_reason_fn_infer_is_low_confidence_and_labeled():
    output = placeholder_reason_fn({"phase": "infer", "premises": [{"x": 1}]})
    assert output["claim"] == "insufficient basis to hypothesize"
    assert output["basis"]["confidence"] == "low"
    assert output["basis"]["placeholder"] is True


@pytest.mark.parametrize(
    "phase,key",
    [
        ("generate_hypotheses", "hypotheses"),
        ("generate_counterarguments", "counterarguments"),
    ],
)
def test_placeholder_reason_fn_list_phases_return_empty(phase, key):
    output = placeholder_reason_fn({"phase": phase})
    assert output[key] == []


def test_placeholder_reason_fn_generate_explanations_returns_empty_string():
    assert placeholder_reason_fn({"phase": "generate_explanations"})["explanation"] == ""


def test_placeholder_reason_fn_synthesize_findings_returns_empty_analysis_shape():
    output = placeholder_reason_fn({"phase": "synthesize_findings"})
    assert output == {"hypotheses": [], "impact_estimate": None}


def test_placeholder_reason_fn_estimate_impact_reports_unknown_significance():
    output = placeholder_reason_fn({"phase": "estimate_impact"})
    assert output["significance"] == "unknown"
    assert "insufficient basis" in output["rationale"]


def test_placeholder_reason_fn_unknown_phase_raises():
    with pytest.raises(ValueError):
        placeholder_reason_fn({"phase": "not_a_real_phase"})


# --- compare: real structural diff ------------------------------------------


def test_compare_finds_only_in_a_only_in_b_and_changed():
    service = _service()
    result = service.compare(
        {"metric": "eps", "result": "beat", "only_a": 1},
        {"metric": "eps", "result": "missed", "only_b": 2},
    )
    assert result["only_in_a"] == {"only_a": 1}
    assert result["only_in_b"] == {"only_b": 2}
    assert result["changed"] == {"result": {"a": "beat", "b": "missed"}}


def test_compare_identical_dicts_produces_no_diff():
    service = _service()
    result = service.compare({"a": 1}, {"a": 1})
    assert result == {"only_in_a": {}, "only_in_b": {}, "changed": {}}


# --- infer / generate_hypotheses / generate_explanations / generate_counterarguments: reason_fn seam ---


def test_infer_calls_reason_fn_and_wraps_result_in_hypothesis():
    def fake_reason_fn(request):
        assert request["phase"] == "infer"
        assert request["premises"] == [{"signal": "revenue_up"}]
        return {"claim": "revenue growth implies price increase", "basis": {"confidence": "high"}}

    service = _service(reason_fn=fake_reason_fn)
    hypothesis = service.infer([{"signal": "revenue_up"}])
    assert hypothesis == Hypothesis(claim="revenue growth implies price increase", basis={"confidence": "high"})


def test_infer_with_default_placeholder_is_honestly_low_confidence():
    service = _service(reason_fn=placeholder_reason_fn)
    hypothesis = service.infer([{"signal": "revenue_up"}])
    assert hypothesis.claim == "insufficient basis to hypothesize"


def test_generate_hypotheses_converts_reason_fn_list_into_hypothesis_objects():
    def fake_reason_fn(request):
        assert request["phase"] == "generate_hypotheses"
        return {
            "hypotheses": [
                {"claim": "AAPL will beat earnings", "basis": {"entity_id": "AAPL"}},
                {"claim": "AAPL will miss earnings", "basis": {"entity_id": "AAPL"}},
            ]
        }

    service = _service(reason_fn=fake_reason_fn)
    hypotheses = service.generate_hypotheses({"event": {}})
    assert hypotheses == [
        Hypothesis(claim="AAPL will beat earnings", basis={"entity_id": "AAPL"}),
        Hypothesis(claim="AAPL will miss earnings", basis={"entity_id": "AAPL"}),
    ]


def test_generate_hypotheses_with_default_placeholder_returns_empty_list():
    service = _service(reason_fn=placeholder_reason_fn)
    assert service.generate_hypotheses({"event": {}}) == []


def test_generate_explanations_returns_reason_fn_explanation():
    service = _service(reason_fn=lambda request: {"explanation": "because revenue beat guidance"})
    hypothesis = Hypothesis(claim="AAPL will rally", basis={})
    assert service.generate_explanations(hypothesis) == "because revenue beat guidance"


def test_generate_counterarguments_returns_reason_fn_list():
    service = _service(reason_fn=lambda request: {"counterarguments": ["market already priced this in"]})
    hypothesis = Hypothesis(claim="AAPL will rally", basis={})
    assert service.generate_counterarguments(hypothesis) == ["market already priced this in"]


# --- test_hypotheses: real hand-off to Evidence & Verification (09), ADR-0013 ---


def _admitted_memory(memory_manager: DefaultMemoryManager, content: dict, confidence: float = 0.9) -> Memory:
    memory = Memory(id=str(uuid.uuid4()), content=content, scope="user", confidence=confidence)
    memory_manager.admit(memory)
    return memory


def test_test_hypotheses_verifies_a_hypothesis_with_real_linkable_evidence():
    infra = _FakeInfrastructure()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    _admitted_memory(memory_manager, {"text": "AAPL revenue beat guidance this quarter"})
    service = DefaultAnalysisReasoning(infrastructure=infra, memory_manager=memory_manager)

    hypothesis = Hypothesis(claim="AAPL revenue beat guidance", basis={"entity_id": "AAPL"})
    tested = service.test_hypotheses([hypothesis])

    assert len(tested) == 1
    result = tested[0]
    assert result.claim == "AAPL revenue beat guidance"
    assert result.basis["entity_id"] == "AAPL"  # original basis preserved, not replaced
    assert result.basis["verified"] is True
    assert result.basis["evidence_count"] >= 1
    assert 0.0 < result.basis["confidence"] <= 1.0
    assert result.basis["was_contradictory"] is False


def test_test_hypotheses_blocks_a_hypothesis_with_no_linkable_evidence_per_adr_0013():
    """A hypothesis whose claim shares no real evidence with anything in
    Memory or Retrieval & Context must be blocked and dropped, not
    silently passed through — ADR-0013's mandatory-evidence rule,
    exercised for real against DefaultMandatoryEvidenceGate/
    DefaultEvidenceLinker, not mocked."""
    infra = _FakeInfrastructure()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    # Deliberately unrelated memory content — no token overlap with the
    # hypothesis claim below, so DefaultEvidenceLinker finds nothing.
    _admitted_memory(memory_manager, {"text": "unrelated housekeeping note about login timestamps"})
    spy_audit_manager = _SpyAuditManager()
    gate = DefaultMandatoryEvidenceGate(audit_manager=spy_audit_manager)
    service = DefaultAnalysisReasoning(
        infrastructure=infra, memory_manager=memory_manager, mandatory_evidence_gate=gate
    )

    hypothesis = Hypothesis(claim="a completely unsupported speculative claim about xyzcorp", basis={})
    tested = service.test_hypotheses([hypothesis])

    assert tested == []  # blocked, not forwarded — never silently passed
    blocked_events = [event for event in spy_audit_manager.events if event[0] == "claim_blocked"]
    assert len(blocked_events) == 1
    assert blocked_events[0][1]["claim_text"] == hypothesis.claim


def test_test_hypotheses_mixed_batch_keeps_evidenced_and_drops_unevidenced():
    infra = _FakeInfrastructure()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    _admitted_memory(memory_manager, {"text": "TSLA delivery numbers exceeded analyst expectations"})
    service = DefaultAnalysisReasoning(infrastructure=infra, memory_manager=memory_manager)

    evidenced = Hypothesis(claim="TSLA delivery numbers exceeded analyst expectations", basis={})
    unevidenced = Hypothesis(claim="a totally unrelated speculative claim about zzzcorp financing", basis={})
    tested = service.test_hypotheses([evidenced, unevidenced])

    assert len(tested) == 1
    assert tested[0].claim == evidenced.claim
    assert tested[0].basis["verified"] is True


def test_test_hypotheses_empty_input_returns_empty_list():
    service = _service()
    assert service.test_hypotheses([]) == []


# --- estimate_impact: real exposure half + reason_fn judgment half ---------


def _snapshot_with_aapl_position(market_value: float = 100.0) -> PortfolioSnapshot:
    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10.0)
    return PortfolioSnapshot(portfolio_id="pf-1", positions=[Position(holding=holding, market_value=market_value)], exposure={})


def test_estimate_impact_computes_real_exposure_and_merges_reason_fn_judgment():
    service = _service(reason_fn=lambda request: {"significance": "high", "rationale": "large earnings surprise"})
    snapshot = _snapshot_with_aapl_position(market_value=100.0)
    hypothesis = Hypothesis(claim="AAPL earnings surprise", basis={"entity_id": "AAPL", "portfolio_snapshot": snapshot})

    result = service.estimate_impact(hypothesis)

    assert result["exposure"]["market_value"] == 100.0
    assert result["exposure"]["weight"] == 1.0
    assert result["significance"] == "high"
    assert result["rationale"] == "large earnings surprise"


def test_estimate_impact_without_snapshot_reports_no_exposure_but_still_gets_judgment():
    service = _service(reason_fn=lambda request: {"significance": "unknown", "rationale": "no exposure data"})
    hypothesis = Hypothesis(claim="AAPL earnings surprise", basis={"entity_id": "AAPL"})

    result = service.estimate_impact(hypothesis)

    assert result["exposure"] == {}
    assert result["significance"] == "unknown"


def test_estimate_impact_default_placeholder_is_honest_about_significance():
    service = _service(reason_fn=placeholder_reason_fn)
    hypothesis = Hypothesis(claim="AAPL earnings surprise", basis={})
    result = service.estimate_impact(hypothesis)
    assert result["significance"] == "unknown"


# --- synthesize_findings: reason_fn judgment + real persistence via Infrastructure ---


def test_synthesize_findings_persists_the_resulting_analysis_via_infrastructure():
    infra = _FakeInfrastructure()
    synthesized = [Hypothesis(claim="kept hypothesis", basis={"confidence": 0.7})]

    def fake_reason_fn(request):
        assert request["phase"] == "synthesize_findings"
        return {"hypotheses": synthesized, "impact_estimate": {"significance": "medium"}}

    service = _service(infra=infra, reason_fn=fake_reason_fn)
    analysis = service.synthesize_findings([Hypothesis(claim="input hypothesis", basis={})])

    assert analysis == Analysis(hypotheses=synthesized, impact_estimate={"significance": "medium"})
    stored = [record for (table, _), record in infra._records.items() if table == "analyses"]
    assert len(stored) == 1
    assert stored[0]["hypotheses"] == [{"claim": "kept hypothesis", "basis": {"confidence": 0.7}}]
    assert stored[0]["impact_estimate"] == {"significance": "medium"}


def test_synthesize_findings_default_placeholder_returns_empty_analysis():
    service = _service(reason_fn=placeholder_reason_fn)
    analysis = service.synthesize_findings([Hypothesis(claim="x", basis={})])
    assert analysis == Analysis(hypotheses=[], impact_estimate=None)


# --- analyze: real fan-in orchestration (07/06/05/01) + cognitive seam -----


def test_analyze_gathers_real_correlated_events_memories_context_and_exposure():
    """Proves analyze()'s own input-gathering is real orchestration, not
    a pass-through: an Event actually stored via Event & Observation's
    own table, a Memory actually admitted via DefaultMemoryManager, and
    a PortfolioSnapshot's real exposure are all visible to
    generate_hypotheses' request — captured here via a reason_fn spy
    rather than asserted through a private-method return value alone."""
    infra = _FakeInfrastructure()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    _admitted_memory(memory_manager, {"text": "AAPL guidance raised for next quarter"})

    infra.store(
        _EVENTS_TABLE,
        {
            "id": "event-1",
            "type": "earnings",
            "entity_ids": ["AAPL"],
            "metric": "eps",
            "magnitude": 0.12,
            "detected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )

    snapshot = _snapshot_with_aapl_position(market_value=50.0)
    captured_inputs = []

    def spy_reason_fn(request):
        if request["phase"] == "generate_hypotheses":
            captured_inputs.append(request["analysis_input"])
            return {"hypotheses": []}
        if request["phase"] == "synthesize_findings":
            return {"hypotheses": [], "impact_estimate": None}
        raise AssertionError(f"unexpected phase {request['phase']!r}")

    service = DefaultAnalysisReasoning(infrastructure=infra, memory_manager=memory_manager, reason_fn=spy_reason_fn)
    event = {"entity_ids": ["AAPL"]}
    context = {"documents": [{"content": "AAPL guidance raised", "reliability": 1.0}], "query_text": "AAPL guidance", "portfolio_snapshot": snapshot}

    analysis = service.analyze(event, context, memory={})

    assert analysis == Analysis(hypotheses=[], impact_estimate=None)
    assert len(captured_inputs) == 1
    gathered = captured_inputs[0]
    assert [correlated.id for correlated in gathered["correlated_events"]] == ["event-1"]
    assert len(gathered["memories"]) == 1
    assert gathered["memories"][0].content == {"text": "AAPL guidance raised for next quarter"}
    assert isinstance(gathered["context_pack"], ContextPack)
    assert len(gathered["context_pack"].documents) == 1
    assert gathered["exposure"] == {"AAPL": {"market_value": 50.0, "weight": 1.0}}


def test_analyze_without_portfolio_snapshot_reports_empty_exposure():
    infra = _FakeInfrastructure()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    captured = []

    def spy_reason_fn(request):
        if request["phase"] == "generate_hypotheses":
            captured.append(request["analysis_input"])
            return {"hypotheses": []}
        return {"hypotheses": [], "impact_estimate": None}

    service = DefaultAnalysisReasoning(infrastructure=infra, memory_manager=memory_manager, reason_fn=spy_reason_fn)
    service.analyze({"entity_ids": []}, {}, memory={})

    assert captured[0]["exposure"] == {}
    assert captured[0]["correlated_events"] == []


def test_analyze_end_to_end_with_default_placeholder_is_honestly_empty():
    """With the default placeholder reason_fn, analyze() runs the full
    real pipeline end to end without error, but — honestly, per
    ADR-0037/ADR-0021 — produces no real hypotheses, since nothing
    cognitive backs generate_hypotheses/synthesize_findings yet."""
    service = _service(reason_fn=placeholder_reason_fn)
    analysis = service.analyze({"entity_ids": ["AAPL"]}, {}, memory={})
    assert analysis == Analysis(hypotheses=[], impact_estimate=None)
