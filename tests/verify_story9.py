"""Verification script for STORY-9 bulk repository methods."""

import sys
from typing import Sequence, List

# Ensure we can import from src
sys.path.insert(0, 'src')

from domain import Holding, Transaction
from repositories.holding_repository import HoldingRepository
from repositories.transaction_repository import TransactionRepository
from infrastructure import Infrastructure


class FakeInfrastructure(Infrastructure):
    """Infrastructure that tracks store calls and raises on unexpected calls."""
    def __init__(self) -> None:
        self.store_calls: List[tuple[str, dict]] = []

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


def test_holding_upsert_many_signature_and_comment() -> None:
    """Check that HoldingRepository.upsert_many has the correct signature and comment."""
    import inspect
    from typing import get_type_hints, Sequence as TypingSequence
    from domain import Holding
    from repositories.holding_repository import HoldingRepository

    # Check signature
    sig = inspect.signature(HoldingRepository.upsert_many)
    params = sig.parameters
    assert 'portfolio_id' in params
    assert 'holdings' in params
    hints = get_type_hints(HoldingRepository.upsert_many)
    assert hints['holdings'] == TypingSequence[Holding]
    assert sig.return_annotation == List[Holding]

    # Check comment exists in the method source
    source = inspect.getsource(HoldingRepository.upsert_many)
    assert "Loop over single-row upsert is deliberate" in source
    assert "performance is not a goal" in source.lower()


def test_transaction_create_many_signature_and_comment() -> None:
    """Check that TransactionRepository.create_many has the correct signature and comment."""
    import inspect
    from typing import get_type_hints, Sequence as TypingSequence
    from domain import Transaction
    from repositories.transaction_repository import TransactionRepository

    # Check signature
    sig = inspect.signature(TransactionRepository.create_many)
    params = sig.parameters
    assert 'portfolio_id' in params
    assert 'transactions' in params
    hints = get_type_hints(TransactionRepository.create_many)
    assert hints['transactions'] == TypingSequence[Transaction]
    assert sig.return_annotation == List[Transaction]

    # Check comment exists in the method source
    source = inspect.getsource(TransactionRepository.create_many)
    assert "Loop over single-row create is deliberate" in source
    assert "performance is not a goal" in source.lower()


def test_validation_occurs_before_writes() -> None:
    """Test that validation happens before any writes for both methods."""
    # HoldingRepository
    fake_infra = FakeInfrastructure()
    holding_repo = HoldingRepository(fake_infra)
    holdings = [
        Holding(portfolio_id="valid", security_id="s1", quantity=1, currency="USD", exchange=None, symbol_suffix=None),
        Holding(portfolio_id="invalid", security_id="s2", quantity=1, currency="USD", exchange=None, symbol_suffix=None),
    ]
    try:
        holding_repo.upsert_many("valid", holdings)
        assert False, "Expected ValueError for HoldingRepository"
    except ValueError:
        pass
    assert fake_infra.store_calls == [], "HoldingRepository made store calls despite validation failure"

    # TransactionRepository
    fake_infra = FakeInfrastructure()
    transaction_repo = TransactionRepository(fake_infra)
    transactions = [
        Transaction(portfolio_id="valid", kind="BUY", amount=100),
        Transaction(portfolio_id="invalid", kind="BUY", amount=100),
    ]
    try:
        transaction_repo.create_many("valid", transactions)
        assert False, "Expected ValueError for TransactionRepository"
    except ValueError:
        pass
    assert fake_infra.store_calls == [], "TransactionRepository made store calls despite validation failure"


def test_holding_upsert_many_is_idempotent() -> None:
    """Test that HoldingRepository.upsert_many is idempotent (simulated)."""
    # We cannot test true idempotency without a real store that does upserts,
    # but we can test that it calls store for each holding and that the
    # repository's upsert method is called (which we know does upsert via natural key).
    fake_infra = FakeInfrastructure()
    holding_repo = HoldingRepository(fake_infra)
    holdings = [
        Holding(portfolio_id="p1", security_id="s1", quantity=1, currency="USD", exchange=None, symbol_suffix=None),
        Holding(portfolio_id="p1", security_id="s2", quantity=2, currency="USD", exchange=None, symbol_suffix=None),
    ]
    result = holding_repo.upsert_many("p1", holdings)
    assert len(result) == 2
    assert len(fake_infra.store_calls) == 2
    # Check that the store calls are for the holdings table and have the correct data
    for i, (table, row) in enumerate(fake_infra.store_calls):
        assert table == "holdings"
        assert row["portfolio_id"] == "p1"
        assert row["security_id"] == holdings[i].security_id


def test_transaction_create_many_is_append_only() -> None:
    """Test that TransactionRepository.create_many results in a store call per transaction."""
    fake_infra = FakeInfrastructure()
    transaction_repo = TransactionRepository(fake_infra)
    transactions = [
        Transaction(portfolio_id="p1", kind="BUY", amount=100),
        Transaction(portfolio_id="p1", kind="SELL", amount=50),
    ]
    result = transaction_repo.create_many("p1", transactions)
    assert len(result) == 2
    assert len(fake_infra.store_calls) == 2
    # Check that the store calls are for the transactions table and have the correct data
    for i, (table, row) in enumerate(fake_infra.store_calls):
        assert table == "transactions"
        assert row["portfolio_id"] == "p1"
        assert row["kind"] == transactions[i].kind
        assert row["amount"] == transactions[i].amount


def test_no_bulk_methods_on_infrastructure_protocol() -> None:
    """Ensure that no bulk methods were added to the Infrastructure Protocol."""
    from infrastructure import Infrastructure
    # List of method names on the Infrastructure Protocol
    infrastructure_methods = {name for name, _ in Infrastructure.__dict__.items() if not name.startswith('_')}
    # We expect that the two bulk methods we were asked to implement (in repositories) are NOT in the Protocol
    forbidden = {'upsert_many', 'create_many'}
    unexpected = infrastructure_methods & forbidden
    assert not unexpected, f"Infrastructure Protocol has forbidden bulk methods: {unexpected}"


if __name__ == "__main__":
    test_holding_upsert_many_signature_and_comment()
    test_transaction_create_many_signature_and_comment()
    test_validation_occurs_before_writes()
    test_holding_upsert_many_is_idempotent()
    test_transaction_create_many_is_append_only()
    test_no_bulk_methods_on_infrastructure_protocol()
    print("All STORY-9 verification checks passed.")