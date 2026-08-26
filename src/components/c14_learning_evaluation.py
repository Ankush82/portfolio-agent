"""Learning & Evaluation (component 14) — the closed-loop improvement
layer.

Whiteboard-level only (Component Whiteboards artifact, card 14) — no
low-level design or ADRs yet. Interfaces: <- Interaction & Notification
(13), -> Memory (06). Whether this component is in scope for the
current build round at all is still an open question (checkpoint.md,
loop.md) — do not assume it's included by writing code against it.
"""

from dataclasses import dataclass


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


class LearningEvaluation:
    def evaluate(self, prediction: Prediction, outcome: Outcome) -> Evaluation:
        raise NotImplementedError

    def replay(self, trajectory_id: str) -> list[dict]:
        raise NotImplementedError

    def measure_outcome(self, prediction: Prediction) -> Outcome:
        raise NotImplementedError

    def compare_prediction_vs_outcome(self, prediction: Prediction, outcome: Outcome) -> dict:
        raise NotImplementedError

    def analyze_errors(self, evaluation: Evaluation) -> dict:
        raise NotImplementedError

    def collect_feedback(self, evaluation: Evaluation) -> dict:
        raise NotImplementedError

    def update_knowledge(self, evaluation: Evaluation) -> None:
        """→ Memory (06)."""
        raise NotImplementedError

    def detect_regression(self, current: Evaluation, baseline: Evaluation) -> bool:
        raise NotImplementedError

    def evaluate_versions(self, a: dict, b: dict) -> dict:
        raise NotImplementedError
