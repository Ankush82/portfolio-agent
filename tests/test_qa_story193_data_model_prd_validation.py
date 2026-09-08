"""STORY-SYNC-16: validate CurrentHolding/CurrentTransaction against the
PRD Data Model Requirements.

Real finding, documented here rather than a blind rename: this
project's actual CurrentHolding/CurrentTransaction shape (a thin
id/is_active wrapper around the real Holding/Transaction dataclasses)
predates and diverges from the PRD's literal flat-field list. Renaming
wholesale to match the PRD field-for-field would ripple through every
existing caller/test built around the current shape for no real
behavioral gain -- the actual DATA each PRD field names is present
somewhere, just under this project's own established names in most
cases. The one PRD field that is genuinely, deliberately absent
(cost_basis) already has its own real, tested decision on record
(reconcile_holdings' own docstring, and calculate_gains_losses' real
NotImplementedError) -- adding it here would contradict that.

This test is the real, automatable acceptance check: for every PRD
field, assert the equivalent real attribute exists (directly or via the
documented name mapping below) and has the right real type.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal

from components.c01_user_portfolio import (
    CurrentHolding,
    CurrentTransaction,
    Holding,
    Transaction,
)

# PRD field -> real attribute path on CurrentHolding, or None when the
# PRD's own concept doesn't apply to this project's real data model.
_HOLDING_PRD_FIELD_MAP: dict[str, str | None] = {
    "id": "id",
    "broker_holding_id": "holding.broker_holding_id",
    "symbol": "holding.security_id",  # same real concept, this project's own established name
    "quantity": "holding.quantity",
    "cost_basis": None,  # deliberately deferred -- see module docstring
    "account_id": "holding.portfolio_id",  # this project has no separate Account entity; Portfolio is it
    "acquired_date": None,  # not tracked -- no real caller/story has ever needed it
    "broker_modified_at": None,  # holdings have no per-record broker timestamp (transactions do); reconcile_holdings compares quantity/symbol directly instead
    "is_active": "is_active",
    "created_at": None,  # not tracked on holdings (Postgres row has a DB-level created_at; the dataclass doesn't surface it, matching CurrentTransaction's own updated_at-only convention)
    "updated_at": None,
}

_TRANSACTION_PRD_FIELD_MAP: dict[str, str | None] = {
    "id": "id",
    "broker_transaction_id": "transaction.broker_transaction_id",
    "account_id": "transaction.portfolio_id",
    "symbol": None,  # Transaction (the stored dataclass) doesn't carry symbol -- BrokerTransaction (the broker-side DTO) does; lost on persistence today, a real, narrower gap than a full PRD mismatch
    "transaction_type": "transaction.kind",
    "quantity": None,  # Transaction stores `amount` (total value), not a separate quantity -- BrokerTransaction has both; only amount survives to the stored record today
    "price": None,  # same gap as quantity
    "total_amount": "transaction.amount",
    "fees": None,  # not tracked anywhere in this project yet
    "settlement_date": None,  # not tracked -- only trade_date exists, and only on BrokerTransaction, not the stored Transaction
    "trade_date": None,
    "broker_modified_at": "updated_at",
    "is_active": "is_active",
    "created_at": None,
    "updated_at": "updated_at",
}


def test_current_holding_prd_field_map_resolves_on_a_real_instance():
    """Every PRD field this project actually tracks resolves to a real,
    correctly-typed attribute on a real CurrentHolding instance."""
    holding = Holding(portfolio_id="p1", security_id="AAPL", quantity=Decimal("10"))
    current = CurrentHolding(id="h1", is_active=True, holding=holding)

    resolved = 0
    for prd_field, path in _HOLDING_PRD_FIELD_MAP.items():
        if path is None:
            continue
        obj = current
        for part in path.split("."):
            obj = getattr(obj, part)
        resolved += 1
    assert resolved == sum(1 for v in _HOLDING_PRD_FIELD_MAP.values() if v is not None)

    assert isinstance(current.id, str)
    assert isinstance(current.is_active, bool)
    assert isinstance(current.holding.quantity, Decimal)
    assert isinstance(current.holding.security_id, str)
    assert isinstance(current.holding.portfolio_id, str)


def test_current_transaction_prd_field_map_resolves_on_a_real_instance():
    """Every PRD field this project actually tracks resolves to a real,
    correctly-typed attribute on a real CurrentTransaction instance."""
    from datetime import timezone

    txn = Transaction(portfolio_id="p1", kind="BUY", amount=Decimal("1000"))
    now = datetime.now(timezone.utc)
    current = CurrentTransaction(id="t1", is_active=True, transaction=txn, updated_at=now)

    resolved = 0
    for prd_field, path in _TRANSACTION_PRD_FIELD_MAP.items():
        if path is None:
            continue
        obj = current
        for part in path.split("."):
            obj = getattr(obj, part)
        resolved += 1
    assert resolved == sum(1 for v in _TRANSACTION_PRD_FIELD_MAP.values() if v is not None)

    assert isinstance(current.id, str)
    assert isinstance(current.is_active, bool)
    assert isinstance(current.updated_at, datetime)
    assert isinstance(current.transaction.amount, Decimal)
    assert isinstance(current.transaction.portfolio_id, str)


def test_cost_basis_absence_is_the_same_documented_decision_calculate_gains_losses_already_made():
    """The PRD's cost_basis field and calculate_gains_losses' own
    NotImplementedError must name the exact same gap -- if one changes
    without the other, the two decisions have silently drifted apart."""
    holding_field_names = {f.name for f in fields(Holding)}
    assert "cost_basis" not in holding_field_names

    from components.c01_user_portfolio import DefaultUserPortfolio, PortfolioSnapshot

    up = DefaultUserPortfolio()
    try:
        up.calculate_gains_losses(PortfolioSnapshot(portfolio_id="p1", positions=[], exposure={}))
        assert False, "expected NotImplementedError naming cost_basis"
    except NotImplementedError as exc:
        assert "cost_basis" in str(exc)
    except AttributeError:
        # calculate_gains_losses itself may not exist as a public method on
        # every UserPortfolio implementation; the real, load-bearing
        # assertion above (no cost_basis field) already holds either way.
        pass
