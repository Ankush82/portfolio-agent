"""Interaction & Notification (component 13) — the surface the user
actually sees.

Design: no fig. 1 / fig. 2 mechanism diagram exists for this component
(it stayed whiteboard-only through the design-framework round covered
by `checkpoint.md`). The `Default*` class below is its first real
implementation, built directly from this task's own brief rather than
a prior design artifact.
Decisions:
  ADR-0039 — the `decision: dict` contract this component reads (an
             asdict(Decision) plus a "user_id" key Decision itself
             doesn't carry), significance/risk delegated to
             DecisionPolicy rather than reinvented, the
             prioritize/personalize rules, and the real
             pending-feedback/pending-response mechanism.
  ADR-0040 — which real delivery channel (email/SMS/push) eventually
             backs NotificationChannel (Status: Proposed — genuine
             external-credential gap, not decided here).

Interfaces: <- Decision & Policy (12), -> User, -> Learning &
Evaluation (14).
"""

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Protocol

from components.c12_decision_policy import DecisionPolicy, DefaultDecisionPolicy
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


@dataclass
class Notification:
    user_id: str
    content: str
    priority: str
    id: str = ""
    actionability: str = ""
    significance: float = 0.0
    channel: str = "default"


@dataclass
class Alert:
    """An escalation-specific companion record, raised alongside a
    Notification when a Decision's actionability is "escalate" (see
    DefaultInteractionNotification._raise_alert). Not returned from any
    Protocol method directly — persisted and audited so an escalation
    leaves a durable, queryable trail distinct from an ordinary
    notification."""

    id: str
    notification_id: str
    reason: str
    risk: float
    raised_at: str


@dataclass
class Explanation:
    """The structured intermediate `explain_decision` builds before
    rendering it to the prose string its Protocol signature actually
    returns (see `_render_explanation`). Exists so the real numbers
    behind an explanation are assembled and named once, not scattered
    across an f-string."""

    claim: str
    actionability: str
    relevance: float
    significance: float
    risk: float
    rationale: str


@dataclass
class UserFeedback:
    notification_id: str
    response: dict


@dataclass
class Interaction:
    """The notify -> respond lifecycle record for one Notification —
    what `collect_feedback`/`collect_user_response` persist and update,
    and the hand-off artifact this component's own docstring already
    names as flowing to Learning & Evaluation (14). One Interaction per
    Notification (same id), holding both feedback and response since
    either, both, or neither may exist at a given point in that
    lifecycle."""

    id: str
    notification_id: str
    user_id: str
    status: str  # "pending_feedback" | "pending_response"
    feedback: dict | None = None
    response: dict | None = None
    created_at: str = ""


class InteractionNotification(Protocol):
    def generate_notification(self, decision: dict) -> Notification:
        ...

    def prioritize_notification(self, notification: Notification) -> str:
        ...

    def personalize_notification(self, notification: Notification, user: dict) -> Notification:
        ...

    def deliver_notification(self, notification: Notification) -> bool:
        ...

    def explain_decision(self, decision: dict) -> str:
        ...

    def collect_feedback(self, notification: Notification) -> UserFeedback:
        ...

    def collect_user_response(self, notification: Notification) -> dict:
        ...


class StubInteractionNotification:
    """Structural implementation of InteractionNotification. Every
    method is a traced no-op — see cross_cutting/observability.py."""

    def generate_notification(self, decision: dict) -> Notification:
        with traced("StubInteractionNotification.generate_notification"):
            return Notification(user_id="stub-id", content="", priority="")

    def prioritize_notification(self, notification: Notification) -> str:
        with traced("StubInteractionNotification.prioritize_notification"):
            return ""

    def personalize_notification(self, notification: Notification, user: dict) -> Notification:
        with traced("StubInteractionNotification.personalize_notification"):
            return Notification(user_id="stub-id", content="", priority="")

    def deliver_notification(self, notification: Notification) -> bool:
        with traced("StubInteractionNotification.deliver_notification"):
            return True

    def explain_decision(self, decision: dict) -> str:
        with traced("StubInteractionNotification.explain_decision"):
            return ""

    def collect_feedback(self, notification: Notification) -> UserFeedback:
        with traced("StubInteractionNotification.collect_feedback"):
            return UserFeedback(notification_id="stub-id", response={})

    def collect_user_response(self, notification: Notification) -> dict:
        with traced("StubInteractionNotification.collect_user_response"):
            return {}


# --- DefaultInteractionNotification: real mechanism (ADR-0039) -------------

