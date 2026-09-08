"""Tests for reconcile_transactions and ReconciliationOp (STORY-SYNC-04).

Tests cover all three reconciliation scenarios (added / updated / unchanged)
with mock broker data and an in-memory fake repository. No I/O, no network,
no environment access.
"""

import pytest

from components.c01_user_portfolio import (
    BrokerTransaction,
    CurrentTransaction,
    ReconciliationOp,
    Transaction,
    _FakeTransactionRepository,
    reconcile_transactions,
)
from datetime import date, datetime, timezone
from decimal import Decimal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _broker_tx(
    broker_transaction_id: str,
    *,
    broker_modified_at: datetime,
) -> BrokerTransaction:
    """Minimal BrokerTransaction factory for these tests."""
    return BrokerTransaction(
        broker_transaction_id=broker_transaction_id,
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 1, 15),
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("150.00"),
        amount=Decimal("150.00"),
        exchange="NASDAQ",
        segment="EQ",
        broker_modified_at=broker_modified_at,
        raw={},
    )


def _current_tx(
    broker_transaction_id: str,
    *,
    updated_at: datetime,
) -> CurrentTransaction:
    """Minimal CurrentTransaction factory for these tests."""
    return CurrentTransaction(
        id=f"db-{broker_transaction_id}",
        is_active=True,
        transaction=Transaction(
            portfolio_id="portfolio-001",
            kind="BUY",
            amount=150.00,
            broker_transaction_id=broker_transaction_id,
        ),
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Scenario: broker_transaction_id NOT found → 'added'
# ---------------------------------------------------------------------------

def test_reconcile_added_when_not_in_repository():
    """A broker transaction whose broker_transaction_id has no stored
    record must be marked 'added'."""
    repo = _FakeTransactionRepository({})
    broker_tx = _broker_tx("tx-new", broker_modified_at=_utc(2024, 6, 1))

    ops = reconcile_transactions([broker_tx], repo)

    assert len(ops) == 1
    assert ops[0].status == 'added'
    assert ops[0].transaction.broker_transaction_id == 'tx-new'


def test_reconcile_added_with_multiple_new_transactions():
    """Multiple new broker transactions all get 'added'."""
    repo = _FakeTransactionRepository({})
    broker_txs = [
        _broker_tx("tx-new-1", broker_modified_at=_utc(2024, 6, 1)),
        _broker_tx("tx-new-2", broker_modified_at=_utc(2024, 6, 2)),
        _broker_tx("tx-new-3", broker_modified_at=_utc(2024, 6, 3)),
    ]

    ops = reconcile_transactions(broker_txs, repo)

    assert len(ops) == 3
    assert all(op.status == 'added' for op in ops)


def test_reconcile_added_preserves_input_order():
    """Output order matches input order even when all are 'added'."""
    repo = _FakeTransactionRepository({})
    broker_txs = [
        _broker_tx("tx-c", broker_modified_at=_utc(2024, 6, 1)),
        _broker_tx("tx-a", broker_modified_at=_utc(2024, 6, 2)),
        _broker_tx("tx-b", broker_modified_at=_utc(2024, 6, 3)),
    ]

    ops = reconcile_transactions(broker_txs, repo)

    ids = [op.transaction.broker_transaction_id for op in ops]
    assert ids == ["tx-c", "tx-a", "tx-b"]


# ---------------------------------------------------------------------------
# Scenario: broker_transaction_id found, broker_modified_at > updated_at → 'updated'
# ---------------------------------------------------------------------------

def test_reconcile_updated_when_broker_is_newer():
    """When a matching record exists but broker_modified_at is strictly
    later than the stored updated_at, the result is 'updated'."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx(
            "tx-existing",
            updated_at=_utc(2024, 5, 1),
        ),
    })
    broker_tx = _broker_tx("tx-existing", broker_modified_at=_utc(2024, 6, 1))

    ops = reconcile_transactions([broker_tx], repo)

    assert len(ops) == 1
    assert ops[0].status == 'updated'
    assert ops[0].transaction.broker_transaction_id == 'tx-existing'


def test_reconcile_updated_only_marginally_newer():
    """A broker timestamp one second later than stored is still 'updated'."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx(
            "tx-existing",
            updated_at=_utc(2024, 6, 1, 12, 0, 0),
        ),
    })
    broker_tx = _broker_tx("tx-existing", broker_modified_at=_utc(2024, 6, 1, 12, 0, 1))

    ops = reconcile_transactions([broker_tx], repo)

    assert ops[0].status == 'updated'


# ---------------------------------------------------------------------------
# Scenario: broker_transaction_id found, broker_modified_at <= updated_at → 'unchanged'
# ---------------------------------------------------------------------------

