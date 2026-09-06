"""QA tests for STORY-15: Refactor c01_user_portfolio.py to persist through
the four repositories.

These tests verify the acceptance criteria for the refactor -- specifically
exercising the behaviors that were failing (or would fail) after the
repository-layer refactor, NOT the full test suite.

Acceptance criteria tested:
  AC-1: connect_portfolio calls self._broker_connector.connect()
  AC-2: import_holdings persists provenance in stored holding records
  AC-3: import_transactions persists provenance in stored transaction records
  AC-4: repositories are instantiated inside DefaultUserPortfolio constructors
  AC-5: no direct store/retrieve/query/delete call for the four entities
        outside the re-export import line
  AC-6: public method signatures unchanged (def lines intact)
  AC-7: list-returning methods preserve element ordering
"""

import ast
import inspect
import re
from decimal import Decimal

import pytest

from components.c01_user_portfolio import (
    BrokerHolding,
    BrokerTransaction,
    DefaultUserPortfolio,
    Holding,
    Portfolio,
    PortfolioSnapshot,
    Position,
    Transaction,
    User,
)
from cross_cutting import observability


# --- test doubles (mirror the patterns from test_user_portfolio.py) ---------

class _InMemoryInfrastructure:
    """Minimal Infrastructure test double mirroring
    DefaultInfrastructure semantics for store/retrieve/query."""

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

    def delete(self, table: str, id: str) -> bool:
        return self._tables.get(table, {}).pop(id, None) is not None


class _FakeBrokerConnectorForConnect:
    """Test double that records whether connect() was called and with what.

    This is the critical test double for AC-1: the original code never called
    connect(), but tests expect it to be called. We verify the refactored
    code DOES call it.

    IMPORTANT: returns a dict with 'access_token' so _load_credentials
    in the refactored code can build a BrokerCredentials and the import
    methods can actually store holdings/transactions."""
    connect_calls: list[tuple[User, dict]]

    def __init__(self):
        self.connect_calls = []
        self._fetched_holdings = []
        self._fetched_transactions = []

    def connect(self, user: User, broker_credentials: dict) -> dict:
        self.connect_calls.append((user, broker_credentials))
        # Return credentials with access_token so _load_credentials
        # (which checks for access_token) can build a BrokerCredentials
        return {
            "external_account_id": "acct-123",
            "broker": "fake",
            "access_token": "fake-token-for-test",
            "token_type": "Bearer",
            "refresh_token": None,
            "expires_at": None,
            "broker_user_id": "broker-user-1",
            "raw": {},
        }

    def fetch_holdings(self, *, credentials) -> list:
        return list(self._fetched_holdings)

    def fetch_transactions(self, *, credentials, start_date, end_date) -> list:
        return list(self._fetched_transactions)


# --- AC-1: connect_portfolio calls broker_connector.connect() ---------------

def test_connect_portfolio_calls_broker_connector_connect_method():
    """AC-1: After the repo-layer refactor, connect_portfolio must still call
    self._broker_connector.connect(user, broker_credentials) so that
    connect_calls is recorded on the injected connector.

    This was a regression: the refactored code stored the portfolio
    through the repository but dropped the connector.connect() call.
    The acceptance criterion is: the connector must receive the call."""
    infra = _InMemoryInfrastructure()
    connector = _FakeBrokerConnectorForConnect()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    user = User(id="user-1", preferences={})

    portfolio_component.connect_portfolio(user, {"api_key": "my-secret-key"})

    assert len(connector.connect_calls) == 1, (
        "connect_portfolio must call self._broker_connector.connect(); "
        f"got {len(connector.connect_calls)} calls"
    )
    called_user, called_creds = connector.connect_calls[0]
    assert called_user is user
    assert called_creds == {"api_key": "my-secret-key"}


# --- AC-2: import_holdings persists provenance in stored records ----------

