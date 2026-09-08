"""Typed CRUD repository for the :class:`User` domain entity.

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
  ``User`` dataclass owns (``id``, ``email``, ``preferences``);
  ``_from_row`` ignores unknown keys (so DB-managed
  ``created_at`` / ``updated_at`` never reach the dataclass
  constructor) and coerces values to the dataclass's declared
  annotation types.

Explicit non-goals for this repository:

* No email lookup (``get_by_email``). The recon doc's V5 section
  records zero existing email-lookup call sites under ``src/`` —
  building one would be unused API.
* No SQL. No imports of ``psycopg``, ``redis``, ``sqlalchemy``, or
  ``src.infrastructure_postgres``.
"""

from __future__ import annotations

from typing import Any, Mapping
import uuid

from domain import USERS_TABLE, User
from infrastructure import Infrastructure
from repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """Typed CRUD over the ``users`` table via the ``Infrastructure``
    Protocol.

    Stateless apart from the injected infrastructure; every method
    goes through ``self._infrastructure.store`` / ``retrieve`` /
    ``query`` / ``delete``. No module-level caches, no connection
    handling, no use of ``cache_get`` / ``cache_set``.
    """

    def __init__(self, infrastructure: Infrastructure) -> None:
        super().__init__(infrastructure, USERS_TABLE)

    # ---- Row mapping -----------------------------------------------------

    def _to_row(self, user: User) -> dict[str, Any]:
        """Emit exactly the keys the ``User`` dataclass owns.

        The set of keys is closed and explicit (``id``, ``email``,
        ``preferences``); DB-managed timestamps (``created_at``,
        ``updated_at``) are deliberately NOT added here — the
        backend manages those itself.
        """
        return {
            "id": user.id,
            "email": user.email,
            "preferences": user.preferences,
        }

    def _from_row(self, row: Mapping[str, Any]) -> User:
        """Build a :class:`User` from a stored row, ignoring unknown
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
        if "email" in row:
            kwargs["email"] = str(row["email"])
        if "preferences" in row:
            # ``preferences`` is annotated ``dict`` on the dataclass.
            # If the row is missing it (e.g. an older row written
            # before the field existed), default to an empty dict
            # rather than failing the round-trip — the dataclass's
            # own ``default_factory=dict`` would produce the same
            # value if ``preferences`` were omitted from the kwargs.
            prefs = row["preferences"]
            kwargs["preferences"] = prefs if isinstance(prefs, dict) else dict(prefs)
        return User(**kwargs)
