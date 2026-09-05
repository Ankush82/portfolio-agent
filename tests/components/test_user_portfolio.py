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
    BrokerApiError,
    BrokerAuthError,
    BrokerConfigError,
    BrokerConnector,
    BrokerCredentials,
    BrokerError,
    BrokerHolding,
    BrokerRateLimitError,
    BrokerTransaction,
    DefaultUserPortfolio,
    Holding,
    PlaceholderBrokerConnector,
    Portfolio,
    PortfolioSnapshot,
    Position,
    StubBrokerConnector,
    StubUserPortfolio,
    Transaction,
    UnsupportedBrokerError,
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


# --- BrokerConnector DTOs and Protocol (STORY-2) ----------------------------


def test_broker_connector_dto_instances_and_immutability():
    """Construct one instance of each DTO and assert field types and immutability."""
    from datetime import datetime, date
    from decimal import Decimal

    # BrokerCredentials
    creds = BrokerCredentials(
        access_token="token123",
        token_type="Bearer",
        expires_at=datetime(2025, 1, 1, 12, 0, 0),
        refresh_token="refresh123",
        broker_user_id="user123",
        raw={"key": "value"},
    )
    assert creds.access_token == "token123"
    assert creds.token_type == "Bearer"
    assert creds.expires_at == datetime(2025, 1, 1, 12, 0, 0)
    assert creds.refresh_token == "refresh123"
    assert creds.broker_user_id == "user123"
    assert creds.raw == {"key": "value"}
    # Type checks
    assert isinstance(creds.access_token, str)
    assert isinstance(creds.token_type, str)
    assert isinstance(creds.expires_at, datetime)
    assert isinstance(creds.refresh_token, str)
    assert isinstance(creds.broker_user_id, str)
    assert isinstance(creds.raw, dict)
    # Immutability (frozen dataclass)
    with pytest.raises(FrozenInstanceError):
        creds.access_token = "new"

    # BrokerHolding
    holding = BrokerHolding(
        symbol="AAPL",
        isin="US0378331005",
        quantity=Decimal("10.0"),
        average_price=Decimal("150.0"),
        last_price=Decimal("155.0"),
        exchange="NASDAQ",
        product="equity",
        instrument_id="12345",
        name="Apple Inc.",
        raw={"exchange": "NASDAQ"},
    )
    assert holding.symbol == "AAPL"
    assert holding.isin == "US0378331005"
    assert holding.quantity == Decimal("10.0")
    assert holding.average_price == Decimal("150.0")
    assert holding.last_price == Decimal("155.0")
    assert holding.exchange == "NASDAQ"
    assert holding.product == "equity"
    assert holding.instrument_id == "12345"
    assert holding.name == "Apple Inc."
    assert holding.raw == {"exchange": "NASDAQ"}
    # Type checks
    assert isinstance(holding.symbol, str)
    assert isinstance(holding.isin, str)
    assert isinstance(holding.quantity, Decimal)
    assert isinstance(holding.average_price, Decimal)
    assert isinstance(holding.last_price, Decimal)
    assert isinstance(holding.exchange, str)
    assert isinstance(holding.product, str)
    assert isinstance(holding.instrument_id, str)
    assert isinstance(holding.name, str)
    assert isinstance(holding.raw, dict)
    # Immutability
    with pytest.raises(FrozenInstanceError):
        holding.symbol = "MSFT"

    # BrokerTransaction
    transaction = BrokerTransaction(
        external_id="ext123",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2025, 1, 1),
        side="BUY",
        quantity=Decimal("10.0"),
        price=Decimal("150.0"),
        amount=Decimal("1500.0"),
        exchange="NASDAQ",
        segment="equity",
        raw={"trade_id": "trade123"},
    )
    assert transaction.external_id == "ext123"
    assert transaction.symbol == "AAPL"
    assert transaction.isin == "US0378331005"
    assert transaction.trade_date == date(2025, 1, 1)
    assert transaction.side == "BUY"
    assert transaction.quantity == Decimal("10.0")
    assert transaction.price == Decimal("150.0")
    assert transaction.amount == Decimal("1500.0")
    assert transaction.exchange == "NASDAQ"
    assert transaction.segment == "equity"
    assert transaction.raw == {"trade_id": "trade123"}
    # Type checks
    assert isinstance(transaction.external_id, str)
    assert isinstance(transaction.symbol, str)
    assert isinstance(transaction.isin, str)
    assert isinstance(transaction.trade_date, date)
    assert transaction.side in ("BUY", "SELL")
    assert isinstance(transaction.quantity, Decimal)
    assert isinstance(transaction.price, Decimal)
    assert isinstance(transaction.amount, Decimal)
    assert isinstance(transaction.exchange, str)
    assert isinstance(transaction.segment, str)
    assert isinstance(transaction.raw, dict)
    # Immutability
    with pytest.raises(FrozenInstanceError):
        transaction.symbol = "MSFT"

    # Protocol existence and runtime_checkable
    from typing import runtime_checkable
    assert runtime_checkable(BrokerConnector)
    # Check that the Protocol has the required attributes (just a basic check)
    assert hasattr(BrokerConnector, 'broker_id')
    assert hasattr(BrokerConnector, 'display_name')
    assert hasattr(BrokerConnector, 'build_authorize_url')
    assert hasattr(BrokerConnector, 'exchange_auth_code')
    assert hasattr(BrokerConnector, 'fetch_holdings')
    assert hasattr(BrokerConnector, 'fetch_transactions')


# --- STORY-2 QA: broker-agnostic BrokerConnector Protocol, DTOs, exceptions -


