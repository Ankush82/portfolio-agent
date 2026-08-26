"""Tests for DefaultLearningEvaluation (src/components/c14_learning_evaluation.py,
ADR-0041).

Uses an in-memory Infrastructure test double (same containment-match
semantics as every other component's own fake) rather than a live
Postgres/Redis connection, paired with real DefaultDataSources
(component 02) and real Memory adapters (component 06,
DefaultMemoryEvaluator/DefaultEntityLinker/DefaultQuarantineGate/
DefaultMemoryManager) — the point of these tests is to prove the real
measurement/comparison/categorization logic ADR-0041 designed, and
that update_knowledge genuinely drives Memory's write path end to end,
not to mock the loop-closing behavior away.
"""

import json
import uuid

import pytest

import alpha_vantage_client
from components.c02_data_sources import (
    DefaultDataSources,
    Source,
    SourceDocument,
    SourceType,
)
from components.c06_memory import (
    QUARANTINE_TABLE,
    SHARED_MEMORY_TABLE,
    DefaultEntityLinker,
    DefaultMemoryEvaluator,
    DefaultMemoryManager,
    DefaultQuarantineGate,
    DefaultScopeRouter,
)
from components.c14_learning_evaluation import (
    DefaultLearningEvaluation,
    Evaluation,
    Outcome,
    Prediction,
    StubLearningEvaluation,
    _epoch,
)


@pytest.fixture(autouse=True)
def _no_alpha_vantage_key_by_default(monkeypatch):
    """This file's `_learning_evaluation()` helper constructs a bare
    `DefaultDataSources()`, which resolves its `SourceFetcher` via
    `get_source_fetcher()` (ADR-0046) — real `ALPHA_VANTAGE_API_KEY`
    configured in this repo's own `.env` for actual use elsewhere would
    otherwise make `test_measure_outcome_under_placeholder_fetcher_is_
    honestly_not_comparable` (which specifically tests the placeholder
    path) machine-dependent instead of deterministic. Same isolation
    `tests/components/test_data_sources.py` and `tests/test_llm.py`
    already use for their own env-gated seams."""
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.setattr(
        alpha_vantage_client, "_ENV_FILE_PATH", alpha_vantage_client._ENV_FILE_PATH.parent / "does-not-exist.env"
    )


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double — same semantics as every
    other component's own fake: store() upserts by record["id"] (or a
    generated id), retrieve() looks up by id, query() does containment
    matching."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self._next_id = 0

    def store(self, table: str, record: dict) -> str:
        self._next_id += 1
        record_id = str(record["id"]) if record.get("id") else f"generated-{self._next_id}"
        self._tables.setdefault(table, {})[record_id] = dict(record, id=record_id)
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        return self._tables.get(table, {}).get(id_)

    def query(self, table: str, filters: dict) -> list[dict]:
        return [
            record
            for record in self._tables.get(table, {}).values()
            if all(record.get(key) == value for key, value in filters.items())
        ]


class _SpyAuditManager:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


class _StaticSourceFetcher:
    """Test double SourceFetcher (same shape as test_data_sources.py's
    own): always returns the same content, so measure_outcome can be
    exercised against a genuinely non-synthetic fetch."""

    def __init__(self, content: bytes, fetched_at: str = "2026-08-26T00:00:00") -> None:
        self._content = content
        self._fetched_at = fetched_at

    def fetch(self, source: Source) -> SourceDocument:
        return SourceDocument(source_id=source.id, content=self._content, fetched_at=self._fetched_at)


def _prediction(**kwargs) -> Prediction:
    defaults = dict(
        claim="AAPL price will rise",
        confidence=0.8,
        id="pred-1",
        entity_id="AAPL",
        metric="price",
        reference_value=100.0,
        predicted_value=110.0,
        source_ids=["evidence-1"],
    )
    defaults.update(kwargs)
    return Prediction(**defaults)


def _outcome(prediction: Prediction, **actual_overrides) -> Outcome:
    actual = {
        "entity_id": prediction.entity_id,
        "metric": prediction.metric,
        "actual_value": 112.0,
        "reliability": 1.0,
        "synthetic": False,
        "fetched_at": "2026-08-26T01:00:00",
    }
    actual.update(actual_overrides)
    return Outcome(prediction=prediction, actual=actual, id="outcome-1", measured_at="2026-08-26T01:00:00")


