"""Typed CRUD repository for the :class:`Portfolio` domain entity.

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

* **Update semantics.** ``update`` is a **full replace** of the
  mutable columns for an existing row — not a partial patch. It
  raises ``KeyError`` when the target row does not exist; it never
  silently inserts (an "upsert" would make a typo silently create
  a ghost row, which the upstream ``DefaultUserPortfolio`` code
  has been bitten by in the past).

* **Row mapping.** ``_to_row`` emits exactly the keys the
  ``Portfolio`` dataclass owns (``id``, ``user_id``);
  ``_from_row`` ignores unknown keys (so DB-managed
  ``created_at`` / ``updated_at`` never reach the dataclass
  constructor) and coerces values to the dataclass's declared
  annotation types.

Explicit non-goals for this repository:

* No user lookup (``get_by_user_id`` beyond list_for_user). The
  recon doc's V5 section records zero existing user-lookup call sites
  under ``src/`` — building one would be unused API.
* No SQL. No imports of ``psycopg``, ``redis``, ``sqlalchemy``, or
  ``src.infrastructure_postgres``.
"""

from __future__ import annotations

from typing import Any, Mapping
import uuid

from domain import PORTFOLIOS_TABLE, Portfolio
from infrastructure import Infrastructure
from repositories.base import BaseRepository


class PortfolioRepository(BaseRepository):
    """Typed CRUD over the ``portfolios`` table via the ``Infrastructure``
    Protocol.

    Stateless apart from the injected infrastructure; every method
    goes through ``self._infrastructure.store`` / ``retrieve`` /
    ``query`` / ``delete``. No module-level caches, no connection
    handling, no use of ``cache_get`` / ``cache_set``.
    """

    def __init__(self, infrastructure: Infrastructure) -> None:
        super().__init__(infrastructure, PORTFOLIOS_TABLE)

    # ---- CRUD ------------------------------------------------------------

    def create(self, portfolio: Portfolio) -> Portfolio:
        """Insert a new portfolio row.

        If ``portfolio.id`` is empty / unset, a fresh uuid4 string is
        generated and used as the row id. Returns the persisted
        entity — with the id that was actually stored, so the
        caller does not have to look it up again.
        """
        return super().create(portfolio)

    def get_by_id(self, portfolio_id: str) -> Portfolio | None:
        """Read a portfolio by id. Returns ``None`` when absent — never
        raises for a not-found row."""
        return super().get_by_id(portfolio_id)

    def update(self, portfolio: Portfolio) -> Portfolio:
        """Full replace of an existing portfolio row.

        Raises ``KeyError`` when the target row does not exist —
        the repository must never silently insert via this path.
        Returns the persisted entity.
        """
        return super().update(portfolio)

    def delete(self, portfolio_id: str) -> bool:
        """Delete a portfolio by id. Idempotent: returns ``True`` when a
        row was actually removed, ``False`` when no matching row
        existed (never raises for a missing id)."""
        return super().delete(portfolio_id)

    # ---- Additional methods ----------------------------------------------

    def list_for_user(self, user_id: str) -> list[Portfolio]:
        """List all portfolios belonging to a user.

        Returns a list (never None) sorted deterministically by id
        when the Protocol's query offers no ordering.
        """
        rows = self._infrastructure.query(self._table, {"user_id": user_id})
        portfolios = [self._from_row(row) for row in rows]
        # Sort by id for deterministic ordering (Protocol query has no ordering)
        portfolios.sort(key=lambda p: p.id)
        return portfolios

    # ---- Row mapping -----------------------------------------------------

    def _to_row(self, portfolio: Portfolio) -> dict[str, Any]:
        """Emit exactly the keys the ``Portfolio`` dataclass owns.

        The set of keys is closed and explicit (``id``, ``user_id``);
        DB-managed timestamps (``created_at``, ``updated_at``) are
        deliberately NOT added here — the backend manages those itself.
        """
        return {
            "id": portfolio.id,
            "user_id": portfolio.user_id,
        }

    def _from_row(self, row: Mapping[str, Any]) -> Portfolio:
        """Build a :class:`Portfolio` from a stored row, ignoring unknown
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
        if "id" in row:
            kwargs["id"] = str(row["id"])
        if "user_id" in row:
            kwargs["user_id"] = str(row["user_id"])
        return Portfolio(**kwargs)