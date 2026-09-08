"""QA tests for STORY-8 — TransactionRepository's append-only CRUD +
list_for_portfolio. Uses a real, small in-memory Infrastructure double
(store/retrieve/query/delete only), so every test runs offline with no
real Postgres.
"""

from typing import Any

import pytest

from domain import Transaction
from repositories.transaction_repository import TransactionRepository


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


def _repo() -> TransactionRepository:
    return TransactionRepository(_InMemoryInfrastructure())


def _transaction(**overrides: Any) -> Transaction:
    base = {"portfolio_id": "pf-1", "kind": "BUY", "amount": 100.0}
    base.update(overrides)
    return Transaction(**base)


def test_no_id_field_added_to_transaction_dataclass():
    assert "id" not in Transaction.__dataclass_fields__


def test_create_generates_a_real_uuid4_synthetic_id():
    import uuid

    repo = _repo()
    repo.create(_transaction())
    (row,) = repo._infrastructure._tables["transactions"].values()
    assert uuid.UUID(row["id"])


def test_get_by_id_returns_none_when_absent():
    assert _repo().get_by_id("nope") is None


def test_update_raises_key_error_when_absent():
    repo = _repo()
    with pytest.raises(KeyError):
        repo.update("nope", _transaction())


def test_update_replaces_the_row_at_the_given_id():
    repo = _repo()
    repo.create(_transaction(amount=100.0))
    (tx_id,) = repo._infrastructure._tables["transactions"].keys()

    repo.update(tx_id, _transaction(amount=250.0))

    result = repo.get_by_id(tx_id)
    assert result.amount == 250.0


def test_delete_returns_true_when_removed_false_when_absent():
    repo = _repo()
    repo.create(_transaction())
    (tx_id,) = repo._infrastructure._tables["transactions"].keys()

    assert repo.delete(tx_id) is True
    assert repo.delete(tx_id) is False


def test_two_field_identical_creates_produce_two_distinct_rows():
    repo = _repo()
    repo.create(_transaction())
    repo.create(_transaction())

    rows = repo._infrastructure._tables["transactions"]
    assert len(rows) == 2
    ids = list(rows.keys())
    assert ids[0] != ids[1]


def test_list_for_portfolio_empty_returns_empty_list():
    assert _repo().list_for_portfolio("pf-empty") == []


def test_list_for_portfolio_returns_only_that_portfolios_transactions():
    repo = _repo()
    repo.create(_transaction(portfolio_id="pf-1"))
    repo.create(_transaction(portfolio_id="pf-2"))

    result = repo.list_for_portfolio("pf-1")

    assert len(result) == 1
    assert result[0].portfolio_id == "pf-1"


def test_list_for_portfolio_is_deterministically_ordered():
    repo = _repo()
    for i in range(3):
        repo.create(_transaction(amount=float(i)))

    first = repo.list_for_portfolio("pf-1")
    second = repo.list_for_portfolio("pf-1")

    assert [t.amount for t in first] == [t.amount for t in second]
    assert len(first) == 3


def test_from_row_coerces_amount_to_the_declared_annotation_type():
    from decimal import Decimal

    repo = _repo()
    repo.create(_transaction(amount=100.0))
    (tx_id,) = repo._infrastructure._tables["transactions"].keys()
    # Simulate what a real Postgres NUMERIC column would hand back.
    repo._infrastructure._tables["transactions"][tx_id]["amount"] = Decimal("100.00")

    result = repo.get_by_id(tx_id)

    assert type(result.amount) is float
    assert result.amount == 100.0


def test_no_upsert_dedupe_or_hash_identity_logic_exists():
    # Checks real, structural facts (method names actually defined, real
    # imports actually made, real dict keys actually assigned) rather
    # than banning words from source text -- this module's own comments
    # legitimately mention "dedupe"/"broker_txn_id" while documenting
    # that neither exists, which a plain substring check can't tell
    # apart from the thing itself actually existing.
    import ast
    import inspect
    import repositories.transaction_repository as module

    tree = ast.parse(inspect.getsource(module))
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "upsert" not in method_names and "upsert_many" not in method_names

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "hashlib" not in imported_modules

    dict_keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert "broker_txn_id" not in dict_keys


def test_class_docstring_states_append_only_no_natural_identity():
    doc = (TransactionRepository.__doc__ or "").lower()
    assert "append-only" in doc
    assert "no correct dedupe key" in doc or "no natural identity" in doc


def test_transaction_repository_module_has_no_db_driver_or_sql_imports():
    import ast
    import inspect
    import repositories.transaction_repository as module

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
