"""Decision & Policy (component 12) — the governance and action-control
layer.

Design: no fig. 1 / fig. 2 mechanism diagram exists for this component
(it stayed whiteboard-only through the design-framework round covered
by `checkpoint.md`). The `Default*` class below is its first real
implementation, built directly from this task's own brief rather than
a prior design artifact.
Decision: ADR-0038 — the `verified_claim` dict contract this component
reads (Analysis & Reasoning (08)'s `Hypothesis.basis`/`estimate_impact`
output merged into one shape, since no prior ADR fixed that), the
weighted relevance/significance/risk rules, the actionability
thresholds, the notification rate-limit policy, and the real
pending-approval mechanism.

Interfaces: <- Agent Runtime (10) (escalation), <- Evidence &
Verification (09), -> Interaction & Notification (13). Also receives
Reliability & Resilience's escalate-when-no-alternative-tool path
(component 15, fig. 15.1).
"""

import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from components.c01_user_portfolio import DefaultUserPortfolio, UserPortfolio
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


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


# --- DefaultDecisionPolicy: real scoring/governance mechanism (ADR-0038) ---

# The verified_claim dict contract (ADR-0038): what every scoring method
# below reads. Every key is optional and read defensively — a missing
# key scores as if the signal weren't there, never a crash and never a
# forced guess.
#   "claim": str, "entity_id": str | None, "confidence": float (0-1),
#   "evidence_count": int, "was_contradictory": bool,
#   "significance": "high" | "medium" | "low" | "unknown",
#   "exposure": {"market_value": float, "weight": float},
#   "relevance": float (0-1) — attached by the caller after running
#     assess_relevance separately; determine_actionability has no
#     `portfolio` parameter to compute it fresh.
_SIGNIFICANCE_LEVEL_SCORES = {"high": 1.0, "medium": 0.6, "low": 0.3, "unknown": 0.0}
_CONTRADICTION_RISK_PENALTY = 0.3

_RELEVANCE_SUPPRESS_THRESHOLD = 0.05
_ESCALATE_RISK_THRESHOLD = 0.60
_ESCALATE_SIGNIFICANCE_THRESHOLD = 0.70
_NOTIFY_SIGNIFICANCE_THRESHOLD = 0.30

_NOTIFICATIONS_TABLE = "decision_policy_notifications"
_APPROVALS_TABLE = "decision_policy_pending_approvals"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches every other component's own timestamp format


def _clamp01(value: float) -> float:
    return max(0.0, min(value, 1.0))