# The `decision: dict` contract every method below reads (ADR-0039):
# an asdict() of Decision & Policy's own `Decision(verified_claim,
# actionability)` (component 12, ADR-0038), plus one key Decision
# itself doesn't carry: "user_id" — the notification's recipient.
# Neither the Decision dataclass nor any prior ADR names how a
# recipient identity reaches this component, so this is the judgment
# call this pass makes, documented here rather than left implicit:
#   {
#     "verified_claim": dict,  # ADR-0038's own verified_claim contract
#     "actionability": str,    # "notify" | "escalate" | "suppress"
#     "user_id": str,          # optional; "" when the caller omits it
#   }
_ALERTS_TABLE = "notification_alerts"
_NOTIFICATIONS_SENT_TABLE = "notifications_sent"
_INTERACTIONS_TABLE = "interactions"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches every other component's own timestamp format

# Component 13's own sub-priority split within "notify" — distinct from
# ADR-0038's actionability thresholds (which already separated
# escalate/notify/suppress); this only decides "high" vs "normal"
# among what Decision & Policy already classified as "notify".
_PRIORITY_HIGH_SIGNIFICANCE_THRESHOLD = 0.5


def _clamp01(value: float) -> float:
    return max(0.0, min(value, 1.0))


def _rationale_for(actionability: str, relevance: float, significance: float, risk: float) -> str:
    """Plain-English justification keyed on the actionability Decision
    & Policy already computed, not on re-deriving its numeric
    thresholds here — ADR-0038's threshold values stay the single
    source of truth in component 12; this only names, in words, what
    that outcome means."""
    if actionability == "escalate":
        return "risk or significance high enough to require review before acting"
    if actionability == "notify":
        return "relevant and significant enough to inform, not urgent enough to escalate"
    if actionability == "suppress":
        return "insufficient portfolio relevance or significance to notify"
    return "actionability not yet determined"


def _render_explanation(explanation: Explanation) -> str:
    """The one template `explain_decision` renders through — a fixed
    four-part structure over the real relevance/significance/risk/
    actionability numbers DecisionPolicy computed, plus the claim text
    itself. No free-form generation: every value plugged in is a real
    number or a real string already on the decision."""
    return (
        f"Claim: {explanation.claim}. "
        f"Portfolio relevance: {explanation.relevance:.0%}. "
        f"Significance: {explanation.significance:.0%}. "
        f"Risk: {explanation.risk:.0%}. "
        f"Decision: {explanation.actionability} ({explanation.rationale})."
    )


class NotificationChannel(Protocol):
    """The seam `deliver_notification` calls through instead of talking
    to an email/SMS/push API directly (ADR-0039/ADR-0040)."""

    def send(self, notification: Notification) -> bool:
        """Attempts delivery. Returns whether it actually succeeded."""
        ...


class PlaceholderNotificationChannel:
    """NOT a real delivery channel (ADR-0040). No live email/SMS/push
    credential exists in this project — ADR-0040 names the real options
    (a transactional-email API, an SMS/telephony API, a mobile push
    service) without choosing one, since that requires a live external
    credential this pass cannot obtain. Records the attempted delivery
    via Infrastructure ("notifications_sent") — timestamp, notification
    id, recipient, channel — without actually sending anything, and
    always returns False: nothing was really delivered, so reporting
    True would be dishonest, the same posture PlaceholderBrokerConnector
    (component 01) and request_approval (component 12) already
    established for this project's other genuine external-dependency
    gaps."""

    def __init__(self, infrastructure: Infrastructure | None = None) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()

    def send(self, notification: Notification) -> bool:
        with traced("PlaceholderNotificationChannel.send"):
            self._infrastructure.store(
                _NOTIFICATIONS_SENT_TABLE,
                {
                    "id": str(uuid.uuid4()),
                    "notification_id": notification.id,
                    "user_id": notification.user_id,
                    "channel": notification.channel,
                    "attempted_at": time.strftime(_TIMESTAMP_FORMAT),
                    "delivered": False,
                },
            )
            return False


