"""Decision & Policy (component 12) — the governance and action-control
layer.

Whiteboard-level only (Component Whiteboards artifact, card 12) — no
low-level design or ADRs yet. Interfaces: <- Agent Runtime (10)
(escalation), <- Evidence & Verification (09), -> Interaction &
Notification (13). Also receives Reliability & Resilience's
escalate-when-no-alternative-tool path (component 15, fig. 15.1).
"""

from dataclasses import dataclass


@dataclass
class Decision:
    verified_claim: dict
    actionability: str  # "notify" | "escalate" | "suppress"


class DecisionPolicy:
    def assess_relevance(self, verified_claim: dict, portfolio: dict) -> float:
        raise NotImplementedError

    def assess_significance(self, verified_claim: dict) -> float:
        raise NotImplementedError

    def assess_risk(self, verified_claim: dict) -> float:
        raise NotImplementedError

    def determine_actionability(self, decision: Decision) -> str:
        raise NotImplementedError

    def authorize_action(self, action: dict) -> bool:
        raise NotImplementedError

    def enforce_policy(self, action: dict) -> bool:
        raise NotImplementedError

    def escalate(self, reason: str, context: dict) -> None:
        raise NotImplementedError

    def request_approval(self, action: dict) -> bool:
        raise NotImplementedError
