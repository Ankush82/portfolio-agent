"""QA test for STORY-9: Broker registry, dependency wiring, and removal of PlaceholderBrokerConnector.

This test specifically exercises the acceptance criteria for STORY-9:
1. get_broker_connector('upstox') returns a DefaultUpstoxBrokerConnector
2. get_broker_connector('zerodha') raises UnsupportedBrokerError naming supported ids
3. grep -r PlaceholderBrokerConnector src/ returns no matches
4. DefaultUserPortfolio no longer references any concrete connector class and accepts BrokerConnector via injection
5. A test registers a throwaway fake connector into the registry under a new id and drives it through DefaultUserPortfolio without modifying DefaultUserPortfolio
6. If UPSTOX_CLIENT_ID/SECRET/REDIRECT_URI are unset, get_broker_connector('upstox') raises BrokerConfigError from STORY-1
"""

import pytest
from datetime import date
from decimal import Decimal

from broker_token_crypto import encrypt_secret
from components.c01_user_portfolio import (
    BrokerConfigError,
    BrokerConnector,
    BrokerCredentials,
    BrokerHolding,
    BrokerTransaction,
    DefaultUpstoxBrokerConnector,
    DefaultUserPortfolio,
    HoldingsImportResult,
    ImportResult,
    UnsupportedBrokerError,
    get_broker_connector,
    register_broker_connector,
    unregister_broker_connector,
)
from infrastructure_postgres import BrokerConnectionRecord


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double for STORY-9 tests."""

    def __init__(self) -> None:
        self._broker_connections: dict[tuple[str, str], dict] = {}
        self._broker_holdings: dict[tuple[str, str], list[dict]] = {}
        self._broker_transactions: list[dict] = []
        self._last_import_calls: list[tuple[str, str]] = []

    def get_broker_connection(self, user_id: str, broker_id: str):
        row = self._broker_connections.get((user_id, broker_id))
        if row is None:
            return None
        return BrokerConnectionRecord(
            id=row.get("id", "test-id"),
            user_id=row["user_id"],
            broker_id=row["broker_id"],
            broker_user_id=row.get("broker_user_id"),
            access_token_encrypted=row.get("access_token_encrypted", "test-encrypted"),
            token_type=row.get("token_type", "Bearer"),
            access_token_expires_at=row.get("access_token_expires_at"),
            status=row.get("status", "CONNECTED"),
            last_error=row.get("last_error"),
            connected_at=row.get("connected_at"),
            last_import_at=row.get("last_import_at"),
            created_at=row.get("created_at", "2024-01-01T00:00:00Z"),
            updated_at=row.get("updated_at", "2024-01-01T00:00:00Z"),
        )

    def replace_broker_holdings(self, user_id: str, broker_id: str, holdings: list[dict]) -> None:
        self._broker_holdings[(user_id, broker_id)] = list(holdings)

    def upsert_broker_transaction(
        self,
        user_id: str,
        broker_id: str,
        external_id: str,
        symbol: str,
        isin: str,
        trade_date: date,
        side: str,
        quantity: Decimal,
        price: Decimal,
        amount: Decimal,
        exchange: str,
        segment: str,
        raw: dict,
    ) -> bool:
        for existing in self._broker_transactions:
            if (existing["user_id"], existing["broker_id"], existing["external_id"]) == (
                user_id, broker_id, external_id
            ):
                existing.update(
                    dict(
                        symbol=symbol, isin=isin, trade_date=str(trade_date),
                        side=side, quantity=str(quantity), price=str(price),
                        amount=str(amount), exchange=exchange, segment=segment, raw=raw,
                    )
                )
                return False
        self._broker_transactions.append(
            dict(
                user_id=user_id, broker_id=broker_id, external_id=external_id,
                symbol=symbol, isin=isin, trade_date=str(trade_date),
                side=side, quantity=str(quantity), price=str(price),
                amount=str(amount), exchange=exchange, segment=segment, raw=raw,
            )
        )
        return True

    def touch_last_import(self, user_id: str, broker_id: str) -> None:
        self._last_import_calls.append((user_id, broker_id))
        key = (user_id, broker_id)
        if key in self._broker_connections:
            self._broker_connections[key]["last_import_at"] = "2024-01-01T00:00:00Z"

    def mark_broker_connection_error(self, user_id: str, broker_id: str, error_message: str) -> None:
        key = (user_id, broker_id)
        if key in self._broker_connections:
            self._broker_connections[key]["status"] = "ERROR"
            self._broker_connections[key]["last_error"] = error_message

    def _put_broker_connection(self, row: dict) -> None:
        """Test helper to seed a broker connection row."""
        self._broker_connections[(row["user_id"], row["broker_id"])] = dict(row)


class _ThrowawayBrokerConnector:
    """A throwaway fake connector registered under a brand-new broker_id.
    Proves DefaultUserPortfolio drives an arbitrary registered broker purely
    through get_broker_connector, with ZERO changes to DefaultUserPortfolio itself.
    """

    broker_id = "throwaway-test-broker"
    display_name = "Throwaway Test Broker"

    def __init__(self) -> None:
        self.fetch_holdings_call_count = 0
        self.fetch_transactions_call_count = 0

    def build_authorize_url(self, *, state: str) -> str:
        return f"https://throwaway.example/auth?state={state}"

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        return BrokerCredentials(access_token=f"throwaway-token-{code}")

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        self.fetch_holdings_call_count += 1
        return [
            BrokerHolding(
                symbol="THROWAWAY",
                isin="THROWAWAY-ISIN-0001",
                quantity=Decimal("10"),
                average_price=Decimal("100"),
                last_price=Decimal("110"),
            )
        ]

    def fetch_transactions(self, *, credentials, start_date, end_date) -> list[BrokerTransaction]:
        self.fetch_transactions_call_count += 1
        return [
            BrokerTransaction(
                external_id="throwaway-tx-001",
                symbol="THROWAWAY",
                isin="THROWAWAY-ISIN-0001",
                trade_date=date(2024, 1, 15),
                side="BUY",
                quantity=Decimal("10"),
                price=Decimal("100"),
                amount=Decimal("1000"),
                exchange="NASDAQ",
                segment="EQ",
                raw={},
            )
        ]


# =============================================================================
# ACCEPTANCE CRITERION 1: get_broker_connector('upstox') returns DefaultUpstoxBrokerConnector
# =============================================================================

def test_get_broker_connector_upstox_returns_default_upstox_broker_connector(monkeypatch):
    """AC: get_broker_connector('upstox') returns a DefaultUpstoxBrokerConnector."""
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/cb")

    connector = get_broker_connector("upstox")

    assert isinstance(connector, DefaultUpstoxBrokerConnector)
    assert connector.broker_id == "upstox"
    assert connector.display_name == "Upstox"


# =============================================================================
# ACCEPTANCE CRITERION 2: get_broker_connector('zerodha') raises UnsupportedBrokerError
# =============================================================================

def test_get_broker_connector_unknown_id_raises_unsupported_broker_error_naming_supported_ids():
    """AC: get_broker_connector('zerodha') raises UnsupportedBrokerError naming the supported ids."""
    with pytest.raises(UnsupportedBrokerError) as exc_info:
        get_broker_connector("zerodha")

    error_message = str(exc_info.value)
    assert "zerodha" in error_message
    assert "upstox" in error_message
    assert "supported ids" in error_message.lower()


# =============================================================================
# ACCEPTANCE CRITERION 3: No PlaceholderBrokerConnector in src/
# =============================================================================

def test_no_placeholder_broker_connector_in_src():
    """AC: grep -r PlaceholderBrokerConnector src/ returns no matches."""
    import subprocess
    result = subprocess.run(
        ["grep", "-r", "PlaceholderBrokerConnector", "src/"],
        capture_output=True,
        text=True
    )
    # grep returns exit code 1 when no matches found (which is what we want)
    assert result.returncode == 1, f"Found PlaceholderBrokerConnector in src/: {result.stdout}"
    assert result.stdout == ""


# =============================================================================
# ACCEPTANCE CRITERION 4: DefaultUserPortfolio accepts BrokerConnector via injection
# =============================================================================

def test_default_user_portfolio_accepts_broker_connector_via_injection():
    """AC: DefaultUserPortfolio no longer references any concrete connector class
    and accepts a BrokerConnector via constructor/parameter injection."""
    import inspect
    from components.c01_user_portfolio import DefaultUserPortfolio

    # Check constructor signature accepts broker_connector
    sig = inspect.signature(DefaultUserPortfolio.__init__)
    assert "broker_connector" in sig.parameters
    param = sig.parameters["broker_connector"]
    assert param.annotation is BrokerConnector or "BrokerConnector" in str(param.annotation)

    # Check class body has no Upstox-specific references -- in real CODE,
    # not in docstrings/comments (which legitimately mention "upstox" when
    # explaining that NO upstox-specific code lives here; a naive raw
    # substring search over inspect.getsource() false-positives on that
    # exact prose). Strip comments and docstrings via tokenize/ast first.
    import ast
    import io
    import tokenize

    source = inspect.getsource(DefaultUserPortfolio)
    code_only_lines = []
    tree = ast.parse(source)
    docstring_line_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                doc_node = node.body[0]
                docstring_line_ranges.append((doc_node.lineno, getattr(doc_node, "end_lineno", doc_node.lineno)))
    docstring_lines = {ln for start, end in docstring_line_ranges for ln in range(start, end + 1)}
    comment_lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])
    for i, line in enumerate(source.splitlines(), start=1):
        if i not in docstring_lines and i not in comment_lines:
            code_only_lines.append(line)
    code_only_source = "\n".join(code_only_lines)

    upstox_terms = [
        "upstox", "api.upstox.com",
        "client_id", "client_secret", "redirect_uri",
        "DefaultUpstoxBrokerConnector",
    ]
    for term in upstox_terms:
        assert term.lower() not in code_only_source.lower(), (
            f"DefaultUserPortfolio must not contain {term!r} in real code "
            "(docstrings/comments excluded); found in source"
        )

    # Verify it can be instantiated with an injected connector
    infra = _InMemoryInfrastructure()
    connector = _ThrowawayBrokerConnector()
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    assert portfolio._broker_connector is connector


# =============================================================================
# ACCEPTANCE CRITERION 5: Register throwaway connector and drive through DefaultUserPortfolio
# =============================================================================

def test_default_user_portfolio_drives_newly_registered_broker_via_registry_alone():
    """AC: A test registers a throwaway fake connector into the registry under a new id
    and drives it through DefaultUserPortfolio without modifying DefaultUserPortfolio."""
    infra = _InMemoryInfrastructure()
    infra._put_broker_connection({
        "user_id": "user-1",
        "broker_id": "throwaway-test-broker",
        "access_token_encrypted": encrypt_secret("seeded-token"),
        "token_type": "Bearer",
        "access_token_expires_at": None,
        "broker_user_id": None,
        "status": "CONNECTED",
    })
    connector = _ThrowawayBrokerConnector()
    register_broker_connector(connector)
    try:
        # No broker_connector injected at construction - must resolve via registry
        portfolio_component = DefaultUserPortfolio(infrastructure=infra)

        # Test import_holdings
        result = portfolio_component.import_holdings("user-1", "throwaway-test-broker")

        assert connector.fetch_holdings_call_count == 1
        assert result.holdings_written == 1
        assert result.skipped == 0

        # Test import_transactions
        result2 = portfolio_component.import_transactions("user-1", "throwaway-test-broker")

        assert connector.fetch_transactions_call_count == 1
        assert result2.transactions_inserted == 1
        assert result2.transactions_skipped_existing == 0
        assert result2.rows_skipped_invalid == 0
    finally:
        unregister_broker_connector("throwaway-test-broker")


# =============================================================================
# ACCEPTANCE CRITERION 6: Missing Upstox env raises BrokerConfigError from STORY-1
# =============================================================================

def test_get_broker_connector_upstox_raises_broker_config_error_when_env_unset(monkeypatch):
    """AC: If UPSTOX_CLIENT_ID/SECRET/REDIRECT_URI are unset, get_broker_connector('upstox')
    raises BrokerConfigError from STORY-1 rather than returning a half-built connector."""
    from upstox_config import BrokerConfigError as UpstoxConfigError

    monkeypatch.delenv("UPSTOX_CLIENT_ID", raising=False)
    monkeypatch.delenv("UPSTOX_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)

    with pytest.raises(UpstoxConfigError):
        get_broker_connector("upstox")


# =============================================================================
# ADDITIONAL: Verify registry docstring explains extension recipe
# =============================================================================

def test_broker_connectors_registry_has_extension_docstring():
    """AC: Add a docstring on the registry explaining the extension recipe
    (new Default<Broker>BrokerConnector class + one registry line, nothing else)."""
    from components.c01_user_portfolio import BROKER_CONNECTORS

    # The registry dict itself doesn't have a docstring -- the real
    # extension recipe lives in a comment immediately above
    # BROKER_CONNECTORS in the module source (not mod.__doc__, which is
    # the module's own top-of-file docstring, a different thing entirely).
    import components.c01_user_portfolio as mod
    import inspect
    import re

    # Comment line-wrapping is real, cosmetic source formatting -- a
    # phrase split across two "# "-prefixed lines (e.g. "...mapping its\n#
    # broker_id...") is still one continuous sentence to a human reader,
    # so normalize whitespace/comment markers before matching rather than
    # asserting on the exact wrap points, which would make this test
    # brittle to any future reflow of the same real comment.
    module_source = inspect.getsource(mod)
    normalized = re.sub(r"\s+", " ", module_source.replace("#", " "))
    assert "STORY-9: the real extension point for adding a new broker" in normalized
    assert "write a real Default<Broker>BrokerConnector class" in normalized
    assert "add ONE line here mapping its broker_id" in normalized
    assert "nothing else in this project needs to change" in normalized


# =============================================================================
# ADDITIONAL: Verify StubBrokerConnector is the only test double
# =============================================================================

def test_stub_broker_connector_is_only_test_double():
    """Verify StubBrokerConnector is the only test double (PlaceholderBrokerConnector removed)."""
    import components.c01_user_portfolio as mod
    from components.c01_user_portfolio import StubBrokerConnector

    # StubBrokerConnector should exist and be usable
    stub = StubBrokerConnector()
    assert stub.broker_id == "stub"
    assert stub.display_name == "Stub Broker"
    assert isinstance(stub, BrokerConnector)

    # PlaceholderBrokerConnector should not exist
    assert not hasattr(mod, "PlaceholderBrokerConnector")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])