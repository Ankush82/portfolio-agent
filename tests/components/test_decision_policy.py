"""Tests for DefaultDecisionPolicy (src/components/c12_decision_policy.py,
ADR-0038).

Uses an in-memory Infrastructure test double (same containment-match
semantics as every other component's own fake) rather than a live
Postgres/Redis connection, paired with a real `DefaultUserPortfolio`
for `assess_relevance` — the point of these tests is to prove the real
weighted scoring, threshold, rate-limit, and pending-approval mechanism
ADR-0038 designed, not to mock the logic away. `authorize_action` and
`escalate` are exercised against spy BoundaryGate/AuditManager doubles
so the exact hand-off arguments are directly assertable.
"""

import uuid

from components.c01_user_portfolio import (
    DefaultUserPortfolio,
    Holding,
    Portfolio,
    PortfolioSnapshot,
    Position,
    User,
)
from components.c12_decision_policy import Decision, DefaultDecisionPolicy


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double — same semantics as
    test_user_portfolio.py's own double: store() upserts by
    record["id"] (or a generated id), retrieve() looks up by id,
    query() does containment matching."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self._next_id = 0

    def store(self, table: str, record: dict) -> str:
        self._next_id += 1
        record_id = str(record["id"]) if "id" in record else f"generated-{self._next_id}"
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


class _SpyBoundaryGate:
    def __init__(self, decision: bool = True) -> None:
        self.decision = decision
        self.authorize_calls: list[tuple[str, str, str]] = []

    def authenticate(self, identity: str) -> bool:
        return True

    def authorize(self, identity: str, action: str, resource: str) -> bool:
        self.authorize_calls.append((identity, action, resource))
        return self.decision

    def tag_provenance(self, content: dict, source: str) -> dict:
        return content


class _SpyAuditManager:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


def _service(infra=None, user_portfolio=None, boundary_gate=None, audit_manager=None, **kwargs) -> DefaultDecisionPolicy:
    infra = infra or _InMemoryInfrastructure()
    return DefaultDecisionPolicy(
        infrastructure=infra,
        user_portfolio=user_portfolio or DefaultUserPortfolio(infrastructure=infra),
        boundary_gate=boundary_gate or _SpyBoundaryGate(),
        audit_manager=audit_manager or _SpyAuditManager(),
        **kwargs,
    )


def _snapshot_with_position(security_id: str = "AAPL", market_value: float = 100.0) -> PortfolioSnapshot:
    holding = Holding(portfolio_id="pf-1", security_id=security_id, quantity=10.0)
    return PortfolioSnapshot(portfolio_id="pf-1", positions=[Position(holding=holding, market_value=market_value)], exposure={})


# --- assess_relevance: real delegation to UserPortfolio (01) ---------------


def test_assess_relevance_with_no_entity_id_is_zero():
    service = _service()
    assert service.assess_relevance({}, {}) == 0.0


def test_assess_relevance_with_portfolio_snapshot_uses_real_exposure_weight():
    service = _service()
    snapshot = _snapshot_with_position("AAPL", market_value=100.0)
    relevance = service.assess_relevance({"entity_id": "AAPL"}, {"portfolio_snapshot": snapshot})
    assert relevance == 1.0  # sole position -> full weight


def test_assess_relevance_with_portfolio_snapshot_entity_not_held_is_zero():
    service = _service()
    snapshot = _snapshot_with_position("AAPL", market_value=100.0)
    relevance = service.assess_relevance({"entity_id": "TSLA"}, {"portfolio_snapshot": snapshot})
    assert relevance == 0.0


def test_assess_relevance_partial_weight_reflects_real_exposure_math():
    service = _service()
    holding_a = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=1.0)
    holding_b = Holding(portfolio_id="pf-1", security_id="TSLA", quantity=1.0)
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[Position(holding=holding_a, market_value=25.0), Position(holding=holding_b, market_value=75.0)],
        exposure={},
    )
    assert service.assess_relevance({"entity_id": "AAPL"}, {"portfolio_snapshot": snapshot}) == 0.25


def test_assess_relevance_falls_back_to_boolean_user_relevance_without_a_snapshot():
    infra = _InMemoryInfrastructure()
    user_portfolio = DefaultUserPortfolio(infrastructure=infra)
    user = user_portfolio.onboard_user({})
    portfolio = user_portfolio.connect_portfolio(user, {})
    infra.store("holdings", {"id": "h1", "portfolio_id": portfolio.id, "security_id": "AAPL", "quantity": 5.0})
    service = _service(infra=infra, user_portfolio=user_portfolio)

    assert service.assess_relevance({"entity_id": "AAPL"}, {"user": user}) == 1.0
    assert service.assess_relevance({"entity_id": "TSLA"}, {"user": user}) == 0.0


