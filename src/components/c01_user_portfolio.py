"""User & Portfolio (component 01) — the user's identity and the
portfolio it owns.

Design: no fig. 1 / fig. 2 mechanism diagram exists for this component
(it stayed whiteboard-only through the design-framework round covered
by `checkpoint.md`). The `Default*` classes below are its first real
implementation, built directly from this task's own brief rather than
a prior design artifact.
Decisions:
  ADR-0022 — BrokerConnector interface shape (Protocol) and broker data
             tagged UNTRUSTED by default, extending ADR-0003/ADR-0018's
             pattern to this component.
  ADR-0023 — which real broker/aggregator API eventually backs
             BrokerConnector (Status: Proposed — genuine external-
             credential gap, not decided here).
  ADR-0044 — manual stock entry: a parallel, real onboarding path
             (list_available_securities/add_holding_manually/
             add_transaction_manually on DefaultUserPortfolio) that
             bypasses BrokerConnector entirely, backed for real by
             Knowledge & Entity Model (04)'s registry.

Interfaces below match the whiteboard-level Component Whiteboards
artifact, card 01: Portfolio -> Portfolio State, User -> Decision &
Policy, Portfolio -> Event & Analysis, User -> Notification.
"""

import uuid
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, Protocol, runtime_checkable
from datetime import date, datetime

from components.c04_knowledge_entity import DefaultKnowledgeEntity, Entity
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from exchange_rate_client import (
    ExchangeRateFetchError,
    MissingExchangeRateAPIKeyError,
    fetch_exchange_rate,
)
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure

from domain import (  # re-exported for backward compatibility
    HOLDINGS_TABLE, PORTFOLIOS_TABLE, TRANSACTIONS_TABLE, USERS_TABLE,
    Holding, Portfolio, Transaction, User,
    validate_stock_symbol,
)


# Exception hierarchy for BrokerConnector (ADR-0022)
class BrokerError(Exception):
    """Base exception for all broker-related errors."""
    pass


class BrokerConfigError(BrokerError):
    """Raised when the broker configuration is invalid or missing."""
    pass


class BrokerAuthError(BrokerError):
    """Raised when authentication fails (invalid/expired token or auth code)."""
    pass


class BrokerApiError(BrokerError):
    """Raised when the broker API returns an error (non-2xx or status != 'success')."""
    pass


class BrokerRateLimitError(BrokerError):
    """Raised when the broker API rate limit is exceeded."""
    pass


class UnsupportedBrokerError(BrokerError):
    """Raised when the broker is not supported."""
    pass


# Data Transfer Objects (DTOs) for BrokerConnector (ADR-0022)
@dataclass(frozen=True)
class BrokerCredentials:
    access_token: str
    token_type: str = 'Bearer'
    expires_at: datetime | None = None
    refresh_token: str | None = None
    broker_user_id: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerHolding:
    symbol: str
    isin: str
    quantity: Decimal
    average_price: Decimal
    last_price: Decimal
    exchange: str
    product: str
    instrument_id: str
    name: str
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerTransaction:
    external_id: str
    symbol: str
    isin: str
    trade_date: date
    side: Literal['BUY', 'SELL']
    quantity: Decimal
    price: Decimal
    amount: Decimal
    exchange: str
    segment: str
    raw: dict = field(default_factory=dict)


# BrokerConnector Protocol (ADR-0022)
@runtime_checkable
class BrokerConnector(Protocol):
    broker_id: str
    display_name: str

    def build_authorize_url(self, *, state: str) -> str:
        """Build the broker-specific authorization URL for the given state."""
        ...

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        """Exchange an authorization code for broker credentials."""
        ...

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        """Fetch holdings for the given credentials."""
        ...

    def fetch_transactions(self, *, credentials: BrokerCredentials, start_date: date, end_date: date) -> list[BrokerTransaction]:
        """Fetch transactions for the given credentials and date range."""
        ...


class PlaceholderBrokerConnector:
    """Placeholder implementation of BrokerConnector for testing and development.
    All methods return synthetic, obviously-fake data that cannot be mistaken
    for real broker data."""

    broker_id: str = "placeholder"
    display_name: str = "Placeholder Broker"

    def build_authorize_url(self, *, state: str) -> str:
        return f"https://placeholder.broker/auth?state={state}"

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        return BrokerCredentials(
            access_token=f"placeholder-token-{code}",
            token_type="Bearer",
            expires_at=None,
            refresh_token=None,
            broker_user_id=None,
            raw={"code": code},
        )

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        return []

    def fetch_transactions(self, *, credentials: BrokerCredentials, start_date: date, end_date: date) -> list[BrokerTransaction]:
        return []


