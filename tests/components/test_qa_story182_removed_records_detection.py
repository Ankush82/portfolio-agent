"""QA tests for STORY-SYNC-05 — removed records detection.

Every test exercises one of the acceptance criteria verbatim against
``find_holdings_to_remove`` and ``find_transactions_to_remove`` with an
in-memory fake repository. No I/O, no network, no environment access.
"""

import pytest

from components.c01_user_portfolio import (
    BrokerTransaction,
    CurrentHolding,
    CurrentTransaction,
    Holding,
    RemovedHoldingOp,
    RemovedTransactionOp,
    Transaction,
    _FakeHoldingRepository,
    _FakeTransactionRepository,
    find_holdings_to_remove,
    find_transactions_to_remove,
)
from datetime import date, datetime, timezone
from decimal import Decimal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _holding(
    broker_holding_id: str,
    *,
    portfolio_id: str = "portfolio-001",
) -> Holding:
    """Minimal Holding factory for these tests."""
    return Holding(
        portfolio_id=portfolio_id,
        security_id="SEC",
        quantity=Decimal("1"),
        broker_holding_id=broker_holding_id,
    )


def _current_holding(
    broker_holding_id: str,
    *,
    is_active: bool = True,
    portfolio_id: str = "portfolio-001",
) -> CurrentHolding:
    """Minimal CurrentHolding factory for these tests."""
    return CurrentHolding(
        id=f"db-holding-{broker_holding_id}",
        is_active=is_active,
        holding=_holding(broker_holding_id, portfolio_id=portfolio_id),
    )


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
    is_active: bool = True,
    updated_at: datetime | None = None,
) -> CurrentTransaction:
    """Minimal CurrentTransaction factory for these tests."""
    return CurrentTransaction(
        id=f"db-tx-{broker_transaction_id}",
        is_active=is_active,
        transaction=Transaction(
            portfolio_id="portfolio-001",
            kind="BUY",
            amount=150.00,
            broker_transaction_id=broker_transaction_id,
        ),
        updated_at=updated_at or _utc(2024, 6, 1),
    )


# ---------------------------------------------------------------------------
# AC: Function accepts set of current broker holding IDs and returns list
# of holdings to mark as 'removed'
# ---------------------------------------------------------------------------

