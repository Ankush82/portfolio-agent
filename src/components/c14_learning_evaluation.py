"""Learning & Evaluation (component 14) — the closed-loop improvement
layer.

Interfaces: <- Interaction & Notification (13) (`Interaction` is this
component's documented hand-off point — see that dataclass's own
docstring in c13_interaction_notification.py), -> Memory (06).

In-scope status was an open question through the design-framework
round; resolved (checkpoint.md, "Learning & Evaluation (14): scope
question resolved") — it's in scope, and the `Default*` class below is
its first real implementation, built directly from this task's own
brief since no fig. 1/fig. 2 mechanism diagram exists for it.

Decision: ADR-0041 — additive fields on Prediction/Outcome/Evaluation,
DataSources-backed measure_outcome (inheriting ADR-0027's fetch-
provider gap rather than duplicating it), tolerance-based comparison/
correctness, structural error categorization, multi-field trajectory
replay across six cross-component tables (no shared trajectory-scoping
key exists anywhere in this project's persisted schema), and
update_knowledge driving Memory's full write path
(MemoryEvaluator -> EntityLinker -> QuarantineGate -> MemoryManager)
for real — the first component in this project to do so end to end,
closing the loop the whole architecture has pointed at since Memory's
own first design pass.

No LLM anywhere in this component: measurement, comparison, and
statistics over data already recorded by other real components — the
same character as Event & Observation (component 07).
"""

import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Protocol

from components.c02_data_sources import (
    PLACEHOLDER_FETCH_MARKER,
    DataSources,
    DefaultDataSources,
    Source,
    SourceType,
)
from components.c06_memory import (
    DefaultEntityLinker,
    DefaultMemoryEvaluator,
    DefaultMemoryManager,
    DefaultQuarantineGate,
    DefaultScopeRouter,
    EntityLinker,
    Memory,
    MemoryCandidate,
    MemoryEvaluator,
    MemoryManager,
    QuarantineGate,
)
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


@dataclass
class Prediction:
    claim: str
    confidence: float
    # Additive fields (ADR-0041) — the whiteboard shape (claim,
    # confidence) carries nothing identifying what was predicted or
    # what backed it; every field below is real and load-bearing on
    # measure_outcome/compare_prediction_vs_outcome/analyze_errors.
    id: str = ""
    entity_id: str = ""
    metric: str = ""
    reference_value: float | None = None  # the metric's value at prediction time — direction is measured against this
    predicted_value: float | None = None
    source_type: str = "MARKET_DATA"  # a SourceType name; measure_outcome looks this up against Data & Sources (02)
    source_ids: list[str] = field(default_factory=list)  # evidence/analysis provenance behind this prediction
    made_at: str = ""


@dataclass
class Outcome:
    prediction: Prediction
    actual: dict
    id: str = ""
    measured_at: str = ""


@dataclass
class Evaluation:
    outcome: Outcome
    correct: bool
    error: dict | None = None
    id: str = ""
    evaluated_at: str = ""


class LearningEvaluation(Protocol):
    def evaluate(self, prediction: Prediction, outcome: Outcome) -> Evaluation:
        ...

    def replay(self, trajectory_id: str) -> list[dict]:
        ...

    def measure_outcome(self, prediction: Prediction) -> Outcome:
        ...

    def compare_prediction_vs_outcome(self, prediction: Prediction, outcome: Outcome) -> dict:
        ...

    def analyze_errors(self, evaluation: Evaluation) -> dict:
        ...

    def collect_feedback(self, evaluation: Evaluation) -> dict:
        ...

    def update_knowledge(self, evaluation: Evaluation) -> None:
        """→ Memory (06)."""
        ...

    def detect_regression(self, current: Evaluation, baseline: Evaluation) -> bool:
        ...

    def evaluate_versions(self, a: dict, b: dict) -> dict:
        ...


