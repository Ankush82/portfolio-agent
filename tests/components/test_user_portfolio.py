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
    validate_stock_symbol,
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


# --- Holding dataclass validation (STORY-1) -------------------------------


def test_holding_accepts_each_valid_currency():
    """Both supported currencies (USD default and INR for Indian
    holdings) instantiate without error."""
    usd = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10)
    inr = Holding(portfolio_id="pf-1", security_id="RELIANCE", quantity=10, currency="INR")
    assert usd.currency == "USD"
    assert inr.currency == "INR"


def test_holding_defaults_currency_to_usd_when_not_specified():
    """The story explicitly says default currency is 'USD' so existing
    callers that don't know about the new field keep working without
    any code change."""
    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10)
    assert holding.currency == "USD"


def test_holding_rejects_an_unknown_currency_with_a_clear_error():
    """Invalid currencies are rejected with a ValueError that names the
    valid set — silently accepting 'EUR' would let a US price feed
    silently mis-expose the position later."""
    with pytest.raises(ValueError, match=r"Holding\.currency must be one of"):
        Holding(portfolio_id="pf-1", security_id="X", quantity=10, currency="EUR")


def test_holding_accepts_each_valid_exchange():
    """All four supported exchanges (NYSE/NASDAQ for US, NSE/BSE for
    Indian) plus None instantiate without error."""
    for exchange in ("NYSE", "NASDAQ", "NSE", "BSE", None):
        holding = Holding(portfolio_id="pf-1", security_id="X", quantity=10, exchange=exchange)
        assert holding.exchange == exchange


def test_holding_defaults_exchange_to_none_when_not_specified():
    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10)
    assert holding.exchange is None


def test_holding_rejects_an_unknown_exchange_with_a_clear_error():
    with pytest.raises(ValueError, match=r"Holding\.exchange must be one of"):
        Holding(portfolio_id="pf-1", security_id="X", quantity=10, exchange="LSE")


def test_holding_accepts_each_valid_symbol_suffix():
    """None (default), '.NS' for NSE-listed, '.BO' for BSE-listed.

    The security_id must be valid for the chosen suffix (1-20 chars
    from [A-Z0-9&-] for .NS, exactly 6 digits for .BO) now that
    STORY-3's full-symbol validator runs in __post_init__."""
    holding_ns = Holding(portfolio_id="pf-1", security_id="X", quantity=10, symbol_suffix=".NS")
    holding_bo = Holding(portfolio_id="pf-1", security_id="100000", quantity=10, symbol_suffix=".BO")
    holding_none = Holding(portfolio_id="pf-1", security_id="X", quantity=10, symbol_suffix=None)
    assert holding_ns.symbol_suffix == ".NS"
    assert holding_bo.symbol_suffix == ".BO"
    assert holding_none.symbol_suffix is None


def test_holding_defaults_symbol_suffix_to_none_when_not_specified():
    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10)
    assert holding.symbol_suffix is None


def test_holding_rejects_an_unknown_symbol_suffix_with_a_clear_error():
    with pytest.raises(ValueError, match=r"Holding\.symbol_suffix must be one of"):
        Holding(portfolio_id="pf-1", security_id="X", quantity=10, symbol_suffix=".L")


def test_holding_quantity_is_a_decimal_with_four_decimal_places_of_precision():
    """Story STORY-1 says price/quantity fields use Decimal with 4
    decimal places of precision (DECIMAL(18,4) intent), not float —
    callers passing int/float must still work, but the stored value
    must be a Decimal quantized to 4 places."""
    from decimal import Decimal

    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=12)
    assert isinstance(holding.quantity, Decimal)
    assert holding.quantity == Decimal("12.0000")


