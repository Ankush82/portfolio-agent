"""Tests for the bulk repository methods added in STORY-9."""

import sys
from typing import Sequence

# Ensure we can import from src
sys.path.insert(0, 'src')

from domain import Holding, Transaction
from repositories.holding_repository import HoldingRepository
from repositories.transaction_repository import TransactionRepository
from infrastructure import Infrastructure


class FakeInfrastructure(Infrastructure):
    """Infrastructure that tracks store calls and raises on unexpected calls."""
    def __init__(self) -> None:
        self.store_calls: list[tuple[str, dict]] = []

    def store(self, table: str, row: dict) -> str:
        self.store_calls.append((table, row))
        # Return a fake id
        return "fake-id"

    def retrieve(self, table: str, id: str) -> dict | None:
        raise NotImplementedError

    def query(self, table: str, where: dict | None) -> list[dict]:
        raise NotImplementedError

    def delete(self, table: str, id: str) -> bool:
        raise NotImplementedError


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