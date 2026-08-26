"""Tests for DefaultInteractionNotification
(src/components/c13_interaction_notification.py, ADR-0039/ADR-0040).

Uses an in-memory Infrastructure test double (same containment-match
semantics as every other component's own fake) paired with a real
DefaultDecisionPolicy for significance/risk delegation — the point of
these tests is to prove the real content-assembly, delegated scoring,
prioritization, personalization, delivery call-through, and
pending-feedback/response mechanism ADR-0039/ADR-0040 designed, not to
mock the logic away. AuditManager is exercised against a spy double so
exact event hand-offs are directly assertable.
"""

from components.c12_decision_policy import DefaultDecisionPolicy
from components.c13_interaction_notification import (
    Alert,
    DefaultInteractionNotification,
    Interaction,
    Notification,
    PlaceholderNotificationChannel,
    StubInteractionNotification,
    UserFeedback,
)


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double — same semantics as
    test_decision_policy.py's own double: store() upserts by
    record["id"] (or a generated id), retrieve() looks up by id,
    query() does containment matching."""

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


class _SpyNotificationChannel:
    """Records exactly what was passed to send() and returns a
    configured outcome, so deliver_notification's call-through and
    AuditManager hand-off are directly assertable independent of
    PlaceholderNotificationChannel's own behavior."""

    def __init__(self, outcome: bool = True) -> None:
        self.outcome = outcome
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return self.outcome


def _service(infra=None, decision_policy=None, notification_channel=None, audit_manager=None) -> DefaultInteractionNotification:
    infra = infra or _InMemoryInfrastructure()
    return DefaultInteractionNotification(
        infrastructure=infra,
        decision_policy=decision_policy or DefaultDecisionPolicy(infrastructure=infra),
        notification_channel=notification_channel or PlaceholderNotificationChannel(infrastructure=infra),
        audit_manager=audit_manager or _SpyAuditManager(),
    )


def _decision(actionability: str = "notify", **claim_overrides) -> dict:
    verified_claim = {
        "claim": "AAPL beat earnings estimates",
        "entity_id": "AAPL",
        "confidence": 0.8,
        "significance": "medium",
        "was_contradictory": False,
        "relevance": 0.9,
    }
    verified_claim.update(claim_overrides)
    return {"verified_claim": verified_claim, "actionability": actionability, "user_id": "user-1"}


# --- generate_notification: real content from the Decision's actual fields -


def test_generate_notification_builds_content_from_real_claim_fields():
    service = _service()
    notification = service.generate_notification(_decision())
    assert "AAPL beat earnings estimates" in notification.content
    assert "80%" in notification.content  # confidence
    assert "AAPL" in notification.content  # entity id
    assert notification.user_id == "user-1"
    assert notification.actionability == "notify"
    assert notification.id  # a real, non-empty id was generated


def test_generate_notification_sets_significance_via_real_decision_policy_delegation():
    infra = _InMemoryInfrastructure()
    decision_policy = DefaultDecisionPolicy(infrastructure=infra)
    service = _service(infra=infra, decision_policy=decision_policy)
    decision = _decision(confidence=0.5, significance="medium")

    notification = service.generate_notification(decision)

    # 0.6 * 0.5 (confidence) + 0.4 * 0.6 (medium level score) = 0.54, DefaultDecisionPolicy's own formula.
    assert round(notification.significance, 10) == round(decision_policy.assess_significance(decision["verified_claim"]), 10)


def test_generate_notification_raises_an_alert_and_records_audit_event_on_escalate():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    service = _service(infra=infra, audit_manager=audit)

    notification = service.generate_notification(_decision(actionability="escalate", significance="high", confidence=0.95))

    alerts = list(infra._tables.get("notification_alerts", {}).values())
    assert len(alerts) == 1
    assert alerts[0]["notification_id"] == notification.id
    assert any(event[0] == "notification_alert_raised" for event in audit.events)


def test_generate_notification_does_not_raise_an_alert_for_a_plain_notify():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)
    service.generate_notification(_decision(actionability="notify"))
    assert infra._tables.get("notification_alerts", {}) == {}


def test_generate_notification_defaults_missing_keys_defensively():
    service = _service()
    notification = service.generate_notification({})
    assert notification.user_id == ""
    assert notification.actionability == ""
    assert notification.significance == 0.0


# --- explain_decision: real templated explanation over real numbers --------


def test_explain_decision_renders_real_relevance_significance_risk_and_claim():
    service = _service()
    text = service.explain_decision(_decision())
    assert "AAPL beat earnings estimates" in text
    assert "Portfolio relevance: 90%" in text
    assert "Decision: notify" in text