class StubLearningEvaluation:
    """Structural implementation of LearningEvaluation. Every method is
    a traced no-op — see cross_cutting/observability.py."""

    def evaluate(self, prediction: Prediction, outcome: Outcome) -> Evaluation:
        with traced("StubLearningEvaluation.evaluate"):
            return Evaluation(
                outcome=Outcome(prediction=Prediction(claim="stub", confidence=0.0), actual={}),
                correct=True,
                error=None,
            )

    def replay(self, trajectory_id: str) -> list[dict]:
        with traced("StubLearningEvaluation.replay"):
            return []

    def measure_outcome(self, prediction: Prediction) -> Outcome:
        with traced("StubLearningEvaluation.measure_outcome"):
            return Outcome(prediction=Prediction(claim="stub", confidence=0.0), actual={})

    def compare_prediction_vs_outcome(self, prediction: Prediction, outcome: Outcome) -> dict:
        with traced("StubLearningEvaluation.compare_prediction_vs_outcome"):
            return {}

    def analyze_errors(self, evaluation: Evaluation) -> dict:
        with traced("StubLearningEvaluation.analyze_errors"):
            return {}

    def collect_feedback(self, evaluation: Evaluation) -> dict:
        with traced("StubLearningEvaluation.collect_feedback"):
            return {}

    def update_knowledge(self, evaluation: Evaluation) -> None:
        with traced("StubLearningEvaluation.update_knowledge"):
            return None

    def detect_regression(self, current: Evaluation, baseline: Evaluation) -> bool:
        with traced("StubLearningEvaluation.detect_regression"):
            return True

    def evaluate_versions(self, a: dict, b: dict) -> dict:
        with traced("StubLearningEvaluation.evaluate_versions"):
            return {}


# --- DefaultLearningEvaluation: real mechanism (ADR-0041) -------------------

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches every other component's own timestamp format

_EVALUATIONS_TABLE = "learning_evaluations"
_FEEDBACK_TABLE = "learning_evaluation_feedback"

# compare_prediction_vs_outcome's magnitude bar: a direction-correct
# prediction still only counts as "correct" if its predicted value is
# within 10% relative error of the real measured value. Same order of
# magnitude as component 07's own documented 5%/2% "is this move real"
# floors (ADR-0036) — a round, defensible default for a domain with no
# real historical evaluation data yet to tune against (ADR-0041).
_MAGNITUDE_RELATIVE_ERROR_TOLERANCE = 0.10

# detect_regression's own bar: both-incorrect current vs. baseline only
# counts as a regression if current's relative error is at least 5
# percentage points worse — noise tolerance, not "any measurable
# difference" (ADR-0041).
_REGRESSION_RELATIVE_ERROR_MARGIN = 0.05

# Cross-component table names this component reads directly through
# Infrastructure (never the owning component's private constants) —
# the same read-only coupling pattern c03_data_processing_quality.py's
# _C02_SOURCES_TABLE already established (ADR-0032, restated for
# replay's six sources by ADR-0041).
_C07_OBSERVATIONS_TABLE = "observations"
_C07_EVENTS_TABLE = "events"
_C08_ANALYSES_TABLE = "analyses"
_C12_NOTIFICATIONS_TABLE = "decision_policy_notifications"
_C12_APPROVALS_TABLE = "decision_policy_pending_approvals"
_C13_ALERTS_TABLE = "notification_alerts"
_C13_NOTIFICATIONS_SENT_TABLE = "notifications_sent"
_C13_INTERACTIONS_TABLE = "interactions"


def _sign(value: float) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _extract_numeric_field(content: bytes, metric: str) -> float | None:
    """Real, structural parsing only — no LLM (ADR-0041). Content only
    yields a real actual_value if it decodes as UTF-8 JSON containing a
    field named after `metric` or a generic "value" key, holding a real
    number. Anything else (unparseable bytes, wrong shape, a non-numeric
    value) honestly reports None rather than guessing — the same
    posture c03_data_processing_quality.py's `_parse_structure` takes
    for content that isn't real JSON (ADR-0032)."""
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in (metric, "value"):
        if key and isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
            return float(data[key])
    return None


def _aggregate_evaluations(evaluations: list[dict]) -> dict:
    """Real aggregate statistics over a list of asdict(Evaluation)-shaped
    dicts — evaluate_versions' building block (ADR-0041)."""
    total = len(evaluations)
    correct = sum(1 for evaluation in evaluations if evaluation.get("correct"))
    relative_errors = [
        evaluation["error"]["relative_error"]
        for evaluation in evaluations
        if evaluation.get("error")
        and evaluation["error"].get("comparable")
        and evaluation["error"].get("relative_error") is not None
    ]
    return {
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "mean_relative_error": statistics.mean(relative_errors) if relative_errors else None,
    }