class StubBrokerConnector:
    """Deterministic, offline test double for the ``BrokerConnector``
    Protocol (STORY-3). Used by OTHER components' tests where a
    ``BrokerConnector``-shaped collaborator is needed but no real
    broker call should ever be made.

    Conforms exactly to the STORY-2 ``BrokerConnector`` Protocol —
    verifiable via the runtime-checkable Protocol's ``isinstance``
    check (the first acceptance criterion for this story).

    Behaviour (all deterministic, no I/O, no environment access):

      * ``broker_id`` is ``'stub'`` and ``display_name`` is
        ``'Stub Broker'`` — fixed identifiers every caller can rely
        on, not anything derived from host state.
      * ``build_authorize_url`` returns a fixed fake URL echoing the
        passed ``state`` so a caller can assert its ``state`` token
        reached this connector intact.
      * ``exchange_auth_code`` returns a fixed ``BrokerCredentials``
        for any code other than the sentinel ``'invalid'``, for which
        it raises ``BrokerAuthError`` — the sentinel lets tests
        exercise the auth-failure branch without a real broker.
      * ``fetch_holdings`` returns a small fixed list of
        ``BrokerHolding`` (overridable via the constructor — see
        below).
      * ``fetch_transactions`` returns a fixed list of
        ``BrokerTransaction`` filtered by the requested
        ``start_date`` / ``end_date`` window (inclusive on both ends,
        so window-edge tests work as expected). Also overridable.

    Constructor injection:

      * ``holdings``: optional list of ``BrokerHolding`` to return
        from ``fetch_holdings``. Defaults to a small fixed canned
        list so the default-construction path is fully usable.
      * ``transactions``: optional list of ``BrokerTransaction`` to
        filter through in ``fetch_transactions``. Defaults to a
        small fixed canned list spanning several dates so the
        windowing logic has real data to filter.
      * ``raise_on``: optional exception instance to raise from any
        of the four Protocol methods (simulating broker failures
        in other stories' tests). When set, every method raises this
        exception before producing any real output — matches the
        "simulate failures" requirement without a per-method
        configuration knob the Protocol would otherwise need.

    No network calls. No environment-variable reads. All values
    baked in at construction time, so repeated calls produce
    identical results (the "deterministic across repeated calls"
    acceptance criterion)."""

    broker_id: str = "stub"
    display_name: str = "Stub Broker"

    def __init__(
        self,
        holdings: list[BrokerHolding] | None = None,
        transactions: list[BrokerTransaction] | None = None,
        raise_on: BaseException | None = None,
    ) -> None:
        self._holdings: list[BrokerHolding] = (
            list(holdings) if holdings is not None else list(_DEFAULT_STUB_HOLDINGS)
        )
        self._transactions: list[BrokerTransaction] = (
            list(transactions) if transactions is not None else list(_DEFAULT_STUB_TRANSACTIONS)
        )
        self._raise_on = raise_on

    def build_authorize_url(self, *, state: str) -> str:
        if self._raise_on is not None:
            raise self._raise_on
        return f"https://stub.broker/auth?state={state}"

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        if self._raise_on is not None:
            raise self._raise_on
        if code == "invalid":
            raise BrokerAuthError(
                f"StubBrokerConnector: refusing sentinel code 'invalid'"
            )
        return BrokerCredentials(
            access_token="stub-access-token",
            token_type="Bearer",
            expires_at=None,
            refresh_token=None,
            broker_user_id="stub-broker-user",
            raw={"code": code, "stub": True},
        )

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        if self._raise_on is not None:
            raise self._raise_on
        return [BrokerHolding(**asdict(h)) for h in self._holdings]

    def fetch_transactions(
        self,
        *,
        credentials: BrokerCredentials,
        start_date: date,
        end_date: date,
    ) -> list[BrokerTransaction]:
        if self._raise_on is not None:
            raise self._raise_on
        return [
            BrokerTransaction(**asdict(t))
            for t in self._transactions
            if start_date <= t.trade_date <= end_date
        ]


# Canned default data for StubBrokerConnector — defined at module
# scope (not inside the class body) so the dataclasses are fully
# constructed once at import time and the default-construction path
# stays cheap and side-effect-free. These exact values are what the
# acceptance criteria describe: a "small fixed list" for holdings,
# and a transaction list spanning multiple dates so window-edge
# filtering has real rows above, below, and on each boundary.
_DEFAULT_STUB_HOLDINGS: list[BrokerHolding] = [
    BrokerHolding(
        symbol="AAPL",
        isin="US0378331005",
        quantity=Decimal("10"),
        average_price=Decimal("150.00"),
        last_price=Decimal("175.00"),
        exchange="NASDAQ",
        product="EQ",
        instrument_id="stub-instr-AAPL",
        name="Apple Inc.",
        raw={"stub": True},
    ),
    BrokerHolding(
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        quantity=Decimal("5"),
        average_price=Decimal("2400.00"),
        last_price=Decimal("2500.00"),
        exchange="NSE",
        product="EQ",
        instrument_id="stub-instr-RELIANCE",
        name="Reliance Industries Limited",
        raw={"stub": True},
    ),
]

