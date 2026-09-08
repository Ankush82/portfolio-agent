"""Tests for the bulk repository methods added in STORY-9."""

import sys
import uuid
from typing import Any, Sequence

# Ensure we can import from src
sys.path.insert(0, 'src')

from domain import Holding, Transaction
from repositories.holding_repository import HoldingRepository
from repositories.transaction_repository import TransactionRepository
from infrastructure import Infrastructure


class FakeInfrastructure(Infrastructure):
    """Infrastructure that tracks store calls and supports retrieve and query for testing idempotency and append-only."""
    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self.store_calls: list[tuple[str, dict]] = []

    def store(self, table: str, record: dict) -> str:
        self.store_calls.append((table, record))
        record_id = str(record.get("id", "")) or str(uuid.uuid4())
        # We'll store the record with the id
        stored = {**record, "id": record_id}
        self._tables.setdefault(table, {})[record_id] = stored
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        row = self._tables.get(table, {}).get(id_)
        if row is None:
            return None
        return {**row}  # shallow copy

    def query(self, table: str, filters: dict) -> list[dict]:
        rows = self._tables.get(table, {}).values()
        return [
            {**row}
            for row in rows
            if all(row.get(key) == value for key, value in filters.items())
        ]

    def delete(self, table: str, id_: str) -> bool:
        return self._tables.get(table, {}).pop(id_, None) is not None

    # We don't need the other methods for our tests, but we must implement them to satisfy the Infrastructure protocol.
    def publish(self, topic: str, event: dict) -> None:
        return None

    def subscribe(self, topic: str, handler: Any) -> None:
        return None

    def schedule(self, delay_seconds: float, task: dict) -> str:
        return ""

    def cache_get(self, key: str) -> Any | None:
        return None

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        return None

    def get_secret(self, name: str) -> str:
        return ""


def test_holding_upsert_many_annotation() -> None:
    """HoldingRepository.upsert_many uses Sequence[Holding] for holdings parameter."""
    import inspect
    from typing import get_type_hints, Sequence as TypingSequence
    from domain import Holding
    hints = get_type_hints(HoldingRepository.upsert_many)
    actual_annotation = hints['holdings']
    expected_annotation = TypingSequence[Holding]
    assert actual_annotation == expected_annotation, (
        f"Expected Sequence[Holding], got {actual_annotation}"
    )


def test_transaction_create_many_annotation() -> None:
    """TransactionRepository.create_many uses Sequence[Transaction] for transactions parameter."""
    import inspect
    from typing import get_type_hints, Sequence as TypingSequence
    from domain import Transaction
    hints = get_type_hints(TransactionRepository.create_many)
    actual_annotation = hints['transactions']
    expected_annotation = TypingSequence[Transaction]
    assert actual_annotation == expected_annotation, (
        f"Expected Sequence[Transaction], got {actual_annotation}"
    )


def test_holding_upsert_many_validation_before_write() -> None:
    """Validation happens before any write; mismatched portfolio_id raises ValueError and no store calls."""
    fake_infra = FakeInfrastructure()
    repo = HoldingRepository(fake_infra)
    holdings = [
        Holding(portfolio_id="valid", security_id="s1", quantity=1, currency="USD", exchange=None, symbol_suffix=None),
        Holding(portfolio_id="invalid", security_id="s2", quantity=1, currency="USD", exchange=None, symbol_suffix=None),
    ]
    try:
        repo.upsert_many("valid", holdings)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "does not match" in str(e)
    # Ensure no store calls were made
    assert fake_infra.store_calls == [], f"Unexpected store calls: {fake_infra.store_calls}"


def test_transaction_create_many_validation_before_write() -> None:
    """Validation happens before any write; mismatched portfolio_id raises ValueError and no store calls."""
    fake_infra = FakeInfrastructure()
    repo = TransactionRepository(fake_infra)
    transactions = [
        Transaction(portfolio_id="valid", kind="BUY", amount=100),
        Transaction(portfolio_id="invalid", kind="BUY", amount=100),
    ]
    try:
        repo.create_many("valid", transactions)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "does not match" in str(e)
    # Ensure no store calls were made
    assert fake_infra.store_calls == [], f"Unexpected store calls: {fake_infra.store_calls}"


def test_holding_upsert_many_loop_behavior() -> None:
    """upsert_many loops over single-row upsert (we can't test idempotency without a real store, but we can test it calls store)."""
    fake_infra = FakeInfrastructure()
    repo = HoldingRepository(fake_infra)
    holdings = [
        Holding(portfolio_id="p1", security_id="s1", quantity=1, currency="USD", exchange=None, symbol_suffix=None),
        Holding(portfolio_id="p1", security_id="s2", quantity=2, currency="USD", exchange=None, symbol_suffix=None),
    ]
    result = repo.upsert_many("p1", holdings)
    assert len(result) == 2
    # Each holding should have resulted in a store call
    assert len(fake_infra.store_calls) == 2
    # Check that the rows passed to store have the expected portfolio_id and security_id
    for i, (table, row) in enumerate(fake_infra.store_calls):
        assert table == "holdings"
        assert row["portfolio_id"] == "p1"
        assert row["security_id"] == holdings[i].security_id


def test_transaction_create_many_loop_behavior() -> None:
    """create_many loops over single-row create (append-only)."""
    fake_infra = FakeInfrastructure()
    repo = TransactionRepository(fake_infra)
    transactions = [
        Transaction(portfolio_id="p1", kind="BUY", amount=100),
        Transaction(portfolio_id="p1", kind="SELL", amount=50),
    ]
    result = repo.create_many("p1", transactions)
    assert len(result) == 2
    # Each transaction should have resulted in a store call
    assert len(fake_infra.store_calls) == 2
    # Check that the rows passed to store have the expected portfolio_id and kind/amount
    for i, (table, row) in enumerate(fake_infra.store_calls):
        assert table == "transactions"
        assert row["portfolio_id"] == "p1"
        assert row["kind"] == transactions[i].kind
        assert row["amount"] == transactions[i].amount


