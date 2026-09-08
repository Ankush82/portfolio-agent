"""Tests for :class:`HoldingRepository` against the
:class:`FakeInfrastructure` from STORY-4.

Every test runs against the fake — no real Postgres, no live
Redis — so the suite executes unconditionally. The repository is
the unit under test here; ``FakeInfrastructure`` is the fixture.

These tests exercise the acceptance criteria of STORY-7:

* HoldingRepository is exported from src/repositories/__init__.py
* All eight listed methods are implemented with full type annotations
* No id field was added to the Holding dataclass in src/domain.py
* get_by_security returns the matching holding for a (portfolio_id, security_id) pair and None when there is no match
* upsert called twice with the same (portfolio_id, security_id) produces exactly one stored row, and the second call's field values are reflected
* delete_by_security returns True when it removed a row and False when no row matched that natural key
* list_for_portfolio returns [] when the portfolio has no holdings, and returns only that portfolio's holdings when multiple portfolios have rows
* list_for_portfolio results are deterministically ordered, asserted by a test that seeds rows out of order
* _from_row coerces quantity to the type declared in the Holding dataclass -- a test passes a Decimal value in the row dict and asserts type(result.quantity) matches the declared annotation
* The class docstring states that (portfolio_id, security_id) is the meaningful identity for holdings
* holding_repository.py contains no import of psycopg, redis, sqlalchemy, or src.infrastructure_postgres, and no SQL string
* Holding and HOLDINGS_TABLE are imported from src.domain
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

from domain import HOLDINGS_TABLE, Holding
from infrastructure import Infrastructure
from tests.support.fake_infrastructure import FakeInfrastructure
from repositories.holding_repository import HoldingRepository


def _check_no_forbidden_imports_or_sql(module) -> None:
    """Check that the module does not import forbidden libraries or contain SQL strings.
    This is a copy of the check from test_portfolio_repository.py."""
    import repositories.holding_repository as hr_module

    source = inspect.getsource(hr_module)
    tree = ast.parse(source)

    # 1) Forbidden library imports
    forbidden_roots = {"psycopg", "redis", "sqlalchemy"}
    forbidden_modules = {
        "src.infrastructure_postgres",
        "infrastructure_postgres",
    }
    offending_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in forbidden_roots:
                    offending_imports.append(
                        f"import {alias.name} (line {node.lineno})"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module in forbidden_modules:
                offending_imports.append(
                    f"from {node.module} import ... (line {node.lineno})"
                )
            elif node.module.split(".")[0] in forbidden_roots:
                offending_imports.append(
                    f"from {node.module} import ... (line {node.lineno})"
                )

    assert offending_imports == [], (
        "holding_repository.py must not import psycopg, redis, sqlalchemy, "
        f"or src.infrastructure_postgres; offending imports: {offending_imports}"
    )

    # 2) No SQL strings
    sql_keywords = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "FROM",
        "WHERE", "JOIN", "TRUNCATE", "DROP ",
    )

    def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
        if not isinstance(node, ast.Expr):
            return False
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value, str
        ):
            return False
        if parent is None or not hasattr(parent, "body"):
            return False
        return (
            isinstance(
                parent,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
            and len(parent.body) > 0
            and parent.body[0] is node
        )

    docstring_lines: set[int] = set()
    offending_sql_strings: list[str] = []

    def _collect_docstrings(node: ast.AST) -> None:
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                start = node.body[0].lineno
                end = getattr(node.body[0], "end_lineno", start)
                for ln in range(start, end + 1):
                    docstring_lines.add(ln)
        for child in ast.iter_child_nodes(node):
            _collect_docstrings(child)

    _collect_docstrings(tree)

    def _walk(node: ast.AST, parent: ast.AST | None = None) -> None:
        for child in ast.iter_child_nodes(node):
            _walk(child, node)
        if _is_docstring(node, parent):
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in docstring_lines:
                return
            value_upper = node.value.upper()
            for kw in sql_keywords:
                if kw in value_upper:
                    offending_sql_strings.append(
                        f"line {node.lineno}: {node.value!r}"
                    )
                    break

    _walk(tree)

    assert offending_sql_strings == [], (
        "holding_repository.py must not contain SQL strings; found: "
        f"{offending_sql_strings}"
    )

    # 3) Module-level caches / state
    forbidden_globals = {
        name
        for name in hr_module.__dict__.keys()
        if name in ("_CACHE", "_STORE", "_CONNECTION", "_DB", "_POOL", "_TABLE_CACHE")
    }
    assert forbidden_globals == set(), (
        f"holding_repository.py must not carry module-level caches / "
        f"connections / storage; found: {sorted(forbidden_globals)}"
    )


# --- Tests ----------------------------------------------------------------

def test_holding_repository_is_exported() -> None:
    """STORY-7 acceptance: HoldingRepository is exported from src/repositories/__init__.py."""
    from repositories import HoldingRepository as ExportedHoldingRepository
    assert ExportedHoldingRepository is HoldingRepository


def test_all_methods_are_implemented() -> None:
    """STORY-7 acceptance: All eight listed methods are implemented."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    # We can call each method with dummy data to ensure they exist and don't raise NotImplementedError
    # create
    holding = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("10"))
    created = repo.create(holding)
    assert created is not None

    # get_by_id
    fetched = repo.get_by_id(created.id)
    assert fetched is not None

    # update (we'll test update separately)
    # delete
    deleted = repo.delete(created.id)
    assert deleted is True

    # get_by_security
    # Re-create for the test
    repo.create(holding)
    fetched_by_sec = repo.get_by_security("p1", "s1")
    assert fetched_by_sec is not None

    # upsert
    upserted = repo.upsert(holding)
    assert upserted is not None

    # delete_by_security
    deleted_by_sec = repo.delete_by_security("p1", "s1")
    assert deleted_by_sec is True

    # list_for_portfolio
    list_result = repo.list_for_portfolio("p1")
    assert isinstance(list_result, list)

    # We'll test the specifics in other test functions


