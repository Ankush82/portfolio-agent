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

* **Update semantics.** This repository does not provide an ``update``
  method because transactions are append-only and immutable once
  created. To correct a transaction, the application must create a
  new reversing transaction.

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

from typing import Any, Mapping, Sequence
import uuid
from decimal import Decimal

from domain import TRANSACTIONS_TABLE, Transaction
from infrastructure import Infrastructure
from repositories.base import BaseRepository


class TransactionRepository(BaseRepository):
    """Typed CRUD over the ``transactions`` table via the ``Infrastructure``
    Protocol.

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
            kwargs["kind"] = str(row["kind"])
        if "amount" in row:
            # The amount is stored as a float; ensure it's a float.
            a = row["amount"]
            if not isinstance(a, float):
                a = float(a)
            kwargs["amount"] = a
        return Transaction(**kwargs)