def test_holding_upsert_many_idempotent():
    """Test that upsert_many is idempotent: calling twice with the same list of holdings
    (same natural keys) updates the rows but does not insert new ones."""
    fake_infra = FakeInfrastructure()
    repo = HoldingRepository(fake_infra)

    # Create two holdings with different natural keys
    holdings = [
        Holding(portfolio_id="p1", security_id="s1", quantity=1, currency="USD", exchange="NASDAQ", symbol_suffix=None),
        Holding(portfolio_id="p1", security_id="s2", quantity=2, currency="USD", exchange="NYSE", symbol_suffix=None),
    ]

    # First call
    result1 = repo.upsert_many("p1", holdings)
    assert len(result1) == 2

    # Check that we have two rows in the holdings table
    rows = fake_infra.query("holdings", {})
    assert len(rows) == 2

    # Now, change the values of the holdings (but keep the same natural keys)
    holdings_updated = [
        Holding(portfolio_id="p1", security_id="s1", quantity=10, currency="INR", exchange="BSE", symbol_suffix=".BO"),
        Holding(portfolio_id="p1", security_id="s2", quantity=20, currency="INR", exchange="NSE", symbol_suffix=".NS"),
    ]

    # Second call with the updated holdings (same natural keys)
    result2 = repo.upsert_many("p1", holdings_updated)
    assert len(result2) == 2

    # Check that we still have two rows (no new rows inserted)
    rows = fake_infra.query("holdings", {})
    assert len(rows) == 2

    # Check that the rows have the updated values
    all_rows = fake_infra.query("holdings", {"portfolio_id": "p1"})
    assert len(all_rows) == 2

    # Convert to a dict by security_id for easy checking
    rows_by_security_id = {row["security_id"]: row for row in all_rows}

    # Check the first holding
    assert rows_by_security_id["s1"]["quantity"] == 10
    assert rows_by_security_id["s1"]["currency"] == "INR"
    assert rows_by_security_id["s1"]["exchange"] == "BSE"
    assert rows_by_security_id["s1"]["symbol_suffix"] == ".BO"

    # Check the second holding
    assert rows_by_security_id["s2"]["quantity"] == 20
    assert rows_by_security_id["s2"]["currency"] == "INR"
    assert rows_by_security_id["s2"]["exchange"] == "NSE"
    assert rows_by_security_id["s2"]["symbol_suffix"] == ".NS"


def test_transaction_create_many_append_only():
    """Test that create_many is append-only: calling twice with the same list of transactions
    results in twice the number of rows."""
    fake_infra = FakeInfrastructure()
    repo = TransactionRepository(fake_infra)

    transactions = [
        Transaction(portfolio_id="p1", kind="BUY", amount=100),
        Transaction(portfolio_id="p1", kind="SELL", amount=50),
    ]

    # First call
    result1 = repo.create_many("p1", transactions)
    assert len(result1) == 2

    # Check we have two rows
    rows = fake_infra.query("transactions", {})
    assert len(rows) == 2

    # Second call with the same transactions
    result2 = repo.create_many("p1", transactions)
    assert len(result2) == 2

    # Now we should have 4 rows
    rows = fake_infra.query("transactions", {})
    assert len(rows) == 4

    # We can also check that the transactions are in the order they were inserted? Not required, but we can check that we have two buys and two sells.
    buys = [row for row in rows if row["kind"] == "BUY"]
    sells = [row for row in rows if row["kind"] == "SELL"]
    assert len(buys) == 2
    assert len(sells) == 2


def test_holding_upsert_many_has_deliberate_comment():
    """Check that the HoldingRepository.upsert_many method contains the deliberate comment."""
    with open("src/repositories/holding_repository.py", "r") as f:
        content = f.read()
    # Check for the two key phrases that indicate the deliberate comment
    assert "loop over single-row" in content.lower(), "Expected phrase 'loop over single-row' not found in holding_repository.py"
    assert "performance is not a goal" in content.lower(), "Expected phrase 'performance is not a goal' not found in holding_repository.py"


def test_transaction_create_many_has_deliberate_comment():
    """Check that the TransactionRepository.create_many method contains the deliberate comment."""
    with open("src/repositories/transaction_repository.py", "r") as f:
        content = f.read()
    # Check for the two key phrases that indicate the deliberate comment
    assert "loop over single-row" in content.lower(), "Expected phrase 'loop over single-row' not found in transaction_repository.py"
    assert "performance is not a goal" in content.lower(), "Expected phrase 'performance is not a goal' not found in transaction_repository.py"


def test_no_bulk_methods_in_infrastructure_protocol():
    """Check that the Infrastructure protocol does not contain bulk methods upsert_many or create_many."""
    with open("src/infrastructure.py", "r") as f:
        content = f.read()
    # Find the Infrastructure protocol class
    import re
    pattern = r"class Infrastructure\(Protocol\):.*?(?=class|\\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        protocol_content = match.group(0)
        # Check that upsert_many and create_many are not in the protocol content
        assert "def upsert_many" not in protocol_content, "upsert_many should not be in Infrastructure protocol"
        assert "def create_many" not in protocol_content, "create_many should not be in Infrastructure protocol"
    else:
        raise AssertionError("Could not find Infrastructure protocol class")