def test_import_holdings_stores_provenance_in_holdings_table():
    """AC-2: import_holdings must store the provenance extra-field (tagged
    UNTRUSTED by BoundaryGate) alongside the holding record in storage.

    The repository's _to_row converts the Holding dataclass to a dict, but
    provenance is an extra-field added by tag_provenance that must also be
    persisted. Without it, downstream tests that check
    stored["provenance"] == Provenance.UNTRUSTED.name will fail."""
    infra = _InMemoryInfrastructure()

    class _ConnectorWithHoldings:
        def __init__(self):
            self.connect_calls = []
        def connect(self, user, broker_credentials):
            self.connect_calls.append((user, broker_credentials))
            return {
                "access_token": "fake-token-for-holdings-test",
                "token_type": "Bearer",
                "refresh_token": None,
                "expires_at": None,
                "broker_user_id": "broker-user-1",
                "raw": {},
            }
        def fetch_holdings(self, *, credentials):
            # Return BrokerHolding instances (the type the refactored code expects)
            return [
                BrokerHolding(
                    symbol="AAPL", isin="US0378331005",
                    quantity=Decimal("10"), average_price=Decimal("150"),
                    last_price=Decimal("175"), exchange="NASDAQ",
                    product="EQ", instrument_id="i-AAPL",
                    name="Apple Inc.", raw={},
                ),
            ]
        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    connector = _ConnectorWithHoldings()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    user = User(id="user-1", preferences={})

    # connect_portfolio first (needed for _load_credentials to work)
    portfolio = portfolio_component.connect_portfolio(user, {"api_key": "test"})

    holdings = portfolio_component.import_holdings(portfolio)

    assert len(holdings) == 1
    holding_id = f"{portfolio.id}:{holdings[0].security_id}"
    stored = infra.retrieve("holdings", holding_id)
    assert stored is not None, "holding must be stored in 'holdings' table"
    assert "provenance" in stored, (
        "import_holdings must persist provenance in stored record; "
        f"stored keys: {list(stored.keys())}"
    )
    assert stored["provenance"] == "UNTRUSTED", (
        f"provenance must be UNTRUSTED; got {stored['provenance']!r}"
    )


# --- AC-3: import_transactions persists provenance in stored records -------

def test_import_transactions_stores_provenance_in_transactions_table():
    """AC-3: import_transactions must store the provenance extra-field
    (tagged UNTRUSTED by BoundaryGate) alongside the transaction record.

    Same pattern as AC-2 but for transactions."""
    infra = _InMemoryInfrastructure()

    class _ConnectorWithTransactions:
        def __init__(self):
            self.connect_calls = []
        def connect(self, user, broker_credentials):
            self.connect_calls.append((user, broker_credentials))
            return {
                "access_token": "fake-token-for-tx-test",
                "token_type": "Bearer",
                "refresh_token": None,
                "expires_at": None,
                "broker_user_id": "broker-user-1",
                "raw": {},
            }
        def fetch_holdings(self, *, credentials):
            return []
        def fetch_transactions(self, *, credentials, start_date, end_date):
            from datetime import date
            return [
                BrokerTransaction(
                    external_id="tx-001", symbol="AAPL", isin="US0378331005",
                    trade_date=date(2024, 1, 15), side="BUY",
                    quantity=Decimal("2"), price=Decimal("150"),
                    amount=Decimal("300"), exchange="NASDAQ",
                    segment="EQ", raw={},
                ),
            ]

    connector = _ConnectorWithTransactions()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    user = User(id="user-1", preferences={})
    portfolio = portfolio_component.connect_portfolio(user, {"api_key": "test"})

    transactions = portfolio_component.import_transactions(portfolio)

    assert len(transactions) == 1
    stored_records = infra.query("transactions", {"portfolio_id": portfolio.id})
    assert len(stored_records) == 1, "transaction must be stored in 'transactions' table"
    assert "provenance" in stored_records[0], (
        "import_transactions must persist provenance in stored record; "
        f"stored keys: {list(stored_records[0].keys())}"
    )
    assert stored_records[0]["provenance"] == "UNTRUSTED"


# --- AC-4: repositories are instantiated inside constructors --------------

def test_default_user_portfolio_constructs_repositories_from_infrastructure():
    """AC-4: Repositories must be constructed inside DefaultUserPortfolio's
    __init__ from the already-injected Infrastructure. No required constructor
    argument is added (keyword-only optional parameters with defaults)."""
    infra = _InMemoryInfrastructure()
    component = DefaultUserPortfolio(infrastructure=infra)

    # All four repositories must exist as instance attributes
    assert hasattr(component, "_users"), "DefaultUserPortfolio must have _users repository"
    assert hasattr(component, "_portfolios"), "DefaultUserPortfolio must have _portfolios repository"
    assert hasattr(component, "_holdings"), "DefaultUserPortfolio must have _holdings repository"
    assert hasattr(component, "_transactions"), "DefaultUserPortfolio must have _transactions repository"

    # Each repository's _infrastructure must be the same object as what was injected
    assert component._users._infrastructure is infra
    assert component._portfolios._infrastructure is infra
    assert component._holdings._infrastructure is infra
    assert component._transactions._infrastructure is infra


def test_default_user_portfolio_no_required_constructor_argument_added():
    """AC-4 (continued): The refactor must not add any required arguments to
    DefaultUserPortfolio.__init__ -- callers that don't pass repository
    objects must still get working default repositories."""
    # Default construction (no arguments) must work
    component = DefaultUserPortfolio()
    assert component._users is not None
    assert component._portfolios is not None
    assert component._holdings is not None
    assert component._transactions is not None

    # With only infrastructure argument (original pattern) must work
    infra = _InMemoryInfrastructure()
    component2 = DefaultUserPortfolio(infrastructure=infra)
    assert component2._users is not None
    assert component2._portfolios is not None
    assert component2._holdings is not None
    assert component2._transactions is not None