def _learning_evaluation(infra=None, data_sources=None, audit_manager=None, **kwargs) -> DefaultLearningEvaluation:
    infra = infra or _InMemoryInfrastructure()
    return DefaultLearningEvaluation(
        infrastructure=infra,
        data_sources=data_sources,
        audit_manager=audit_manager or _SpyAuditManager(),
        **kwargs,
    )


# --- StubLearningEvaluation untouched ---------------------------------------


def test_stub_learning_evaluation_untouched():
    stub = StubLearningEvaluation()
    evaluation = stub.evaluate(Prediction(claim="x", confidence=0.0), Outcome(prediction=Prediction(claim="x", confidence=0.0), actual={}))
    assert evaluation.correct is True
    assert stub.replay("anything") == []
    assert stub.detect_regression(evaluation, evaluation) is True


# --- measure_outcome ---------------------------------------------------------


def test_measure_outcome_under_placeholder_fetcher_is_honestly_not_comparable():
    """ADR-0027's inherited gap: with only PlaceholderSourceFetcher
    wired in (the DefaultDataSources default), no real numeric value
    can ever be derived."""
    learning_evaluation = _learning_evaluation()
    prediction = _prediction()

    outcome = learning_evaluation.measure_outcome(prediction)

    assert outcome.actual["synthetic"] is True
    assert outcome.actual["actual_value"] is None
    assert 0.0 < outcome.actual["reliability"] < 1.0
    assert outcome.prediction is prediction


def test_measure_outcome_extracts_real_numeric_value_from_genuine_fetch():
    infra = _InMemoryInfrastructure()
    content = json.dumps({"price": 123.45}).encode("utf-8")
    data_sources = DefaultDataSources(infrastructure=infra, source_fetcher=_StaticSourceFetcher(content))
    learning_evaluation = _learning_evaluation(infra=infra, data_sources=data_sources)
    prediction = _prediction()

    outcome = learning_evaluation.measure_outcome(prediction)

    assert outcome.actual["synthetic"] is False
    assert outcome.actual["actual_value"] == pytest.approx(123.45)
    assert outcome.actual["reliability"] == 1.0


def test_measure_outcome_falls_back_to_market_data_for_unrecognized_source_type():
    infra = _InMemoryInfrastructure()
    content = json.dumps({"value": 5.0}).encode("utf-8")
    data_sources = DefaultDataSources(infrastructure=infra, source_fetcher=_StaticSourceFetcher(content))
    learning_evaluation = _learning_evaluation(infra=infra, data_sources=data_sources)
    prediction = _prediction(source_type="NOT_A_REAL_TYPE")

    outcome = learning_evaluation.measure_outcome(prediction)

    assert outcome.actual["actual_value"] == 5.0  # ran fine — no crash on the bad source_type


def test_measure_outcome_generic_value_key_used_when_metric_name_absent():
    infra = _InMemoryInfrastructure()
    content = json.dumps({"value": 42.0}).encode("utf-8")
    data_sources = DefaultDataSources(infrastructure=infra, source_fetcher=_StaticSourceFetcher(content))
    learning_evaluation = _learning_evaluation(infra=infra, data_sources=data_sources)
    prediction = _prediction(metric="not_present_in_document")

    outcome = learning_evaluation.measure_outcome(prediction)

    assert outcome.actual["actual_value"] == 42.0


# --- compare_prediction_vs_outcome -------------------------------------------


def test_compare_not_comparable_when_actual_value_missing():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction()
    outcome = _outcome(prediction, actual_value=None)

    comparison = learning_evaluation.compare_prediction_vs_outcome(prediction, outcome)

    assert comparison == {"comparable": False, "predicted_value": 110.0, "actual_value": None}


def test_compare_not_comparable_when_reference_value_missing():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=None)
    outcome = _outcome(prediction)

    comparison = learning_evaluation.compare_prediction_vs_outcome(prediction, outcome)

    assert comparison["comparable"] is False


def test_compare_direction_correct_and_within_tolerance():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=100.0, predicted_value=110.0)
    outcome = _outcome(prediction, actual_value=112.0)  # up, within 10% of predicted

    comparison = learning_evaluation.compare_prediction_vs_outcome(prediction, outcome)

    assert comparison["comparable"] is True
    assert comparison["predicted_direction"] == "up"
    assert comparison["actual_direction"] == "up"
    assert comparison["direction_correct"] is True
    assert comparison["magnitude_within_tolerance"] is True


