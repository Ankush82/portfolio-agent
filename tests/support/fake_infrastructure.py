"""Full-fidelity in-memory test double for the Infrastructure Protocol.

Lives under ``tests/`` (never ``src/``) and is **not** imported by any
production code. All fast repository tests in this repo run against an
instance of ``FakeInfrastructure`` instead of against a real Postgres
+ Redis backend, so they execute unconditionally — no live service
required.

Why a separate, fuller fake and not the existing
``_InMemoryInfrastructure`` (in ``tests/components/test_user_portfolio.py``)?
That private class is component-scoped and intentionally minimal
(it only implements the four data methods, no Protocol-wide
conformance, no deep-copy semantics). This module implements the
**full** ``Infrastructure`` Protocol surface so a single instance can
stand in for the real backend in any test that needs it.

Design notes — read these before extending the fake:

* **State shape:** ``self._tables: dict[str, dict[str, dict]]``
  keyed by ``(table_name, row_id)``. Matches the recon-documented
  shape so the data is addressable exactly the way the real backend
  is.

* **Deep-copy semantics on both store and retrieve:**
  - ``store`` deep-copies the incoming ``record`` dict before storing
    it, so a caller mutating the original dict after storing cannot
    corrupt the stored state.
  - ``retrieve`` returns a deep copy of the stored row (or ``None``),
    so a caller mutating the *returned* dict cannot corrupt the
    stored state either. This matches what one gets from a real
    JSONB round-trip: the caller never has a live reference to the
    backend's internal storage.

* **Upsert-by-id for ``store``:** mirrors ``DefaultInfrastructure``'s
  ``INSERT ... ON CONFLICT (table_name, id) DO UPDATE`` behavior. The
  caller may pass ``record["id"]`` to control identity, or omit it
  and the fake generates a uuid4 (matching the real
  ``DefaultInfrastructure.store``). Returns the id used.

* **Containment-match ``query``:** filters stored rows by the same
  JSONB-``@>``-style predicate the real ``DefaultInfrastructure.query``
  documents — every ``key=value`` in ``filters`` must be present in
  the row with that value. No ordering or limits (the real Protocol
  exposes none).

* **delete:** removes by ``(table, id)`` and returns ``True`` when a
  row was actually removed, ``False`` otherwise (mirrors
  ``cursor.rowcount > 0`` semantics from
  ``DefaultInfrastructure.delete``).

* **Other Protocol methods (publish, subscribe, schedule, cache_get,
  cache_set, get_secret):** present as minimal no-op / ``None``-returning
  stubs. Repositories don't use them; they are here only so
  ``FakeInfrastructure`` structurally satisfies the ``Infrastructure``
  Protocol (see ``test_fake_satisfies_infrastructure_protocol``).

Explicit non-goals — this is **NOT** a Postgres emulator:

* **No type coercion.** A value stored as the string ``"1"`` stays
  ``"1"``; the fake will not coerce it to an int the way a real
  Postgres column might on insert.
* **No foreign-key enforcement.** ``store("holdings", {"portfolio_id":
  "ghost"})`` will happily store a row whose parent ``portfolios`` row
  never existed.
* **No unique-constraint enforcement** beyond the upsert-by-id
  semantic of ``store`` itself. Cross-row uniqueness on non-id fields
  is not checked.

Those three concerns are covered **only** by the real-Postgres tests
in ``tests/test_infrastructure_postgres.py``. Treating them as bugs
here would be a mistake — they are deliberate omissions so the fake
stays simple and fast.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from infrastructure import Infrastructure


class FakeInfrastructure:
    """In-memory test double that satisfies the ``Infrastructure`` Protocol.

    See the module docstring for the full design contract (state shape,
    deep-copy semantics, containment match, explicit non-goals).
    """

    def __init__(self) -> None:
        # dict[table_name] -> dict[row_id] -> row dict
        self._tables: dict[str, dict[str, dict]] = {}

    # ---- the four data methods (full semantics) ------------------------

    def store(self, table: str, record: dict) -> str:
        """Upsert a record by id. Deep-copies on write. Returns the
        row id used (``record["id"]`` if present, otherwise a fresh
        uuid4)."""
        record_id = (
            str(record["id"]) if "id" in record else str(uuid.uuid4())
        )
        # Deep copy so caller-side mutation of `record` cannot corrupt
        # stored state, including any nested dict/list values.
        stored: dict = copy.deepcopy(record)
        stored["id"] = record_id
        self._tables.setdefault(table, {})[record_id] = stored
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        """Read a record by id. Returns a deep copy (or ``None``) so
        caller-side mutation of the returned dict cannot corrupt
        stored state."""
        row = self._tables.get(table, {}).get(id_)
        if row is None:
            return None
        return copy.deepcopy(row)

    def query(self, table: str, filters: dict) -> list[dict]:
        """Return rows whose every ``key=value`` pair in ``filters`` is
        present with that value — the same JSONB ``@>`` containment
        match ``DefaultInfrastructure.query`` documents.

        Each returned row is a deep copy so caller-side mutation
        cannot corrupt stored state. There is no ordering or limit
        (the real Protocol exposes neither)."""
        rows = self._tables.get(table, {}).values()
        return [
            copy.deepcopy(row)
            for row in rows
            if all(row.get(key) == value for key, value in filters.items())
        ]

    def delete(self, table: str, id: str) -> bool:
        """Remove a row by id. Returns ``True`` when a row was actually
        removed, ``False`` otherwise (including when ``table`` itself
        was unknown). Does not raise on a missing id."""
        return self._tables.get(table, {}).pop(id, None) is not None

    # ---- remaining Protocol methods (no-op / None-returning stubs) -----

    def publish(self, topic: str, event: dict) -> None:
        """No-op. Repositories do not publish events; this is present
        only to satisfy the Protocol surface."""
        return None

    def subscribe(self, topic: str, handler: Any) -> None:
        """No-op. Repositories do not subscribe to topics."""
        return None

    def schedule(self, delay_seconds: float, task: dict) -> str:
        """No-op. Returns an empty schedule id so the return type
        still matches the Protocol."""
        return ""

    def cache_get(self, key: str) -> Any | None:
        """No-op cache read; always misses."""
        return None

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """No-op cache write."""
        return None

    def get_secret(self, name: str) -> str:
        """No-op secret lookup; returns empty string. Real secrets
        must never be touched from a test double."""
        return ""


# ``runtime_checkable`` lets ``isinstance(fake, Infrastructure)`` work
# at runtime; without it, the conformance test falls back to per-method
# ``inspect.signature`` comparison. We do not declare it on the Protocol
# ourselves (that's a src/ decision) — the conformance test handles
# both cases.
try:  # pragma: no cover - defensive; the actual assertion is in tests
    isinstance(FakeInfrastructure(), Infrastructure)
except TypeError:  # Infrastructure is not runtime_checkable
    pass