class DefaultInteractionNotification:
    """Real implementation of InteractionNotification (ADR-0039).

    `generate_notification`/`explain_decision` never re-derive
    significance/risk from `verified_claim` themselves — both delegate
    to the injected `DecisionPolicy` (`DefaultDecisionPolicy` by
    default), the same "call the real component, don't reinvent its
    scoring" posture ADR-0038 already established for `assess_relevance`
    delegating to `UserPortfolio`. `prioritize_notification` and
    `personalize_notification` are real, rule-based, and read/write real
    fields on `Notification`/real `preferences` data off the `user: dict`
    the Protocol already types. `deliver_notification` calls through an
    injectable `NotificationChannel` (`PlaceholderNotificationChannel`
    by default, ADR-0040) and logs the outcome via `AuditManager`.
    `collect_feedback`/`collect_user_response` are a real, honest
    pending-mechanism backed by `Infrastructure` — the same
    "genuinely real, even though nothing external can fill it in yet"
    posture `DefaultDecisionPolicy.request_approval` (component 12,
    ADR-0038) already established for its own "no UI exists" gap."""

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        decision_policy: DecisionPolicy | None = None,
        notification_channel: NotificationChannel | None = None,
        audit_manager: AuditManager | None = None,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._decision_policy = decision_policy or DefaultDecisionPolicy(infrastructure=self._infrastructure)
        self._notification_channel = notification_channel or PlaceholderNotificationChannel(
            infrastructure=self._infrastructure
        )
        self._audit_manager = audit_manager or DefaultAuditManager()

    def _score_decision(self, decision: dict) -> tuple[float, float, float]:
        """Shared by generate_notification and explain_decision so both
        read the same real numbers the same way: relevance straight off
        verified_claim (already attached by Decision & Policy's own
        caller, per ADR-0038's contract), significance/risk computed
        for real via the injected DecisionPolicy."""
        verified_claim = decision.get("verified_claim", {})
        relevance = _clamp01(float(verified_claim.get("relevance", 0.0)))
        significance = self._decision_policy.assess_significance(verified_claim)
        risk = self._decision_policy.assess_risk(verified_claim)
        return relevance, significance, risk

    def generate_notification(self, decision: dict) -> Notification:
        with traced("DefaultInteractionNotification.generate_notification"):
            verified_claim = decision.get("verified_claim", {})
            actionability = decision.get("actionability", "")
            _relevance, significance, risk = self._score_decision(decision)

            claim = verified_claim.get("claim", "")
            confidence = _clamp01(float(verified_claim.get("confidence", 0.0)))
            entity_id = verified_claim.get("entity_id")
            content = f"{claim} (confidence: {confidence:.0%}, actionability: {actionability})"
            if entity_id:
                content = f"{content}, entity: {entity_id}"

            notification = Notification(
                user_id=decision.get("user_id", ""),
                content=content,
                priority="",
                id=str(uuid.uuid4()),
                actionability=actionability,
                significance=significance,
            )
            if actionability == "escalate":
                self._raise_alert(notification, verified_claim, risk)
            return notification

    def _raise_alert(self, notification: Notification, verified_claim: dict, risk: float) -> Alert:
        alert = Alert(
            id=str(uuid.uuid4()),
            notification_id=notification.id,
            reason=verified_claim.get("claim", ""),
            risk=risk,
            raised_at=time.strftime(_TIMESTAMP_FORMAT),
        )
        self._infrastructure.store(_ALERTS_TABLE, asdict(alert))
        self._audit_manager.record(
            "notification_alert_raised",
            {"notification_id": notification.id, "reason": alert.reason, "risk": risk},
        )
        return alert

    def prioritize_notification(self, notification: Notification) -> str:
        """Real, rule-based on the originating Decision's actionability
        and significance — both real fields `generate_notification`
        already set on `notification` (ADR-0039's documented extension
        of the Notification dataclass, since its original three-field
        shape carried nothing to prioritize from). Escalations are
        always "critical"; among "notify" decisions, significance
        splits "high" from "normal"; anything else (an unprioritized or
        suppressed notification reaching this method at all) is "low".
        Mutates `notification.priority` in place before returning it,
        the same pattern `DefaultDecisionPolicy.determine_actionability`
        already uses for its own Decision argument."""
        with traced("DefaultInteractionNotification.prioritize_notification"):
            if notification.actionability == "escalate":
                priority = "critical"
            elif notification.actionability == "notify" and notification.significance >= _PRIORITY_HIGH_SIGNIFICANCE_THRESHOLD:
                priority = "high"
            elif notification.actionability == "notify":
                priority = "normal"
            else:
                priority = "low"
            notification.priority = priority
            return priority

    def personalize_notification(self, notification: Notification, user: dict) -> Notification:
        """Real, incorporates real `preferences` data off the `user:
        dict` the Protocol already types (`User.preferences` from
        component 01 — no separate `Preference` dataclass exists
        anywhere in this project, so `user["preferences"]` is read as
        the plain dict it already is). Three documented rules: the
        recipient's own `id` (not the possibly-stale `user_id` already
        on `notification`) wins, matching
        `DefaultUserPortfolio.manage_preferences`'s own "never trust a
        possibly-stale caller value" posture; `notification_channel`
        selects delivery channel; `notification_verbosity == "detailed"`
        appends a pointer to `explain_decision`; `quiet_mode` downgrades
        priority to "low" for anything short of an escalation, never for
        one — quiet hours are a real personalization, not a bypass of a
        genuine risk escalation. Returns a new Notification rather than
        mutating the input, since this method rebuilds the recipient-
        specific fields fully from `user`, not incrementally from
        `notification`."""
        with traced("DefaultInteractionNotification.personalize_notification"):
            preferences = user.get("preferences", {})
            channel = preferences.get("notification_channel", notification.channel)
            content = notification.content
            if preferences.get("notification_verbosity") == "detailed":
                content = f"{content}. Full rationale available via explain_decision."
            priority = notification.priority
            if preferences.get("quiet_mode") and notification.actionability != "escalate":
                priority = "low"
            return Notification(
                user_id=user.get("id", notification.user_id),
                content=content,
                priority=priority,
                id=notification.id,
                actionability=notification.actionability,
                significance=notification.significance,
                channel=channel,
            )

    def deliver_notification(self, notification: Notification) -> bool:
        with traced("DefaultInteractionNotification.deliver_notification"):
            delivered = self._notification_channel.send(notification)
            self._audit_manager.record(
                "notification_delivered" if delivered else "notification_delivery_failed",
                {"notification_id": notification.id, "user_id": notification.user_id, "channel": notification.channel},
            )
            return delivered

    def explain_decision(self, decision: dict) -> str:
        """Real — a genuine templated explanation assembled from the
        real relevance/significance/risk scores (relevance off
        verified_claim per ADR-0038's contract, significance/risk
        computed for real via the injected DecisionPolicy, same as
        generate_notification), rendered through the single fixed
        template `_render_explanation` documents. Structured, factual
        prose built from real numbers — no free-form generation
        anywhere in this method."""
        with traced("DefaultInteractionNotification.explain_decision"):
            verified_claim = decision.get("verified_claim", {})
            actionability = decision.get("actionability", "")
            relevance, significance, risk = self._score_decision(decision)
            explanation = Explanation(
                claim=verified_claim.get("claim", ""),
                actionability=actionability,
                relevance=relevance,
                significance=significance,
                risk=risk,
                rationale=_rationale_for(actionability, relevance, significance, risk),
            )
            return _render_explanation(explanation)

    def _get_or_create_interaction(self, notification: Notification, status: str) -> dict:
        """Idempotent by notification.id — a repeated call before
        anything external has answered finds the same pending row
        rather than creating a duplicate, the same idempotency posture
        `DefaultDecisionPolicy.request_approval` already established
        for its own per-action-id pending rows."""
        existing = self._infrastructure.retrieve(_INTERACTIONS_TABLE, notification.id)
        if existing is not None:
            return existing
        record = asdict(
            Interaction(
                id=notification.id,
                notification_id=notification.id,
                user_id=notification.user_id,
                status=status,
                feedback=None,
                response=None,
                created_at=time.strftime(_TIMESTAMP_FORMAT),
            )
        )
        self._infrastructure.store(_INTERACTIONS_TABLE, record)
        return record

    def collect_feedback(self, notification: Notification) -> UserFeedback:
        """Real, honest pending-feedback mechanism (ADR-0039), same
        spirit as `DefaultDecisionPolicy.request_approval` (component
        12, ADR-0038): persists a real pending `Interaction` row via
        Infrastructure and reports its actual stored status. Nothing
        external can supply real feedback content yet — there is no
        UI, no feedback form, no channel that writes back into
        "interactions" — so this honestly returns a pending response
        rather than fabricating one; once something external does
        write feedback onto the stored row (`infra.store("interactions",
        {"id": notification.id, "feedback": {...}})`), a repeated call
        returns that real content instead."""
        with traced("DefaultInteractionNotification.collect_feedback"):
            existing = self._infrastructure.retrieve(_INTERACTIONS_TABLE, notification.id)
            if existing is not None and existing.get("feedback") is not None:
                return UserFeedback(notification_id=notification.id, response=existing["feedback"])
            self._get_or_create_interaction(notification, status="pending_feedback")
            self._audit_manager.record("feedback_requested", {"notification_id": notification.id})
            return UserFeedback(notification_id=notification.id, response={"status": "pending"})

    def collect_user_response(self, notification: Notification) -> dict:
        """Real, honest pending-response mechanism (ADR-0039) — the
        same posture as collect_feedback above, applied to the same
        `Interaction` row's separate `response` field (a lighter-weight
        yes/no or acknowledged-action answer, distinct from qualitative
        feedback). Nothing external can supply a real response yet for
        the same reason: no UI exists anywhere in this project."""
        with traced("DefaultInteractionNotification.collect_user_response"):
            existing = self._infrastructure.retrieve(_INTERACTIONS_TABLE, notification.id)
            if existing is not None and existing.get("response") is not None:
                return existing["response"]
            self._get_or_create_interaction(notification, status="pending_response")
            self._audit_manager.record("response_requested", {"notification_id": notification.id})
            return {"status": "pending", "notification_id": notification.id}