def test_holding_quantity_coerces_a_float_to_quantized_decimal():
    """Existing callers (and stored JSONB float quantities) keep
    working: a float input is coerced through Decimal(str(value)) and
    quantized to 4 places, not silently turned into a Decimal with
    whatever float-precision artifact it happened to carry."""
    from decimal import Decimal

    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=12.5)
    assert isinstance(holding.quantity, Decimal)
    assert holding.quantity == Decimal("12.5000")


def test_holding_quantity_quantizes_extra_decimal_digits_to_four_places():
    """A caller-supplied Decimal with more than 4 decimal places is
    rounded to 4 (matches DECIMAL(18,4) — no truncation surprises
    beyond the documented precision)."""
    from decimal import Decimal

    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=Decimal("12.123456789"))
    assert holding.quantity == Decimal("12.1235")


def test_holding_quantity_rejects_non_numeric_input_with_a_clear_error():
    """Strings that aren't parseable as numbers (or any non-numeric
    type) fail loudly rather than silently storing garbage."""
    with pytest.raises(ValueError, match=r"Holding\.quantity must be a real number"):
        Holding(portfolio_id="pf-1", security_id="AAPL", quantity="not-a-number")


# --- STORY-1 QA: targeted cross-field acceptance criteria -------------------


def test_qa_story1_holding_field_defaults_and_validation_round_trip():
    """Targeted QA for STORY-1's own acceptance criteria #1 and #2 —
    one self-contained round trip exercising the Holding dataclass the
    way this story's own change did: every new field is present with
    its required default, every documented valid value instantiates,
    and every documented invalid value raises ValueError with a
    message naming the valid set. Independent of the wider file so a
    regression in any of these three fields is caught as one failure.
    """
    from decimal import Decimal

    # --- AC #1: currency='USD' default + USD/INR accepted, anything else rejected ---
    usd_default = Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=10)
    assert usd_default.currency == "USD", "default currency must be USD"
    inr_explicit = Holding(portfolio_id="pf-qa", security_id="RELIANCE", quantity=10, currency="INR")
    assert inr_explicit.currency == "INR"
    for bad_currency in ("EUR", "usd", "Usd", "US", "INDIAN", "", "GBP"):
        with pytest.raises(ValueError) as exc_info:
            Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=10, currency=bad_currency)
        # The error message must name the valid set so callers can self-correct.
        assert "USD" in str(exc_info.value) and "INR" in str(exc_info.value), (
            f"ValueError for currency={bad_currency!r} must name the valid set "
            f"(USD, INR); got {exc_info.value!r}"
        )

    # --- AC #1: exchange default None + every documented value, invalid rejected ---
    assert usd_default.exchange is None, "default exchange must be None"
    for valid_exchange in ("NYSE", "NASDAQ", "NSE", "BSE", None):
        Holding(portfolio_id="pf-qa", security_id="X", quantity=10, exchange=valid_exchange)
    for bad_exchange in ("LSE", "TSX", "nyse", "BSEX", "NASDAQ:", "BSE-1"):
        with pytest.raises(ValueError) as exc_info:
            Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=10, exchange=bad_exchange)
        assert any(name in str(exc_info.value) for name in ("NYSE", "NASDAQ", "NSE", "BSE")), (
            f"ValueError for exchange={bad_exchange!r} must name the valid set; "
            f"got {exc_info.value!r}"
        )

    # --- AC #1: symbol_suffix default None + valid set, invalid rejected ---
    assert usd_default.symbol_suffix is None, "default symbol_suffix must be None"
    # security_id must satisfy validate_stock_symbol's real per-suffix
    # format rules (added by a later story) once symbol_suffix is one
    # of .NS/.BO -- "X" alone is a valid NSE body ([A-Z0-9&-]{1,20}) but
    # not a valid BSE one (exactly 6 digits), so a single placeholder ID
    # can't cover all three cases the way it could before that story.
    _placeholder_security_id = {None: "X", ".NS": "X", ".BO": "500325"}
    for valid_suffix in (None, ".NS", ".BO"):
        Holding(
            portfolio_id="pf-qa",
            security_id=_placeholder_security_id[valid_suffix],
            quantity=10,
            symbol_suffix=valid_suffix,
        )
    for bad_suffix in (".L", ".N", ".NSX", ".BSE", "NS", ".ns", ".bo", ""):
        with pytest.raises(ValueError) as exc_info:
            Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=10, symbol_suffix=bad_suffix)
        assert ".NS" in str(exc_info.value) and ".BO" in str(exc_info.value), (
            f"ValueError for symbol_suffix={bad_suffix!r} must name the valid set "
            f"(.NS, .BO); got {exc_info.value!r}"
        )

    # --- AC #2: quantity is Decimal with 4-decimal precision, not float ---
    # int input
    h_int = Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=10)
    assert isinstance(h_int.quantity, Decimal), (
        f"int input must coerce to Decimal; got {type(h_int.quantity).__name__}"
    )
    assert h_int.quantity == Decimal("10.0000"), (
        f"int input must quantize to 4 decimal places; got {h_int.quantity}"
    )
    # float input — must be coerced, not stored as float (this is the
    # load-bearing difference: a stored float would lose the DECIMAL(18,4)
    # intent the story calls out).
    h_float = Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=12.5)
    assert isinstance(h_float.quantity, Decimal), (
        f"float input must coerce to Decimal; got {type(h_float.quantity).__name__}"
    )
    assert h_float.quantity == Decimal("12.5000"), (
        f"float input must quantize to 4 decimal places; got {h_float.quantity}"
    )
    # str input that parses as a number
    h_str = Holding(portfolio_id="pf-qa", security_id="AAPL", quantity="7.125")
    assert isinstance(h_str.quantity, Decimal)
    assert h_str.quantity == Decimal("7.1250")
    # over-precision Decimal input — quantize to 4 places (banker's
    # rounding via Decimal.quantize; must be one of the two nearest
    # half-away values).
    h_over = Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=Decimal("0.123456"))
    assert h_over.quantity == Decimal("0.1235"), (
        f"over-precision must quantize to 4 places; got {h_over.quantity}"
    )
    # non-numeric string fails loudly with the documented message
    with pytest.raises(ValueError, match=r"Holding\.quantity must be a real number"):
        Holding(portfolio_id="pf-qa", security_id="AAPL", quantity="not-a-number")
    # None / object / list also rejected (not parseable as a number)
    for bad_q in (None, object(), [1, 2, 3], {"x": 1}):
        with pytest.raises(ValueError, match=r"Holding\.quantity must be a real number"):
            Holding(portfolio_id="pf-qa", security_id="AAPL", quantity=bad_q)