def test_assess_relevance_with_neither_snapshot_nor_user_is_zero():
    service = _service()
    assert service.assess_relevance({"entity_id": "AAPL"}, {}) == 0.0


# --- assess_significance: real weighted formula -----------------------------


def test_assess_significance_high_confidence_and_high_level_is_near_one():
    service = _service()
    assert service.assess_significance({"confidence": 1.0, "significance": "high"}) == 1.0


def test_assess_significance_missing_keys_defaults_to_zero():
    service = _service()
    assert service.assess_significance({}) == 0.0


def test_assess_significance_is_the_documented_weighted_combination():
    service = _service()
    # 0.6 * 0.5 (confidence) + 0.4 * 0.6 (medium level score) = 0.54
    result = service.assess_significance({"confidence": 0.5, "significance": "medium"})
    assert round(result, 10) == 0.54


def test_assess_significance_unknown_level_string_scores_as_zero_contribution():
    service = _service()
    result = service.assess_significance({"confidence": 0.5, "significance": "not_a_real_level"})
    assert round(result, 10) == 0.3  # 0.6 * 0.5 + 0.4 * 0.0


# --- assess_risk: significance x uncertainty + contradiction penalty -------


def test_assess_risk_is_low_when_significant_and_confident_and_not_contradictory():
    service = _service()
    risk = service.assess_risk({"confidence": 1.0, "significance": "high", "was_contradictory": False})
    assert risk == 0.0  # uncertainty is zero, no penalty


def test_assess_risk_climbs_as_confidence_drops_on_a_significant_claim():
    service = _service()
    risk_low_confidence = service.assess_risk({"confidence": 0.0, "significance": "high", "was_contradictory": False})
    risk_high_confidence = service.assess_risk({"confidence": 1.0, "significance": "high", "was_contradictory": False})
    # significance(0.0) = 0.4, uncertainty = 1.0 -> risk = 0.4; significance(1.0) = 1.0, uncertainty = 0.0 -> risk = 0.0
    assert risk_low_confidence == 0.4
    assert risk_high_confidence == 0.0
    assert risk_low_confidence > risk_high_confidence


def test_assess_risk_adds_a_flat_contradiction_penalty():
    service = _service()
    risk_without = service.assess_risk({"confidence": 1.0, "significance": "high", "was_contradictory": False})
    risk_with = service.assess_risk({"confidence": 1.0, "significance": "high", "was_contradictory": True})
    assert round(risk_with - risk_without, 10) == 0.3


def test_assess_risk_clamps_out_of_range_confidence_defensively():
    service = _service()
    # confidence clamped to 1.0 -> significance 1.0, uncertainty 0.0 -> risk is just the penalty.
    risk = service.assess_risk({"confidence": 5.0, "significance": "high", "was_contradictory": True})
    assert risk == 0.3


# --- determine_actionability: real thresholds -------------------------------


def test_determine_actionability_suppresses_when_relevance_below_threshold():
    service = _service()
    decision = Decision(verified_claim={"relevance": 0.0, "confidence": 1.0, "significance": "high"}, actionability="")
    assert service.determine_actionability(decision) == "suppress"
    assert decision.actionability == "suppress"  # mutated in place


def test_determine_actionability_escalates_on_high_risk_even_when_significance_alone_would_not():
    service = _service()
    # confidence ~1/6, significance "high", contradictory: significance = 0.6*(1/6) + 0.4*1.0 = 0.5
    # (below the 0.70 escalate-by-significance threshold on its own), but
    # risk = 0.5 * (1 - 1/6) + 0.3 (contradiction penalty) ~= 0.717, above the 0.60 escalate-by-risk threshold.
    verified_claim = {"relevance": 1.0, "confidence": 1 / 6, "significance": "high", "was_contradictory": True}
    assert service.assess_significance(verified_claim) < 0.70
    assert service.assess_risk(verified_claim) >= 0.60
    decision = Decision(verified_claim=verified_claim, actionability="")
    assert service.determine_actionability(decision) == "escalate"


def test_determine_actionability_escalates_on_high_significance_even_with_low_risk():
    service = _service()
    decision = Decision(
        verified_claim={"relevance": 1.0, "confidence": 1.0, "significance": "high", "was_contradictory": False},
        actionability="",
    )
    assert service.determine_actionability(decision) == "escalate"


def test_determine_actionability_notifies_on_moderate_significance():
    service = _service()
    decision = Decision(
        verified_claim={"relevance": 1.0, "confidence": 0.6, "significance": "medium", "was_contradictory": False},
        actionability="",
    )
    assert service.determine_actionability(decision) == "notify"