def test_explain_decision_rationale_matches_the_real_actionability():
    service = _service()
    escalate_text = service.explain_decision(_decision(actionability="escalate"))
    suppress_text = service.explain_decision(_decision(actionability="suppress"))
    assert "require review before acting" in escalate_text
    assert "insufficient portfolio relevance or significance" in suppress_text


def test_explain_decision_uses_the_same_scoring_as_generate_notification():
    infra = _InMemoryInfrastructure()
    decision_policy = DefaultDecisionPolicy(infrastructure=infra)
    service = _service(infra=infra, decision_policy=decision_policy)
    decision = _decision(confidence=0.9, significance="high", was_contradictory=True)

    notification = service.generate_notification(decision)
    explanation_text = service.explain_decision(decision)

    expected_significance_pct = f"{decision_policy.assess_significance(decision['verified_claim']):.0%}"
    assert f"Significance: {expected_significance_pct}" in explanation_text
    assert notification.significance == decision_policy.assess_significance(decision["verified_claim"])


# --- prioritize_notification: real rule-based priority ---------------------


def test_prioritize_notification_is_critical_for_escalate():
    service = _service()
    notification = Notification(user_id="u", content="c", priority="", actionability="escalate", significance=0.1)
    assert service.prioritize_notification(notification) == "critical"
    assert notification.priority == "critical"  # mutated in place


def test_prioritize_notification_is_high_for_notify_with_significant_score():
    service = _service()
    notification = Notification(user_id="u", content="c", priority="", actionability="notify", significance=0.75)
    assert service.prioritize_notification(notification) == "high"


def test_prioritize_notification_is_normal_for_notify_with_low_significance():
    service = _service()
    notification = Notification(user_id="u", content="c", priority="", actionability="notify", significance=0.1)
    assert service.prioritize_notification(notification) == "normal"


def test_prioritize_notification_is_low_for_suppress_or_unset_actionability():
    service = _service()
    suppress_notification = Notification(user_id="u", content="c", priority="", actionability="suppress", significance=0.9)
    unset_notification = Notification(user_id="u", content="c", priority="")
    assert service.prioritize_notification(suppress_notification) == "low"
    assert service.prioritize_notification(unset_notification) == "low"


# --- personalize_notification: real Preference data from User & Portfolio --


def test_personalize_notification_replaces_stale_user_id_with_the_real_one():
    service = _service()
    notification = Notification(user_id="stale-id", content="c", priority="normal")
    personalized = service.personalize_notification(notification, {"id": "real-id", "preferences": {}})
    assert personalized.user_id == "real-id"


def test_personalize_notification_selects_channel_from_preferences():
    service = _service()
    notification = Notification(user_id="u", content="c", priority="normal")
    personalized = service.personalize_notification(notification, {"id": "u", "preferences": {"notification_channel": "sms"}})
    assert personalized.channel == "sms"


def test_personalize_notification_appends_detail_pointer_for_detailed_verbosity():
    service = _service()
    notification = Notification(user_id="u", content="Earnings beat.", priority="normal")
    personalized = service.personalize_notification(
        notification, {"id": "u", "preferences": {"notification_verbosity": "detailed"}}
    )
    assert "explain_decision" in personalized.content
    assert personalized.content.startswith("Earnings beat.")


def test_personalize_notification_quiet_mode_downgrades_priority_except_for_escalate():
    service = _service()
    normal_notification = Notification(user_id="u", content="c", priority="high", actionability="notify")
    escalate_notification = Notification(user_id="u", content="c", priority="critical", actionability="escalate")
    quiet_preferences = {"id": "u", "preferences": {"quiet_mode": True}}

    assert service.personalize_notification(normal_notification, quiet_preferences).priority == "low"
    assert service.personalize_notification(escalate_notification, quiet_preferences).priority == "critical"


# --- deliver_notification: real call-through + AuditManager trail ----------


def test_deliver_notification_calls_through_the_injected_channel():
    channel = _SpyNotificationChannel(outcome=True)
    service = _service(notification_channel=channel)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-1")

    assert service.deliver_notification(notification) is True
    assert channel.sent == [notification]