# --- STORY-3: validate_stock_symbol (server-side, NSE/BSE/US) -----------


def test_validate_stock_symbol_accepts_the_storys_nse_example_reliance_ns():
    """AC: NSE example RELIANCE.NS passes."""
    assert validate_stock_symbol("RELIANCE.NS") is None


def test_validate_stock_symbol_accepts_the_storys_nse_example_m_and_m_ns_with_ampersand():
    """AC: NSE example M&M.NS passes -- ampersand is in [A-Z0-9&-]."""
    assert validate_stock_symbol("M&M.NS") is None


def test_validate_stock_symbol_accepts_the_storys_bse_example_500325_bo():
    """AC: BSE example 500325.BO passes (exactly 6 digits + .BO)."""
    assert validate_stock_symbol("500325.BO") is None


def test_validate_stock_symbol_accepts_nse_body_at_minimum_length_one_char():
    """AC: NSE body is 1-20 chars from [A-Z0-9&-]; the 1-char edge
    must work."""
    assert validate_stock_symbol("A.NS") is None


def test_validate_stock_symbol_accepts_nse_body_at_maximum_length_twenty_chars():
    """AC: NSE body is 1-20 chars from [A-Z0-9&-]; the 20-char edge
    must work."""
    twenty_char_body = "A" * 20
    assert validate_stock_symbol(f"{twenty_char_body}.NS") is None


