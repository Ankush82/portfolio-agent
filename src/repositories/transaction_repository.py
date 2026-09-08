"""Typed CRUD repository for the :class:`Transaction` domain entity.

Talks to storage exclusively through the injected
``Infrastructure`` Protocol — never through a concrete backend, and
never by emitting SQL. The Protocol's four data methods (``store``,
``retrieve``, ``query``, ``delete``) are the only boundary this
repository crosses.

Design notes — read these before extending this repository:

* **Constructor injection.** ``__init__`` takes a single
  ``Infrastructure``-typed argument. The repository holds it as an
  instance attribute (``self._infrastructure``) but adds nothing
  else — no module-level caches, no connection handling, no
  use of ``cache_get``/``cache_set``. All persistence goes through
  the injected instance.

* **Sync methods (matching the Protocol).** ``store`` /
  ``retrieve`` / ``query`` / ``delete`` are all synchronous on the
  ``Infrastructure`` Protocol (see ``docs/repo-layer-recon.md`` V1),
  so the repository methods are also synchronous. There is no
  ``async`` anywhere in this file.

* **Id handling.** ``create`` generates a uuid4 string id when the
  caller did not supply one (``str(uuid.uuid4())``), and returns
  the persisted entity so the caller sees the id that was actually
  stored.

* **Update semantics.** ``update`` exists (STORY-8's own AC requires
  it) but takes an explicit ``transaction_id`` alongside the entity --
  ``Transaction`` has no id field (an explicit AC constraint) and no
  natural identity either (two genuinely distinct trades can be
  field-identical, the same reason this repository has no upsert/
  dedupe), so a bare ``update(transaction)`` could never know which
  stored row to target. A caller must independently track the opaque
  id a row was actually stored under. This is NOT the "correct a
  transaction by editing it" operation a mutable entity would offer --
  correcting a mistake is still a new reversing transaction via
  ``create``; ``update`` here only replaces a specific, already-known
  row's field values in place, for the same structural-uniformity
  reason ``get_by_id``/``delete`` exist.

* **Row mapping.** ``_to_row`` emits exactly the keys the
  ``Transaction`` dataclass owns plus a synthetic ``id``; ``_from_row``
  ignores unknown keys (so DB-managed ``created_at`` / ``updated_at``
  never reach the dataclass constructor) and coerces values to the
  dataclass's declared annotation types.

Explicit non-goals for this repository:

* No SQL. No imports of ``psycopg``, ``redis``, ``sqlalchemy``, or
  ``src.infrastructure_postgres``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence, get_type_hints
import uuid
from decimal import Decimal
from enum import Enum

from domain import TRANSACTIONS_TABLE, Transaction
from infrastructure import Infrastructure
from repositories.base import BaseRepository


class TransactionRepository(BaseRepository):
    """Typed CRUD over the ``transactions`` table via the ``Infrastructure``
    Protocol.

    CRITICAL, deliberate design constraint: transactions are
    APPEND-ONLY. ``Transaction(portfolio_id, kind, amount)`` carries no
    broker reference, no timestamp, and no natural identity -- two
    genuinely distinct trades can be field-identical. There is NO
    correct dedupe key. This repository never implements upsert,
    dedupe, or field-hashing behavior, and never gains a
    ``broker_txn_id``-style column -- inventing an identity where none
    exists would silently drop real, distinct rows a caller genuinely
    meant to keep. ``create``/``create_many`` always insert a new row,
    every time, by design.

    Stateless apart from the injected infrastructure; every method
    goes through ``self._infrastructure.store`` / ``retrieve`` /
    ``query`` / ``delete``. No module-level caches, no connection
    handling, no use of ``cache_get`` / ``cache_set``.
    """

    def __init__(self, infrastructure: Infrastructure) -> None:
        super().__init__(infrastructure, TRANSACTIONS_TABLE)

    # ---- CRUD ------------------------------------------------------------

    def create(self, transaction: Transaction) -> Transaction:
        """Insert a new transaction row.

        If ``transaction.id`` is empty / unset, a fresh uuid4 string is
        generated and used as the row id. Returns the persisted
        entity — with the id that was actually stored, so the
        caller does not have to look it up again.
        """
        return super().create(transaction)

    def update(self, transaction_id: str, transaction: Transaction) -> Transaction:
        """Replace an already-known row's field values in place.

        Takes an explicit ``transaction_id`` (see the class docstring
        above and this module's own design-notes docstring for why: a
        bare ``Transaction`` carries no id and no natural identity, so
        it alone can never say which stored row to target). Raises
        ``KeyError`` if no row exists at ``transaction_id``."""
        existing = self._infrastructure.retrieve(self._table, transaction_id)
        if existing is None:
            raise KeyError(transaction_id)
        row = self._to_row(transaction)
        row["id"] = transaction_id
        self._infrastructure.store(self._table, row)
        return self._from_row(row)

    def list_for_portfolio(self, portfolio_id: str) -> list[Transaction]:
        """All transactions for one portfolio -- the real read path for
        transactions (per this story's own AC). [] when there are
        none. Deterministically ordered by the row's own synthetic id
        (the AC's explicit ordering rule for this repository)."""
        rows = self._infrastructure.query(self._table, {"portfolio_id": portfolio_id})
        rows_sorted = sorted(rows, key=lambda row: str(row.get("id", "")))
        return [self._from_row(row) for row in rows_sorted]

    def create_many(
        self, portfolio_id: str, transactions: Sequence[Transaction]
    ) -> list[Transaction]:
        """Create multiple transactions, validating portfolio_id matches.

        APPEND-ONLY, deliberately not an upsert: calling it twice with
        the same input produces 2N rows. This is correct behavior given
        transactions have no natural identity -- inventing a dedupe key
        would silently drop real duplicate trades.

        Implemented as a loop over the existing single-row create.
        Performance is explicitly not a goal of this feature; correctness
        and Infrastructure Protocol stability are.

        Raises ValueError if any transaction's portfolio_id does not match
        the given portfolio_id.
        """
        for transaction in transactions:
            if transaction.portfolio_id != portfolio_id:
                raise ValueError(
                    f"Transaction.portfolio_id {transaction.portfolio_id!r} does not match "
                    f"expected {portfolio_id!r}"
                )
        # Validation passed; now perform writes.
        result: list[Transaction] = []
        for transaction in transactions:
            result.append(self.create(transaction))
        return result

    # ---- Row mapping -----------------------------------------------------

    def _to_row(self, transaction: Transaction) -> dict[str, Any]:
        """Emit the keys the ``Transaction`` dataclass owns plus a synthetic
        ``id`` column for storage.

        The set of keys is closed and explicit (portfolio_id, kind, amount, id);
        DB-managed timestamps (``created_at``, ``updated_at``) are deliberately NOT
        added here — the backend manages those itself.
        """
        return {
            "portfolio_id": transaction.portfolio_id,
            "kind": transaction.kind,
            "amount": transaction.amount,
        }

    def _from_row(self, row: Mapping[str, Any]) -> Transaction:
        """Build a :class:`Transaction` from a stored row, ignoring unknown
        keys (so DB-managed ``created_at`` / ``updated_at`` never
        reach the dataclass constructor) and coercing values to the
        dataclass's declared annotation types."""
        # Build kwargs only from the keys this repository wrote.
        # Any extra key the row carries (e.g. a backend-managed
        # ``created_at``) is silently dropped — the dataclass
        # constructor would reject it as an unexpected kwarg, and
        # even with ``__init__`` that accepted **kwargs, a real
        # timestamp column has no business living on a domain
        # entity.
        kwargs: dict[str, Any] = {}
        if "portfolio_id" in row:
            kwargs["portfolio_id"] = str(row["portfolio_id"])
        if "kind" in row:
            # kind is a plain str on the real, current Transaction
            # dataclass (not an Enum/Literal today) -- driven by the
            # same real declared-type lookup as amount below, so this
            # keeps working correctly if kind is ever changed to an
            # Enum/Literal without this method needing to change too.
            kwargs["kind"] = self._coerce_to_declared_type(row["kind"], "kind")
        if "amount" in row:
            kwargs["amount"] = self._coerce_to_declared_type(row["amount"], "amount")
        return Transaction(**kwargs)

    @staticmethod
    def _coerce_to_declared_type(value: Any, field_name: str) -> Any:
        """Coerce ``value`` to whatever type ``Transaction.<field_name>``
        is actually declared as, driven by the real, current dataclass
        annotation (via ``get_type_hints``) rather than a hardcoded
        assumption. A value coming back from a real backend can differ
        from the dataclass's own declared type (Postgres NUMERIC ->
        Decimal even when the dataclass says float); an Enum/Literal
        field is stored as its plain ``.value``/literal string and
        coerced back here (the AC's explicit rule), though the real,
        current ``kind`` field is a plain ``str`` today, not either."""
        declared_type = get_type_hints(Transaction).get(field_name)
        if isinstance(declared_type, type) and isinstance(value, declared_type):
            return value
        if isinstance(declared_type, type) and issubclass(declared_type, Enum):
            return declared_type(value)
        if declared_type is float:
            return float(value)
        if declared_type is int:
            return int(value)
        if declared_type is Decimal:
            return Decimal(str(value))
        if declared_type is str:
            return str(value)
        return value