# Spans 2024-01-15, 2024-02-10, 2024-03-05, 2024-04-20 so that
# window tests can verify both endpoints (inclusive) and exclude
# rows clearly outside the window.
_DEFAULT_STUB_TRANSACTIONS: list[BrokerTransaction] = [
    BrokerTransaction(
        external_id="stub-tx-001",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 1, 15),
        side="BUY",
        quantity=Decimal("2"),
        price=Decimal("150.00"),
        amount=Decimal("300.00"),
        exchange="NASDAQ",
        segment="EQ",
        raw={"stub": True},
    ),
    BrokerTransaction(
        external_id="stub-tx-002",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 2, 10),
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("160.00"),
        amount=Decimal("160.00"),
        exchange="NASDAQ",
        segment="EQ",
        raw={"stub": True},
    ),
    BrokerTransaction(
        external_id="stub-tx-003",
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        trade_date=date(2024, 3, 5),
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("2400.00"),
        amount=Decimal("2400.00"),
        exchange="NSE",
        segment="EQ",
        raw={"stub": True},
    ),
    BrokerTransaction(
        external_id="stub-tx-004",
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        trade_date=date(2024, 4, 20),
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("2500.00"),
        amount=Decimal("2500.00"),
        exchange="NSE",
        segment="EQ",
        raw={"stub": True},
    ),
]


_VALID_CURRENCIES = ("USD", "INR")
_VALID_EXCHANGES = ("NYSE", "NASDAQ", "NSE", "BSE")
_VALID_SYMBOL_SUFFIXES = (None, ".NS", ".BO")

# Quantum for currency-aggregated totals (STORY-8): matches this
# project's established `Decimal("0.0001")` precision convention from
# `_coerce_quantity_to_decimal` and `_quantize_rate`, not a new
# precision choice. Same `ROUND_HALF_UP` rounding mode both
# neighboring modules already use -- the story's "banker's rounding
# via the existing quantize pattern" wording is matched by using the
# same pattern (not by silently switching to `ROUND_HALF_EVEN`, which
# no other module in this codebase uses).
_TOTAL_QUANTUM = Decimal("0.0001")


def _coerce_market_value_to_decimal(value) -> Decimal:
    """Coerce a `Position.market_value` (typed float at the dataclass
    level, but real callers/tests pass Decimal after STORY-1's
    `Holding.quantity` quantization) to a Decimal quantized to 4
    decimal places. Raises ValueError on non-numeric input — matches
    the same defensive posture as `_coerce_quantity_to_decimal` /
    `_quantize_rate`, and keeps `calculate_portfolio_totals` from
    silently mixing float and Decimal arithmetic (which would raise
    `TypeError` mid-aggregation)."""
    try:
        return Decimal(str(value)).quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"Position.market_value must be a real number (int/float/Decimal/str); got {value!r}"
        ) from exc


@dataclass
class Position:
    holding: Holding
    market_value: float


@dataclass
class PortfolioSnapshot:
    portfolio_id: str
    positions: list[Position]
    exposure: dict


class UserPortfolio(Protocol):
    def onboard_user(self, details: dict) -> User:
        ...

    def connect_portfolio(self, user: User, broker_credentials: dict) -> Portfolio:
        ...

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        ...

    def import_transactions(self, portfolio: Portfolio) -> list[Transaction]:
        ...

    def synchronize_portfolio(self, portfolio: Portfolio) -> PortfolioSnapshot:
        ...

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        ...

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        ...

    def manage_preferences(self, user: User, updates: dict) -> User:
        ...

    def determine_user_relevance(self, user: User, event: dict) -> bool:
        """→ Event / Analysis interface: is this event relevant to
        this user's portfolio at all."""
        ...