def test_find_holdings_to_remove_returns_list_of_removed_holdings():
    """When a stored holding's broker_holding_id is absent from the
    current broker ID set, that holding must appear in the returned
    list with status 'removed'."""
    repo = _FakeHoldingRepository({
        "isin-existing": _current_holding("isin-existing"),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids=set(),   # broker returned nothing
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 1
    assert ops[0].status == 'removed'
    assert ops[0].current_holding.holding.broker_holding_id == 'isin-existing'


def test_find_holdings_to_remove_accepts_set_type():
    """The function accepts a plain Python set as the current IDs
    argument — not just a frozenset or a list."""
    repo = _FakeHoldingRepository({
        "isin-existing": _current_holding("isin-existing"),
    })

    # ``set()`` is a plain set, not frozenset
    ops = find_holdings_to_remove(
        current_broker_holding_ids={"isin-other"},  # existing holding NOT in this set
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 1
    assert ops[0].status == 'removed'


# ---------------------------------------------------------------------------
# AC: Function accepts set of current broker transaction IDs and returns
# list of transactions to mark as 'removed'
# ---------------------------------------------------------------------------

def test_find_transactions_to_remove_returns_list_of_removed_transactions():
    """When a stored transaction's broker_transaction_id is absent from
    the current broker ID set, that transaction must appear in the
    returned list with status 'removed'."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx("tx-existing"),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids=set(),  # broker returned nothing
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 1
    assert ops[0].status == 'removed'
    assert ops[0].current_transaction.transaction.broker_transaction_id == 'tx-existing'


def test_find_transactions_to_remove_accepts_set_type():
    """The function accepts a plain Python set as the current IDs
    argument — not just a frozenset or a list."""
    repo = _FakeTransactionRepository({
        "tx-existing": _current_tx("tx-existing"),
    })

    # ``set()`` is a plain set, not frozenset
    ops = find_transactions_to_remove(
        current_broker_transaction_ids={"tx-other"},  # existing tx NOT in this set
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 1
    assert ops[0].status == 'removed'


# ---------------------------------------------------------------------------
# AC: Only records matching the synced account_id are considered
# ---------------------------------------------------------------------------

def test_find_holdings_to_remove_scoped_to_account_id():
    """Holdings stored for a different account_id must not be returned
    as removed, even when their broker_holding_id is absent from the
    current broker ID set."""
    repo = _FakeHoldingRepository({
        "isin-same-account": _current_holding(
            "isin-same-account",
            portfolio_id="portfolio-001",
        ),
        "isin-other-account": _current_holding(
            "isin-other-account",
            portfolio_id="portfolio-002",  # different account
        ),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids={"isin-same-account"},  # same-account still present
        repository=repo,
        account_id="portfolio-001",   # only syncing this account
    )

    # Only the same-account holding should be considered; the
    # other-account holding must not appear even though its ID
    # is also absent from the current broker set.
    assert len(ops) == 0


def test_find_transactions_to_remove_scoped_to_account_id():
    """Transactions stored for a different account_id must not be
    returned as removed, even when their broker_transaction_id is
    absent from the current broker ID set."""
    repo = _FakeTransactionRepository({
        "tx-same-account": _current_tx(
            "tx-same-account",
        ),   # portfolio_id defaults to "portfolio-001"
        "tx-other-account": _current_tx(
            "tx-other-account",
        ),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids={"tx-same-account"},  # same-account still present
        repository=repo,
        account_id="portfolio-001",   # only syncing this account
    )

    # Only the same-account transaction should be considered; the
    # other-account transaction must not appear.
    assert len(ops) == 0


# ---------------------------------------------------------------------------
# AC: Only active records are considered
# ---------------------------------------------------------------------------

def test_find_holdings_to_remove_excludes_inactive_records():
    """A holding with ``is_active=False`` must not be returned as
    removed, even when its broker_holding_id is absent from the
    current broker ID set."""
    repo = _FakeHoldingRepository({
        "isin-active": _current_holding("isin-active", is_active=True),
        "isin-inactive": _current_holding("isin-inactive", is_active=False),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids=set(),
        repository=repo,
        account_id="portfolio-001",
    )

    # Only the active record should appear; the inactive one is excluded.
    assert len(ops) == 1
    assert ops[0].current_holding.holding.broker_holding_id == 'isin-active'


def test_find_transactions_to_remove_excludes_inactive_records():
    """A transaction with ``is_active=False`` must not be returned as
    removed, even when its broker_transaction_id is absent from the
    current broker ID set."""
    repo = _FakeTransactionRepository({
        "tx-active": _current_tx("tx-active", is_active=True),
        "tx-inactive": _current_tx("tx-inactive", is_active=False),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids=set(),
        repository=repo,
        account_id="portfolio-001",
    )

    # Only the active record should appear; the inactive one is excluded.
    assert len(ops) == 1
    assert ops[0].current_transaction.transaction.broker_transaction_id == 'tx-active'


# ---------------------------------------------------------------------------
# AC: Unit tests: when broker returns subset, missing records are
# identified as 'removed'
# ---------------------------------------------------------------------------

def test_find_holdings_to_remove_subset_returns_missing_as_removed():
    """When the broker returns a strict subset of previously-stored
    holdings, exactly the missing ones are returned with status
    'removed'."""
    repo = _FakeHoldingRepository({
        "isin-a": _current_holding("isin-a"),
        "isin-b": _current_holding("isin-b"),
        "isin-c": _current_holding("isin-c"),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids={"isin-a", "isin-b"},  # c is missing
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 1
    assert ops[0].status == 'removed'
    assert ops[0].current_holding.holding.broker_holding_id == 'isin-c'


def test_find_transactions_to_remove_subset_returns_missing_as_removed():
    """When the broker returns a strict subset of previously-stored
    transactions, exactly the missing ones are returned with status
    'removed'."""
    repo = _FakeTransactionRepository({
        "tx-001": _current_tx("tx-001"),
        "tx-002": _current_tx("tx-002"),
        "tx-003": _current_tx("tx-003"),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids={"tx-001", "tx-002"},  # tx-003 is missing
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 1
    assert ops[0].status == 'removed'
    assert ops[0].current_transaction.transaction.broker_transaction_id == 'tx-003'


def test_find_holdings_to_remove_no_changes_returns_empty():
    """When the broker returns the same holdings as stored (complete
    superset), nothing is returned as removed."""
    repo = _FakeHoldingRepository({
        "isin-a": _current_holding("isin-a"),
        "isin-b": _current_holding("isin-b"),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids={"isin-a", "isin-b"},
        repository=repo,
        account_id="portfolio-001",
    )

    assert ops == []


def test_find_transactions_to_remove_no_changes_returns_empty():
    """When the broker returns the same transactions as stored
    (complete superset), nothing is returned as removed."""
    repo = _FakeTransactionRepository({
        "tx-001": _current_tx("tx-001"),
        "tx-002": _current_tx("tx-002"),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids={"tx-001", "tx-002"},
        repository=repo,
        account_id="portfolio-001",
    )

    assert ops == []


def test_find_holdings_to_remove_empty_broker_returns_all_active_as_removed():
    """When the broker returns an empty set (e.g. account disconnected
    or all positions closed), every active stored holding for that
    account is returned as removed."""
    repo = _FakeHoldingRepository({
        "isin-x": _current_holding("isin-x"),
        "isin-y": _current_holding("isin-y"),
        "isin-inactive": _current_holding("isin-inactive", is_active=False),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids=set(),
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 2
    removed_ids = {op.current_holding.holding.broker_holding_id for op in ops}
    assert removed_ids == {"isin-x", "isin-y"}
    assert "isin-inactive" not in removed_ids


def test_find_transactions_to_remove_empty_broker_returns_all_active_as_removed():
    """When the broker returns an empty set (e.g. date range with no
    transactions), every active stored transaction for that account
    is returned as removed."""
    repo = _FakeTransactionRepository({
        "tx-a": _current_tx("tx-a"),
        "tx-b": _current_tx("tx-b"),
        "tx-inactive": _current_tx("tx-inactive", is_active=False),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids=set(),
        repository=repo,
        account_id="portfolio-001",
    )

    assert len(ops) == 2
    removed_ids = {op.current_transaction.transaction.broker_transaction_id for op in ops}
    assert removed_ids == {"tx-a", "tx-b"}
    assert "tx-inactive" not in removed_ids


# ---------------------------------------------------------------------------
# AC: RemovedHoldingOp / RemovedTransactionOp are frozen/immutable
# ---------------------------------------------------------------------------

def test_removed_holding_op_is_frozen():
    """RemovedHoldingOp must be immutable (frozen=True)."""
    op = RemovedHoldingOp(
        current_holding=_current_holding("isin-test"),
    )
    with pytest.raises(AttributeError):
        op.status = 'updated'  # type: ignore[assignment]


def test_removed_transaction_op_is_frozen():
    """RemovedTransactionOp must be immutable (frozen=True)."""
    op = RemovedTransactionOp(
        current_transaction=_current_tx("tx-test"),
    )
    with pytest.raises(AttributeError):
        op.status = 'updated'  # type: ignore[assignment]


def test_removed_holding_op_status_is_always_removed():
    """A RemovedHoldingOp's status field is always 'removed' — it is
    a frozen dataclass and the field is defaulted to 'removed'."""
    op = RemovedHoldingOp(current_holding=_current_holding("isin-test"))
    assert op.status == 'removed'


def test_removed_transaction_op_status_is_always_removed():
    """A RemovedTransactionOp's status field is always 'removed' — it
    is a frozen dataclass and the field is defaulted to 'removed'."""
    op = RemovedTransactionOp(current_transaction=_current_tx("tx-test"))
    assert op.status == 'removed'


# ---------------------------------------------------------------------------
# Pre-existing bugs that dev claims to have fixed:
#   1. Transaction had a duplicate @dataclass decorator (could not instantiate)
#   2. CurrentTransaction was missing @dataclass (could not instantiate)
#   3. _FakeTransactionRepository.find_active_by_account_id did not filter by
#      portfolio_id (tested above via account_id scoping tests)
# ---------------------------------------------------------------------------

def test_transaction_and_current_transaction_are_instantiable():
    """STORY-SYNC-05 bug-fix: Transaction and CurrentTransaction must be
    proper dataclasses with a real __init__, not bare classes with only
    type annotations. A bare class (no @dataclass) has no __init__ and
    raises TypeError on construction — a real regression caught by this test."""
    import datetime

    # (1) Transaction must be instantiable with positional args
    tx = Transaction(
        portfolio_id="portfolio-001",
        kind="BUY",
        amount=250.0,
        broker_transaction_id="tx-fix-test",
    )
    assert tx.portfolio_id == "portfolio-001"
    assert tx.kind == "BUY"
    assert tx.amount == 250.0
    assert tx.broker_transaction_id == "tx-fix-test"

    # (2) CurrentTransaction must also be instantiable with positional args
    now = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)
    ct = CurrentTransaction(
        id="db-fix-test",
        is_active=True,
        transaction=tx,
        updated_at=now,
    )
    assert ct.id == "db-fix-test"
    assert ct.is_active is True
    assert ct.transaction is tx
    assert ct.updated_at == now

    # (3) Transaction broker_transaction_id may be None (manual entry)
    tx_manual = Transaction(
        portfolio_id="portfolio-001",
        kind="BUY",
        amount=100.0,
        broker_transaction_id=None,
    )
    assert tx_manual.broker_transaction_id is None


# ---------------------------------------------------------------------------
# AC: Only records matching the synced account_id are considered
# (explicit different-account fixture — the other test used the same
# portfolio_id for both because _current_tx defaults portfolio_id="portfolio-001"
# for every call, making its comment inaccurate. This test explicitly passes
# a different portfolio_id so the account-scoping logic is genuinely exercised.)
# ---------------------------------------------------------------------------

def test_find_transactions_to_remove_respects_account_id_with_explicit_different_portfolio():
    """Transactions stored for a different portfolio_id must not be returned
    as removed when syncing a specific account — not just those with a
    different broker ID. The acceptance criterion requires that ONLY records
    matching account_id (portfolio_id) are considered.

    Setup:
      - tx-in-scope:   portfolio-001, in broker set     → NOT removed
      - tx-removed:    portfolio-001, NOT in broker set → IS removed (correct)
      - tx-other-acct: portfolio-999, NOT in broker set → filtered out (wrong account)
    """
    repo = _FakeTransactionRepository({
        "tx-in-scope": _current_tx("tx-in-scope"),  # portfolio_id="portfolio-001"; in broker set
        "tx-removed": _current_tx("tx-removed"),     # portfolio_id="portfolio-001"; NOT in broker set → removed
        "tx-other-acct": CurrentTransaction(
            id="db-tx-out",
            is_active=True,
            transaction=Transaction(
                portfolio_id="portfolio-999",  # ← explicitly different account
                kind="BUY",
                amount=100.0,
                broker_transaction_id="tx-other-acct",
            ),
            updated_at=_utc(2024, 6, 1),
        ),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids={"tx-in-scope"},  # broker only knows about tx-in-scope
        repository=repo,
        account_id="portfolio-001",  # only syncing this account
    )

    # tx-removed is from portfolio-001 and IS missing from the broker set → correctly removed.
    # tx-other-acct is from portfolio-999 → must NOT appear even though it's also missing.
    assert len(ops) == 1
    assert ops[0].current_transaction.transaction.broker_transaction_id == "tx-removed"
    # Verify the removed record really belongs to the synced account.
    assert ops[0].current_transaction.transaction.portfolio_id == "portfolio-001"


# ---------------------------------------------------------------------------
# Edge case: holding/transaction with None broker ID is not removed
# ---------------------------------------------------------------------------

def test_find_holdings_to_remove_skips_null_broker_holding_id():
    """A stored holding with ``broker_holding_id=None`` (e.g. a manually
    entered holding) is never returned as removed, because there is no
    broker ID to compare against."""
    repo = _FakeHoldingRepository({
        "isin-real": _current_holding("isin-real"),
        "isin-null": CurrentHolding(
            id="db-null",
            is_active=True,
            holding=Holding(
                portfolio_id="portfolio-001",
                security_id="MANUAL",
                quantity=Decimal("5"),
                broker_holding_id=None,  # manually entered, no broker ID
            ),
        ),
    })

    ops = find_holdings_to_remove(
        current_broker_holding_ids=set(),
        repository=repo,
        account_id="portfolio-001",
    )

    # Only the real holding should be returned; the null-ID one is skipped.
    assert len(ops) == 1
    assert ops[0].current_holding.holding.broker_holding_id == 'isin-real'


def test_find_transactions_to_remove_skips_null_broker_transaction_id():
    """A stored transaction with ``broker_transaction_id=None`` (e.g. a
    manually entered transaction) is never returned as removed,
    because there is no broker ID to compare against."""
    repo = _FakeTransactionRepository({
        "tx-real": _current_tx("tx-real"),
        "tx-null": CurrentTransaction(
            id="db-null-tx",
            is_active=True,
            transaction=Transaction(
                portfolio_id="portfolio-001",
                kind="BUY",
                amount=100.0,
                broker_transaction_id=None,  # manually entered, no broker ID
            ),
            updated_at=_utc(2024, 6, 1),
        ),
    })

    ops = find_transactions_to_remove(
        current_broker_transaction_ids=set(),
        repository=repo,
        account_id="portfolio-001",
    )

    # Only the real transaction should be returned; the null-ID one is skipped.
    assert len(ops) == 1
    assert ops[0].current_transaction.transaction.broker_transaction_id == 'tx-real'
