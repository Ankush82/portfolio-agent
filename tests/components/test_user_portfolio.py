"""Tests for DefaultUserPortfolio, BrokerConnector, and
PlaceholderBrokerConnector (src/components/c01_user_portfolio.py).

Uses an in-memory Infrastructure test double rather than a live
Postgres/Redis connection, so these tests run unconditionally (unlike
tests/test_infrastructure_postgres.py, which needs a live service and
skips cleanly without one) — DefaultUserPortfolio's own logic (real
persistence calls, real provenance tagging, real exposure math, real
relevance lookups) is what's under test here, not DefaultInfrastructure
itself, which already has its own dedicated test suite.
"""

import json

import pytest

from components.c01_user_portfolio import (
    DefaultUserPortfolio,
    Holding,
    PlaceholderBrokerConnector,
    Portfolio,
    PortfolioSnapshot,
    Position,
    StubUserPortfolio,
    Transaction,
    User,
)
from components.c04_knowledge_entity import DefaultKnowledgeEntity
from cross_cutting import observability
from cross_cutting.security import DefaultBoundaryGate, Provenance


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double. store/retrieve/query mirror
    DefaultInfrastructure's real semantics closely enough for this
    component's tests: store() upserts by record["id"] (or a generated
    id), retrieve() looks up by id, query() returns records that
    contain every key/value in the filter dict (the same containment
    match DefaultInfrastructure.query documents for its JSONB `@>`
    operator). publish/subscribe/schedule/cache_get/cache_set/get_secret
    are unused by DefaultUserPortfolio and are not implemented."""

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


class _FakeBrokerConnector:
    """Test double returning caller-configured holdings/transactions,
    so the import/synchronize/exposure/relevance pipeline can be
    exercised with real (non-empty) data — PlaceholderBrokerConnector
    always returns empty lists by design (ADR-0022), which is correct
    for it but not useful for testing what happens when a connector
    actually returns something."""

    def __init__(self, holdings: list[dict] | None = None, transactions: list[dict] | None = None) -> None:
        self._holdings = holdings or []
        self._transactions = transactions or []
        self.connect_calls: list[tuple[User, dict]] = []

    def connect(self, user: User, broker_credentials: dict) -> dict:
        self.connect_calls.append((user, broker_credentials))
        return {"external_account_id": "acct-123", "broker": "fake"}

    def fetch_holdings(self, portfolio: Portfolio) -> list[dict]:
        return list(self._holdings)

    def fetch_transactions(self, portfolio: Portfolio) -> list[dict]:
        return list(self._transactions)


# --- onboard_user / manage_preferences -----------------------------------


def test_onboard_user_generates_a_real_id_and_persists_preferences():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)

    user = portfolio_component.onboard_user({"preferences": {"risk_tolerance": "low"}})

    assert user.id
    assert user.preferences == {"risk_tolerance": "low"}
    stored = infra.retrieve("users", user.id)
    assert stored["preferences"] == {"risk_tolerance": "low"}


def test_onboard_user_generates_distinct_ids_for_distinct_users():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())

    first = portfolio_component.onboard_user({})
    second = portfolio_component.onboard_user({})

    assert first.id != second.id


def test_onboard_user_persists_email_when_given():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)

    user = portfolio_component.onboard_user({"email": "real@example.com"})

    assert user.email == "real@example.com"
    assert infra.retrieve("users", user.id)["email"] == "real@example.com"


def test_onboard_user_defaults_email_to_empty_string_when_omitted():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())

    user = portfolio_component.onboard_user({})

    assert user.email == ""


def test_manage_preferences_preserves_stored_email_across_a_preference_update():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    user = portfolio_component.onboard_user({"email": "real@example.com", "preferences": {"risk_tolerance": "low"}})

    updated = portfolio_component.manage_preferences(User(id=user.id, preferences={}), {"notify_on_drift": True})

    assert updated.email == "real@example.com"
    assert infra.retrieve("users", user.id)["email"] == "real@example.com"


def test_manage_preferences_merges_updates_onto_stored_preferences_not_just_the_passed_in_user():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    user = portfolio_component.onboard_user({"preferences": {"risk_tolerance": "low"}})

    # Caller passes a stale User object (no preferences attached) — the
    # merge must come from what's actually stored, not from this object.
    stale_user = User(id=user.id, preferences={})
    updated = portfolio_component.manage_preferences(stale_user, {"notify_on_drift": True})

    assert updated.preferences == {"risk_tolerance": "low", "notify_on_drift": True}
    assert infra.retrieve("users", user.id)["preferences"] == {
        "risk_tolerance": "low",
        "notify_on_drift": True,
    }


def test_manage_preferences_on_a_never_stored_user_falls_back_to_the_passed_in_user():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    unknown_user = User(id="never-onboarded", preferences={"a": 1})

    updated = portfolio_component.manage_preferences(unknown_user, {"b": 2})

    assert updated.preferences == {"a": 1, "b": 2}


# --- connect_portfolio -----------------------------------------------------


def test_connect_portfolio_tags_broker_connection_untrusted_and_persists_it():
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnector()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    user = User(id="user-1", preferences={})

    portfolio = portfolio_component.connect_portfolio(user, {"api_key": "irrelevant"})

    assert portfolio.user_id == "user-1"
    assert portfolio.id
    assert connector.connect_calls == [(user, {"api_key": "irrelevant"})]

    stored = infra.retrieve("portfolios", portfolio.id)
    assert stored["broker_connection"]["provenance"] == Provenance.UNTRUSTED.name
    assert stored["broker_connection"]["external_account_id"] == "acct-123"


def test_connect_portfolio_with_placeholder_connector_produces_synthetic_unmistakable_connection():
    portfolio_component = DefaultUserPortfolio(
        infrastructure=_InMemoryInfrastructure(), broker_connector=PlaceholderBrokerConnector()
    )
    user = User(id="user-1", preferences={})

    portfolio = portfolio_component.connect_portfolio(user, {})

    assert portfolio.user_id == "user-1"
    # PlaceholderBrokerConnector's own contract (ADR-0022): synthetic,
    # never a value a real broker would return.
    assert portfolio.id  # a real portfolio is still constructed


def test_connect_portfolio_records_an_audit_event(tmp_path, monkeypatch):
    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    portfolio_component = DefaultUserPortfolio(
        infrastructure=_InMemoryInfrastructure(), broker_connector=_FakeBrokerConnector()
    )
    user = User(id="user-1", preferences={})

    portfolio = portfolio_component.connect_portfolio(user, {})

    lines = audit_log_path.read_text().splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["event_type"] == "portfolio_connected"
    assert logged["detail"]["portfolio_id"] == portfolio.id
    assert logged["detail"]["provenance"] == Provenance.UNTRUSTED.name


# --- import_holdings / import_transactions ---------------------------------


def test_import_holdings_with_placeholder_connector_returns_a_real_empty_list():
    portfolio_component = DefaultUserPortfolio(
        infrastructure=_InMemoryInfrastructure(), broker_connector=PlaceholderBrokerConnector()
    )
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    holdings = portfolio_component.import_holdings(portfolio)

    assert holdings == []


def test_import_holdings_with_data_builds_tagged_holdings_and_persists_them():
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnector(holdings=[{"security_id": "AAPL", "quantity": 10.0}])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    holdings = portfolio_component.import_holdings(portfolio)

    assert holdings == [Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10.0)]
    stored = infra.retrieve("holdings", "pf-1:AAPL")
    assert stored["provenance"] == Provenance.UNTRUSTED.name
    assert stored["security_id"] == "AAPL"
    assert stored["quantity"] == 10.0


def test_import_transactions_with_data_builds_tagged_transactions_and_persists_them():
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnector(transactions=[{"kind": "buy", "amount": 500.0}])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    transactions = portfolio_component.import_transactions(portfolio)

    assert transactions == [Transaction(portfolio_id="pf-1", kind="buy", amount=500.0)]
    stored_records = infra.query("transactions", {"portfolio_id": "pf-1"})
    assert len(stored_records) == 1
    assert stored_records[0]["provenance"] == Provenance.UNTRUSTED.name


# --- synchronize_portfolio / track_portfolio_state -------------------------


def test_synchronize_portfolio_reimports_then_assembles_a_snapshot():
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnector(holdings=[{"security_id": "AAPL", "quantity": 10.0}])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    snapshot = portfolio_component.synchronize_portfolio(portfolio)

    assert isinstance(snapshot, PortfolioSnapshot)
    assert snapshot.portfolio_id == "pf-1"
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].holding.security_id == "AAPL"
    assert snapshot.exposure == {"AAPL": {"market_value": 10.0, "weight": 1.0}}


def test_track_portfolio_state_reads_previously_stored_holdings_without_calling_the_broker_again():
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnector(holdings=[{"security_id": "MSFT", "quantity": 5.0}])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio = Portfolio(id="pf-2", user_id="user-1")
    portfolio_component.import_holdings(portfolio)

    # Swap in a connector that would raise if it were ever called, to
    # prove track_portfolio_state only reads storage.
    class _ExplodingConnector(PlaceholderBrokerConnector):
        def fetch_holdings(self, portfolio):
            raise AssertionError("track_portfolio_state must not call the broker connector")

    portfolio_component._broker_connector = _ExplodingConnector()
    snapshot = portfolio_component.track_portfolio_state(portfolio)

    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].holding.security_id == "MSFT"


def test_track_portfolio_state_on_a_portfolio_with_no_stored_holdings_returns_an_empty_snapshot():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    portfolio = Portfolio(id="pf-empty", user_id="user-1")

    snapshot = portfolio_component.track_portfolio_state(portfolio)

    assert snapshot.positions == []
    assert snapshot.exposure == {}


# --- calculate_exposure -----------------------------------------------------


def test_calculate_exposure_aggregates_positions_sharing_a_security_and_computes_weight():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    portfolio = Portfolio(id="pf-3", user_id="user-1")
    infra.store("holdings", {"id": "pf-3:AAPL", "portfolio_id": "pf-3", "security_id": "AAPL", "quantity": 6.0})
    infra.store("holdings", {"id": "pf-3:GOOG", "portfolio_id": "pf-3", "security_id": "GOOG", "quantity": 4.0})

    snapshot = portfolio_component.track_portfolio_state(portfolio)

    assert snapshot.exposure == {
        "AAPL": {"market_value": 6.0, "weight": 0.6},
        "GOOG": {"market_value": 4.0, "weight": 0.4},
    }


def test_calculate_exposure_on_zero_total_market_value_does_not_divide_by_zero():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-4",
        positions=[
            Position(
                holding=Holding(portfolio_id="pf-4", security_id="ZERO", quantity=0.0),
                market_value=0.0,
            )
        ],
        exposure={},
    )

    exposure = portfolio_component.calculate_exposure(snapshot)

    assert exposure == {"ZERO": {"market_value": 0.0, "weight": 0.0}}


# --- determine_user_relevance -----------------------------------------------


def test_determine_user_relevance_true_when_event_security_is_held():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    user = User(id="user-1", preferences={})
    infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})
    infra.store("holdings", {"id": "pf-1:AAPL", "portfolio_id": "pf-1", "security_id": "AAPL", "quantity": 1.0})

    assert portfolio_component.determine_user_relevance(user, {"security_id": "AAPL"}) is True


def test_determine_user_relevance_false_when_event_security_is_not_held():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    user = User(id="user-1", preferences={})
    infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})
    infra.store("holdings", {"id": "pf-1:AAPL", "portfolio_id": "pf-1", "security_id": "AAPL", "quantity": 1.0})

    assert portfolio_component.determine_user_relevance(user, {"security_id": "TSLA"}) is False


def test_determine_user_relevance_false_when_event_has_no_security_id():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    user = User(id="user-1", preferences={})

    assert portfolio_component.determine_user_relevance(user, {"headline": "market moves"}) is False


def test_determine_user_relevance_false_for_a_user_with_no_portfolios():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    user = User(id="lonely-user", preferences={})

    assert portfolio_component.determine_user_relevance(user, {"security_id": "AAPL"}) is False


# --- manual stock entry (ADR-0044) ------------------------------------------


def test_list_available_securities_on_an_empty_registry_returns_an_empty_list():
    """A fresh, unseeded Knowledge & Entity Model registry honestly
    has nothing to offer a dropdown — this is a correct answer, not a
    bug (ADR-0044)."""
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())

    assert portfolio_component.list_available_securities() == []


def test_list_available_securities_returns_a_real_entity_registered_via_knowledge_entity():
    infra = _InMemoryInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Security", "name": "Apple Inc"})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)

    securities = portfolio_component.list_available_securities()

    assert securities == [apple]


def test_list_available_securities_merges_both_tradeable_kinds_and_excludes_non_tradeable_kinds():
    infra = _InMemoryInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    security = knowledge_entity.create_entity({"kind": "Security", "name": "Apple Inc"})
    company = knowledge_entity.create_entity({"kind": "Company", "name": "Microsoft Corp"})
    knowledge_entity.create_entity({"kind": "Sector", "name": "Technology"})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)

    securities = portfolio_component.list_available_securities()

    assert {entity.id for entity in securities} == {security.id, company.id}


def test_list_available_securities_filters_by_query_as_a_dropdown_would_while_typing():
    infra = _InMemoryInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Security", "name": "Apple Inc"})
    knowledge_entity.create_entity({"kind": "Security", "name": "Microsoft Corp"})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)

    assert portfolio_component.list_available_securities(query="app") == [apple]
    assert portfolio_component.list_available_securities(query="nonexistent") == []


def test_default_user_portfolio_uses_a_real_default_knowledge_entity_sharing_its_own_infrastructure():
    """No knowledge_entity injected — confirms the default constructor
    argument is a real DefaultKnowledgeEntity (ADR-0044) sharing this
    DefaultUserPortfolio's own Infrastructure, by registering an
    entity directly through Infrastructure and confirming it's found."""
    infra = _InMemoryInfrastructure()
    infra.store("entities", {"id": "entity-1", "kind": "Security", "name": "Apple Inc", "aliases": []})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)

    assert isinstance(portfolio_component._knowledge_entity, DefaultKnowledgeEntity)
    securities = portfolio_component.list_available_securities()
    assert [entity.id for entity in securities] == ["entity-1"]