def test_compare_direction_wrong():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=100.0, predicted_value=110.0)  # predicted "up"
    outcome = _outcome(prediction, actual_value=90.0)  # actually went "down"

    comparison = learning_evaluation.compare_prediction_vs_outcome(prediction, outcome)

    assert comparison["direction_correct"] is False
    assert comparison["actual_direction"] == "down"


def test_compare_direction_right_but_magnitude_outside_tolerance():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=100.0, predicted_value=110.0)  # predicts 110
    outcome = _outcome(prediction, actual_value=150.0)  # really went up, but far past 10% tolerance

    comparison = learning_evaluation.compare_prediction_vs_outcome(prediction, outcome)

    assert comparison["direction_correct"] is True
    assert comparison["magnitude_within_tolerance"] is False


def test_compare_handles_zero_actual_value_without_dividing_by_zero():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=10.0, predicted_value=10.0)
    outcome = _outcome(prediction, actual_value=0.0)

    comparison = learning_evaluation.compare_prediction_vs_outcome(prediction, outcome)

    assert comparison["relative_error"] == float("inf")


# --- evaluate ------------------------------------------------------------


def test_evaluate_correct_when_comparable_direction_right_and_within_tolerance():
    infra = _InMemoryInfrastructure()
    learning_evaluation = _learning_evaluation(infra=infra)
    prediction = _prediction()
    outcome = _outcome(prediction, actual_value=112.0)

    evaluation = learning_evaluation.evaluate(prediction, outcome)

    assert evaluation.correct is True
    assert evaluation.error is None
    assert evaluation.id
    assert evaluation.evaluated_at
    stored = infra.retrieve("learning_evaluations", evaluation.id)
    assert stored is not None
    assert stored["correct"] is True


def test_evaluate_incorrect_populates_error_with_real_comparison():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction()
    outcome = _outcome(prediction, actual_value=90.0)  # wrong direction

    evaluation = learning_evaluation.evaluate(prediction, outcome)

    assert evaluation.correct is False
    assert evaluation.error["direction_correct"] is False


def test_evaluate_not_comparable_is_incorrect_not_fabricated_success():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction()
    outcome = _outcome(prediction, actual_value=None, synthetic=True)

    evaluation = learning_evaluation.evaluate(prediction, outcome)

    assert evaluation.correct is False
    assert evaluation.error["comparable"] is False


# --- analyze_errors --------------------------------------------------------


def test_analyze_errors_correct_evaluation_has_no_category():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction()
    evaluation = Evaluation(outcome=_outcome(prediction, actual_value=112.0), correct=True, error=None, id="e1")

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "none"


def test_analyze_errors_insufficient_evidence_when_no_source_ids():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(source_ids=[])
    outcome = _outcome(prediction, actual_value=None, synthetic=True)
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "insufficient_evidence"


def test_analyze_errors_stale_data_when_synthetic_source():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(source_ids=["evidence-1"])
    outcome = _outcome(prediction, actual_value=None, synthetic=True)
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "stale_data"


def test_analyze_errors_unverifiable_source_data_when_real_fetch_but_unparseable():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(source_ids=["evidence-1"])
    outcome = _outcome(prediction, actual_value=None, synthetic=False)
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "unverifiable_source_data"


def test_analyze_errors_wrong_entity_resolution():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(entity_id="AAPL")
    outcome = _outcome(prediction, actual_value=112.0, entity_id="MSFT")
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "wrong_entity_resolution"


def test_analyze_errors_direction_miss():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=100.0, predicted_value=110.0)
    outcome = _outcome(prediction, actual_value=90.0)
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "direction_miss"


def test_analyze_errors_magnitude_miss():
    learning_evaluation = _learning_evaluation()
    prediction = _prediction(reference_value=100.0, predicted_value=110.0)
    outcome = _outcome(prediction, actual_value=150.0)
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    result = learning_evaluation.analyze_errors(evaluation)

    assert result["category"] == "magnitude_miss"


# --- collect_feedback --------------------------------------------------------


