"""Typed CRUD repository for the :class:`Holding` domain entity.

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

* **Id handling.** This repository uses a synthetic id derived from
  the natural key (portfolio_id, security_id) to enable upsert
  via the Infrastructure Protocol's store method, which upserts on
  its ``id`` column. The domain ``Holding`` entity has no id
  attribute; the synthetic id is an implementation detail not
  exposed to the domain.

* **Update semantics.** ``upsert`` is a natural-key upsert: insert
  if no row exists for (portfolio_id, security_id), otherwise update
  the mutable fields (quantity, currency, exchange, symbol_suffix).

* **Row mapping.** ``_to_row`` emits exactly the keys the
  ``Holding`` dataclass owns plus a synthetic ``id``; ``_from_row``
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

from domain import HOLDINGS_TABLE, Holding
from infrastructure import Infrastructure
from repositories.base import BaseRepository


class HoldingRepository(BaseRepository):
    """Typed CRUD over the ``holdings`` table via the ``Infrastructure``
    Protocol.

    Stateless apart from the injected infrastructure; every method
    goes through ``self._infrastructure.store`` / ``retrieve`` /
    ``query`` / ``delete``. No module-level caches, no connection
    handling, no use of ``cache_get`` / ``cache_set``.
    """

    def __init__(self, infrastructure: Infrastructure) -> None:
        super().__init__(infrastructure, HOLDINGS_TABLE)

    # ---- CRUD ------------------------------------------------------------

    def upsert(self, holding: Holding) -> Holding:
        """Upsert a holding by natural key (portfolio_id, security_id).

        If a row with the same (portfolio_id, security_id) exists,
        update its quantity/currency/exchange/symbol_suffix in place.
        Otherwise insert a new row. Returns the persisted entity.
        """
        row = self._to_row(holding)
        # The store method uses the record's "id" if present, otherwise
        # generates a uuid4. We set the id to a deterministic string
        # derived from the natural key so that store becomes an upsert
        # on that natural key.
        row["id"] = f"{holding.portfolio_id}:{holding.security_id}"
        record_id = self._infrastructure.store(self._table, row)
        # The id we used is the record_id returned by store.
        return self._from_row(row)

    def upsert_many(
        self, portfolio_id: str, holdings: Sequence[Holding]
    ) -> list[Holding]:
        """Upsert multiple holdings, validating portfolio_id matches.

        Implemented as a loop over the existing single-row upsert.
        Performance is explicitly not a goal of this feature; correctness
        and Infrastructure Protocol stability are.

        Raises ValueError if any holding's portfolio_id does not match
        the given portfolio_id.
        """
        # Loop over single-row upsert is deliberate: performance is not a goal; correctness and Infrastructure Protocol stability are.
        for holding in holdings:
            if holding.portfolio_id != portfolio_id:
                raise ValueError(
                    f"Holding.portfolio_id {holding.portfolio_id!r} does not match "
                    f"expected {portfolio_id!r}"
                )
        # Validation passed; now perform writes.
        result: list[Holding] = []
        for holding in holdings:
            result.append(self.upsert(holding))
        return result

    # ---- Row mapping -----------------------------------------------------

    def _to_row(self, holding: Holding) -> dict[str, Any]:
        """Emit the keys the ``Holding`` dataclass owns plus a synthetic
        ``id`` column for upsert-by-natural-key.

        The set of keys is closed and explicit (portfolio_id, security_id,
        quantity, currency, exchange, symbol_suffix, id); DB-managed
        timestamps (``created_at``, ``updated_at``) are deliberately NOT
        added here — the backend manages those itself.
        """
        return {
            "portfolio_id": holding.portfolio_id,
            "security_id": holding.security_id,
            "quantity": holding.quantity,
            "currency": holding.currency,
            "exchange": holding.exchange,
            "symbol_suffix": holding.symbol_suffix,
        }

    def list_for_portfolio(self, portfolio_id: str) -> list[Holding]:
        """List all holdings belonging to a portfolio.

        Returns a list (never None) sorted deterministically by
        security_id when the Protocol's query offers no ordering.
        """
        rows = self._infrastructure.query(self._table, {"portfolio_id": portfolio_id})
        holdings = [self._from_row(row) for row in rows]
        # Sort by security_id for deterministic ordering (Protocol query has no ordering)
        holdings.sort(key=lambda h: h.security_id)
        return holdings

    def _from_row(self, row: Mapping[str, Any]) -> Holding:
        """Build a :class:`Holding` from a stored row, ignoring unknown
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
        if "security_id" in row:
            kwargs["security_id"] = str(row["security_id"])
        if "quantity" in row:
            # The quantity is stored as a Decimal; ensure it's a Decimal.
            q = row["quantity"]
            if not isinstance(q, Decimal):
                q = Decimal(str(q))
            kwargs["quantity"] = q
        if "currency" in row:
            kwargs["currency"] = str(row["currency"])
        if "exchange" in row:
            # exchange may be None
            kwargs["exchange"] = str(row["exchange"]) if row["exchange"] is not None else None
        if "symbol_suffix" in row:
            # symbol_suffix may be None
            kwargs["symbol_suffix"] = (
                str(row["symbol_suffix"]) if row["symbol_suffix"] is not None else None
            )
        return Holding(**kwargs)