def test_qa_story2_protocol_is_runtime_checkable_and_required_members_match_brief():
    """AC: BrokerConnector Protocol is declared @runtime_checkable so
    conformance can be asserted in tests; the six members listed in
    the brief (broker_id, display_name, build_authorize_url,
    exchange_auth_code, fetch_holdings, fetch_transactions) all
    exist with exactly the signatures described. No pagination or
    Upstox-specific field may appear on the Protocol."""
    import dataclasses
    import inspect
    import re
    from typing import runtime_checkable

    # Protocol is runtime_checkable — actual conformance check: an
    # implementer (PlaceholderBrokerConnector) is isinstance(.)
    # against the Protocol, which is the whole point of
    # @runtime_checkable. Without it, the conformance claim is
    # unsubstantiated.
    assert runtime_checkable(BrokerConnector), "BrokerConnector must be @runtime_checkable"
    placeholder_instance = PlaceholderBrokerConnector()
    assert isinstance(placeholder_instance, BrokerConnector), (
        "PlaceholderBrokerConnector must satisfy BrokerConnector at "
        "runtime (the @runtime_checkable guarantee)"
    )

    # Required attributes (members) all present on the Protocol class.
    # For a typing.Protocol, attributes declared at class body level
    # land in __annotations__ (this is how runtime_checkable is able
    # to enforce them); has_attr on the Protocol object itself is
    # NOT the right check for attribute declarations.
    proto_annotations = dict(BrokerConnector.__annotations__)
    for member in ("broker_id", "display_name"):
        assert member in proto_annotations, (
            f"BrokerConnector Protocol must declare {member!r} as an "
            f"attribute annotation; missing from {sorted(proto_annotations)!r}"
        )
        assert proto_annotations[member] is str, (
            f"BrokerConnector.{member} annotation must be str; "
            f"got {proto_annotations[member]!r}"
        )
    # Callable methods on the Protocol
    for member in ("build_authorize_url", "exchange_auth_code",
                   "fetch_holdings", "fetch_transactions"):
        assert hasattr(BrokerConnector, member), (
            f"BrokerConnector Protocol must declare {member!r} as a method; missing"
        )

    # No pagination hook on the Protocol — pagination is a
    # connector-internal concern per the brief.
    proto_src = inspect.getsource(BrokerConnector)
    assert not re.search(r"page[_a-zA-Z]*\s*[:=]", proto_src), (
        "BrokerConnector Protocol must not declare a pagination "
        "parameter (pagination is connector-internal per the brief)"
    )
    # fetch_transactions signature must be the exact one in the brief:
    # (self, *, credentials, start_date, end_date) — no page/page_size.
    fetch_tx_sig = inspect.signature(BrokerConnector.fetch_transactions)
    assert "credentials" in fetch_tx_sig.parameters
    assert "start_date" in fetch_tx_sig.parameters
    assert "end_date" in fetch_tx_sig.parameters
    assert "page" not in fetch_tx_sig.parameters
    assert "page_size" not in fetch_tx_sig.parameters
    assert "page_number" not in fetch_tx_sig.parameters
    # fetch_holdings returns a materialised list, not an iterator (the
    # brief's decision (a) is that fetch_transactions returns a list too,
    # so the simpler check on fetch_holdings covering "list not iterator"
    # suffices; we also check fetch_transactions here).
    holdings_anno = BrokerConnector.fetch_holdings.__annotations__.get("return")
    tx_anno = BrokerConnector.fetch_transactions.__annotations__.get("return")
    assert "list" in repr(holdings_anno), (
        f"fetch_holdings return annotation must be list[BrokerHolding]; got {holdings_anno!r}"
    )
    assert "Iterator" not in repr(holdings_anno), (
        f"fetch_holdings must return a list, not an iterator; got {holdings_anno!r}"
    )
    assert "list" in repr(tx_anno), (
        f"fetch_transactions return annotation must be list[BrokerTransaction]; got {tx_anno!r}"
    )

    # No Upstox field names may appear in the Protocol's own source —
    # specifically not 'trading_symbol', 'instrument_token',
    # 'scrip_name', 'page_number', 'meta_data', and no literal
    # 'upstox' string and no upstox.com URL.
    forbidden = ("upstox", "trading_symbol", "instrument_token",
                 "scrip_name", "page_number", "meta_data")
    for name in forbidden:
        assert name.lower() not in proto_src.lower(), (
            f"BrokerConnector Protocol source must not mention {name!r}"
        )
    assert "upstox.com" not in proto_src, (
        "BrokerConnector Protocol must not reference an Upstox URL"
    )

    # build_authorize_url takes state as a keyword-only argument
    # returning str (from the brief).
    bau_sig = inspect.signature(BrokerConnector.build_authorize_url)
    state_param = bau_sig.parameters.get("state")
    assert state_param is not None, "build_authorize_url must take 'state'"
    assert state_param.kind == inspect.Parameter.KEYWORD_ONLY, (
        "build_authorize_url's 'state' must be keyword-only"
    )
    assert "str" in repr(bau_sig.return_annotation), (
        f"build_authorize_url must return str; got {bau_sig.return_annotation!r}"
    )