def test_determine_actionability_suppresses_on_weak_but_relevant_claim():
    service = _service()
    decision = Decision(
        verified_claim={"relevance": 1.0, "confidence": 0.0, "significance": "unknown", "was_contradictory": False},
        actionability="",
    )
    assert service.determine_actionability(decision) == "suppress"


# --- authorize_action: real call-through to BoundaryGate (ADR-0020) --------


def test_authorize_action_calls_boundary_gate_with_destructured_action():
    gate = _SpyBoundaryGate(decision=True)
    service = _service(boundary_gate=gate)
    result = service.authorize_action({"identity": "user-1", "action": "notify", "resource": "portfolio:pf-1"})
    assert result is True
    assert gate.authorize_calls == [("user-1", "notify", "portfolio:pf-1")]


def test_authorize_action_returns_whatever_the_gate_decides():
    gate = _SpyBoundaryGate(decision=False)
    service = _service(boundary_gate=gate)
    assert service.authorize_action({}) is False


# --- enforce_policy: real sliding-window rate limit -------------------------


def test_enforce_policy_allows_calls_under_the_limit():
    service = _service(rate_limit_max_per_window=3)
    action = {"identity": "user-1", "action": "notify"}
    assert service.enforce_policy(action) is True
    assert service.enforce_policy(action) is True
    assert service.enforce_policy(action) is True


def test_enforce_policy_blocks_once_the_window_limit_is_reached():
    audit = _SpyAuditManager()
    service = _service(audit_manager=audit, rate_limit_max_per_window=2)
    action = {"identity": "user-1", "action": "notify"}
    assert service.enforce_policy(action) is True
    assert service.enforce_policy(action) is True
    assert service.enforce_policy(action) is False
    blocked = [event for event in audit.events if event[0] == "policy_rate_limit_blocked"]
    assert len(blocked) == 1
    assert blocked[0][1]["identity"] == "user-1"


def test_enforce_policy_tracks_identities_independently():
    service = _service(rate_limit_max_per_window=1)
    assert service.enforce_policy({"identity": "user-1"}) is True
    assert service.enforce_policy({"identity": "user-2"}) is True  # separate identity, separate budget
    assert service.enforce_policy({"identity": "user-1"}) is False


def test_enforce_policy_ignores_notifications_outside_the_window():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra, rate_limit_window_seconds=60.0, rate_limit_max_per_window=1)
    # Pre-seed a stale record well outside the 60s window.
    infra.store(
        "decision_policy_notifications",
        {"id": str(uuid.uuid4()), "identity": "user-1", "issued_at": 0.0, "action": "notify"},
    )
    assert service.enforce_policy({"identity": "user-1", "action": "notify"}) is True


# --- escalate: real AuditManager hand-off, matching DefaultRecoveryManager --


def test_escalate_records_reason_and_context_via_audit_manager():
    audit = _SpyAuditManager()
    service = _service(audit_manager=audit)
    service.escalate("risk threshold exceeded", {"claim": "AAPL earnings surprise"})
    assert audit.events == [
        ("decision_policy_escalation", {"reason": "risk threshold exceeded", "context": {"claim": "AAPL earnings surprise"}})
    ]


# --- request_approval: real pending-state mechanism -------------------------


def test_request_approval_creates_a_pending_record_and_returns_false():
    infra = _InMemoryInfrastructure()
    audit = _SpyAuditManager()
    service = _service(infra=infra, audit_manager=audit)

    result = service.request_approval({"id": "action-1", "kind": "large_trade_notification"})

    assert result is False
    stored = infra.retrieve("decision_policy_pending_approvals", "action-1")
    assert stored["status"] == "pending"
    assert stored["action"] == {"id": "action-1", "kind": "large_trade_notification"}
    assert any(event[0] == "approval_requested" for event in audit.events)


def test_request_approval_is_idempotent_for_a_repeated_id_while_still_pending():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)

    service.request_approval({"id": "action-1"})
    service.request_approval({"id": "action-1"})

    # Still pending, still False, and no duplicate record was created.
    assert service.request_approval({"id": "action-1"}) is False
    assert len(infra._tables["decision_policy_pending_approvals"]) == 1


def test_request_approval_reports_true_once_externally_marked_approved():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)
    service.request_approval({"id": "action-1"})

    # Nothing in this project can do this yet (ADR-0038's named gap) —
    # simulated here as "some future external mechanism did."
    infra.store("decision_policy_pending_approvals", {"id": "action-1", "status": "approved"})

    assert service.request_approval({"id": "action-1"}) is True


def test_request_approval_without_an_id_creates_a_fresh_request_each_call():
    infra = _InMemoryInfrastructure()
    service = _service(infra=infra)

    assert service.request_approval({"kind": "notify"}) is False
    assert service.request_approval({"kind": "notify"}) is False

    assert len(infra._tables["decision_policy_pending_approvals"]) == 2