def test_deliver_notification_records_success_and_failure_via_audit_manager():
    audit = _SpyAuditManager()
    success_service = _service(notification_channel=_SpyNotificationChannel(outcome=True), audit_manager=audit)
    success_service.deliver_notification(Notification(user_id="u", content="c", priority="normal", id="n-1"))
    assert audit.events == [("notification_delivered", {"notification_id": "n-1", "user_id": "u", "channel": "default"})]

    audit_failure = _SpyAuditManager()
    failure_service = _service(notification_channel=_SpyNotificationChannel(outcome=False), audit_manager=audit_failure)
    failure_service.deliver_notification(Notification(user_id="u", content="c", priority="normal", id="n-2"))
    assert audit_failure.events == [
        ("notification_delivery_failed", {"notification_id": "n-2", "user_id": "u", "channel": "default"})
    ]


# --- PlaceholderNotificationChannel: honest, never claims real delivery ----


def test_placeholder_notification_channel_records_attempt_and_returns_false():
    infra = _InMemoryInfrastructure()
    channel = PlaceholderNotificationChannel(infrastructure=infra)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-1", channel="email")

    assert channel.send(notification) is False
    sent_records = list(infra._tables.get("notifications_sent", {}).values())
    assert len(sent_records) == 1
    assert sent_records[0]["notification_id"] == "n-1"
    assert sent_records[0]["delivered"] is False
    assert sent_records[0]["channel"] == "email"


# --- collect_feedback / collect_user_response: real, honest pending state --


def test_collect_feedback_creates_a_pending_interaction_and_returns_pending():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    service = _service(infra=infra, audit_manager=audit)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-1")

    feedback = service.collect_feedback(notification)

    assert feedback == UserFeedback(notification_id="n-1", response={"status": "pending"})
    stored = infra.retrieve("interactions", "n-1")
    assert stored["status"] == "pending_feedback"
    assert stored["feedback"] is None
    assert any(event[0] == "feedback_requested" for event in audit.events)


def test_collect_feedback_is_idempotent_and_does_not_duplicate_the_interaction_row():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-1")

    service.collect_feedback(notification)
    service.collect_feedback(notification)

    assert len(infra._tables["interactions"]) == 1


def test_collect_feedback_reports_real_feedback_once_externally_written():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-1")
    service.collect_feedback(notification)

    # Nothing in this project can do this yet (ADR-0039's named gap) —
    # simulated here as "some future external mechanism did."
    infra.store("interactions", {"id": "n-1", "feedback": {"rating": "useful"}})

    result = service.collect_feedback(notification)
    assert result == UserFeedback(notification_id="n-1", response={"rating": "useful"})


def test_collect_user_response_creates_a_pending_interaction_and_returns_pending():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    service = _service(infra=infra, audit_manager=audit)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-2")

    response = service.collect_user_response(notification)

    assert response == {"status": "pending", "notification_id": "n-2"}
    stored = infra.retrieve("interactions", "n-2")
    assert stored["status"] == "pending_response"
    assert any(event[0] == "response_requested" for event in audit.events)


def test_collect_user_response_reports_real_response_once_externally_written():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-2")
    service.collect_user_response(notification)

    infra.store("interactions", {"id": "n-2", "response": {"action": "dismissed"}})

    assert service.collect_user_response(notification) == {"action": "dismissed"}


def test_collect_feedback_and_collect_user_response_share_the_same_interaction_row():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)
    notification = Notification(user_id="u", content="c", priority="normal", id="n-3")

    service.collect_feedback(notification)
    service.collect_user_response(notification)

    assert len(infra._tables["interactions"]) == 1
    stored = infra.retrieve("interactions", "n-3")
    assert stored["notification_id"] == "n-3"
    assert stored["user_id"] == "u"


# --- StubInteractionNotification stays untouched ----------------------------


def test_stub_interaction_notification_is_unaffected_by_the_real_implementation():
    stub = StubInteractionNotification()
    notification = stub.generate_notification({"anything": "at all"})
    assert notification == Notification(user_id="stub-id", content="", priority="")
    assert stub.prioritize_notification(notification) == ""
    assert stub.explain_decision({}) == ""
    assert stub.collect_feedback(notification) == UserFeedback(notification_id="stub-id", response={})
    assert stub.collect_user_response(notification) == {}


# --- new dataclasses are real, importable shapes ----------------------------


def test_alert_explanation_interaction_dataclasses_hold_the_documented_fields():
    alert = Alert(id="a-1", notification_id="n-1", reason="risk spike", risk=0.7, raised_at="2026-08-26T00:00:00")
    interaction = Interaction(id="n-1", notification_id="n-1", user_id="u", status="pending_feedback")
    assert alert.notification_id == "n-1"
    assert interaction.feedback is None and interaction.response is None