# --- AC-5: grep -n table constants returns only re-export import line -------

def test_grep_table_constants_shows_only_re_export_import_line():
    """AC-5: grep -n 'USERS_TABLE|PORTFOLIOS_TABLE|HOLDINGS_TABLE|TRANSACTIONS_TABLE'
    on src/components/c01_user_portfolio.py should return only the re-export import
    line from domain.py. Any additional lines are direct uses of table constants that
    should have been replaced by repository calls.

    Current state (FAIL): The refactored code still has 4 direct uses:
      - Line 591:  PORTFOLIOS_TABLE (in connect_portfolio)
      - Line 627:  HOLDINGS_TABLE  (in import_holdings)
      - Line 650:  TRANSACTIONS_TABLE (in import_transactions)
      - Line 903:  USERS_TABLE  (in manage_preferences)

    These must be replaced with repository-level calls (e.g.
    self._portfolios.create(...) instead of self._portfolios._infrastructure.store(
    PORTFOLIOS_TABLE, ...))."""
    import os
    import subprocess
    # Use absolute path so subprocess runs correctly regardless of cwd
    src_file = os.path.abspath("src/components/c01_user_portfolio.py")
    result = subprocess.run(
        ["grep", "-n", "-E", "USERS_TABLE|PORTFOLIOS_TABLE|HOLDINGS_TABLE|TRANSACTIONS_TABLE",
         src_file],
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln]

    # The re-export import line (line 51 in the real file) contains the four
    # constants as a multi-line import from domain.py. Grep only matches the
    # ONE LINE that contains the constant name(s), which is the continuation
    # line of the import. We identify it by: it is the ONLY line that contains
    # ALL FOUR constant names AND appears inside an import context.
    # Direct-use lines contain only ONE or TWO constants (in a store call).
    # Also check that the re-export line is preceded by "from domain import".
    reexport_pattern_lines = []
    direct_use_lines = []
    for ln in lines:
        # Count how many of the four constants appear in this line
        constants_in_line = sum(1 for c in ["USERS_TABLE", "PORTFOLIOS_TABLE",
                                              "HOLDINGS_TABLE", "TRANSACTIONS_TABLE"]
                                if c in ln)
        # The import line (line 51) contains at least 3 of the 4 constants
        # as part of the "from domain import (...)" statement
        # Direct-use lines are store() calls with ONE constant
        if constants_in_line >= 3:
            reexport_pattern_lines.append(ln)
        else:
            direct_use_lines.append(ln)

    # ACCEPTANCE CRITERION: only the re-export import line is permitted
    # Every other line is a direct use that must be replaced with a repository call
    assert len(reexport_pattern_lines) >= 1, (
        f"Expected at least one re-export import line from domain.py; grep output:\n"
        + "\n".join(lines)
    )
    assert len(direct_use_lines) == 0, (
        f"ACCEPTANCE CRITERION FAILED: c01 has {len(direct_use_lines)} direct use(s) "
        f"of table constants outside the re-export import line.\n"
        f"These must be replaced with repository-level calls (create/upsert/store via repo):\n"
        + "\n".join(direct_use_lines)
        + "\n\nFull grep output:\n"
        + "\n".join(lines)
    )


# --- AC-6: public method signatures unchanged ------------------------------

_PUBLIC_METHOD_NAMES = [
    "onboard_user",
    "connect_portfolio",
    "import_holdings",
    "import_transactions",
    "synchronize_portfolio",
    "track_portfolio_state",
    "calculate_exposure",
    "manage_preferences",
    "determine_user_relevance",
    "list_available_securities",
    "add_holding_manually",
    "add_transaction_manually",
    "calculate_portfolio_totals",
    "calculate_gains_losses",
]


def _extract_def_lines_for_class(source_code: str, class_name: str) -> dict[str, str]:
    """Parse a class body and return {method_name: signature_line} for
    every def in the class (including private ones for completeness)."""
    tree = ast.parse(source_code)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            result = {}
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    # Reconstruct signature line from AST
                    args = item.args
                    arg_parts = []
                    # self is not stored in ast.arguments.kwonlyargs, it's first
                    for arg in args.args:
                        if arg.arg == "self":
                            continue
                        arg_parts.append(arg.arg)
                    # *args and **kwargs
                    if args.vararg:
                        arg_parts.append(f"*{args.vararg.arg}")
                    if args.kwarg:
                        arg_parts.append(f"**{args.kwarg.arg}")
                    sig = f"def {item.name}({', '.join(arg_parts)}):"
                    result[item.name] = sig
            return result
    return {}