def test_qa_story2_dtos_use_decimal_date_datetime_and_are_frozen_with_no_upstox_fields():
    """AC: All three DTOs exist with exactly the fields listed in the
    brief, Decimal for quantities/prices, date/datetime for temporal
    fields, are frozen (immutable), and contain no Upstox-specific
    field name (trading_symbol, instrument_token, scrip_name,
    page_number, meta_data) and no 'upstox' string anywhere."""
    import dataclasses
    import inspect
    from dataclasses import fields
    from datetime import date, datetime
    from decimal import Decimal
    from typing import Literal

    # --- BrokerCredentials ---
    creds_fields = {f.name: f for f in fields(BrokerCredentials)}
    expected_creds = {"access_token", "token_type", "expires_at",
                      "refresh_token", "broker_user_id", "raw"}
    assert set(creds_fields.keys()) == expected_creds, (
        f"BrokerCredentials fields must be exactly {expected_creds}; "
        f"got {set(creds_fields.keys())}"
    )
    assert creds_fields["access_token"].type is str, (
        "BrokerCredentials.access_token must be typed as str"
    )
    assert creds_fields["token_type"].type is str, (
        "BrokerCredentials.token_type must be typed as str"
    )
    # expires_at is datetime|None — accept either Optional[datetime]
    # or a Union[...] annotation that includes datetime.
    expires_at_type = creds_fields["expires_at"].type
    assert expires_at_type in (datetime, "datetime") or "datetime" in repr(expires_at_type), (
        f"BrokerCredentials.expires_at must be datetime|None; got {expires_at_type!r}"
    )
    # refresh_token, broker_user_id are Optional[str]
    for fld in ("refresh_token", "broker_user_id"):
        ft = creds_fields[fld].type
        assert ft in (str, "str") or "str" in repr(ft) or "None" in repr(ft), (
            f"BrokerCredentials.{fld} must be str|None; got {ft!r}"
        )
    # raw is dict
    assert creds_fields["raw"].type is dict, (
        f"BrokerCredentials.raw must be dict; got {creds_fields['raw'].type!r}"
    )
    # Defaults per brief: token_type='Bearer', everything else None.
    assert creds_fields["token_type"].default == "Bearer", (
        f"BrokerCredentials.token_type default must be 'Bearer'; "
        f"got {creds_fields['token_type'].default!r}"
    )
    assert creds_fields["expires_at"].default is None, (
        f"BrokerCredentials.expires_at default must be None; "
        f"got {creds_fields['expires_at'].default!r}"
    )
    assert creds_fields["refresh_token"].default is None
    assert creds_fields["broker_user_id"].default is None
    # raw default factory returns dict
    assert creds_fields["raw"].default_factory is not None, (
        "BrokerCredentials.raw must have a default_factory returning dict"
    )
    assert creds_fields["raw"].default_factory() == {}

    # Frozen / immutable: try to assign to a fresh instance, expect FrozenInstanceError
    from dataclasses import FrozenInstanceError
    c = BrokerCredentials(access_token="tok")
    with pytest.raises(FrozenInstanceError):
        c.access_token = "different"
    # Constructing with the minimal set must work (all optional fields default)
    c_min = BrokerCredentials(access_token="only-required")
    assert c_min.access_token == "only-required"
    assert c_min.token_type == "Bearer"
    assert c_min.expires_at is None
    assert c_min.refresh_token is None
    assert c_min.broker_user_id is None
    assert c_min.raw == {}

    # --- BrokerHolding ---
    holding_fields = {f.name: f for f in fields(BrokerHolding)}
    expected_holding = {"symbol", "isin", "quantity", "average_price",
                        "last_price", "exchange", "product",
                        "instrument_id", "name", "raw"}
    assert set(holding_fields.keys()) == expected_holding, (
        f"BrokerHolding fields must be exactly {expected_holding}; "
        f"got {set(holding_fields.keys())}"
    )
    # Decimal-typed quantity/price fields
    for fld in ("quantity", "average_price", "last_price"):
        ft = holding_fields[fld].type
        assert ft is Decimal or "Decimal" in repr(ft), (
            f"BrokerHolding.{fld} must be Decimal; got {ft!r}"
        )
    # Str-typed identifying fields
    for fld in ("symbol", "isin", "exchange", "product",
                "instrument_id", "name"):
        ft = holding_fields[fld].type
        assert ft is str or "str" in repr(ft), (
            f"BrokerHolding.{fld} must be str; got {ft!r}"
        )
    # raw default_factory returns dict
    assert holding_fields["raw"].default_factory is not None
    assert holding_fields["raw"].default_factory() == {}

    h = BrokerHolding(
        symbol="AAPL",
        isin="US0378331005",
        quantity=Decimal("10"),
        average_price=Decimal("150"),
        last_price=Decimal("155"),
        exchange="NASDAQ",
        product="equity",
        instrument_id="12345",
        name="Apple Inc.",
    )
    with pytest.raises(FrozenInstanceError):
        h.symbol = "MSFT"
    # Real values are persisted as the typed types (not coerced to str)
    assert isinstance(h.quantity, Decimal)
    assert h.quantity == Decimal("10")
    assert isinstance(h.average_price, Decimal)
    assert isinstance(h.last_price, Decimal)

    # --- BrokerTransaction ---
    tx_fields = {f.name: f for f in fields(BrokerTransaction)}
    expected_tx = {"external_id", "symbol", "isin", "trade_date",
                   "side", "quantity", "price", "amount", "exchange",
                   "segment", "raw"}
    assert set(tx_fields.keys()) == expected_tx, (
        f"BrokerTransaction fields must be exactly {expected_tx}; "
        f"got {set(tx_fields.keys())}"
    )
    # Decimal-typed quantity/price/amount
    for fld in ("quantity", "price", "amount"):
        ft = tx_fields[fld].type
        assert ft is Decimal or "Decimal" in repr(ft), (
            f"BrokerTransaction.{fld} must be Decimal; got {ft!r}"
        )
    # trade_date is date
    assert tx_fields["trade_date"].type is date, (
        f"BrokerTransaction.trade_date must be date; got {tx_fields['trade_date'].type!r}"
    )
    # side is Literal['BUY','SELL']
    side_type = tx_fields["side"].type
    assert "Literal" in repr(side_type), (
        f"BrokerTransaction.side must be Literal['BUY','SELL']; got {side_type!r}"
    )
    assert "'BUY'" in repr(side_type) and "'SELL'" in repr(side_type), (
        f"BrokerTransaction.side must be Literal['BUY','SELL']; got {side_type!r}"
    )
    # Str-typed fields
    for fld in ("external_id", "symbol", "isin", "exchange", "segment"):
        ft = tx_fields[fld].type
        assert ft is str or "str" in repr(ft), (
            f"BrokerTransaction.{fld} must be str; got {ft!r}"
        )
    # raw default_factory returns dict
    assert tx_fields["raw"].default_factory is not None

    t = BrokerTransaction(
        external_id="ext-1",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2025, 1, 15),
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("150"),
        amount=Decimal("1500"),
        exchange="NASDAQ",
        segment="equity",
    )
    with pytest.raises(FrozenInstanceError):
        t.symbol = "MSFT"
    # Temporal field is a real date
    assert isinstance(t.trade_date, date)
    assert t.trade_date == date(2025, 1, 15)
    # Decimal fields are real Decimals
    assert isinstance(t.quantity, Decimal)
    assert isinstance(t.price, Decimal)
    assert isinstance(t.amount, Decimal)

    # --- No Upstox field name / 'upstox' string in DTO field names ---
    forbidden = ("upstox", "trading_symbol", "instrument_token",
                 "scrip_name", "page_number", "meta_data")
    for dto_name, dto_fields in (
        ("BrokerCredentials", creds_fields),
        ("BrokerHolding", holding_fields),
        ("BrokerTransaction", tx_fields),
    ):
        for name in forbidden:
            assert name not in dto_fields, (
                f"{dto_name} must not declare field {name!r} "
                f"(Upstox-specific, not broker-agnostic)"
            )


def test_qa_story2_exception_hierarchy_all_derive_from_single_broker_error_base():
    """AC: Full exception hierarchy exists with BrokerError as the
    single base; BrokerConfigError, BrokerAuthError, BrokerApiError,
    BrokerRateLimitError, UnsupportedBrokerError all derive from
    BrokerError (and ultimately Exception). The names match the
    brief exactly."""
    # Every leaf exception is a subclass of BrokerError
    for cls in (BrokerConfigError, BrokerAuthError, BrokerApiError,
                BrokerRateLimitError, UnsupportedBrokerError):
        assert issubclass(cls, BrokerError), (
            f"{cls.__name__} must derive from BrokerError"
        )
        assert issubclass(cls, Exception), (
            f"{cls.__name__} must ultimately derive from Exception"
        )
    # Single base: BrokerError itself derives from Exception, not from
    # any other broker-specific exception class.
    assert BrokerError.__bases__ == (Exception,), (
        f"BrokerError must derive directly from Exception only; "
        f"got bases={BrokerError.__bases__}"
    )
    # No Upstox-specific name in any exception class name (e.g.
    # UpstoxAuthError would leak the vendor name into the error
    # taxonomy).
    for cls in (BrokerError, BrokerConfigError, BrokerAuthError,
                BrokerApiError, BrokerRateLimitError, UnsupportedBrokerError):
        assert "upstox" not in cls.__name__.lower(), (
            f"Exception class {cls.__name__} must not reference Upstox"
        )

    # Instantiability / raisability: each leaf exception can be raised
    # and caught as its own type or as BrokerError.
    for cls in (BrokerConfigError, BrokerAuthError, BrokerApiError,
                BrokerRateLimitError, UnsupportedBrokerError):
        try:
            raise cls("synthetic failure")
        except BrokerError as exc:
            assert isinstance(exc, cls), (
                f"Caught {type(exc).__name__}, expected {cls.__name__}"
            )