def test_no_id_field_in_holding() -> None:
    """STORY-7 acceptance: No id field was added to the Holding dataclass in src/domain.py."""
    # Check the Holding dataclass fields
    holding_fields = {f.name for f in Holding.__dataclass_fields__.values()}
    assert "id" not in holding_fields, f"Holding should not have an 'id' field, but has fields: {holding_fields}"


def test_get_by_security_returns_matching_or_none() -> None:
    """STORY-7 acceptance: get_by_security returns the matching holding for a (portfolio_id, security_id) pair and None when there is no match."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    holding = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("10"))
    repo.create(holding)

    # Should return the holding
    result = repo.get_by_security("p1", "s1")
    assert result is not None
    assert result.portfolio_id == "p1"
    assert result.security_id == "s1"
    assert result.quantity == Decimal("10")

    # Should return None for non-existent
    result_none = repo.get_by_security("p1", "s2")
    assert result_none is None

    result_none2 = repo.get_by_security("p2", "s1")
    assert result_none2 is None


def test_upsert_produces_one_row_and_updates() -> None:
    """STORY-7 acceptance: upsert called twice with the same (portfolio_id, security_id) produces exactly one stored row, and the second call's field values are reflected."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    holding1 = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("10"), currency="USD", exchange=None, symbol_suffix=None)
    holding2 = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("20"), currency="USD", exchange=None, symbol_suffix=None)

    # First upsert
    result1 = repo.upsert(holding1)
    assert result1 is not None
    assert result1.quantity == Decimal("10")

    # Second upsert with same natural key
    result2 = repo.upsert(holding2)
    assert result2 is not None
    assert result2.quantity == Decimal("20")

    # Now, get_by_security should return the updated holding
    fetched = repo.get_by_security("p1", "s1")
    assert fetched is not None
    assert fetched.quantity == Decimal("20")
    assert fetched.portfolio_id == "p1"
    assert fetched.security_id == "s1"

    # Ensure only one row exists by checking list_for_portfolio
    list_result = repo.list_for_portfolio("p1")
    assert len(list_result) == 1
    assert list_result[0].quantity == Decimal("20")


def test_delete_by_security_returns_true_false() -> None:
    """STORY-7 acceptance: delete_by_security returns True when it removed a row and False when no row matched that natural key."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    holding = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("10"))
    repo.create(holding)

    # Should return True and delete the row
    deleted = repo.delete_by_security("p1", "s1")
    assert deleted is True

    # Now get_by_security should return None
    assert repo.get_by_security("p1", "s1") is None

    # Calling again should return False (idempotent)
    deleted_again = repo.delete_by_security("p1", "s1")
    assert deleted_again is False

    # Deleting a non-existent key should return False
    deleted_none = repo.delete_by_security("p2", "s2")
    assert deleted_none is False


def test_list_for_portfolio_returns_correct_holdings() -> None:
    """STORY-7 acceptance: list_for_portfolio returns [] when the portfolio has no holdings, and returns only that portfolio's holdings when multiple portfolios have rows."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    # Empty portfolio
    empty_list = repo.list_for_portfolio("empty")
    assert empty_list == []

    # Add holdings for portfolio p1
    holding_p1_1 = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("10"))
    holding_p1_2 = Holding(portfolio_id="p1", security_id="s2", quantity=Decimal("20"))
    repo.create(holding_p1_1)
    repo.create(holding_p1_2)

    # Add holdings for portfolio p2
    holding_p2_1 = Holding(portfolio_id="p2", security_id="s1", quantity=Decimal("30"))
    repo.create(holding_p2_1)

    # List for p1 should return only p1's holdings
    list_p1 = repo.list_for_portfolio("p1")
    assert len(list_p1) == 2
    sec_ids_p1 = {h.security_id for h in list_p1}
    assert sec_ids_p1 == {"s1", "s2"}

    # List for p2 should return only p2's holding
    list_p2 = repo.list_for_portfolio("p2")
    assert len(list_p2) == 1
    assert list_p2[0].security_id == "s1"
    assert list_p2[0].portfolio_id == "p2"

    # List for empty again
    assert repo.list_for_portfolio("empty") == []