def test_validate_stock_symbol_accepts_another_valid_bse_6digit_body():
    """AC: BSE body is exactly 6 digits; another valid body passes."""
    assert validate_stock_symbol("100000.BO") is None


def test_validate_stock_symbol_rejects_nse_symbol_with_invalid_char_at_sign():
    """AC: invalid NSE example REL@IANCE.NS fails. '@' is not in
    [A-Z0-9&-]."""
    with pytest.raises(ValueError, match=r"invalid NSE stock symbol"):
        validate_stock_symbol("REL@IANCE.NS")


def test_validate_stock_symbol_rejects_nse_body_longer_than_twenty_chars():
    """AC: NSE body is 1-20 chars; a 21-char body is rejected with a
    clear error mentioning the NSE format."""
    twenty_one_char_body = "A" * 21
    with pytest.raises(ValueError, match=r"invalid NSE stock symbol"):
        validate_stock_symbol(f"{twenty_one_char_body}.NS")


def test_validate_stock_symbol_rejects_nse_symbol_with_empty_body_before_suffix():
    """AC: NSE body must be 1-20 chars; an empty body is rejected."""
    with pytest.raises(ValueError, match=r"invalid NSE stock symbol"):
        validate_stock_symbol(".NS")


def test_validate_stock_symbol_rejects_bse_symbol_with_only_five_digits():
    """AC: invalid BSE example 12345.BO fails (only 5 digits)."""
    with pytest.raises(ValueError, match=r"invalid BSE stock symbol"):
        validate_stock_symbol("12345.BO")


def test_validate_stock_symbol_rejects_bse_symbol_with_seven_digits():
    """AC: BSE body must be exactly 6 digits; 7 digits are rejected."""
    with pytest.raises(ValueError, match=r"invalid BSE stock symbol"):
        validate_stock_symbol("1234567.BO")


def test_validate_stock_symbol_rejects_bse_symbol_with_letters_in_body():
    """AC: BSE body must be exactly 6 *digits*; letters in the body
    are rejected."""
    with pytest.raises(ValueError, match=r"invalid BSE stock symbol"):
        validate_stock_symbol("ABC123.BO")


def test_validate_stock_symbol_rejects_lowercase_ns_suffix_with_a_clear_case_sensitive_error():
    """AC: lowercase suffix .ns is rejected with a clear error
    mentioning case-sensitivity (the story's reliance.ns example)."""
    with pytest.raises(ValueError, match=r"(?i)case-sensitive|lowercase"):
        validate_stock_symbol("reliance.ns")


def test_validate_stock_symbol_rejects_lowercase_bo_suffix_with_a_clear_case_sensitive_error():
    """AC: lowercase suffix .bo is rejected with a clear error
    mentioning case-sensitivity."""
    with pytest.raises(ValueError, match=r"(?i)case-sensitive|lowercase"):
        validate_stock_symbol("reliance.bo")


def test_validate_stock_symbol_rejects_uppercase_body_with_lowercase_ns_suffix():
    """AC: suffix case-sensitivity is independent of body case --
    RELIANCE.ns is still rejected for its lowercase suffix."""
    with pytest.raises(ValueError, match=r"(?i)case-sensitive|lowercase"):
        validate_stock_symbol("RELIANCE.ns")


def test_validate_stock_symbol_rejects_bse_body_with_lowercase_bo_suffix():
    """AC: 500325.bo (lowercase suffix on a valid BSE body) is still
    rejected for the suffix being lowercase."""
    with pytest.raises(ValueError, match=r"(?i)case-sensitive|lowercase"):
        validate_stock_symbol("500325.bo")