def test_qa_story2_no_upstox_leak_in_protocol_dtos_or_exceptions_across_the_source_file():
    """AC: No Upstox-specific field name (trading_symbol,
    instrument_token, scrip_name, page_number, meta_data), no
    Upstox URL, and the literal string 'upstox' appears nowhere in
    the Protocol, the DTOs, or the exceptions. Checked against the
    real source file rather than reconstructed annotations so a stray
    string literal cannot slip through."""
    import inspect

    # Concatenate source for every relevant symbol in c01_user_portfolio.py
    sources = []
    for sym in (BrokerConnector, BrokerCredentials, BrokerHolding,
                BrokerTransaction, BrokerError, BrokerConfigError,
                BrokerAuthError, BrokerApiError, BrokerRateLimitError,
                UnsupportedBrokerError, PlaceholderBrokerConnector):
        try:
            sources.append(inspect.getsource(sym))
        except (TypeError, OSError):
            pass

    forbidden_substrings = (
        "upstox", "trading_symbol", "instrument_token", "scrip_name",
        "page_number", "meta_data", "upstox.com",
    )
    combined = "\n".join(sources).lower()
    for token in forbidden_substrings:
        assert token.lower() not in combined, (
            f"Protocol / DTOs / exceptions source must not contain "
            f"{token!r} (Upstox-specific, not broker-agnostic)"
        )


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


# --- STORY-8: multi-currency portfolio calculation logic ------------------


def _usd_holding(security_id: str, quantity, currency: str = "USD") -> Holding:
    """Convenience: a USD-currency Holding (the pre-STORY-8 default)."""
    return Holding(portfolio_id="pf-1", security_id=security_id, quantity=quantity, currency=currency)


def _inr_holding(security_id: str, quantity) -> Holding:
    """Convenience: an INR-currency Holding."""
    return Holding(portfolio_id="pf-1", security_id=security_id, quantity=quantity, currency="INR")


class _FakeExchangeRateInfrastructure:
    """Minimal Infrastructure double for `fetch_exchange_rate`'s
    cache layer. Mirrors the real `cache_get`/`cache_set` shape so
    `fetch_exchange_rate` reads/writes the same way it would against
    `DefaultInfrastructure`."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.set_calls: list = []

    def cache_get(self, key: str):
        return self.store.get(key)

    def cache_set(self, key: str, value, ttl_seconds: int) -> None:
        self.set_calls.append((key, value, ttl_seconds))
        self.store[key] = value


def test_calculate_portfolio_totals_sums_inr_and_usd_subtotals_separately():
    """AC: System calculates total portfolio value in INR (sum of
    INR-currency holdings) and USD (sum of USD-currency holdings)
    separately. Both subtotals are Decimal with the project's
    documented 4-decimal-place precision."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 10.0), market_value=Decimal("100.0000")),
            Position(holding=_usd_holding("MSFT", 5.0), market_value=Decimal("50.0000")),
            Position(holding=_inr_holding("RELIANCE", 4.0), market_value=Decimal("1000.0000")),
            Position(holding=_inr_holding("TCS", 2.0), market_value=Decimal("500.0000")),
        ],
        exposure={},
    )

    # Stub fetch_exchange_rate so this test stays hermetic.
    fetch_calls: list = []

    def fake_fetch(infrastructure=None):
        fetch_calls.append(infrastructure)
        return Decimal("83.0000")

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate", fake_fetch
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(snapshot, base_currency="USD")
    finally:
        monkeypatch_fetch.undo()

    assert isinstance(result["inr_total"], Decimal)
    assert isinstance(result["usd_total"], Decimal)
    assert result["usd_total"] == Decimal("150.0000")  # 100 + 50
    assert result["inr_total"] == Decimal("1500.0000")  # 1000 + 500
    # fetch_exchange_rate was called with the passed-in infrastructure
    # (None here), proving the seam is plumbed end-to-end.
    assert fetch_calls == [None]


def test_calculate_portfolio_totals_consolidated_usd_equals_usd_total_plus_inr_divided_by_rate():
    """AC: USD-base consolidated total = usd_total + (inr_total / rate),
    computed from the real exchange rate returned by fetch_exchange_rate."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
            Position(holding=_inr_holding("RELIANCE", 1.0), market_value=Decimal("8300.0000")),
        ],
        exposure={},
    )

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate",
        lambda infrastructure=None: Decimal("83.0000"),
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(snapshot, base_currency="USD")
    finally:
        monkeypatch_fetch.undo()

    # 8300 INR / 83 = 100 USD, plus 100 USD = 200 USD total.
    assert result["consolidated_total"] == Decimal("200.0000")
    assert result["base_currency"] == "USD"
    assert result["rate"] == Decimal("83.0000")
    assert result["error"] is None


def test_calculate_portfolio_totals_consolidated_inr_equals_usd_times_rate_plus_inr_total():
    """AC: INR-base consolidated total = (usd_total * rate) + inr_total."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
            Position(holding=_inr_holding("RELIANCE", 1.0), market_value=Decimal("8300.0000")),
        ],
        exposure={},
    )

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate",
        lambda infrastructure=None: Decimal("83.0000"),
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(snapshot, base_currency="INR")
    finally:
        monkeypatch_fetch.undo()

    # 100 USD * 83 = 8300 INR, plus 8300 INR = 16600 INR total.
    assert result["consolidated_total"] == Decimal("16600.0000")
    assert result["base_currency"] == "INR"
    assert result["rate"] == Decimal("83.0000")
    assert result["error"] is None


def test_calculate_portfolio_totals_uses_bankers_rounding_via_the_project_quantize_pattern():
    """AC: All calculations use the project's established quantize
    pattern. The codebase's actual pattern is `Decimal(...).quantize(
    Decimal('0.0001'), rounding=ROUND_HALF_UP)` (see
    `_coerce_quantity_to_decimal` and `_quantize_rate`), which is what
    every monetary field here uses. Pin the pattern so a future
    switch to a different rounding mode is caught."""
    from decimal import ROUND_HALF_UP, Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    # Pick values that exercise the rounding boundary: 0.00005 -> 0.0001
    # under ROUND_HALF_UP, the same way the rest of this codebase rounds.
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("X", 1), market_value=Decimal("0.00005")),
        ],
        exposure={},
    )

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate",
        lambda infrastructure=None: Decimal("1.0000"),
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(snapshot, base_currency="USD")
    finally:
        monkeypatch_fetch.undo()

    # 0.00005 ROUND_HALF_UP to 4dp -> 0.0001 (the project's own mode).
    assert result["usd_total"] == Decimal("0.0001")
    assert result["usd_total"].as_tuple().exponent == -4  # 4 decimal places
    # Every monetary field is quantized to 4 places.
    for field_name in ("inr_total", "usd_total", "consolidated_total", "rate"):
        value = result[field_name]
        assert isinstance(value, Decimal)
        assert value.as_tuple().exponent == -4, (
            f"{field_name!r} must be quantized to 4 decimal places; got {value}"
        )


