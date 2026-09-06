"""Base class for repositories reducing duplication of common CRUD mechanics.

Repositories are stateless apart from the injected infrastructure.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Protocol

from infrastructure import Infrastructure


class Entity(Protocol):
    """Protocol for domain entities that have an id attribute."""
    id: str


class BaseRepository:
    """Common CRUD mechanics for repositories talking through Infrastructure.

    Subclasses must implement _to_row and _from_row and set the table name.
    """

    def __init__(self, infrastructure: Infrastructure, table: str) -> None:
        self._infrastructure = infrastructure
        self._table = table

    # ---- CRUD ------------------------------------------------------------

    def create(self, entity: Entity) -> Entity:
        """Insert a new row.

        If the entity's id is empty/unset, a fresh uuid4 string is
        generated and used as the row id. Returns the persisted entity
        (with the id that was actually stored).
        """
        row = self._to_row(entity)
        if not row.get("id"):
            row["id"] = str(uuid.uuid4())
        record_id = self._infrastructure.store(self._table, row)
        return self._from_row(row)

    def get_by_id(self, id_: str) -> Entity | None:
        """Read a row by id. Returns None when absent."""
        row = self._infrastructure.retrieve(self._table, id_)
        if row is None:
            return None
        return self._from_row(row)

    def update(self, entity: Entity) -> Entity:
        """Full replace of an existing row.

        Raises KeyError when the target row does not exist.
        Returns the persisted entity.
        """
        # Existence check first to avoid silent insert via store's upsert-by-id.
        existing = self._infrastructure.retrieve(self._table, entity.id)
        if existing is None:
            raise KeyError(entity.id)
        row = self._to_row(entity)
        self._infrastructure.store(self._table, row)
        return self._from_row(row)

    def delete(self, id_: str) -> bool:
        """Delete a row by id. Idempotent: True if removed, False if absent."""
        return self._infrastructure.delete(self._table, id_)

    # ---- Row mapping -----------------------------------------------------

    def _to_row(self, entity: Entity) -> dict[str, Any]:
        """Emit the keys the domain entity owns. Must be implemented by subclass."""
        raise NotImplementedError

    def _from_row(self, row: Mapping[str, Any]) -> Entity:
        """Build a domain entity from a stored row, ignoring unknown keys.
        Must be implemented by subclass."""
        raise NotImplementedError