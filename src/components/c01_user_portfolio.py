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
from typing import Protocol

from components.c04_knowledge_entity import DefaultKnowledgeEntity, Entity
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


@dataclass
class User:
    id: str
    preferences: dict = field(default_factory=dict)


@dataclass
class Portfolio:
    id: str
    user_id: str


@dataclass
class Holding:
    portfolio_id: str
    security_id: str
    quantity: float


@dataclass
class Position:
    holding: Holding
    market_value: float


@dataclass
class Transaction:
    portfolio_id: str
    kind: str
    amount: float


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


class BrokerConnector(Protocol):
    """The seam `connect_portfolio`/`import_holdings`/`import_transactions`
    call through instead of talking to a broker/DMAT API directly
    (ADR-0022). A `Protocol` with three named methods, not a single
    injected `Callable` — see ADR-0022's Decision section for why the
    three real-world operations here (establish a session, read
    holdings, read transactions) don't share one shape the way Agent
    Runtime's `reason_fn` does.

    Every method returns plain, untagged dicts. Tagging broker-sourced
    content UNTRUSTED (ADR-0022, extending ADR-0003/ADR-0018) is
    `DefaultUserPortfolio`'s responsibility, at the point each result
    is used — not this connector's, so a future real implementation
    doesn't have to know about provenance at all."""

    def connect(self, user: User, broker_credentials: dict) -> dict:
        """Establishes (or simulates) a broker/DMAT session for
        `user`. Returns broker-supplied connection metadata (e.g. an
        external account identifier) as a plain dict."""
        ...

    def fetch_holdings(self, portfolio: Portfolio) -> list[dict]:
        """Each returned dict is expected to carry at least
        `security_id` and `quantity` — the fields DefaultUserPortfolio
        needs to build a Holding."""
        ...

    def fetch_transactions(self, portfolio: Portfolio) -> list[dict]:
        """Each returned dict is expected to carry at least `kind` and
        `amount` — the fields DefaultUserPortfolio needs to build a
        Transaction."""
        ...


class PlaceholderBrokerConnector:
    """NOT a real broker connection (ADR-0022, ADR-0023). No live
    broker/DMAT API credential exists in this project — ADR-0023 names
    the real options (Zerodha Kite Connect, Upstox, the RBI Account
    Aggregator framework) without choosing one, since that requires a
    live external credential this pass cannot obtain. This class exists
    purely so DefaultUserPortfolio's connect_portfolio/import_holdings/
    import_transactions are buildable, runnable, and testable end to
    end without one.

    `connect()` returns synthetic connection metadata carrying an
    obviously-fake account identifier — never a value a real broker API
    would return. `fetch_holdings()`/`fetch_transactions()` return
    empty lists rather than inventing synthetic positions or trades:
    fabricated financial data would look real to anything downstream
    that doesn't already know this path is a placeholder, where an
    empty list cannot be mistaken for real holdings."""

    def connect(self, user: User, broker_credentials: dict) -> dict:
        with traced("PlaceholderBrokerConnector.connect"):
            return {
                "external_account_id": f"placeholder-account-{uuid.uuid4()}",
                "broker": "placeholder",
            }

    def fetch_holdings(self, portfolio: Portfolio) -> list[dict]:
        with traced("PlaceholderBrokerConnector.fetch_holdings"):
            return []

    def fetch_transactions(self, portfolio: Portfolio) -> list[dict]:
        with traced("PlaceholderBrokerConnector.fetch_transactions"):
            return []


USERS_TABLE = "users"
PORTFOLIOS_TABLE = "portfolios"
HOLDINGS_TABLE = "holdings"
TRANSACTIONS_TABLE = "transactions"

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
            user = User(id=str(uuid.uuid4()), preferences=dict(details.get("preferences", {})))
            self._infrastructure.store(USERS_TABLE, {"id": user.id, "preferences": user.preferences})
            return user

    def connect_portfolio(self, user: User, broker_credentials: dict) -> Portfolio:
        with traced("DefaultUserPortfolio.connect_portfolio"):
            raw_connection = self._broker_connector.connect(user, broker_credentials)
            tagged_connection = self._boundary_gate.tag_provenance(raw_connection, source="broker_connector")
            portfolio = Portfolio(id=str(uuid.uuid4()), user_id=user.id)
            self._infrastructure.store(
                PORTFOLIOS_TABLE,
                {
                    "id": portfolio.id,
                    "user_id": portfolio.user_id,
                    "broker_connection": tagged_connection,
                },
            )
            self._audit_manager.record(
                "portfolio_connected",
                {
                    "portfolio_id": portfolio.id,
                    "user_id": user.id,
                    "provenance": tagged_connection.get("provenance"),
                },
            )
            return portfolio

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        with traced("DefaultUserPortfolio.import_holdings"):
            raw_holdings = self._broker_connector.fetch_holdings(portfolio)
            holdings = []
            for raw in raw_holdings:
                tagged = self._boundary_gate.tag_provenance(raw, source="broker_connector")
                holding = Holding(
                    portfolio_id=portfolio.id,
                    security_id=tagged["security_id"],
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

    def import_transactions(self, portfolio: Portfolio) -> list[Transaction]:
        with traced("DefaultUserPortfolio.import_transactions"):
            raw_transactions = self._broker_connector.fetch_transactions(portfolio)
            transactions = []
            for raw in raw_transactions:
                tagged = self._boundary_gate.tag_provenance(raw, source="broker_connector")
                transaction = Transaction(
                    portfolio_id=portfolio.id,
                    kind=tagged["kind"],
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
            total_market_value = sum(position.market_value for position in snapshot.positions)
            exposure: dict[str, dict] = {}
            for position in snapshot.positions:
                security_id = position.holding.security_id
                entry = exposure.setdefault(security_id, {"market_value": 0.0, "weight": 0.0})
                entry["market_value"] += position.market_value
            if total_market_value > 0:
                for entry in exposure.values():
                    entry["weight"] = entry["market_value"] / total_market_value
            return exposure

    def manage_preferences(self, user: User, updates: dict) -> User:
        with traced("DefaultUserPortfolio.manage_preferences"):
            stored = self._infrastructure.retrieve(USERS_TABLE, user.id)
            current_preferences = dict(stored["preferences"]) if stored else dict(user.preferences)
            current_preferences.update(updates)
            self._infrastructure.store(USERS_TABLE, {"id": user.id, "preferences": current_preferences})
            return User(id=user.id, preferences=current_preferences)

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