def test_collect_feedback_first_call_is_pending_and_persists_and_audits():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    learning_evaluation = _learning_evaluation(infra=infra, audit_manager=audit)
    evaluation = Evaluation(outcome=_outcome(_prediction()), correct=True, id="eval-fb-1")

    feedback = learning_evaluation.collect_feedback(evaluation)

    assert feedback == {"status": "pending", "evaluation_id": "eval-fb-1"}
    stored = infra.retrieve("learning_evaluation_feedback", "eval-fb-1")
    assert stored["status"] == "pending_feedback"
    assert ("evaluation_feedback_requested", {"evaluation_id": "eval-fb-1"}) in audit.events


def test_collect_feedback_is_idempotent_no_duplicate_row_or_audit_event():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    learning_evaluation = _learning_evaluation(infra=infra, audit_manager=audit)
    evaluation = Evaluation(outcome=_outcome(_prediction()), correct=True, id="eval-fb-2")

    learning_evaluation.collect_feedback(evaluation)
    learning_evaluation.collect_feedback(evaluation)

    assert len(audit.events) == 1


def test_collect_feedback_returns_real_feedback_once_externally_written():
    infra = _InMemoryInfrastructure()
    learning_evaluation = _learning_evaluation(infra=infra)
    evaluation = Evaluation(outcome=_outcome(_prediction()), correct=True, id="eval-fb-3")
    learning_evaluation.collect_feedback(evaluation)

    infra.store(
        "learning_evaluation_feedback",
        {"id": "eval-fb-3", "evaluation_id": "eval-fb-3", "status": "answered", "feedback": {"useful": True}},
    )

    feedback = learning_evaluation.collect_feedback(evaluation)

    assert feedback == {"useful": True}


# --- update_knowledge: the loop-closing write path --------------------------


def _real_memory_dependencies(infra):
    return dict(
        memory_manager=DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter()),
        memory_evaluator=DefaultMemoryEvaluator(min_confidence=0.3),
        entity_linker=DefaultEntityLinker(),
        quarantine_gate=DefaultQuarantineGate(infrastructure=infra),
    )


def test_update_knowledge_below_confidence_threshold_is_skipped_not_written():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    learning_evaluation = _learning_evaluation(
        infra=infra, audit_manager=audit, **_real_memory_dependencies(infra)
    )
    prediction = _prediction(confidence=0.05)  # below DefaultMemoryEvaluator's 0.3 default
    outcome = _outcome(prediction, actual_value=112.0, synthetic=False)
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    learning_evaluation.update_knowledge(evaluation)

    assert infra.query(SHARED_MEMORY_TABLE, {}) == []
    assert infra.query(QUARANTINE_TABLE, {}) == []
    assert any(event_type == "knowledge_update_skipped" for event_type, _ in audit.events)


def test_update_knowledge_genuinely_grounded_evaluation_admits_a_retrievable_memory():
    """The real point of this component (ADR-0041): a comparable,
    non-synthetic outcome closes the loop for real — the resulting
    Memory is genuinely retrievable via DefaultMemoryManager, not just
    written and forgotten."""
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=DefaultScopeRouter())
    learning_evaluation = _learning_evaluation(
        infra=infra,
        audit_manager=audit,
        memory_manager=memory_manager,
        memory_evaluator=DefaultMemoryEvaluator(min_confidence=0.3),
        entity_linker=DefaultEntityLinker(),
        quarantine_gate=DefaultQuarantineGate(infrastructure=infra),
    )
    prediction = _prediction(confidence=0.9)
    outcome = _outcome(prediction, actual_value=112.0, synthetic=False)  # genuinely fetched, non-synthetic
    evaluation = learning_evaluation.evaluate(prediction, outcome)
    assert evaluation.correct is True  # sanity: this is the real admit-eligible case

    learning_evaluation.update_knowledge(evaluation)

    expected_memory_id = f"learning-evaluation-{evaluation.id}"
    retrieved = memory_manager.retrieve({}, scope="shared")
    assert any(memory.id == expected_memory_id for memory in retrieved)
    stored_directly = infra.retrieve(SHARED_MEMORY_TABLE, expected_memory_id)
    assert stored_directly is not None
    assert stored_directly["quarantined"] is False
    assert infra.query(QUARANTINE_TABLE, {}) == []  # never quarantined — admitted directly
    assert any(event_type == "knowledge_updated" and detail.get("admitted") is True for event_type, detail in audit.events)