def test_add_holding_manually_for_a_real_known_security_persists_and_returns_a_real_holding():
    infra = _InMemoryInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Security", "name": "Apple Inc"})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    holding = portfolio_component.add_holding_manually(portfolio, apple.id, 12.0)

    assert holding == Holding(portfolio_id="pf-1", security_id=apple.id, quantity=12.0)
    stored = infra.retrieve("holdings", f"pf-1:{apple.id}")
    assert stored["security_id"] == apple.id
    assert stored["quantity"] == 12.0


def test_add_holding_manually_does_not_tag_provenance_unlike_broker_imported_holdings():
    """Contrast with import_holdings (ADR-0022): manual entry is
    direct, validated user input, not external broker content, so it
    is never tagged UNTRUSTED (ADR-0044)."""
    infra = _InMemoryInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Security", "name": "Apple Inc"})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    portfolio_component.add_holding_manually(portfolio, apple.id, 12.0)

    stored = infra.retrieve("holdings", f"pf-1:{apple.id}")
    assert "provenance" not in stored


def test_add_holding_manually_with_an_unresolvable_security_id_fails_clearly():
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    with pytest.raises(ValueError):
        portfolio_component.add_holding_manually(portfolio, "no-such-security", 5.0)

    # No holding was silently created for the nonexistent security.
    infra = portfolio_component._infrastructure
    assert infra.query("holdings", {"portfolio_id": "pf-1"}) == []


