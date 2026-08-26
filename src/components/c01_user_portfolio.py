"""User & Portfolio (component 01) — the user's identity and the
portfolio it owns.

Whiteboard-level only (Component Whiteboards artifact, card 01) — no
low-level design or ADRs yet. Interfaces below match what that artifact
stated: Portfolio -> Portfolio State, User -> Decision & Policy,
Portfolio -> Event & Analysis, User -> Notification.
"""

from dataclasses import dataclass, field

from cross_cutting.observability import traced


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


class UserPortfolio:
    def onboard_user(self, details: dict) -> User:
        with traced("UserPortfolio.onboard_user"):
            return User(id="stub-id", preferences={})

    def connect_portfolio(self, user: User, broker_credentials: dict) -> Portfolio:
        with traced("UserPortfolio.connect_portfolio"):
            return Portfolio(id="stub-id", user_id="stub-id")

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        with traced("UserPortfolio.import_holdings"):
            return []

    def import_transactions(self, portfolio: Portfolio) -> list[Transaction]:
        with traced("UserPortfolio.import_transactions"):
            return []

    def synchronize_portfolio(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("UserPortfolio.synchronize_portfolio"):
            return PortfolioSnapshot(portfolio_id="stub-id", positions=[], exposure={})

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("UserPortfolio.track_portfolio_state"):
            return PortfolioSnapshot(portfolio_id="stub-id", positions=[], exposure={})

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        with traced("UserPortfolio.calculate_exposure"):
            return {}

    def manage_preferences(self, user: User, updates: dict) -> User:
        with traced("UserPortfolio.manage_preferences"):
            return User(id="stub-id", preferences={})

    def determine_user_relevance(self, user: User, event: dict) -> bool:
        """→ Event / Analysis interface: is this event relevant to
        this user's portfolio at all."""
        with traced("UserPortfolio.determine_user_relevance"):
            return True
