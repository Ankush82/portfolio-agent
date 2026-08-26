"""Decision & Policy (component 12) — the governance and action-control
layer.

Whiteboard-level only (Component Whiteboards artifact, card 12) — no
low-level design or ADRs yet. Interfaces: <- Agent Runtime (10)
(escalation), <- Evidence & Verification (09), -> Interaction &
Notification (13). Also receives Reliability & Resilience's
escalate-when-no-alternative-tool path (component 15, fig. 15.1).
"""

from dataclasses import dataclass
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class Decision:
    verified_claim: dict
    actionability: str  # "notify" | "escalate" | "suppress"


class DecisionPolicy(Protocol):
    def assess_relevance(self, verified_claim: dict, portfolio: dict) -> float:
        ...

    def assess_significance(self, verified_claim: dict) -> float:
        ...

    def assess_risk(self, verified_claim: dict) -> float:
        ...

    def determine_actionability(self, decision: Decision) -> str:
        ...

    def authorize_action(self, action: dict) -> bool:
        ...

    def enforce_policy(self, action: dict) -> bool:
        ...

    def escalate(self, reason: str, context: dict) -> None:
        ...

    def request_approval(self, action: dict) -> bool:
        ...


class StubDecisionPolicy:
    """Structural implementation of DecisionPolicy. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def assess_relevance(self, verified_claim: dict, portfolio: dict) -> float:
        with traced("StubDecisionPolicy.assess_relevance"):
            return 0.0

    def assess_significance(self, verified_claim: dict) -> float:
        with traced("StubDecisionPolicy.assess_significance"):
            return 0.0

    def assess_risk(self, verified_claim: dict) -> float:
        with traced("StubDecisionPolicy.assess_risk"):
            return 0.0

    def determine_actionability(self, decision: Decision) -> str:
        with traced("StubDecisionPolicy.determine_actionability"):
            return ""

    def authorize_action(self, action: dict) -> bool:
        with traced("StubDecisionPolicy.authorize_action"):
            return True

    def enforce_policy(self, action: dict) -> bool:
        with traced("StubDecisionPolicy.enforce_policy"):
            return True

    def escalate(self, reason: str, context: dict) -> None:
        with traced("StubDecisionPolicy.escalate"):
            return None

    def request_approval(self, action: dict) -> bool:
        with traced("StubDecisionPolicy.request_approval"):
            return True