def test_add_transaction_manually_mirrors_import_transactions_shape_and_persists():
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    transaction = portfolio_component.add_transaction_manually(portfolio, kind="buy", amount=250.0)

    assert transaction == Transaction(portfolio_id="pf-1", kind="buy", amount=250.0)
    stored_records = infra.query("transactions", {"portfolio_id": "pf-1"})
    assert len(stored_records) == 1
    assert stored_records[0]["kind"] == "buy"
    assert "provenance" not in stored_records[0]


def test_manually_added_holding_flows_through_track_portfolio_state_and_calculate_exposure_like_a_broker_imported_one():
    """The whole point of ADR-0044: downstream methods (track_portfolio_
    state, calculate_exposure) must treat a manually-entered holding
    exactly like one that came from import_holdings — they only ever
    consume Holding/PortfolioSnapshot, indifferent to provenance of
    origin."""
    infra = _InMemoryInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Security", "name": "Apple Inc"})
    msft = knowledge_entity.create_entity({"kind": "Security", "name": "Microsoft Corp"})
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    portfolio_component.add_holding_manually(portfolio, apple.id, 6.0)
    portfolio_component.add_holding_manually(portfolio, msft.id, 4.0)

    snapshot = portfolio_component.track_portfolio_state(portfolio)

    assert len(snapshot.positions) == 2
    assert {position.holding.security_id for position in snapshot.positions} == {apple.id, msft.id}
    assert snapshot.exposure == {
        apple.id: {"market_value": 6.0, "weight": 0.6},
        msft.id: {"market_value": 4.0, "weight": 0.4},
    }
    # determine_user_relevance's structural lookup also sees it, the
    # same way it would for a broker-imported holding.
    infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})
    user = User(id="user-1", preferences={})
    assert portfolio_component.determine_user_relevance(user, {"security_id": apple.id}) is True