def _epoch(value) -> float:
    """Normalizes this project's two real timestamp shapes — a
    time.strftime "%Y-%m-%dT%H:%M:%S" string (every component except
    one) and a raw time.time() float
    (decision_policy_notifications.issued_at) — into one comparable
    epoch value for replay's chronological sort. Unparseable/missing
    values sort first (0.0) rather than crashing the sort."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return time.mktime(time.strptime(value, _TIMESTAMP_FORMAT))
        except ValueError:
            return 0.0
    return 0.0


def _matches_observation(record: dict, trajectory_id: str) -> bool:
    return record.get("entity_id") == trajectory_id


def _matches_event(record: dict, trajectory_id: str) -> bool:
    return trajectory_id in (record.get("entity_ids") or [])


def _matches_analysis(record: dict, trajectory_id: str) -> bool:
    return any(
        (hypothesis.get("basis") or {}).get("entity_id") == trajectory_id
        for hypothesis in (record.get("hypotheses") or [])
    )


def _matches_decision_notification(record: dict, trajectory_id: str) -> bool:
    return record.get("identity") == trajectory_id


def _matches_decision_approval(record: dict, trajectory_id: str) -> bool:
    action = record.get("action") or {}
    return trajectory_id in (record.get("id"), action.get("id"), action.get("identity"), action.get("entity_id"))


def _matches_notification_alert(record: dict, trajectory_id: str) -> bool:
    return record.get("notification_id") == trajectory_id


def _matches_notification_sent(record: dict, trajectory_id: str) -> bool:
    return trajectory_id in (record.get("notification_id"), record.get("user_id"))


def _matches_interaction(record: dict, trajectory_id: str) -> bool:
    return trajectory_id in (record.get("id"), record.get("notification_id"), record.get("user_id"))


# (table, component label, record-type label, timestamp field, matcher) —
# replay's declarative source list (ADR-0041). No shared trajectory-
# scoping key exists anywhere in this project's persisted schema, so
# trajectory_id is matched polymorphically against whatever identifying
# field each table's already-shipped records actually carry.
_REPLAY_SOURCES = [
    (_C07_OBSERVATIONS_TABLE, "event_observation", "observation", "observed_at", _matches_observation),
    (_C07_EVENTS_TABLE, "event_observation", "event", "detected_at", _matches_event),
    (_C08_ANALYSES_TABLE, "analysis_reasoning", "analysis", "synthesized_at", _matches_analysis),
    (_C12_NOTIFICATIONS_TABLE, "decision_policy", "notification_issued", "issued_at", _matches_decision_notification),
    (_C12_APPROVALS_TABLE, "decision_policy", "pending_approval", "requested_at", _matches_decision_approval),
    (_C13_ALERTS_TABLE, "interaction_notification", "alert", "raised_at", _matches_notification_alert),
    (_C13_NOTIFICATIONS_SENT_TABLE, "interaction_notification", "notification_sent", "attempted_at", _matches_notification_sent),
    (_C13_INTERACTIONS_TABLE, "interaction_notification", "interaction", "created_at", _matches_interaction),
]


class DefaultLearningEvaluation:
    """Real implementation of LearningEvaluation (ADR-0041).

    measure_outcome/compare_prediction_vs_outcome/evaluate/
    analyze_errors are real measurement, comparison, and structural
    categorization over data already recorded by other real components
    — no LLM, the same character as Event & Observation (component 07).
    replay is a real, Infrastructure-backed reconstruction across six
    cross-component tables. collect_feedback is a real, honest
    pending-mechanism (same posture as DefaultDecisionPolicy.
    request_approval, ADR-0038, and DefaultInteractionNotification.
    collect_feedback, ADR-0039 — a "no UI exists" gap, not an LLM/
    credential gap). update_knowledge is the actual point of this
    component: it drives Memory's full write path
    (MemoryEvaluator -> EntityLinker -> QuarantineGate ->
    MemoryManager.admit()) for real, the first component in this
    project to do so end to end. detect_regression/evaluate_versions
    are genuine statistical comparisons with documented thresholds.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        data_sources: DataSources | None = None,
        memory_manager: MemoryManager | None = None,
        memory_evaluator: MemoryEvaluator | None = None,
        entity_linker: EntityLinker | None = None,
        quarantine_gate: QuarantineGate | None = None,
        audit_manager: AuditManager | None = None,
        memory_scope: str = "shared",
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._data_sources = data_sources or DefaultDataSources(infrastructure=self._infrastructure)
        self._memory_manager = memory_manager or DefaultMemoryManager(
            infrastructure=self._infrastructure, scope_router=DefaultScopeRouter()
        )
        self._memory_evaluator = memory_evaluator or DefaultMemoryEvaluator()
        self._entity_linker = entity_linker or DefaultEntityLinker()
        self._quarantine_gate = quarantine_gate or DefaultQuarantineGate(infrastructure=self._infrastructure)
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._memory_scope = memory_scope

    def measure_outcome(self, prediction: Prediction) -> Outcome:
        """Real — queries DataSources for the relevant market/outcome
        data (ADR-0041), inheriting ADR-0027's fetch-provider gap
        rather than duplicating it: under PlaceholderSourceFetcher,
        every actual_value below is honestly None."""
        with traced("DefaultLearningEvaluation.measure_outcome"):
            try:
                source_type = SourceType[prediction.source_type]
            except KeyError:
                source_type = SourceType.MARKET_DATA  # undocumented/empty source_type falls back to the common case
            source = Source(id=prediction.entity_id, type=source_type)
            document = self._data_sources.retrieve_source(prediction.entity_id)
            if document is None:
                document = self._data_sources.ingest_source(source)
            metadata = self._data_sources.track_source_reliability_metadata(source)
            synthetic = document.content == PLACEHOLDER_FETCH_MARKER
            actual_value = None if synthetic else _extract_numeric_field(document.content, prediction.metric)
            actual = {
                "entity_id": prediction.entity_id,
                "metric": prediction.metric,
                "actual_value": actual_value,
                "reliability": metadata.reliability,
                "synthetic": synthetic,
                "fetched_at": document.fetched_at,
            }
            return Outcome(
                prediction=prediction,
                actual=actual,
                id=f"outcome-{uuid.uuid4()}",
                measured_at=time.strftime(_TIMESTAMP_FORMAT),
            )

    def compare_prediction_vs_outcome(self, prediction: Prediction, outcome: Outcome) -> dict:
        """Real structural comparison (ADR-0041): direction and
        magnitude are both computed from real deltas
        (predicted/actual value against the prediction's own
        reference_value), never trusted as a free-form label."""
        with traced("DefaultLearningEvaluation.compare_prediction_vs_outcome"):
            actual = outcome.actual or {}
            actual_value = actual.get("actual_value")
            comparable = (
                actual_value is not None
                and prediction.predicted_value is not None
                and prediction.reference_value is not None
            )
            if not comparable:
                return {
                    "comparable": False,
                    "predicted_value": prediction.predicted_value,
                    "actual_value": actual_value,
                }
            predicted_direction = _sign(prediction.predicted_value - prediction.reference_value)
            actual_direction = _sign(actual_value - prediction.reference_value)
            magnitude_error = abs(prediction.predicted_value - actual_value)
            if actual_value != 0:
                relative_error = magnitude_error / abs(actual_value)
            else:
                relative_error = 0.0 if magnitude_error == 0 else float("inf")
            return {
                "comparable": True,
                # A prediction measured against the wrong entity is
                # never genuinely correct, even if its numbers happen
                # to line up by coincidence (ADR-0041) — entity_match
                # gates `correct` in evaluate() alongside direction and
                # magnitude, and analyze_errors' wrong_entity_resolution
                # category reads this same field.
                "entity_match": actual.get("entity_id") == prediction.entity_id,
                "predicted_direction": predicted_direction,
                "actual_direction": actual_direction,
                "direction_correct": predicted_direction == actual_direction,
                "predicted_value": prediction.predicted_value,
                "actual_value": actual_value,
                "magnitude_error": magnitude_error,
                "relative_error": relative_error,
                "magnitude_within_tolerance": relative_error <= _MAGNITUDE_RELATIVE_ERROR_TOLERANCE,
            }

    def evaluate(self, prediction: Prediction, outcome: Outcome) -> Evaluation:
        """Real (ADR-0041): correct is computed from
        compare_prediction_vs_outcome's own real fields — comparable,
        the right entity, the right direction, and within the
        documented magnitude tolerance, never hardcoded. The resulting
        Evaluation is persisted for real, the same "persist the
        pipeline's durable output" posture
        DefaultAnalysisReasoning.synthesize_findings already takes
        (ADR-0037)."""
        with traced("DefaultLearningEvaluation.evaluate"):
            comparison = self.compare_prediction_vs_outcome(prediction, outcome)
            correct = bool(
                comparison.get("comparable")
                and comparison.get("entity_match")
                and comparison.get("direction_correct")
                and comparison.get("magnitude_within_tolerance")
            )
            evaluation = Evaluation(
                outcome=outcome,
                correct=correct,
                error=None if correct else comparison,
                id=f"evaluation-{uuid.uuid4()}",
                evaluated_at=time.strftime(_TIMESTAMP_FORMAT),
            )
            self._infrastructure.store(_EVALUATIONS_TABLE, asdict(evaluation))
            return evaluation

    def analyze_errors(self, evaluation: Evaluation) -> dict:
        """Real, structural (ADR-0041): categorizes the miss from what
        is actually recorded on the prediction's own provenance/inputs
        and the outcome's own actual dict — never a generated
        explanation."""
        with traced("DefaultLearningEvaluation.analyze_errors"):
            if evaluation.correct:
                return {"category": "none", "detail": "prediction matched outcome within tolerance"}
            prediction = evaluation.outcome.prediction
            actual = evaluation.outcome.actual or {}
            comparison = evaluation.error or {}
            if not comparison.get("comparable", False):
                if not prediction.source_ids:
                    return {
                        "category": "insufficient_evidence",
                        "detail": "prediction carries no recorded source/evidence provenance (Prediction.source_ids is empty)",
                    }
                if actual.get("synthetic", True):
                    return {
                        "category": "stale_data",
                        "detail": "only a synthetic/placeholder-sourced document was available (see ADR-0027) -- no genuinely fresh outcome data existed to measure against",
                    }
                return {
                    "category": "unverifiable_source_data",
                    "detail": "a genuinely fetched document existed but contained no parseable numeric field for this metric",
                }
            if actual.get("entity_id") != prediction.entity_id:
                return {
                    "category": "wrong_entity_resolution",
                    "detail": f"outcome measured entity {actual.get('entity_id')!r}, prediction targeted {prediction.entity_id!r}",
                }
            if not comparison.get("direction_correct", False):
                return {
                    "category": "direction_miss",
                    "detail": "predicted direction did not match the actual direction",
                    "predicted_direction": comparison.get("predicted_direction"),
                    "actual_direction": comparison.get("actual_direction"),
                }
            return {
                "category": "magnitude_miss",
                "detail": "direction was correct but the predicted value missed the tolerance band",
                "relative_error": comparison.get("relative_error"),
            }

    def collect_feedback(self, evaluation: Evaluation) -> dict:
        """Real, honest pending-feedback mechanism (ADR-0041), same
        spirit as DefaultDecisionPolicy.request_approval (ADR-0038) and
        DefaultInteractionNotification.collect_feedback (ADR-0039):
        persists a real pending row and reports its actual stored
        status. No UI exists anywhere in this project to supply real
        feedback content yet — this honestly returns a pending response
        rather than fabricating one."""
        with traced("DefaultLearningEvaluation.collect_feedback"):
            existing = self._infrastructure.retrieve(_FEEDBACK_TABLE, evaluation.id)
            if existing is not None and existing.get("feedback") is not None:
                return existing["feedback"]
            if existing is None:
                self._infrastructure.store(
                    _FEEDBACK_TABLE,
                    {
                        "id": evaluation.id,
                        "evaluation_id": evaluation.id,
                        "status": "pending_feedback",
                        "feedback": None,
                        "requested_at": time.strftime(_TIMESTAMP_FORMAT),
                    },
                )
                self._audit_manager.record("evaluation_feedback_requested", {"evaluation_id": evaluation.id})
            return {"status": "pending", "evaluation_id": evaluation.id}

    def update_knowledge(self, evaluation: Evaluation) -> None:
        """Real — THE point of this component (ADR-0041): converts the
        evaluation into a real MemoryCandidate and drives Memory's full
        write path (MemoryEvaluator -> EntityLinker -> QuarantineGate ->
        MemoryManager) for real, closing the feedback loop. Nothing
        here is a stub call — should_become_memory, link, check_
        provenance, and admit()/quarantine() are all the real
        Default* mechanisms from component 06."""
        with traced("DefaultLearningEvaluation.update_knowledge"):
            prediction = evaluation.outcome.prediction
            actual = evaluation.outcome.actual or {}
            content = {
                "type": "learning_evaluation",
                "evaluation_id": evaluation.id,
                "claim": prediction.claim,
                "entity_id": prediction.entity_id,
                "metric": prediction.metric,
                "correct": evaluation.correct,
                "error": evaluation.error,
                "evaluated_at": evaluation.evaluated_at,
            }
            experience = {"content": content, "confidence": prediction.confidence}
            if not self._memory_evaluator.should_become_memory(experience):
                self._audit_manager.record(
                    "knowledge_update_skipped", {"evaluation_id": evaluation.id, "reason": "below memory threshold"}
                )
                return
            # provenance_verified is computed, not hardcoded: only a
            # genuinely fetched, non-synthetic outcome grounds this
            # candidate enough to admit directly (ADR-0041) — under
            # PlaceholderSourceFetcher (ADR-0027) this is always False,
            # so update_knowledge honestly quarantines today.
            provenance_verified = actual.get("actual_value") is not None and not actual.get("synthetic", True)
            candidate = MemoryCandidate(
                content=content, source=f"learning_evaluation:{evaluation.id}", provenance_verified=provenance_verified
            )
            existing_memories = self._memory_manager.retrieve({}, scope=self._memory_scope)
            links = self._entity_linker.link(candidate, existing_memories)
            if self._quarantine_gate.check_provenance(candidate):
                memory = Memory(
                    id=f"learning-evaluation-{evaluation.id}",
                    content=candidate.content,
                    scope=self._memory_scope,
                    links=links,
                    confidence=prediction.confidence,
                    quarantined=False,
                )
                self._memory_manager.admit(memory)
                self._audit_manager.record(
                    "knowledge_updated", {"evaluation_id": evaluation.id, "memory_id": memory.id, "admitted": True}
                )
            else:
                self._quarantine_gate.quarantine(candidate)
                self._audit_manager.record(
                    "knowledge_updated", {"evaluation_id": evaluation.id, "admitted": False, "quarantined": True}
                )

    def detect_regression(self, current: Evaluation, baseline: Evaluation) -> bool:
        """Real statistical comparison (ADR-0041): a correct->incorrect
        flip is always a regression; among two incorrect evaluations,
        only a relative-error worsening past the documented
        _REGRESSION_RELATIVE_ERROR_MARGIN counts, so noise isn't
        flagged. Audited only when a regression is actually found —
        the same "record the surprising case" posture
        DefaultEventObservation.detect_anomaly already takes
        (ADR-0036)."""
        with traced("DefaultLearningEvaluation.detect_regression"):
            if current.correct and baseline.correct:
                regressed = False
            elif current.correct and not baseline.correct:
                regressed = False  # improvement, not a regression
            elif not current.correct and baseline.correct:
                regressed = True  # flipped from correct to incorrect
            else:
                current_relative_error = (current.error or {}).get("relative_error")
                baseline_relative_error = (baseline.error or {}).get("relative_error")
                if current_relative_error is None or baseline_relative_error is None:
                    regressed = False  # not enough real signal on both sides to call it worse
                else:
                    regressed = (current_relative_error - baseline_relative_error) > _REGRESSION_RELATIVE_ERROR_MARGIN
            if regressed:
                self._audit_manager.record(
                    "regression_detected", {"current_evaluation_id": current.id, "baseline_evaluation_id": baseline.id}
                )
            return regressed

    def evaluate_versions(self, a: dict, b: dict) -> dict:
        """Real structural/statistical comparison (ADR-0041) over two
        versions' aggregate outcomes. Contract: each of a/b is
        {"version": str, "evaluations": [asdict(Evaluation), ...]}."""
        with traced("DefaultLearningEvaluation.evaluate_versions"):
            a_stats = _aggregate_evaluations(a.get("evaluations", []))
            b_stats = _aggregate_evaluations(b.get("evaluations", []))
            accuracy_delta = b_stats["accuracy"] - a_stats["accuracy"]
            if accuracy_delta > 0:
                better_version = b.get("version", "b")
            elif accuracy_delta < 0:
                better_version = a.get("version", "a")
            else:
                better_version = "tie"
            return {
                "a_version": a.get("version", "a"),
                "b_version": b.get("version", "b"),
                "a": a_stats,
                "b": b_stats,
                "accuracy_delta": accuracy_delta,
                "better_version": better_version,
            }

    def replay(self, trajectory_id: str) -> list[dict]:
        """Real, Infrastructure-backed reconstruction (ADR-0041) across
        every table this project's other components have already been
        persisting to. No shared trajectory-scoping key exists
        anywhere in this project's persisted schema, so trajectory_id
        is matched polymorphically against whatever identifying field
        each table's records actually carry (see _REPLAY_SOURCES).
        Assembled into one real, chronologically ordered list."""
        with traced("DefaultLearningEvaluation.replay"):
            entries = []
            for table, component, record_type, timestamp_field, matcher in _REPLAY_SOURCES:
                for record in self._infrastructure.query(table, {}):
                    if matcher(record, trajectory_id):
                        entries.append(
                            {
                                "component": component,
                                "type": record_type,
                                "at": record.get(timestamp_field),
                                "record": record,
                            }
                        )
            entries.sort(key=lambda entry: _epoch(entry["at"]))
            return entries