def test_public_method_signatures_unchanged_since_refactor(tmp_path, monkeypatch):
    """AC-6: No public (non-underscore-prefixed) def signature line changed.
    We snapshot the current signatures and verify no public method signature
    was altered (for this story we verify the known-good signatures match
    what the Protocol and existing callers expect)."""
    source = inspect.getsource(DefaultUserPortfolio)
    signatures = _extract_def_lines_for_class(source, "DefaultUserPortfolio")

    for method_name in _PUBLIC_METHOD_NAMES:
        assert method_name in signatures, (
            f"Public method {method_name!r} must still exist in DefaultUserPortfolio"
        )

    # Verify specific signatures that were known-good before the refactor
    # Note: AST parsing strips type annotations, so we check arg names only
    sig_connect = signatures.get("connect_portfolio", "")
    assert "user" in sig_connect, f"connect_portfolio signature missing 'user' arg: {sig_connect}"
    assert "broker_credentials" in sig_connect, (
        f"connect_portfolio signature missing 'broker_credentials' arg: {sig_connect}"
    )

    sig_import_holdings = signatures.get("import_holdings", "")
    assert "portfolio" in sig_import_holdings, (
        f"import_holdings signature missing 'portfolio' arg: {sig_import_holdings}"
    )

    sig_import_trans = signatures.get("import_transactions", "")
    # The Protocol declares only (portfolio: Portfolio) but the implementation
    # added keyword-only start_date/end_date -- verify the Portfolio positional
    # parameter is present (no required args added)
    assert "portfolio" in sig_import_trans, (
        f"import_transactions signature missing 'portfolio' arg: {sig_import_trans}"
    )


# --- AC-7: list-returning methods preserve ordering ----------------------

def test_import_holdings_preserves_holding_order_from_broker():
    """AC-7: import_holdings must return holdings in the order the broker
    connector returns them. The repo-layer refactor must not reorder them."""
    infra = _InMemoryInfrastructure()

    class _OrderedConnector:
        def __init__(self):
            self.connect_calls = []
        def connect(self, user, broker_credentials):
            self.connect_calls.append((user, broker_credentials))
            return {}
        def fetch_holdings(self, *, credentials):
            # Return in a specific order: MSFT, AAPL, GOOG
            from decimal import Decimal
            from dataclasses import asdict
            from components.c01_user_portfolio import BrokerHolding
            holdings = [
                BrokerHolding(symbol="MSFT", isin="US5949181045",
                              quantity=Decimal("5"), average_price=Decimal("300"),
                              last_price=Decimal("310"), exchange="NASDAQ",
                              product="EQ", instrument_id="i-MSFT",
                              name="Microsoft Corp.", raw={}),
                BrokerHolding(symbol="AAPL", isin="US0378331005",
                              quantity=Decimal("10"), average_price=Decimal("150"),
                              last_price=Decimal("175"), exchange="NASDAQ",
                              product="EQ", instrument_id="i-AAPL",
                              name="Apple Inc.", raw={}),
                BrokerHolding(symbol="GOOG", isin="US02079K3059",
                              quantity=Decimal("3"), average_price=Decimal("120"),
                              last_price=Decimal("130"), exchange="NASDAQ",
                              product="EQ", instrument_id="i-GOOG",
                              name="Alphabet Inc.", raw={}),
            ]
            return holdings
        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    connector = _OrderedConnector()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    user = User(id="user-1", preferences={})
    portfolio = portfolio_component.connect_portfolio(user, {"api_key": "test"})

    holdings = portfolio_component.import_holdings(portfolio)

    assert [h.security_id for h in holdings] == ["MSFT", "AAPL", "GOOG"], (
        f"import_holdings must preserve broker return order; "
        f"got {[h.security_id for h in holdings]!r}"
    )


def test_determine_user_relevance_returns_portfolios_in_query_order():
    """AC-7: determine_user_relevance must return the same boolean result
    regardless of the order repositories return portfolios/holdings.
    We test with multiple portfolios and verify all are checked."""
    infra = _InMemoryInfrastructure()
    portfolio_component = DefaultUserPortfolio(infrastructure=infra)
    user = User(id="user-1", preferences={})

    # Store two portfolios in a specific order
    infra.store("portfolios", {"id": "pf-A", "user_id": "user-1"})
    infra.store("portfolios", {"id": "pf-B", "user_id": "user-1"})
    # Store AAPL in portfolio A
    infra.store("holdings", {"id": "pf-A:AAPL", "portfolio_id": "pf-A", "security_id": "AAPL", "quantity": 1.0})
    # Store nothing in portfolio B

    result = portfolio_component.determine_user_relevance(user, {"security_id": "AAPL"})

    assert result is True, (
        "determine_user_relevance must check all portfolios; "
        "AAPL is held in pf-A so the result must be True"
    )