# --- PlaceholderBrokerConnector ---------------------------------------------


def test_placeholder_broker_connector_connect_is_synthetic_and_never_looks_real():
    connector = PlaceholderBrokerConnector()
    user = User(id="user-1", preferences={})

    result = connector.connect(user, {"api_key": "whatever"})

    assert result["broker"] == "placeholder"
    assert result["external_account_id"].startswith("placeholder-account-")


def test_placeholder_broker_connector_fetch_methods_return_empty_not_synthetic_positions():
    connector = PlaceholderBrokerConnector()
    portfolio = Portfolio(id="pf-1", user_id="user-1")

    assert connector.fetch_holdings(portfolio) == []
    assert connector.fetch_transactions(portfolio) == []


# --- DefaultBoundaryGate wiring, end to end ---------------------------------


def test_default_user_portfolio_uses_a_real_default_boundary_gate_by_default():
    """No boundary_gate injected — confirms the default constructor
    argument is a real DefaultBoundaryGate (ADR-0022), not a stub, by
    checking the untrusted tag actually shows up in storage."""
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnector(holdings=[{"security_id": "AAPL", "quantity": 1.0}])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    assert isinstance(portfolio_component._boundary_gate, DefaultBoundaryGate)

    portfolio = Portfolio(id="pf-1", user_id="user-1")
    portfolio_component.import_holdings(portfolio)

    assert infra.retrieve("holdings", "pf-1:AAPL")["provenance"] == Provenance.UNTRUSTED.name


# --- StubUserPortfolio untouched --------------------------------------------


def test_stub_user_portfolio_untouched():
    """StubUserPortfolio stays a lightweight test double — every method
    still a traced no-op returning stub-shaped values. Guards against
    accidental edits to the stub while adding DefaultUserPortfolio
    alongside it."""
    stub = StubUserPortfolio()

    user = stub.onboard_user({"anything": "ignored"})
    assert user == User(id="stub-id", preferences={})

    portfolio = stub.connect_portfolio(user, {})
    assert portfolio == Portfolio(id="stub-id", user_id="stub-id")

    assert stub.import_holdings(portfolio) == []
    assert stub.import_transactions(portfolio) == []
    assert stub.calculate_exposure(PortfolioSnapshot(portfolio_id="x", positions=[], exposure={})) == {}
    assert stub.determine_user_relevance(user, {}) is True