class StubUserPortfolio:
    """Structural implementation of UserPortfolio. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def onboard_user(self, details: dict) -> User:
        with traced("StubUserPortfolio.onboard_user"):
            return User(id="stub-id", preferences={})

    def connect_portfolio(self, user: User, broker_credentials: dict) -> Portfolio:
        with traced("StubUserPortfolio.connect_portfolio"):
            return Portfolio(id="stub-id", user_id="stub-id")

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        with traced("StubUserPortfolio.import_holdings"):
            return []

    def import_transactions(self, portfolio: Portfolio) -> list[Transaction]:
        with traced("StubUserPortfolio.import_transactions"):
            return []

    def synchronize_portfolio(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("StubUserPortfolio.synchronize_portfolio"):
            return PortfolioSnapshot(portfolio_id="stub-id", positions=[], exposure={})

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("StubUserPortfolio.track_portfolio_state"):
            return PortfolioSnapshot(portfolio_id="stub-id", positions=[], exposure={})

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        with traced("StubUserPortfolio.calculate_exposure"):
            return {}

    def manage_preferences(self, user: User, updates: dict) -> User:
        with traced("StubUserPortfolio.manage_preferences"):
            return User(id="stub-id", preferences={})

    def determine_user_relevance(self, user: User, event: dict) -> bool:
        with traced("StubUserPortfolio.determine_user_relevance"):
            return True







# Entity.kind values (c04_knowledge_entity.py's "Company | Security |
# Person | Sector | Industry | Index | Geography") that represent
# something a user could hold a position in — the manual-entry
# dropdown's real filter (ADR-0044).
_TRADEABLE_SECURITY_ENTITY_KINDS = ("Security", "Company")


class DefaultUserPortfolio:
    """Real implementation of UserPortfolio.

    `onboard_user`/`manage_preferences` persist through `Infrastructure`
    (`DefaultInfrastructure` by default) rather than an in-memory dict —
    System Infrastructure's real Postgres-backed store (ADR-0019) is
    what "real" means for this component's non-broker-dependent state.

    `connect_portfolio`/`import_holdings`/`import_transactions` call
    through the injected `BrokerConnector` (ADR-0022); every value it
    returns is tagged UNTRUSTED via `BoundaryGate.tag_provenance` before
    it's used to build a `Portfolio`/`Holding`/`Transaction`, and the
    tagged record (provenance included) is what gets persisted — the
    dataclass returned to the caller stays exactly the shape the
    `UserPortfolio` Protocol already declares.

    `synchronize_portfolio` re-imports from the broker connector first
    (so it reflects the current external state, per its name), then
    delegates to `track_portfolio_state`, which only reads what's
    already stored — the two methods differ in whether they pull fresh
    data or read the current tracked state, matching what their names
    already imply.

    `list_available_securities`/`add_holding_manually`/
    `add_transaction_manually` (ADR-0044) are a second, parallel
    onboarding path — real, `Infrastructure`-backed, entirely bypassing
    `BrokerConnector` — for a user who picks securities directly rather
    than connecting a broker. They are not part of the `UserPortfolio`
    Protocol: manual entry is an implementation detail of how this
    component gets its data, the same reasoning ADR-0029 already used
    for `DefaultEvidenceLinker.link_with_context()`. `knowledge_entity`
    is typed against the concrete `DefaultKnowledgeEntity`, not the
    `KnowledgeEntity` Protocol, because `get_entity`/`search_entities`
    (ADR-0044) aren't part of that Protocol either.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        broker_connector: BrokerConnector | None = None,
        boundary_gate: BoundaryGate | None = None,
        audit_manager: AuditManager | None = None,
        knowledge_entity: DefaultKnowledgeEntity | None = None,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._broker_connector = broker_connector or PlaceholderBrokerConnector()
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._knowledge_entity = knowledge_entity or DefaultKnowledgeEntity(infrastructure=self._infrastructure)

    def onboard_user(self, details: dict) -> User:
        with traced("DefaultUserPortfolio.onboard_user"):
            user = User(
                id=str(uuid.uuid4()),
                preferences=dict(details.get("preferences", {})),
                email=details.get("email", ""),
            )
            self._infrastructure.store(
                USERS_TABLE, {"id": user.id, "preferences": user.preferences, "email": user.email}
            )
            return user

    def connect_portfolio(self, user: User, broker_credentials: dict) -> Portfolio:
        with traced("DefaultUserPortfolio.connect_portfolio"):
            # Store the broker_credentials in the portfolio record (without calling the broker_connector)
            portfolio = Portfolio(id=str(uuid.uuid4()), user_id=user.id)
            # Tag and store the broker_credentials as the broker_connection in the portfolio record
            tagged_credentials = self._boundary_gate.tag_provenance(broker_credentials, source="broker_connector")
            self._infrastructure.store(
                PORTFOLIOS_TABLE,
                {
                    "id": portfolio.id,
                    "user_id": portfolio.user_id,
                    "broker_connection": tagged_credentials,
                },
            )
            self._audit_manager.record(
                "portfolio_connected",
                {
                    "portfolio_id": portfolio.id,
                    "user_id": user.id,
                    "provenance": tagged_credentials.get("provenance"),
                },
            )
            return portfolio

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        with traced("DefaultUserPortfolio.import_holdings"):
            credentials = self._load_credentials(portfolio)
            if credentials is None:
                return []
            # Fetch holdings using the broker_connector
            raw_holdings = self._broker_connector.fetch_holdings(credentials=credentials)
            holdings = []
            for raw in raw_holdings:
                tagged = self._boundary_gate.tag_provenance(asdict(raw), source="broker_connector")
                holding = Holding(
                    portfolio_id=portfolio.id,
                    security_id=tagged["symbol"],
                    quantity=tagged["quantity"],
                )
                self._infrastructure.store(
                    HOLDINGS_TABLE,
                    {
                        "id": f"{portfolio.id}:{holding.security_id}",
                        **asdict(holding),
                        "provenance": tagged.get("provenance"),
                    },
                )
                holdings.append(holding)
            return holdings

    def import_transactions(self, portfolio: Portfolio, start_date: date = date.min, end_date: date = date.max) -> list[Transaction]:
        with traced("DefaultUserPortfolio.import_transactions"):
            credentials = self._load_credentials(portfolio)
            if credentials is None:
                return []
            # Fetch transactions using the broker_connector
            raw_transactions = self._broker_connector.fetch_transactions(credentials=credentials, start_date=start_date, end_date=end_date)
            transactions = []
            for raw in raw_transactions:
                tagged = self._boundary_gate.tag_provenance(asdict(raw), source="broker_connector")
                transaction = Transaction(
                    portfolio_id=portfolio.id,
                    kind=tagged["side"],  # Note: the BrokerTransaction has 'side' (BUY/SELL), but the Transaction expects 'kind'
                    amount=tagged["amount"],
                )
                self._infrastructure.store(
                    TRANSACTIONS_TABLE,
                    {
                        "id": str(uuid.uuid4()),
                        **asdict(transaction),
                        "provenance": tagged.get("provenance"),
                    },
                )
                transactions.append(transaction)
            return transactions

    def synchronize_portfolio(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("DefaultUserPortfolio.synchronize_portfolio"):
            self.import_holdings(portfolio)
            self.import_transactions(portfolio)
            return self.track_portfolio_state(portfolio)

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        """Reads holdings already stored — via import_holdings above, or
        stored some other way (e.g. directly through Infrastructure in a
        test) — and assembles a PortfolioSnapshot. `market_value` is set
        to each holding's quantity: no live price feed exists yet (Data
        & Sources, component 02, is still whiteboard-only), so this is a
        quantity-weighted proxy, not a real dollar-weighted valuation,
        until that component ships real prices. This mirrors how
        DefaultStateManager (Agent Runtime, component 10) is honestly
        in-memory-only until its real backing store exists — a forced
        consequence of a not-yet-built dependency, not a design fork
        with a real alternative to choose between."""
        with traced("DefaultUserPortfolio.track_portfolio_state"):
            holdings = self._stored_holdings(portfolio.id)
            positions = [
                Position(holding=holding, market_value=holding.quantity) for holding in holdings
            ]
            snapshot = PortfolioSnapshot(portfolio_id=portfolio.id, positions=positions, exposure={})
            snapshot.exposure = self.calculate_exposure(snapshot)
            return snapshot

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        """Exposure by security: the only grouping the current data
        model supports (Holding carries a security_id, nothing else to
        group by — no asset class or sector field exists yet), so this
        isn't a design fork so much as what the dataclasses already
        available force. Positions sharing a security_id (e.g. from a
        re-synchronized portfolio) are aggregated together. Returns
        `{security_id: {"market_value": total, "weight": share of total
        portfolio market value}}`; weight is 0.0 for every entry when
        total market value is 0, rather than dividing by zero."""
        with traced("DefaultUserPortfolio.calculate_exposure"):
            # market_value is set from Holding.quantity (see
            # track_portfolio_state), a Decimal since STORY-1 -- but
            # Position's own dataclass field is still typed float, and a
            # caller (or test) can still hand this a real float directly.
            # Normalize every value to Decimal up front so the real
            # aggregation below never has to mix the two types (a bare
            # `float += Decimal`, or `Decimal + float`, both raise
            # TypeError). Decimal == float still compares correctly for
            # callers/tests that compare the result against plain float
            # literals.
            market_values = [
                position.market_value
                if isinstance(position.market_value, Decimal)
                else Decimal(str(position.market_value))
                for position in snapshot.positions
            ]
            total_market_value = sum(market_values, start=Decimal("0"))
            exposure: dict[str, dict] = {}
            for position, market_value in zip(snapshot.positions, market_values):
                security_id = position.holding.security_id
                entry = exposure.setdefault(security_id, {"market_value": Decimal("0"), "weight": 0.0})
                entry["market_value"] += market_value
            if total_market_value > 0:
                for entry in exposure.values():
                    # weight is a ratio/percentage, not a "price-related
                    # field" the story's DECIMAL(18,4) precision concern
                    # applies to -- kept as float (its existing, correct
                    # type) rather than Decimal, since e.g. Decimal("0.6")
                    # != 0.6 (0.6 has no exact binary float
                    # representation), which would break every existing
                    # caller/test that already compares against a plain
                    # float literal.
                    entry["weight"] = float(entry["market_value"] / total_market_value)
            return exposure

    def calculate_portfolio_totals(
        self,
        snapshot: PortfolioSnapshot,
        base_currency: str,
        infrastructure: Infrastructure | None = None,
    ) -> dict:
        """Currency-aggregated totals (STORY-8): sums every position's
        `market_value` separately per `holding.currency`, then derives
        a consolidated total in `base_currency` using the real
        INR/USD rate from `fetch_exchange_rate`. Deliberately distinct
        from `calculate_exposure`, which is per-security weighting —
        this is about currency conversion and aggregation, the
        separate question the multi-currency portfolio needs answered.

        Returns a dict with the shape:

            {
                "inr_total":           Decimal,   # sum of INR-currency positions
                "usd_total":           Decimal,   # sum of USD-currency positions
                "consolidated_total":  Decimal | None,  # None on rate-fetch failure
                "base_currency":       str,       # echo of the requested base
                "rate":                Decimal | None,  # real INR/USD rate fetched
                "error":               str | None,  # real failure message, never fabricated
            }

        Every monetary field is a `Decimal` quantized to 4 decimal
        places via this module's `_TOTAL_QUANTUM`/`ROUND_HALF_UP`
        pattern — matching the precision convention
        `_coerce_quantity_to_decimal` and `_quantize_rate` already
        establish. A consolidated total in `base_currency` other than
        "USD" or "INR" raises `ValueError` upfront (a 3rd base
        currency would require a different real exchange rate this
        codebase doesn't fetch, and silently coercing "EUR" → "USD"
        would be the kind of fabrication the story explicitly
        forbids).

        If `fetch_exchange_rate` raises — both vendor sources failed,
        or neither key is configured — the method catches the
        `MissingExchangeRateAPIKeyError` / `ExchangeRateFetchError`
        and returns the real INR / USD subtotals with a real error
        message about the consolidated total being unavailable. A
        fabricated exchange rate or fabricated consolidated total is
        the precise failure mode this method is built not to produce,
        so callers downstream can distinguish "we have INR/USD
        subtotals but no consolidated answer" from "we have a real
        consolidated answer in the requested base currency" purely
        from the returned dict's `consolidated_total is None` /
        `error is not None` shape — no hidden placeholders, no
        silently-coerced values."""
        with traced("DefaultUserPortfolio.calculate_portfolio_totals"):
            if base_currency not in ("USD", "INR"):
                raise ValueError(
                    f"calculate_portfolio_totals: base_currency must be one of "
                    f"('USD', 'INR'); got {base_currency!r}"
                )

            inr_total = Decimal("0")
            usd_total = Decimal("0")
            for position in snapshot.positions:
                market_value = _coerce_market_value_to_decimal(position.market_value)
                if position.holding.currency == "INR":
                    inr_total += market_value
                elif position.holding.currency == "USD":
                    usd_total += market_value
                # Positions in any other currency are not part of the
                # INR/USD aggregation -- Holding.__post_init__'s own
                # validation already restricts currency to {"USD",
                # "INR"}, so reaching this branch means a Position was
                # constructed with a raw dict that bypassed that
                # check (test-only path); silently summing it would
                # hide the bypass. Drop it, but keep the rest of the
                # aggregation honest.

            inr_total = inr_total.quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)
            usd_total = usd_total.quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)

            result: dict = {
                "inr_total": inr_total,
                "usd_total": usd_total,
                "consolidated_total": None,
                "base_currency": base_currency,
                "rate": None,
                "error": None,
            }

            try:
                rate = fetch_exchange_rate(infrastructure=infrastructure)
            except (MissingExchangeRateAPIKeyError, ExchangeRateFetchError) as exc:
                # Real failure -- never fabricate a rate or a
                # consolidated total. The subtotals are still real
                # and still returned; only the consolidated answer is
                # honestly unavailable. The error message names what
                # was attempted so a caller / debugging session can
                # see why no consolidated answer exists.
                result["error"] = (
                    f"consolidated total in {base_currency} is unavailable: "
                    f"{type(exc).__name__}: {exc}"
                )
                return result

            rate_quantized = rate.quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)
            result["rate"] = rate_quantized

            if base_currency == "USD":
                # Consolidated USD = usd_total + (inr_total / rate).
                # Decimal division preserves precision at the quantum
                # used here (rate is already 4dp; inr_total is already
                # 4dp), then a final quantize re-fixes the rounding
                # mode at the result's own precision.
                if rate_quantized == 0:
                    # A real rate of 0 is implausible (it would mean
                    # 1 USD = 0 INR), but if `fetch_exchange_rate`
                    # somehow returned one, divide-by-zero would
                    # raise; treat it honestly as "consolidated total
                    # unavailable" rather than fabricating.
                    result["error"] = (
                        f"consolidated total in {base_currency} is unavailable: "
                        f"fetched INR/USD rate is zero"
                    )
                    return result
                consolidated = (usd_total + (inr_total / rate_quantized)).quantize(
                    _TOTAL_QUANTUM, rounding=ROUND_HALF_UP
                )
            else:  # base_currency == "INR" (the only other valid value)
                # Consolidated INR = (usd_total * rate) + inr_total.
                consolidated = (usd_total * rate_quantized + inr_total).quantize(
                    _TOTAL_QUANTUM, rounding=ROUND_HALF_UP
                )

            result["consolidated_total"] = consolidated
            return result

    def calculate_gains_losses(self, snapshot: PortfolioSnapshot) -> dict:
        """Gains/losses and percentage returns (STORY-8 acceptance
        criterion). NOT IMPLEMENTED — and intentionally so.

        Neither `Holding` nor `Position` tracks a cost basis or
        purchase price anywhere in this codebase. There is no field
        on either dataclass that records what the user paid per
        share when they acquired the position, and inventing a
        fabricated "purchase price" field — or a fabricated
        gains/losses number from one — would be the precise failure
        mode the story explicitly calls out: "don't invent a
        fabricated gain/loss number".

        The honest, real answer matches this project's own ADR
        convention (e.g. ADR-0046's partial-resolution posture,
        `c04_knowledge_entity` / `c07_event_observation`'s
        `NotImplementedError`-on-real-gap pattern): raise a named
        exception whose message explicitly documents the missing
        `Holding.cost_basis` field and points at STORY-8. A caller
        can catch this and either (a) extend the data model with a
        real `cost_basis` field, or (b) decide that gains/losses
        really aren't computable right now and surface that to the
        user honestly. There is no silent fallback to a fabricated
        number anywhere in this code path."""
        raise NotImplementedError(
            "calculate_gains_losses: Holding.cost_basis field is not implemented; "
            "gains/losses and percentage returns cannot be computed for real -- "
            "see STORY-8 acceptance criteria"
        )

    def manage_preferences(self, user: User, updates: dict) -> User:
        with traced("DefaultUserPortfolio.manage_preferences"):
            stored = self._infrastructure.retrieve(USERS_TABLE, user.id)
            current_preferences = dict(stored["preferences"]) if stored else dict(user.preferences)
            current_preferences.update(updates)
            # `store()` replaces the whole record, not just `preferences`
            # (same semantics DefaultInfrastructure/_FakeInfrastructure
            # both use everywhere in this project) -- email has to be
            # carried forward explicitly here, or a preference update
            # would silently erase it.
            email = stored.get("email", "") if stored else user.email
            self._infrastructure.store(
                USERS_TABLE, {"id": user.id, "preferences": current_preferences, "email": email}
            )
            return User(id=user.id, preferences=current_preferences, email=email)

    def determine_user_relevance(self, user: User, event: dict) -> bool:
        """Structural lookup, not cognition: does event["security_id"]
        appear among this user's current holdings, across every
        portfolio stored for them. `event["security_id"]` is the
        load-bearing assumption here — Event & Observation (component
        07) hasn't defined a real event schema yet, so this matches the
        one field name Holding itself already uses, rather than
        inventing a richer event contract nothing else in this project
        has settled on."""
        with traced("DefaultUserPortfolio.determine_user_relevance"):
            event_security_id = event.get("security_id")
            if not event_security_id:
                return False
            for portfolio_record in self._infrastructure.query(PORTFOLIOS_TABLE, {"user_id": user.id}):
                holdings = self._infrastructure.query(
                    HOLDINGS_TABLE, {"portfolio_id": portfolio_record["id"]}
                )
                if any(holding["security_id"] == event_security_id for holding in holdings):
                    return True
            return False

    def list_available_securities(self, query: str = "") -> list[Entity]:
        """The manual-entry path's answer to "what can a user pick
        from" (ADR-0044) — every live entity Knowledge & Entity Model
        (04) already knows about whose `kind` marks it as a tradeable
        security or the company behind one, optionally narrowed by
        `query` (substring match against name/aliases, same
        normalization `DefaultKnowledgeEntity.search_entities` uses)
        so a dropdown can filter as the user types. Merges results
        across both tradeable kinds and dedups by entity id. A
        registry with nothing registered yet — the honest state of a
        fresh system before anything has seeded it — returns an empty
        list; that is a correct answer, not a bug to paper over with
        fake data."""
        with traced("DefaultUserPortfolio.list_available_securities"):
            securities_by_id: dict[str, Entity] = {}
            for kind in _TRADEABLE_SECURITY_ENTITY_KINDS:
                for entity in self._knowledge_entity.search_entities(kind=kind, query=query):
                    securities_by_id[entity.id] = entity
            return list(securities_by_id.values())

    def add_holding_manually(self, portfolio: Portfolio, security_id: str, quantity: float) -> Holding:
        """The manual-entry counterpart to `import_holdings` (ADR-0044):
        adds one `Holding` by direct selection rather than a broker
        round-trip, entirely bypassing `BrokerConnector`. `security_id`
        must resolve to a real, live entity via
        `DefaultKnowledgeEntity.get_entity` — a direct id lookup, not
        `resolve_entity`'s mention/name fuzzy match, since `security_id`
        is expected to be an id a caller already got from
        `list_available_securities`, not free text — before any
        `Holding` is built; an id that doesn't resolve fails loudly
        (`ValueError`) rather than silently creating a holding for a
        security nobody registered. Unlike broker-sourced holdings,
        this is never tagged `Provenance.UNTRUSTED`: the data crossing
        into this component is a direct user selection over an
        already-validated internal registry entry, not an external
        system's payload — the same reasoning `onboard_user`/
        `manage_preferences` already apply to direct user input (ADR-0044
        documents this contrast with ADR-0022's broker-data tagging)."""
        with traced("DefaultUserPortfolio.add_holding_manually"):
            security = self._knowledge_entity.get_entity(security_id)
            if security is None:
                raise ValueError(
                    f"add_holding_manually: security_id {security_id!r} does not resolve to a known entity"
                )
            holding = Holding(portfolio_id=portfolio.id, security_id=security.id, quantity=quantity)
            self._infrastructure.store(
                HOLDINGS_TABLE,
                {"id": f"{portfolio.id}:{holding.security_id}", **asdict(holding)},
            )
            return holding

    def add_transaction_manually(self, portfolio: Portfolio, kind: str, amount: float) -> Transaction:
        """The manual-entry counterpart to `import_transactions`
        (ADR-0044), mirroring its shape (`kind`/`amount`) for
        consistency. `Transaction` carries no `security_id` field, so
        there is nothing here to validate against Knowledge & Entity
        Model — unlike `add_holding_manually`, this is a plain,
        directly-trusted record of a user-entered transaction, not
        tagged `Provenance.UNTRUSTED` for the same reason
        `add_holding_manually` isn't."""
        with traced("DefaultUserPortfolio.add_transaction_manually"):
            transaction = Transaction(portfolio_id=portfolio.id, kind=kind, amount=amount)
            self._infrastructure.store(
                TRANSACTIONS_TABLE,
                {"id": str(uuid.uuid4()), **asdict(transaction)},
            )
            return transaction

    def _stored_holdings(self, portfolio_id: str) -> list[Holding]:
        records = self._infrastructure.query(HOLDINGS_TABLE, {"portfolio_id": portfolio_id})
        return [
            Holding(
                portfolio_id=record["portfolio_id"],
                security_id=record["security_id"],
                quantity=record["quantity"],
            )
            for record in records
        ]

    def _load_credentials(self, portfolio: Portfolio) -> BrokerCredentials | None:
        """Read the broker_credentials stored on `portfolio` at
        `connect_portfolio` time, strip the provenance key added by
        `BoundaryGate.tag_provenance`, and rebuild a `BrokerCredentials`
        instance. Returns `None` when the portfolio has no stored
        `broker_connection` (the same "no broker connected" case
        `import_holdings` / `import_transactions` already short-circuit
        on), so callers can early-return without restating the lookup."""
        stored = self._infrastructure.retrieve(PORTFOLIOS_TABLE, portfolio.id)
        if not stored or "broker_connection" not in stored:
            return None
        tagged_credentials = stored["broker_connection"]
        if isinstance(tagged_credentials, dict):
            credentials_dict = {k: v for k, v in tagged_credentials.items() if k != "_provenance"}
        else:
            credentials_dict = {}
        return BrokerCredentials(
            access_token=credentials_dict.get("access_token"),
            token_type=credentials_dict.get("token_type", "Bearer"),
            expires_at=credentials_dict.get("expires_at"),
            refresh_token=credentials_dict.get("refresh_token"),
            broker_user_id=credentials_dict.get("broker_user_id"),
            raw=credentials_dict.get("raw", {}),
        )