def test_reconcile_unchanged_when_broker_is_older():
    """When a matching record exists and broker_modified_at is strictly
    earlier than updated_at, the result is 'unchanged'."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx(
            "tx-existing",
            updated_at=_utc(2024, 6, 1),
        ),
    })
    broker_tx = _broker_tx("tx-existing", broker_modified_at=_utc(2024, 5, 1))

    ops = reconcile_transactions([broker_tx], repo)

    assert len(ops) == 1
    assert ops[0].status == 'unchanged'
    assert ops[0].transaction.broker_transaction_id == 'tx-existing'


def test_reconcile_unchanged_when_timestamps_are_equal():
    """Identical timestamps (broker_modified_at == updated_at) means
    'unchanged' — only strictly greater triggers an update."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx(
            "tx-existing",
            updated_at=_utc(2024, 6, 1, 12, 0, 0),
        ),
    })
    broker_tx = _broker_tx("tx-existing", broker_modified_at=_utc(2024, 6, 1, 12, 0, 0))

    ops = reconcile_transactions([broker_tx], repo)

    assert ops[0].status == 'unchanged'


# ---------------------------------------------------------------------------
# Mixed scenarios: one list containing all three statuses
# ---------------------------------------------------------------------------

def test_reconcile_mixed_all_three_statuses_in_one_call():
    """A single call with transactions that cover all three statuses
    returns the correct status for each one."""
    repo = _FakeTransactionRepository({
        "tx-updated": _current_tx(
            "tx-updated",
            updated_at=_utc(2024, 5, 1),
        ),
        "tx-unchanged": _current_tx(
            "tx-unchanged",
            updated_at=_utc(2024, 7, 1),
        ),
    })
    broker_txs = [
        _broker_tx("tx-new",      broker_modified_at=_utc(2024, 6, 1)),  # added
        _broker_tx("tx-updated",  broker_modified_at=_utc(2024, 6, 2)),  # updated
        _broker_tx("tx-unchanged", broker_modified_at=_utc(2024, 6, 3)),  # unchanged
    ]

    ops = reconcile_transactions(broker_txs, repo)

    assert len(ops) == 3
    assert {op.transaction.broker_transaction_id: op.status for op in ops} == {
        "tx-new":       "added",
        "tx-updated":   "updated",
        "tx-unchanged": "unchanged",
    }


def test_reconcile_empty_broker_list_returns_empty():
    """Passing an empty broker_transactions list returns an empty list."""
    repo = _FakeTransactionRepository({})

    ops = reconcile_transactions([], repo)

    assert ops == []


# ---------------------------------------------------------------------------
# ReconciliationOp dataclass invariants
# ---------------------------------------------------------------------------

def test_reconciliation_op_is_frozen():
    """ReconciliationOp must be immutable (frozen=True)."""
    op = ReconciliationOp(
        transaction=_broker_tx("tx-1", broker_modified_at=_utc(2024, 6, 1)),
        status='added',
    )
    with pytest.raises(AttributeError):
        op.status = 'updated'  # type: ignore[assignment]


def test_reconciliation_op_transaction_preserved():
    """The transaction returned in the op must be the same object
    that was passed in (identity, not a copy)."""
    broker_tx = _broker_tx("tx-1", broker_modified_at=_utc(2024, 6, 1))
    repo = _FakeTransactionRepository({})

    ops = reconcile_transactions([broker_tx], repo)

    assert ops[0].transaction is broker_tx


# ---------------------------------------------------------------------------
# Input type: broker_transactions must be iterable
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Diagnostic: confirm CurrentTransaction is a proper dataclass (FAILS until fixed)
# ---------------------------------------------------------------------------

def test_current_transaction_is_a_dataclass():
    """Confirm CurrentTransaction can be instantiated with keyword arguments
    as a proper dataclass. This is a prerequisite for all other tests."""
    import inspect
    from dataclasses import fields as dc_fields, is_dataclass
    from components.c01_user_portfolio import CurrentTransaction
    assert is_dataclass(CurrentTransaction), (
        f"CurrentTransaction must be a dataclass but is {type(CurrentTransaction)}"
    )
    # Verify all expected fields exist
    field_names = {f.name for f in dc_fields(CurrentTransaction)}
    expected = {"id", "is_active", "transaction", "updated_at"}
    assert expected.issubset(field_names), (
        f"CurrentTransaction fields {field_names} missing expected {expected}"
    )
    # Verify it can be instantiated
    tx = CurrentTransaction(
        id="test-001",
        is_active=True,
        transaction=Transaction(
            portfolio_id="p-001",
            kind="BUY",
            amount=100.0,
            broker_transaction_id="btx-001",
        ),
        updated_at=_utc(2024, 6, 1),
    )
    assert tx.id == "test-001"
    assert tx.is_active is True


def test_reconcile_accepts_generator():
    """reconcile_transactions accepts a generator/iterator as input,
    not just a concrete list."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx("tx-existing", updated_at=_utc(2024, 5, 1)),
    })

    def tx_gen():
        yield _broker_tx("tx-new",  broker_modified_at=_utc(2024, 6, 1))
        yield _broker_tx("tx-existing", broker_modified_at=_utc(2024, 6, 2))

    ops = reconcile_transactions(tx_gen(), repo)

    assert [op.status for op in ops] == ["added", "updated"]