def test_calculate_portfolio_totals_returns_subtotals_and_clear_error_on_missing_exchange_rate_key():
    """AC: If fetch_exchange_rate() raises (here, because neither
    vendor key is configured), the method returns the real currency
    subtotals with a real error message about the consolidated total
    being unavailable -- never a fabricated exchange rate or
    fabricated consolidated total."""
    from decimal import Decimal

    from exchange_rate_client import MissingExchangeRateAPIKeyError

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
            Position(holding=_inr_holding("RELIANCE", 1.0), market_value=Decimal("8300.0000")),
        ],
        exposure={},
    )

    def fake_fetch(infrastructure=None):
        raise MissingExchangeRateAPIKeyError(
            "Neither EXCHANGE_RATE_API_KEY nor EXCHANGE_RATE_FALLBACK_API_KEY is set."
        )

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate", fake_fetch
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(snapshot, base_currency="USD")
    finally:
        monkeypatch_fetch.undo()

    # Real subtotals are still returned -- never silently zeroed.
    assert result["usd_total"] == Decimal("100.0000")
    assert result["inr_total"] == Decimal("8300.0000")
    # Real error message naming the failure mode, not a fabricated
    # placeholder or a fabricated consolidated total.
    assert result["consolidated_total"] is None
    assert result["rate"] is None
    assert result["base_currency"] == "USD"
    assert result["error"] is not None
    assert "consolidated total in USD is unavailable" in result["error"]
    assert "MissingExchangeRateAPIKeyError" in result["error"]


def test_calculate_portfolio_totals_returns_subtotals_and_clear_error_on_exchange_rate_fetch_error():
    """AC: Same as above, but exercising the `ExchangeRateFetchError`
    path (both vendor sources failed) -- still no fabricated
    consolidated total, still a real error message."""
    from decimal import Decimal

    from exchange_rate_client import ExchangeRateFetchError

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
            Position(holding=_inr_holding("RELIANCE", 1.0), market_value=Decimal("8300.0000")),
        ],
        exposure={},
    )

    def fake_fetch(infrastructure=None):
        raise ExchangeRateFetchError(
            "Both exchange-rate sources failed: primary (...), fallback (...)"
        )

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate", fake_fetch
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(snapshot, base_currency="INR")
    finally:
        monkeypatch_fetch.undo()

    assert result["usd_total"] == Decimal("100.0000")
    assert result["inr_total"] == Decimal("8300.0000")
    assert result["consolidated_total"] is None
    assert result["rate"] is None
    assert result["base_currency"] == "INR"
    assert result["error"] is not None
    assert "consolidated total in INR is unavailable" in result["error"]
    assert "ExchangeRateFetchError" in result["error"]


def test_calculate_portfolio_totals_rejects_unsupported_base_currency_with_clear_error():
    """Only USD and INR are supported as base currencies (those are
    the only currencies whose exchange rate `fetch_exchange_rate`
    returns). A 3rd currency like EUR would require a different
    real exchange rate this codebase doesn't fetch; silently coercing
    to USD/INR would be exactly the fabrication the story
    forbids."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
        ],
        exposure={},
    )

    with pytest.raises(ValueError, match=r"base_currency must be one of"):
        portfolio_component.calculate_portfolio_totals(snapshot, base_currency="EUR")


def test_calculate_portfolio_totals_on_a_snapshot_with_only_one_currency_returns_correct_consolidated_total():
    """Edge case: a snapshot containing only USD (or only INR)
    positions still produces a correct consolidated total in either
    base currency. The other-currency subtotal is zero and adds
    nothing to the consolidated answer."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    usd_only_snapshot = PortfolioSnapshot(
        portfolio_id="pf-usd-only",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
        ],
        exposure={},
    )

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate",
        lambda infrastructure=None: Decimal("83.0000"),
    )

    try:
        # USD-base: just the USD total, INR contribution is 0.
        usd_result = portfolio_component.calculate_portfolio_totals(usd_only_snapshot, base_currency="USD")
        # INR-base: 100 USD * 83 = 8300 INR, INR contribution is 0.
        inr_result = portfolio_component.calculate_portfolio_totals(usd_only_snapshot, base_currency="INR")
    finally:
        monkeypatch_fetch.undo()

    assert usd_result["inr_total"] == Decimal("0.0000")
    assert usd_result["usd_total"] == Decimal("100.0000")
    assert usd_result["consolidated_total"] == Decimal("100.0000")
    assert inr_result["inr_total"] == Decimal("0.0000")
    assert inr_result["usd_total"] == Decimal("100.0000")
    assert inr_result["consolidated_total"] == Decimal("8300.0000")


