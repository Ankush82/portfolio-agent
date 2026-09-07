"""Tests for reconcile_holdings and HoldingReconciliationOp (STORY-SYNC-03).

Tests cover all four reconciliation scenarios (added / updated /
unchanged / reactivated-inactive) with mock broker data and the existing
in-memory fake repository. No I/O, no network, no environment access.
"""

import pytest
from decimal import Decimal

from components.c01_user_portfolio import (
    BrokerHolding,
    CurrentHolding,
    Holding,
    HoldingReconciliationOp,
    _FakeHoldingRepository,
    reconcile_holdings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _broker_holding(
    isin: str,
    *,
    symbol: str = "RELIANCE",
    quantity: Decimal = Decimal("10"),
) -> BrokerHolding:
    """Minimal BrokerHolding factory for these tests."""
    return BrokerHolding(
        symbol=symbol,
        isin=isin,
        quantity=quantity,
        average_price=Decimal("2400.00"),
        last_price=Decimal("2500.00"),
        exchange="NSE",
    )


def _current_holding(
    isin: str,
    *,
    is_active: bool = True,
    symbol: str = "RELIANCE",
    quantity: Decimal = Decimal("10"),
) -> CurrentHolding:
    """Minimal CurrentHolding factory for these tests."""
    return CurrentHolding(
        id=f"db-{isin}",
        is_active=is_active,
        holding=Holding(
            portfolio_id="portfolio-001",
            security_id=symbol,
            quantity=quantity,
            broker_holding_id=isin,
        ),
    )


# ---------------------------------------------------------------------------
# Scenario: isin NOT found -> 'added'
# ---------------------------------------------------------------------------

def test_reconcile_added_when_not_in_repository():
    """A broker holding whose isin has no stored record must be marked 'added'."""
    repo = _FakeHoldingRepository({})
    broker_holding = _broker_holding("INE002A01018")

    ops = reconcile_holdings([broker_holding], repo)

    assert len(ops) == 1
    assert ops[0].status == 'added'
    assert ops[0].holding.isin == 'INE002A01018'


def test_reconcile_added_with_multiple_new_holdings():
    """Multiple new broker holdings all get 'added'."""
    repo = _FakeHoldingRepository({})
    broker_holdings = [
        _broker_holding("ISIN-1"),
        _broker_holding("ISIN-2"),
        _broker_holding("ISIN-3"),
    ]

    ops = reconcile_holdings(broker_holdings, repo)

    assert len(ops) == 3
    assert all(op.status == 'added' for op in ops)


def test_reconcile_added_preserves_input_order():
    """Output order matches input order even when all are 'added'."""
    repo = _FakeHoldingRepository({})
    broker_holdings = [
        _broker_holding("ISIN-C"),
        _broker_holding("ISIN-A"),
        _broker_holding("ISIN-B"),
    ]

    ops = reconcile_holdings(broker_holdings, repo)

    assert [op.holding.isin for op in ops] == ["ISIN-C", "ISIN-A", "ISIN-B"]


# ---------------------------------------------------------------------------
# Scenario: isin found, existing record active with different values -> 'updated'
# ---------------------------------------------------------------------------

def test_reconcile_updated_when_quantity_differs():
    """When a matching active record exists but quantity differs, the
    result is 'updated'."""
    repo = _FakeHoldingRepository({
        "INE002A01018": _current_holding("INE002A01018", quantity=Decimal("10")),
    })
    broker_holding = _broker_holding("INE002A01018", quantity=Decimal("15"))

    ops = reconcile_holdings([broker_holding], repo)

    assert len(ops) == 1
    assert ops[0].status == 'updated'


def test_reconcile_updated_when_symbol_differs():
    """When a matching active record exists but symbol differs, the
    result is 'updated'."""
    repo = _FakeHoldingRepository({
        "INE002A01018": _current_holding("INE002A01018", symbol="RELIANCE"),
    })
    broker_holding = _broker_holding("INE002A01018", symbol="RELIANCE-NEW")

    ops = reconcile_holdings([broker_holding], repo)

    assert ops[0].status == 'updated'


# ---------------------------------------------------------------------------
# Scenario: isin found, existing record INACTIVE -> reactivated / 'updated'
# ---------------------------------------------------------------------------

def test_reconcile_updated_when_existing_is_inactive():
    """A matching but inactive stored record is reactivated -- marked
    'updated' regardless of whether its field values also match."""
    repo = _FakeHoldingRepository({
        "INE002A01018": _current_holding("INE002A01018", is_active=False, quantity=Decimal("10")),
    })
    broker_holding = _broker_holding("INE002A01018", quantity=Decimal("10"))  # same values, still inactive

    ops = reconcile_holdings([broker_holding], repo)

    assert ops[0].status == 'updated'


# ---------------------------------------------------------------------------
# Scenario: isin found, existing record active with identical values -> 'unchanged'
# ---------------------------------------------------------------------------

def test_reconcile_unchanged_when_values_match():
    """A matching, active stored record with identical quantity and
    symbol is 'unchanged'."""
    repo = _FakeHoldingRepository({
        "INE002A01018": _current_holding("INE002A01018", symbol="RELIANCE", quantity=Decimal("10")),
    })
    broker_holding = _broker_holding("INE002A01018", symbol="RELIANCE", quantity=Decimal("10"))

    ops = reconcile_holdings([broker_holding], repo)

    assert len(ops) == 1
    assert ops[0].status == 'unchanged'


# ---------------------------------------------------------------------------
# Mixed scenarios: one list covering all statuses
# ---------------------------------------------------------------------------

def test_reconcile_mixed_all_statuses_in_one_call():
    """A single call with holdings that cover added/updated/unchanged/
    reactivated all returns the correct status for each one."""
    repo = _FakeHoldingRepository({
        "ISIN-UPDATED": _current_holding("ISIN-UPDATED", quantity=Decimal("5")),
        "ISIN-UNCHANGED": _current_holding("ISIN-UNCHANGED", quantity=Decimal("20")),
        "ISIN-REACTIVATE": _current_holding("ISIN-REACTIVATE", is_active=False, quantity=Decimal("1")),
    })
    broker_holdings = [
        _broker_holding("ISIN-NEW", quantity=Decimal("3")),                   # added
        _broker_holding("ISIN-UPDATED", quantity=Decimal("7")),               # updated
        _broker_holding("ISIN-UNCHANGED", quantity=Decimal("20")),            # unchanged
        _broker_holding("ISIN-REACTIVATE", quantity=Decimal("1")),            # reactivated/updated
    ]

    ops = reconcile_holdings(broker_holdings, repo)

    assert {op.holding.isin: op.status for op in ops} == {
        "ISIN-NEW": "added",
        "ISIN-UPDATED": "updated",
        "ISIN-UNCHANGED": "unchanged",
        "ISIN-REACTIVATE": "updated",
    }


def test_reconcile_empty_broker_list_returns_empty():
    """Passing an empty broker_holdings list returns an empty list."""
    repo = _FakeHoldingRepository({})

    ops = reconcile_holdings([], repo)

    assert ops == []


# ---------------------------------------------------------------------------
# HoldingReconciliationOp dataclass invariants
# ---------------------------------------------------------------------------

def test_holding_reconciliation_op_is_frozen():
    """HoldingReconciliationOp must be immutable (frozen=True)."""
    op = HoldingReconciliationOp(holding=_broker_holding("ISIN-1"), status='added')
    with pytest.raises(AttributeError):
        op.status = 'updated'  # type: ignore[assignment]


def test_holding_reconciliation_op_holding_preserved():
    """The holding returned in the op must be the same object that was
    passed in (identity, not a copy)."""
    broker_holding = _broker_holding("ISIN-1")
    repo = _FakeHoldingRepository({})

    ops = reconcile_holdings([broker_holding], repo)

    assert ops[0].holding is broker_holding


def test_reconcile_updated_inactive_with_different_values():
    """When a matching inactive record exists but with different values, the
    result is 'updated' (reactivated and updated)."""
    repo = _FakeHoldingRepository({
        "INE002A01018": _current_holding("INE002A01018", is_active=False, quantity=Decimal("10"), symbol="RELIANCE"),
    })
    broker_holding = _broker_holding("INE002A01018", quantity=Decimal("15"), symbol="RELIANCE-NEW")

    ops = reconcile_holdings([broker_holding], repo)

    assert len(ops) == 1
    assert ops[0].status == 'updated'
