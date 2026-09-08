"""QA tests for STORY-7 — HoldingRepository's full CRUD + natural-key
surface. Uses a real, small in-memory Infrastructure double (store/
retrieve/query/delete only — HoldingRepository never touches
publish/subscribe/schedule/cache_get/cache_set/get_secret), so every
test runs offline with no real Postgres.

list_for_portfolio itself already has real, existing coverage
elsewhere (it landed in an earlier commit on this branch) -- this file
covers what STORY-7 still needed: create/update/get_by_security/
delete_by_security and the natural-key identity contract.
"""

from decimal import Decimal
from typing import Any

import pytest

from domain import Holding
from repositories.holding_repository import HoldingRepository


class _InMemoryInfrastructure:
    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}

    def store(self, table: str, record: dict) -> str:
        record_id = str(record["id"])
        self._tables.setdefault(table, {})[record_id] = dict(record)
        return record_id

    def retrieve(self, table: str, id_: str) -> "dict | None":
        row = self._tables.get(table, {}).get(id_)
        return dict(row) if row is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        return [
            dict(row)
            for row in self._tables.get(table, {}).values()
            if all(row.get(key) == value for key, value in filters.items())
        ]

    def delete(self, table: str, id_: str) -> bool:
        return self._tables.get(table, {}).pop(id_, None) is not None


def _repo() -> HoldingRepository:
    return HoldingRepository(_InMemoryInfrastructure())


def _holding(**overrides: Any) -> Holding:
    base = {
        "portfolio_id": "pf-1",
        "security_id": "AAPL",
        "quantity": Decimal("10"),
        "currency": "USD",
        "exchange": "NASDAQ",
        "symbol_suffix": None,
    }
    base.update(overrides)
    return Holding(**base)


def test_no_id_field_added_to_holding_dataclass():
    assert "id" not in Holding.__dataclass_fields__


def test_create_generates_a_real_uuid4_synthetic_id():
    import uuid

    repo = _repo()
    repo.create(_holding())
    (row,) = repo._infrastructure._tables["holdings"].values()
    assert uuid.UUID(row["id"])


def test_get_by_id_returns_none_when_absent():
    assert _repo().get_by_id("does-not-exist") is None


def test_update_raises_key_error_when_absent():
    with pytest.raises(KeyError):
        _repo().update(_holding())


def test_update_persists_changed_fields_for_an_existing_holding():
    repo = _repo()
    repo.upsert(_holding(quantity=Decimal("5")))

    repo.update(_holding(quantity=Decimal("99")))

    result = repo.get_by_security("pf-1", "AAPL")
    assert result.quantity == Decimal("99")


def test_delete_returns_true_when_removed_false_when_absent():
    repo = _repo()
    repo.create(_holding())
    (holding_id,) = repo._infrastructure._tables["holdings"].keys()

    assert repo.delete(holding_id) is True
    assert repo.delete(holding_id) is False


def test_get_by_security_returns_match_and_none_for_no_match():
    repo = _repo()
    repo.upsert(_holding(portfolio_id="pf-1", security_id="AAPL"))

    assert repo.get_by_security("pf-1", "AAPL") is not None
    assert repo.get_by_security("pf-1", "MSFT") is None
    assert repo.get_by_security("pf-2", "AAPL") is None


def test_upsert_called_twice_produces_exactly_one_row_with_latest_values():
    repo = _repo()
    repo.upsert(_holding(quantity=Decimal("5")))
    repo.upsert(_holding(quantity=Decimal("12")))

    rows = repo._infrastructure._tables["holdings"]
    matching = [r for r in rows.values() if r["portfolio_id"] == "pf-1" and r["security_id"] == "AAPL"]
    assert len(matching) == 1
    assert matching[0]["quantity"] == Decimal("12")


def test_delete_by_security_returns_true_when_removed_false_when_absent():
    repo = _repo()
    repo.upsert(_holding())

    assert repo.delete_by_security("pf-1", "AAPL") is True
    assert repo.delete_by_security("pf-1", "AAPL") is False


def test_from_row_coerces_decimal_quantity_to_the_declared_annotation_type():
    repo = _repo()
    repo.upsert(_holding(quantity=Decimal("7.5")))

    result = repo.get_by_security("pf-1", "AAPL")

    assert type(result.quantity) is Decimal
    assert result.quantity == Decimal("7.5")


def test_holding_repository_module_has_no_db_driver_or_sql_imports():
    import ast
    import inspect
    import repositories.holding_repository as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {"psycopg", "redis", "sqlalchemy", "infrastructure_postgres"}
    assert not (imported_modules & forbidden), imported_modules & forbidden
    assert "SELECT" not in source.upper() and "INSERT INTO" not in source.upper()


def test_class_docstring_states_natural_key_is_the_meaningful_identity():
    doc = (HoldingRepository.__doc__ or "").lower()
    assert "natural key" in doc
    assert "portfolio_id" in doc and "security_id" in doc