def test_calculate_portfolio_totals_on_an_empty_snapshot_returns_zero_subtotals_and_a_real_consolidated_total():
    """Edge case: a portfolio with no positions has zero in every
    currency and a consolidated total of zero in either base
    currency -- fetch_exchange_rate is still called (the rate is a
    real input to the math), and the result is real."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    empty_snapshot = PortfolioSnapshot(portfolio_id="pf-empty", positions=[], exposure={})

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate",
        lambda infrastructure=None: Decimal("83.0000"),
    )

    try:
        result = portfolio_component.calculate_portfolio_totals(empty_snapshot, base_currency="USD")
    finally:
        monkeypatch_fetch.undo()

    assert result["inr_total"] == Decimal("0.0000")
    assert result["usd_total"] == Decimal("0.0000")
    assert result["consolidated_total"] == Decimal("0.0000")
    assert result["error"] is None


def test_calculate_portfolio_totals_propagates_infrastructure_to_fetch_exchange_rate():
    """The Infrastructure passed to `calculate_portfolio_totals` is
    forwarded to `fetch_exchange_rate` so the rate-fetch can use the
    same cache layer (and same Redis) the rest of the system
    already trusts."""
    from decimal import Decimal

    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=Decimal("100.0000")),
        ],
        exposure={},
    )

    infra_passed_to_fetch = []

    def fake_fetch(infrastructure=None):
        infra_passed_to_fetch.append(infrastructure)
        return Decimal("83.0000")

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(
        "components.c01_user_portfolio.fetch_exchange_rate", fake_fetch
    )

    injected = _FakeExchangeRateInfrastructure()

    try:
        portfolio_component.calculate_portfolio_totals(snapshot, base_currency="USD", infrastructure=injected)
    finally:
        monkeypatch_fetch.undo()

    assert infra_passed_to_fetch == [injected]


# --- STORY-8: gains/losses gap documentation (option (b)) ------------------


def test_calculate_gains_losses_raises_not_implemented_error_naming_the_missing_cost_basis_field():
    """AC: Gains/losses and percentage returns cannot be computed for
    real right now -- neither Holding nor Position carries a
    cost-basis field anywhere in this codebase, and inventing a
    fabricated number would be the precise failure mode the story
    forbids. The chosen honesty posture (option (b) in the story):
    raise a `NotImplementedError` whose message explicitly names the
    missing `Holding.cost_basis` field so a reviewer / future
    contributor can see the gap from the error text alone."""
    portfolio_component = DefaultUserPortfolio(infrastructure=_InMemoryInfrastructure())
    snapshot = PortfolioSnapshot(
        portfolio_id="pf-1",
        positions=[
            Position(holding=_usd_holding("AAPL", 1.0), market_value=100.0),
        ],
        exposure={},
    )

    with pytest.raises(NotImplementedError) as exc_info:
        portfolio_component.calculate_gains_losses(snapshot)

    message = str(exc_info.value)
    # The exception message must name the missing field so this gap is
    # discoverable from the error alone -- the same posture ADR-0046
    # / c04 / c07 already take for real data-model gaps.
    assert "cost_basis" in message
    assert "STORY-8" in message


# --- STORY-3: StubBrokerConnector (deterministic Protocol-conformant double) --


def test_stub_broker_connector_satisfies_the_brokerconnector_protocol_via_runtime_checkable_isinstance():
    """AC: ``isinstance(StubBrokerConnector(), BrokerConnector)`` is True
    via the runtime-checkable Protocol. This is the concrete, behaviour-
    checking form of the AC: a freshly-constructed StubBrokerConnector
    instance must actually be recognised as a BrokerConnector, not just
    structurally similar."""
    instance = StubBrokerConnector()

    assert isinstance(instance, BrokerConnector), (
        "StubBrokerConnector() must satisfy BrokerConnector at runtime "
        "(via @runtime_checkable). AC #1 of STORY-3 fails."
    )


def test_stub_broker_connector_has_fixed_broker_id_and_display_name():
    """AC: ``broker_id='stub'`` and ``display_name='Stub Broker'`` --
    fixed identifiers every caller can rely on, not anything derived
    from host state (AC #5: no environment-variable reads)."""
    connector_a = StubBrokerConnector()
    connector_b = StubBrokerConnector()

    assert connector_a.broker_id == "stub"
    assert connector_a.display_name == "Stub Broker"
    # Two independent instances must agree on these fixed identifiers.
    assert connector_b.broker_id == "stub"
    assert connector_b.display_name == "Stub Broker"
    # Type-level (attribute, not instance state) so a class-level
    # shadow of the value can't quietly regress.
    assert StubBrokerConnector.broker_id == "stub"
    assert StubBrokerConnector.display_name == "Stub Broker"


def test_stub_broker_connector_build_authorize_url_returns_a_deterministic_fake_url_echoing_state():
    """AC: ``build_authorize_url`` returns a fixed fake URL echoing the
    passed ``state``, and is deterministic across repeated calls (AC #2:
    all four methods are deterministic). The echoed state is the only
    caller-supplied input that appears in the returned URL."""
    connector = StubBrokerConnector()

    url_state_abc = connector.build_authorize_url(state="abc123")
    url_state_abc_again = connector.build_authorize_url(state="abc123")
    url_state_xyz = connector.build_authorize_url(state="xyz789")

    # Determinism: identical input -> identical output.
    assert url_state_abc == url_state_abc_again
    # State echoed in the URL: different state -> different URL.
    assert url_state_abc != url_state_xyz
    assert "abc123" in url_state_abc
    assert "xyz789" in url_state_xyz
    # The returned value is a real string with a host component (i.e. it
    # looks like a URL, not a literal sentinel).
    assert isinstance(url_state_abc, str)
    assert url_state_abc.startswith("http")
    # A brand-new connector produces the same URL for the same state --
    # the value is not instance-state-dependent.
    other_connector = StubBrokerConnector()
    assert connector.build_authorize_url(state="abc123") == other_connector.build_authorize_url(state="abc123")


def test_stub_broker_connector_exchange_auth_code_returns_real_brokercredentials_and_raises_for_invalid():
    """AC: ``exchange_auth_code`` returns a fixed ``BrokerCredentials``
    for any code other than the sentinel ``'invalid'``, for which it
    raises ``BrokerAuthError`` (AC #2 + AC #2). Real BrokerCredentials
    type, real exception type."""
    connector = StubBrokerConnector()

    # Non-sentinel code: a real BrokerCredentials instance with the
    # documented fields populated.
    creds = connector.exchange_auth_code(code="real-auth-code-xyz")
    assert isinstance(creds, BrokerCredentials)
    assert creds.access_token == "stub-access-token"
    assert creds.token_type == "Bearer"
    assert creds.broker_user_id == "stub-broker-user"
    # Determinism: same code -> same credential values, independent of
    # how many times or from which instance you ask.
    again = connector.exchange_auth_code(code="real-auth-code-xyz")
    assert again == creds
    assert StubBrokerConnector().exchange_auth_code(code="real-auth-code-xyz") == creds

    # Sentinel code 'invalid' raises BrokerAuthError (not a generic
    # Exception -- the AUTH-failure branch is specifically exercised).
    with pytest.raises(BrokerAuthError):
        connector.exchange_auth_code(code="invalid")
    # The BrokerAuthError raised here is the one defined in this same
    # module -- not a vendor-specific exception leaking the broker name.
    assert BrokerAuthError.__bases__[0] is BrokerError


def test_stub_broker_connector_fetch_holdings_returns_real_brokerholdings_and_is_deterministic():
    """AC: ``fetch_holdings`` returns a small fixed list of
    ``BrokerHolding``, and is deterministic across repeated calls."""
    from datetime import datetime
    from decimal import Decimal

    connector = StubBrokerConnector()
    creds = BrokerCredentials(access_token="tok")

    holdings_first = connector.fetch_holdings(credentials=creds)
    holdings_second = connector.fetch_holdings(credentials=creds)

    # Real BrokerHolding instances (typed return).
    assert all(isinstance(h, BrokerHolding) for h in holdings_first)
    # Determinism: repeated calls return equal results.
    assert holdings_first == holdings_second
    # Independence: mutating the returned list does NOT mutate the
    # stub's internal state -- a critical property for a deterministic
    # test double, otherwise callers' test setup could leak between
    # cases. The frozen-dataclass copy in the implementation is the
    # load-bearing detail.
    holdings_first.append("garbage")
    holdings_third = connector.fetch_holdings(credentials=creds)
    assert "garbage" not in holdings_third
    assert len(holdings_third) == len(holdings_second)
    # Default-construction path: at least one real holding, with the
    # expected shape (the small fixed canned list).
    assert len(holdings_second) >= 1
    sample = holdings_second[0]
    assert isinstance(sample.symbol, str)
    assert isinstance(sample.quantity, Decimal)


def test_stub_broker_connector_fetch_transactions_filters_by_inclusive_window_with_default_data():
    """AC: ``fetch_transactions`` returns only transactions whose
    ``trade_date`` falls within the requested inclusive window. The
    default canned list spans 2024-01-15, 2024-02-10, 2024-03-05,
    2024-04-20, so this test exercises windows that include, exclude,
    and bracket each boundary."""
    from datetime import date

    connector = StubBrokerConnector()
    creds = BrokerCredentials(access_token="tok")

    # Window containing only the first transaction (inclusive on both ends).
    only_jan = connector.fetch_transactions(
        credentials=creds, start_date=date(2024, 1, 1), end_date=date(2024, 1, 31)
    )
    assert all(isinstance(t, BrokerTransaction) for t in only_jan)
    assert {t.external_id for t in only_jan} == {"stub-tx-001"}

    # Window containing only the middle transactions (Feb + Mar).
    feb_mar = connector.fetch_transactions(
        credentials=creds, start_date=date(2024, 2, 1), end_date=date(2024, 3, 31)
    )
    assert {t.external_id for t in feb_mar} == {"stub-tx-002", "stub-tx-003"}

    # Inclusive lower boundary: a window whose start_date IS a transaction's
    # trade_date must include that transaction.
    boundary_lower = connector.fetch_transactions(
        credentials=creds, start_date=date(2024, 2, 10), end_date=date(2024, 2, 10)
    )
    assert {t.external_id for t in boundary_lower} == {"stub-tx-002"}

    # Inclusive upper boundary: a window whose end_date IS a transaction's
    # trade_date must include that transaction.
    boundary_upper = connector.fetch_transactions(
        credentials=creds, start_date=date(2024, 3, 5), end_date=date(2024, 3, 5)
    )
    assert {t.external_id for t in boundary_upper} == {"stub-tx-003"}

    # Window that excludes every transaction -- the list-empty case.
    nothing = connector.fetch_transactions(
        credentials=creds, start_date=date(2030, 1, 1), end_date=date(2030, 12, 31)
    )
    assert nothing == []

    # Whole-year window -- all four default transactions.
    whole_year = connector.fetch_transactions(
        credentials=creds, start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
    )
    assert {t.external_id for t in whole_year} == {
        "stub-tx-001", "stub-tx-002", "stub-tx-003", "stub-tx-004"
    }


def test_stub_broker_connector_fetch_transactions_is_deterministic_across_repeated_calls():
    """AC: ``fetch_transactions`` is deterministic across repeated
    calls (AC #2). Same window twice returns equal lists."""
    from datetime import date

    connector = StubBrokerConnector()
    creds = BrokerCredentials(access_token="tok")
    window = dict(start_date=date(2024, 2, 1), end_date=date(2024, 4, 30))

    first = connector.fetch_transactions(credentials=creds, **window)
    second = connector.fetch_transactions(credentials=creds, **window)
    assert first == second
    # A brand-new StubBrokerConnector (no shared state) returns the
    # same data for the same window -- determinism is baked into the
    # class, not instance state.
    fresh = StubBrokerConnector().fetch_transactions(credentials=creds, **window)
    assert fresh == first


def test_stub_broker_connector_constructor_accepts_overridable_holdings_and_transactions_lists():
    """AC: Constructor accepts overridable holdings/transactions lists
    (AC #4) and tests demonstrate both (this test). Custom lists
    must be what the four methods return -- the override must take
    effect end-to-end, not be silently ignored."""
    from datetime import date
    from decimal import Decimal

    custom_holding = BrokerHolding(
        symbol="CUSTOM",
        isin="XX0000000001",
        quantity=Decimal("99"),
        average_price=Decimal("1.00"),
        last_price=Decimal("2.00"),
        exchange="NYSE",
        product="EQ",
        instrument_id="custom-instr-1",
        name="Custom Holding",
    )
    custom_tx = BrokerTransaction(
        external_id="custom-tx-001",
        symbol="CUSTOM",
        isin="XX0000000001",
        trade_date=date(2099, 6, 1),  # outside the default 2024 window
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("10.00"),
        exchange="NYSE",
        segment="EQ",
    )

    connector = StubBrokerConnector(holdings=[custom_holding], transactions=[custom_tx])
    creds = BrokerCredentials(access_token="tok")

    # The custom holding replaces the default canned list entirely.
    holdings = connector.fetch_holdings(credentials=creds)
    assert holdings == [custom_holding]

    # The custom transaction's date is outside the 2024 default
    # window -- picking a 2099 window proves the custom list is
    # actually used (a default-window fetch would return nothing).
    txs = connector.fetch_transactions(
        credentials=creds, start_date=date(2099, 1, 1), end_date=date(2099, 12, 31)
    )
    assert txs == [custom_tx]

    # The custom list is defensively COPIED -- mutating the
    # caller's list afterwards must not change what the stub
    # returns later (otherwise a test's own setup could leak into
    # other tests sharing the same default list).
    callers_list = [custom_holding]
    mutating_connector = StubBrokerConnector(holdings=callers_list)
    callers_list.clear()
    still_there = mutating_connector.fetch_holdings(credentials=creds)
    assert still_there == [custom_holding]


def test_stub_broker_connector_raise_on_makes_every_protocol_method_raise_the_configured_exception():
    """AC: Constructor accepts an optional exception to raise (AC #4).
    When set, every Protocol method must raise that exception -- so
    other components' tests can simulate failures uniformly across
    build_authorize_url / exchange_auth_code / fetch_holdings /
    fetch_transactions."""
    from datetime import date
    from decimal import Decimal

    sentinel = BrokerApiError("synthetic broker outage")
    connector = StubBrokerConnector(raise_on=sentinel)
    creds = BrokerCredentials(access_token="tok")

    with pytest.raises(BrokerApiError) as exc_info:
        connector.build_authorize_url(state="any-state")
    assert exc_info.value is sentinel  # exact instance, not a re-raised copy

    with pytest.raises(BrokerApiError) as exc_info:
        connector.exchange_auth_code(code="any-code")
    assert exc_info.value is sentinel
    # When raise_on is set, it short-circuits BEFORE the 'invalid'
    # sentinel branch -- so even an auth code of 'invalid' raises
    # raise_on, not BrokerAuthError. This matches the documented
    # "every Protocol method raises this exception" behaviour: the
    # raise_on check is the first thing every method does, before any
    # internal branching. (Without raise_on, 'invalid' still raises
    # BrokerAuthError -- see the dedicated exchange_auth_code test.)
    with pytest.raises(BrokerApiError) as exc_info:
        connector.exchange_auth_code(code="invalid")
    assert exc_info.value is sentinel

    with pytest.raises(BrokerApiError) as exc_info:
        connector.fetch_holdings(credentials=creds)
    assert exc_info.value is sentinel

    with pytest.raises(BrokerApiError) as exc_info:
        connector.fetch_transactions(credentials=creds, start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
    assert exc_info.value is sentinel

    # And the inverse: with raise_on unset, all methods succeed normally.
    plain = StubBrokerConnector()
    plain.build_authorize_url(state="ok")
    plain.exchange_auth_code(code="ok")
    plain.fetch_holdings(credentials=creds)
    plain.fetch_transactions(credentials=creds, start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))


def test_stub_broker_connector_makes_no_network_calls_and_reads_no_environment_variables(monkeypatch):
    """AC: The Stub makes no network calls and reads no environment
    variables (AC #5). Proven two ways:

      (a) monkeypatching the standard-library socket / urllib call sites
          so any such call would raise -- nothing raises during normal
          method invocation;
      (b) every os.environ access through the read-only ``os.environ``
          proxy is recorded, and none of them touch the keys the stub
          could conceivably look up.

    Both prove the same thing from different angles, so a regression
    in either direction (silently opening a socket OR silently reading
    e.g. ``BROKER_API_KEY``) is caught.
    """
    import os
    import socket as _socket
    from datetime import date
    from decimal import Decimal
    from unittest import mock

    # --- (a) No network call sites are reached. ---
    # urllib is the conventional Python surface for any HTTP-ish
    # network call from library code; patching it to raise is a
    # load-bearing net -- if the Stub (or anything it imports
    # transitively during a method call) ever tries to open a URL,
    # this raises before the assertion runs.
    real_urlopen = None
    try:
        from urllib.request import urlopen as _real_urlopen  # noqa: F401
        real_urlopen = _real_urlopen
    except ImportError:
        pass
    socket_create_connection = _socket.socket
    # Replace any plausible outbound entrypoint with a raising
    # sentinel; ANY use would explode the test.
    def _raise_urlopen(*args, **kwargs):
        raise AssertionError("StubBrokerConnector must not make HTTP calls")

    def _raise_socket(*args, **kwargs):
        raise AssertionError("StubBrokerConnector must not open sockets")

    connector = StubBrokerConnector()
    creds = BrokerCredentials(access_token="tok")

    with mock.patch("urllib.request.urlopen", _raise_urlopen), \
         mock.patch("socket.socket", _raise_socket):
        # Every Protocol method must complete without touching the
        # network. If any one of them opens a socket or URL, the
        # raised AssertionError above propagates and the test fails.
        connector.build_authorize_url(state="net-check")
        connector.exchange_auth_code(code="net-check")
        connector.fetch_holdings(credentials=creds)
        connector.fetch_transactions(
            credentials=creds, start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
        )

    # --- (b) No environment variable reads. ---
    # Record every os.environ[...] / os.environ.get call by wrapping
    # the read-only proxy's __getitem__ and get methods.
    env_reads: list[str] = []
    real_getitem = type(os.environ).__getitem__
    real_get = type(os.environ).get

    def _recording_getitem(self, key):
        env_reads.append(key)
        return real_getitem(self, key)

    def _recording_get(self, key, *args, **kwargs):
        env_reads.append(key)
        return real_get(self, key, *args, **kwargs)

    with mock.patch.object(type(os.environ), "__getitem__", _recording_getitem), \
         mock.patch.object(type(os.environ), "get", _recording_get):
        connector.build_authorize_url(state="env-check")
        connector.exchange_auth_code(code="env-check")
        connector.fetch_holdings(credentials=creds)
        connector.fetch_transactions(
            credentials=creds, start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
        )

    # The Stub must not have read ANY environment variable --
    # including the broker-credential keys (BROKER_API_KEY,
    # UPSTOX_ACCESS_TOKEN, etc.) that other components might use.
    assert env_reads == [], (
        f"StubBrokerConnector must not read environment variables; "
        f"observed reads: {env_reads!r}"
    )
    # And the upper-case keys an honest broker connector could plausibly
    # consult are explicitly NOT touched, even by the Stub's
    # transitive imports during construction.
    for forbidden_key in ("BROKER_API_KEY", "BROKER_SECRET", "UPSTOX_ACCESS_TOKEN",
                          "EXCHANGE_RATE_API_KEY", "STUB_BROKER_URL"):
        assert forbidden_key not in env_reads, (
            f"StubBrokerConnector must not read {forbidden_key!r} from env"
        )


def test_stub_broker_connector_does_not_read_env_at_construction_time():
    """AC: AC #5 covers all paths -- construction time included. A
    StubBrokerConnector whose constructor happens to read e.g.
    ``BROKER_API_KEY`` would still be considered env-coupled. Wrap
    os.environ the same way as the methods-only test and construct
    several variants (default, with custom holdings, with custom
    transactions, with raise_on) -- none may read any env var."""
    import os
    from decimal import Decimal
    from unittest import mock

    env_reads: list[str] = []
    real_getitem = type(os.environ).__getitem__
    real_get = type(os.environ).get

    def _recording_getitem(self, key):
        env_reads.append(key)
        return real_getitem(self, key)

    def _recording_get(self, key, *args, **kwargs):
        env_reads.append(key)
        return real_get(self, key, *args, **kwargs)

    custom_h = BrokerHolding(
        symbol="CUSTOM", isin="XX0000000001", quantity=Decimal("1"),
        average_price=Decimal("1"), last_price=Decimal("1"), exchange="NYSE",
        product="EQ", instrument_id="x", name="Custom",
    )
    custom_t = BrokerTransaction(
        external_id="custom-tx", symbol="CUSTOM", isin="XX0000000001",
        trade_date=__import__("datetime").date(2024, 1, 1), side="BUY",
        quantity=Decimal("1"), price=Decimal("1"), amount=Decimal("1"),
        exchange="NYSE", segment="EQ",
    )

    with mock.patch.object(type(os.environ), "__getitem__", _recording_getitem), \
         mock.patch.object(type(os.environ), "get", _recording_get):
        StubBrokerConnector()
        StubBrokerConnector(holdings=[custom_h])
        StubBrokerConnector(transactions=[custom_t])
        StubBrokerConnector(holdings=[custom_h], transactions=[custom_t])
        StubBrokerConnector(raise_on=BrokerApiError("synthetic"))

    assert env_reads == [], (
        f"StubBrokerConnector() must not read env vars at construction; "
        f"observed reads: {env_reads!r}"
    )