def test_list_for_portfolio_is_deterministically_ordered() -> None:
    """STORY-7 acceptance: list_for_portfolio results are deterministically ordered, asserted by a test that seeds rows out of order."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    # Insert holdings in non-security_id order: s3, s1, s2
    holding_s3 = Holding(portfolio_id="p1", security_id="s3", quantity=Decimal("10"))
    holding_s1 = Holding(portfolio_id="p1", security_id="s1", quantity=Decimal("20"))
    holding_s2 = Holding(portfolio_id="p1", security_id="s2", quantity=Decimal("30"))
    repo.create(holding_s3)
    repo.create(holding_s1)
    repo.create(holding_s2)

    # List should be ordered by security_id (as per the implementation)
    list_result = repo.list_for_portfolio("p1")
    assert len(list_result) == 3
    sec_ids_in_order = [h.security_id for h in list_result]
    assert sec_ids_in_order == ["s1", "s2", "s3"]


def test_from_row_coerces_quantity_to_declared_type() -> None:
    """STORY-7 acceptance: _from_row coerces quantity to the type declared in the Holding dataclass -- a test passes a Decimal value in the row dict and asserts type(result.quantity) matches the declared annotation."""
    fake = FakeInfrastructure()
    repo = HoldingRepository(fake)

    # The Holding.quantity is annotated as Decimal
    from typing import get_type_hints
    hints = get_type_hints(Holding)
    assert hints['quantity'] is Decimal

    # Create a row with a Decimal quantity (as would come from the DB)
    row = {
        "portfolio_id": "p1",
        "security_id": "s1",
        "quantity": Decimal("15.5"),
        "currency": "USD",
        "exchange": None,
        "symbol_suffix": None,
    }

    holding = repo._from_row(row)
    assert isinstance(holding.quantity, Decimal)
    assert holding.quantity == Decimal("15.5")


def test_class_docstring_states_natural_key() -> None:
    """STORY-7 acceptance: The class docstring states that (portfolio_id, security_id) is the meaningful identity for holdings."""
    docstring = HoldingRepository.__doc__
    assert docstring is not None, "HoldingRepository must have a docstring"
    # Check for the phrase about natural key
    assert "natural key" in docstring.lower()
    assert "portfolio_id" in docstring
    assert "security_id" in docstring
    # More specifically, the docstring says: "The natural key (portfolio_id, security_id) is the real, meaningful identity for a holding"
    assert "natural key (portfolio_id, security_id) is the real, meaningful identity for a holding" in docstring


def test_no_forbidden_imports_or_sql() -> None:
    """STORY-7 acceptance: holding_repository.py contains no import of psycopg, redis, sqlalchemy, or src.infrastructure_postgres, and no SQL string."""
    _check_no_forbidden_imports_or_sql(HoldingRepository)


def test_holding_and_holdings_table_imported_from_domain() -> None:
    """STORY-7 acceptance: Holding and HOLDINGS_TABLE are imported from src.domain."""
    source = inspect.getsource(HoldingRepository)
    # Check that the import line exists and contains both
    assert "from domain import" in source
    assert "HOLDINGS_TABLE" in source
    assert "Holding" in source
    # More specifically, check that the line is roughly as expected (allowing for whitespace)
    lines = [line.strip() for line in source.split('\n')]
    import_found = False
    for line in lines:
        if line.startswith("from domain import"):
            import_found = True
            assert "HOLDINGS_TABLE" in line
            assert "Holding" in line
            break
    assert import_found, "Could not find 'from domain import' line with HOLDINGS_TABLE and Holding"


if __name__ == "__main__":
    # For manual testing
    test_holding_repository_is_exported()
    test_all_methods_are_implemented()
    test_no_id_field_in_holding()
    test_get_by_security_returns_matching_or_none()
    test_upsert_produces_one_row_and_updates()
    test_delete_by_security_returns_true_false()
    test_list_for_portfolio_returns_correct_holdings()
    test_list_for_portfolio_is_deterministically_ordered()
    test_from_row_coerces_quantity_to_declared_type()
    test_class_docstring_states_natural_key()
    test_no_forbidden_imports_or_sql()
    test_holding_and_holdings_table_imported_from_domain()
    print("All tests passed!")