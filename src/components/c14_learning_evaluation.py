"""Learning & Evaluation (component 14) — the closed-loop improvement
layer.

Whiteboard-level only (Component Whiteboards artifact, card 14) — no
low-level design or ADRs yet. Interfaces: <- Interaction & Notification
(13), -> Memory (06). Whether this component is in scope for the
current build round at all is still an open question (checkpoint.md,
loop.md) — do not assume it's included by writing code against it.
"""

from dataclasses import dataclass
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class Prediction:
    claim: str
    confidence: float


@dataclass
class Outcome:
    prediction: Prediction
    actual: dict


@dataclass
class Evaluation:
    outcome: Outcome
    correct: bool
    error: dict | None = None


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