def test_update_knowledge_unverified_evaluation_quarantines_not_admits():
    """Under PlaceholderSourceFetcher (ADR-0027's inherited gap), every
    real evaluation is synthetic-derived and honestly quarantines
    rather than admitting directly (ADR-0041's documented consequence)."""
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    learning_evaluation = _learning_evaluation(
        infra=infra, audit_manager=audit, **_real_memory_dependencies(infra)
    )
    prediction = _prediction(confidence=0.9)
    outcome = learning_evaluation.measure_outcome(prediction)  # real placeholder-backed measurement
    evaluation = learning_evaluation.evaluate(prediction, outcome)

    learning_evaluation.update_knowledge(evaluation)

    expected_memory_id = f"learning-evaluation-{evaluation.id}"
    assert infra.retrieve(SHARED_MEMORY_TABLE, expected_memory_id) is None
    quarantined = infra.query(QUARANTINE_TABLE, {})
    assert len(quarantined) == 1
    assert quarantined[0]["status"] == "pending"
    assert any(event_type == "knowledge_updated" and detail.get("admitted") is False for event_type, detail in audit.events)


# --- detect_regression --------------------------------------------------------


def _evaluation(correct: bool, relative_error: float | None = None, evaluation_id: str = "e") -> Evaluation:
    error = None if correct else {"comparable": relative_error is not None, "relative_error": relative_error}
    return Evaluation(outcome=_outcome(_prediction()), correct=correct, error=error, id=evaluation_id)


def test_detect_regression_true_when_correct_flips_to_incorrect():
    learning_evaluation = _learning_evaluation()
    baseline = _evaluation(correct=True, evaluation_id="baseline")
    current = _evaluation(correct=False, relative_error=0.5, evaluation_id="current")

    assert learning_evaluation.detect_regression(current, baseline) is True


def test_detect_regression_false_when_both_correct():
    learning_evaluation = _learning_evaluation()
    baseline = _evaluation(correct=True, evaluation_id="baseline")
    current = _evaluation(correct=True, evaluation_id="current")

    assert learning_evaluation.detect_regression(current, baseline) is False


def test_detect_regression_false_on_improvement():
    learning_evaluation = _learning_evaluation()
    baseline = _evaluation(correct=False, relative_error=0.5, evaluation_id="baseline")
    current = _evaluation(correct=True, evaluation_id="current")

    assert learning_evaluation.detect_regression(current, baseline) is False


def test_detect_regression_true_when_both_incorrect_but_meaningfully_worse():
    learning_evaluation = _learning_evaluation()
    baseline = _evaluation(correct=False, relative_error=0.10, evaluation_id="baseline")
    current = _evaluation(correct=False, relative_error=0.20, evaluation_id="current")  # +10pts > 5pt margin

    assert learning_evaluation.detect_regression(current, baseline) is True


def test_detect_regression_false_when_both_incorrect_within_margin():
    learning_evaluation = _learning_evaluation()
    baseline = _evaluation(correct=False, relative_error=0.10, evaluation_id="baseline")
    current = _evaluation(correct=False, relative_error=0.12, evaluation_id="current")  # +2pts <= 5pt margin

    assert learning_evaluation.detect_regression(current, baseline) is False


def test_detect_regression_audits_only_when_regression_found():
    audit = _SpyAuditManager()
    learning_evaluation = _learning_evaluation(audit_manager=audit)
    baseline = _evaluation(correct=True, evaluation_id="baseline")
    current = _evaluation(correct=True, evaluation_id="current")

    learning_evaluation.detect_regression(current, baseline)
    assert audit.events == []

    regressive_current = _evaluation(correct=False, relative_error=0.5, evaluation_id="current-bad")
    learning_evaluation.detect_regression(regressive_current, baseline)
    assert len(audit.events) == 1
    assert audit.events[0][0] == "regression_detected"


# --- evaluate_versions --------------------------------------------------------


def _asdict_evaluation(correct: bool, relative_error: float | None = None) -> dict:
    error = None if correct else {"comparable": relative_error is not None, "relative_error": relative_error}
    return {"correct": correct, "error": error}


def test_evaluate_versions_reports_accuracy_and_better_version():
    learning_evaluation = _learning_evaluation()
    a = {"version": "v1", "evaluations": [_asdict_evaluation(True), _asdict_evaluation(False, 0.3)]}
    b = {"version": "v2", "evaluations": [_asdict_evaluation(True), _asdict_evaluation(True)]}

    result = learning_evaluation.evaluate_versions(a, b)

    assert result["a"]["accuracy"] == pytest.approx(0.5)
    assert result["b"]["accuracy"] == pytest.approx(1.0)
    assert result["accuracy_delta"] == pytest.approx(0.5)
    assert result["better_version"] == "v2"