class DefaultDecisionPolicy:
    """Real implementation of DecisionPolicy (ADR-0038).

    Real, rule-based scoring throughout — no LLM anywhere in this
    component, matching the same "concrete numeric signals in, a
    weighted formula out" shape `DefaultAnalysisReasoning.estimate_impact`'s
    real half and `DefaultClaimVerifier.score_confidence` already use.

    `assess_relevance` is delegated entirely to `UserPortfolio`
    (`DefaultUserPortfolio` by default) — real exposure-weight lookup
    when a portfolio snapshot is available, a real boolean fallback via
    `determine_user_relevance` otherwise. `assess_significance`/
    `assess_risk` are genuine weighted formulas over whatever confidence/
    impact signals are present on `verified_claim`. `determine_actionability`
    combines all three via documented thresholds into `"notify"`/
    `"escalate"`/`"suppress"`. `authorize_action` calls through
    `BoundaryGate.authorize` (`DefaultBoundaryGate` by default) for
    real — this component never bypasses or reimplements authorization,
    it inherits ADR-0020's interim fail-open posture rather than working
    around it. `enforce_policy` is a real sliding-window notification
    rate limit per identity, backed by `Infrastructure`. `escalate`
    records via `AuditManager`, the same pattern
    `DefaultRecoveryManager.escalate` (component 10) already
    established. `request_approval` is a real pending-approval
    mechanism backed by `Infrastructure` — honestly incomplete only in
    that nothing external can mark a request approved yet (a named,
    real gap, not a disguised placeholder; see ADR-0038's Context)."""

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        user_portfolio: UserPortfolio | None = None,
        boundary_gate: BoundaryGate | None = None,
        audit_manager: AuditManager | None = None,
        rate_limit_window_seconds: float = 3600.0,
        rate_limit_max_per_window: int = 5,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._user_portfolio = user_portfolio or DefaultUserPortfolio(infrastructure=self._infrastructure)
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._rate_limit_window_seconds = rate_limit_window_seconds
        self._rate_limit_max_per_window = rate_limit_max_per_window

    def assess_relevance(self, verified_claim: dict, portfolio: dict) -> float:
        """Real, delegated to UserPortfolio (01), never reinvented
        (ADR-0038). `portfolio` bundles whichever of `"portfolio_snapshot"`
        or `"user"` the caller has on hand — the same bundle-dict
        convention `DefaultAnalysisReasoning._gather_analysis_input`
        uses for `context["portfolio_snapshot"]`. A snapshot gives a
        real, continuous exposure-weight signal via
        `calculate_exposure`; a user with no snapshot falls back to a
        real boolean via `determine_user_relevance`; neither gives
        0.0 — no entity to check ever gives 0.0 too."""
        with traced("DefaultDecisionPolicy.assess_relevance"):
            entity_id = verified_claim.get("entity_id")
            if entity_id is None:
                return 0.0
            portfolio_snapshot = portfolio.get("portfolio_snapshot")
            if portfolio_snapshot is not None:
                exposure = self._user_portfolio.calculate_exposure(portfolio_snapshot)
                entry = exposure.get(entity_id)
                return _clamp01(entry["weight"]) if entry is not None else 0.0
            user = portfolio.get("user")
            if user is not None:
                is_relevant = self._user_portfolio.determine_user_relevance(user, {"security_id": entity_id})
                return 1.0 if is_relevant else 0.0
            return 0.0

    def assess_significance(self, verified_claim: dict) -> float:
        """Real weighted score (ADR-0038): 0.6 * confidence (the real,
        evidence-backed number from ClaimVerifier, ADR-0031) + 0.4 *
        the significance-level score from estimate_impact's judgment
        half. Confidence carries the larger weight because it's the
        harder, better-grounded signal of the two."""
        with traced("DefaultDecisionPolicy.assess_significance"):
            confidence = _clamp01(float(verified_claim.get("confidence", 0.0)))
            level_score = _SIGNIFICANCE_LEVEL_SCORES.get(verified_claim.get("significance", "unknown"), 0.0)
            return _clamp01(0.6 * confidence + 0.4 * level_score)

    def assess_risk(self, verified_claim: dict) -> float:
        """Real score (ADR-0038): significance * uncertainty, plus a
        flat penalty when the underlying evidence was contradictory
        even after automatic resolution (ADR-0014). A significant,
        confident, non-contradictory claim is low risk to act on; risk
        climbs when stakes and doubt combine."""
        with traced("DefaultDecisionPolicy.assess_risk"):
            significance = self.assess_significance(verified_claim)
            confidence = _clamp01(float(verified_claim.get("confidence", 0.0)))
            uncertainty = 1.0 - confidence
            contradiction_penalty = _CONTRADICTION_RISK_PENALTY if verified_claim.get("was_contradictory") else 0.0
            return _clamp01(significance * uncertainty + contradiction_penalty)

    def determine_actionability(self, decision: Decision) -> str:
        """Real, threshold-based (ADR-0038). Relevance gates first and
        absolutely — irrelevant claims are always suppressed. Above
        that floor, high risk or high significance alone escalates;
        moderate significance notifies; everything else suppresses.
        Sets `decision.actionability` to the computed result before
        returning it, so the same Decision object reflects the real
        answer, not just the return value."""
        with traced("DefaultDecisionPolicy.determine_actionability"):
            verified_claim = decision.verified_claim
            relevance = _clamp01(float(verified_claim.get("relevance", 0.0)))
            significance = self.assess_significance(verified_claim)
            risk = self.assess_risk(verified_claim)

            if relevance < _RELEVANCE_SUPPRESS_THRESHOLD:
                actionability = "suppress"
            elif risk >= _ESCALATE_RISK_THRESHOLD or significance >= _ESCALATE_SIGNIFICANCE_THRESHOLD:
                actionability = "escalate"
            elif significance >= _NOTIFY_SIGNIFICANCE_THRESHOLD:
                actionability = "notify"
            else:
                actionability = "suppress"

            decision.actionability = actionability
            return actionability

    def authorize_action(self, action: dict) -> bool:
        """Real — calls through BoundaryGate.authorize (ADR-0020) for
        real, never bypassed or reimplemented (ADR-0038)."""
        with traced("DefaultDecisionPolicy.authorize_action"):
            return self._boundary_gate.authorize(
                action.get("identity", ""), action.get("action", ""), action.get("resource", "")
            )

    def enforce_policy(self, action: dict) -> bool:
        """Real — one concrete policy: a sliding-window notification
        rate limit per identity, backed by Infrastructure (ADR-0038).
        Infrastructure.query's filter dict is exact-match only, so the
        time-window bound is applied client-side over the
        identity-filtered rows."""
        with traced("DefaultDecisionPolicy.enforce_policy"):
            identity = action.get("identity", "")
            now = time.time()
            window_start = now - self._rate_limit_window_seconds
            recent_count = sum(
                1
                for record in self._infrastructure.query(_NOTIFICATIONS_TABLE, {"identity": identity})
                if record.get("issued_at", 0.0) >= window_start
            )
            if recent_count >= self._rate_limit_max_per_window:
                self._audit_manager.record(
                    "policy_rate_limit_blocked",
                    {"identity": identity, "recent_count": recent_count, "action": action.get("action", "")},
                )
                return False
            self._infrastructure.store(
                _NOTIFICATIONS_TABLE,
                {"id": str(uuid.uuid4()), "identity": identity, "issued_at": now, "action": action.get("action", "")},
            )
            return True

    def escalate(self, reason: str, context: dict) -> None:
        """Real — records via AuditManager, matching
        DefaultRecoveryManager.escalate's own pattern (component 10)."""
        with traced("DefaultDecisionPolicy.escalate"):
            self._audit_manager.record("decision_policy_escalation", {"reason": reason, "context": context})

    def request_approval(self, action: dict) -> bool:
        """Real pending-approval mechanism, backed by Infrastructure
        (ADR-0038). A stable `action["id"]` makes repeated calls
        idempotent — the same id reports whatever status is stored,
        never re-requests. No id means every call is a fresh request
        (never spuriously reports approved). Always False today: no
        UI or notification-and-wait mechanism exists anywhere in this
        project yet to flip a stored status to "approved" — that is
        the one genuine, named gap here, not a disguised placeholder."""
        with traced("DefaultDecisionPolicy.request_approval"):
            action_id = action.get("id") or str(uuid.uuid4())
            existing = self._infrastructure.retrieve(_APPROVALS_TABLE, action_id)
            if existing is not None:
                return existing.get("status") == "approved"
            self._infrastructure.store(
                _APPROVALS_TABLE,
                {
                    "id": action_id,
                    "action": action,
                    "status": "pending",
                    "requested_at": time.strftime(_TIMESTAMP_FORMAT),
                },
            )
            self._audit_manager.record("approval_requested", {"action_id": action_id, "action": action})
            return False