def test_validate_stock_symbol_accepts_us_format_symbols_without_any_new_rules():
    """AC: US-format symbols (no .NS/.BO suffix) are accepted
    unchanged -- no new US-specific rules invented. AAPL, BRK.B, and
    a long ticker all pass without rejection."""
    assert validate_stock_symbol("AAPL") is None
    assert validate_stock_symbol("BRK.B") is None
    assert validate_stock_symbol("A" * 30) is None  # arbitrary length US-style
    assert validate_stock_symbol("123") is None  # pure digits, no suffix


def test_validate_stock_symbol_rejects_non_string_input_with_a_clear_error():
    """Defensive: a non-string input (None, int, list) raises a clear
    ValueError rather than crashing inside the regex match."""
    with pytest.raises(ValueError, match=r"stock symbol must be a string"):
        validate_stock_symbol(None)
    with pytest.raises(ValueError, match=r"stock symbol must be a string"):
        validate_stock_symbol(12345)
    with pytest.raises(ValueError, match=r"stock symbol must be a string"):
        validate_stock_symbol(["RELIANCE", ".NS"])


# --- STORY-3 integration: Holding.__post_init__ calls the validator ------


def test_holding_accepts_a_valid_nse_full_symbol_via_security_id_and_suffix():
    """Integration: Holding(security_id='RELIANCE', symbol_suffix='.NS')
    constructs successfully -- the new validator wired into
    Holding.__post_init__ accepts the full symbol string."""
    holding = Holding(portfolio_id="pf-1", security_id="RELIANCE", quantity=10, symbol_suffix=".NS")
    assert holding.security_id == "RELIANCE"
    assert holding.symbol_suffix == ".NS"


def test_holding_accepts_a_valid_bse_full_symbol_via_security_id_and_suffix():
    """Integration: Holding(security_id='500325', symbol_suffix='.BO')
    constructs successfully."""
    holding = Holding(portfolio_id="pf-1", security_id="500325", quantity=10, symbol_suffix=".BO")
    assert holding.security_id == "500325"
    assert holding.symbol_suffix == ".BO"


def test_holding_rejects_invalid_nse_body_via_security_id_with_a_clear_error():
    """Integration: an invalid NSE body (REL@IANCE with '@') fails
    through Holding.__post_init__'s call to validate_stock_symbol, with
    the same clear error as the standalone function."""
    with pytest.raises(ValueError, match=r"invalid NSE stock symbol"):
        Holding(portfolio_id="pf-1", security_id="REL@IANCE", quantity=10, symbol_suffix=".NS")


def test_holding_rejects_invalid_bse_body_with_only_5_digits_via_security_id():
    """Integration: 12345 (only 5 digits before .BO) fails through
    Holding.__post_init__ -- the standalone '12345.BO' example
    flows through Holding too."""
    with pytest.raises(ValueError, match=r"invalid BSE stock symbol"):
        Holding(portfolio_id="pf-1", security_id="12345", quantity=10, symbol_suffix=".BO")


def test_holding_skips_validate_stock_symbol_for_us_format_symbols_with_no_suffix():
    """Integration: a US-format symbol (symbol_suffix=None) skips the
    new validator entirely -- no new US rules are invented; existing
    callers keep working without any code change."""
    # Plain US symbol, no exchange/suffix set -- exactly the pre-STORY-3
    # behavior, must keep constructing.
    holding = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10)
    assert holding.security_id == "AAPL"
    assert holding.symbol_suffix is None


def test_holding_still_rejects_lowercase_suffix_via_the_existing_symbol_suffix_check():
    """Integration: a lowercase suffix passed as symbol_suffix is
    caught by the *existing* _VALID_SYMBOL_SUFFIXES check (which runs
    before the new validator), so callers see the same clear error
    about the valid suffix set they saw before STORY-3."""
    with pytest.raises(ValueError, match=r"Holding\.symbol_suffix must be one of"):
        Holding(portfolio_id="pf-1", security_id="RELIANCE", quantity=10, symbol_suffix=".ns")