def test_evaluate_versions_tie_when_accuracy_equal():
    learning_evaluation = _learning_evaluation()
    a = {"version": "v1", "evaluations": [_asdict_evaluation(True)]}
    b = {"version": "v2", "evaluations": [_asdict_evaluation(True)]}

    result = learning_evaluation.evaluate_versions(a, b)

    assert result["better_version"] == "tie"


def test_evaluate_versions_mean_relative_error_over_comparable_incorrect_only():
    learning_evaluation = _learning_evaluation()
    a = {
        "version": "v1",
        "evaluations": [_asdict_evaluation(False, 0.2), _asdict_evaluation(False, 0.4), _asdict_evaluation(True)],
    }
    b = {"version": "v2", "evaluations": []}

    result = learning_evaluation.evaluate_versions(a, b)

    assert result["a"]["mean_relative_error"] == pytest.approx(0.3)
    assert result["b"]["mean_relative_error"] is None


# --- replay --------------------------------------------------------------------


def test_replay_finds_and_orders_records_across_all_six_owning_tables():
    infra = _InMemoryInfrastructure()
    learning_evaluation = _learning_evaluation(infra=infra)
    trajectory_id = "AAPL"

    infra.store("observations", {"id": "obs-1", "entity_id": trajectory_id, "observed_at": "2026-08-26T09:00:00"})
    infra.store("events", {"id": "evt-1", "entity_ids": [trajectory_id, "MSFT"], "detected_at": "2026-08-26T09:05:00"})
    infra.store(
        "analyses",
        {
            "id": "an-1",
            "hypotheses": [{"claim": "x", "basis": {"entity_id": trajectory_id}}],
            "synthesized_at": "2026-08-26T09:10:00",
        },
    )
    infra.store(
        "decision_policy_notifications",
        {"id": "dpn-1", "identity": trajectory_id, "issued_at": 1798700000.0},
    )
    infra.store(
        "decision_policy_pending_approvals",
        {"id": "apr-1", "action": {"entity_id": trajectory_id}, "requested_at": "2026-08-26T09:15:00"},
    )
    infra.store("notification_alerts", {"id": "alert-1", "notification_id": "notif-1", "raised_at": "2026-08-26T09:20:00"})
    # notif-1 correlates via notification_id, not entity_id — real, honest limitation (ADR-0041)
    infra.store(
        "notifications_sent",
        {"id": "sent-1", "notification_id": "notif-1", "attempted_at": "2026-08-26T09:21:00"},
    )
    infra.store("interactions", {"id": "notif-1", "notification_id": "notif-1", "created_at": "2026-08-26T09:22:00"})
    # A non-matching record in every table to prove filtering is real, not "return everything".
    infra.store("observations", {"id": "obs-2", "entity_id": "UNRELATED", "observed_at": "2026-08-26T09:00:00"})

    entries = learning_evaluation.replay(trajectory_id)

    types_found = {entry["type"] for entry in entries}
    assert types_found == {"observation", "event", "analysis", "notification_issued", "pending_approval"}
    epochs = [_epoch(entry["at"]) for entry in entries]
    assert epochs == sorted(epochs)
    assert all(entry["record"]["id"] != "obs-2" for entry in entries)


def test_replay_finds_notification_records_by_notification_id():
    infra = _InMemoryInfrastructure()
    learning_evaluation = _learning_evaluation(infra=infra)

    infra.store("notification_alerts", {"id": "alert-1", "notification_id": "notif-42", "raised_at": "2026-08-26T09:20:00"})
    infra.store("notifications_sent", {"id": "sent-1", "notification_id": "notif-42", "attempted_at": "2026-08-26T09:21:00"})
    infra.store("interactions", {"id": "notif-42", "notification_id": "notif-42", "created_at": "2026-08-26T09:22:00"})

    entries = learning_evaluation.replay("notif-42")

    types_found = {entry["type"] for entry in entries}
    assert types_found == {"alert", "notification_sent", "interaction"}


def test_replay_returns_empty_list_when_nothing_matches():
    learning_evaluation = _learning_evaluation()

    assert learning_evaluation.replay(str(uuid.uuid4())) == []
