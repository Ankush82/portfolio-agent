"""Real acceptance-criterion verification for STORY-2.

These tests exercise the story's specific acceptance criteria against
the real c01 and domain modules. They are NOT the same as the existing
test_qa_story2_domain_extraction.py — they verify additional, distinct
properties that the story requires:

  * The four table-name constants have the EXACT values the story
    specifies ("users", "portfolios", "holdings", "transactions") --
    not just that they are importable, but that their string values
    are exactly what the PRD says.
  * The four dataclasses' field lists, type annotations, and defaults
    are byte-for-byte identical to what docs/repo-layer-recon.md
    (STORY-1's source of truth) records. If anyone silently added an
    `id` field to Holding or Transaction (or removed/renamed any
    field), this test would catch it.
  * src/domain.py is genuinely stdlib-only -- not a single import of
    any infrastructure, component, or third-party package.
  * The c01 re-export block truly replaces the original definitions:
    c01 has NO module-level class statement for User/Portfolio/Holding
    /Transaction and NO module-level assignment for the four table
    constants. (A future regression that re-introduces a duplicate
    definition in c01 will fail this test, even if the re-export
    import is also still present.)
  * `validate_stock_symbol` -- which is defined in domain and was
    imported by tests/components/test_user_portfolio.py -- is also
    re-exported from c01, so that test file's collection is not
    broken (the story requires "the existing c01 test file passes
    with zero modifications to that file").
"""

from __future__ import annotations

import ast
import importlib
import inspect
from dataclasses import fields, MISSING
from pathlib import Path

import pytest


def _load_modules():
    domain_module = importlib.import_module("domain")
    c01_module = importlib.import_module("components.c01_user_portfolio")
    return domain_module, c01_module


# ---------- 1. Table-name constants have exactly the required values --------

EXPECTED_TABLE_VALUES = {
    "USERS_TABLE": "users",
    "PORTFOLIOS_TABLE": "portfolios",
    "HOLDINGS_TABLE": "holdings",
    "TRANSACTIONS_TABLE": "transactions",
}


def test_table_name_constants_have_exact_required_string_values():
    """Acceptance criterion: USERS_TABLE='users', PORTFOLIOS_TABLE='portfolios',
    HOLDINGS_TABLE='holdings', TRANSACTIONS_TABLE='transactions'."""
    domain_module, _c01_module = _load_modules()
    for name, expected_value in EXPECTED_TABLE_VALUES.items():
        actual = getattr(domain_module, name, None)
        assert actual == expected_value, (
            f"{name} in domain is {actual!r}, expected exactly {expected_value!r}"
        )


# ---------- 2. Dataclass fields/annotations/defaults are byte-identical ----

# The expected field lists below are copied verbatim from
# docs/repo-layer-recon.md (STORY-1's authoritative record).
EXPECTED_USER_FIELDS = (
    # (name, annotation_string, has_default, default_repr)
    ("id", "str", False, None),
    ("preferences", "dict", True, "<default_factory>"),
    ("email", "str", True, "''"),
)
EXPECTED_PORTFOLIO_FIELDS = (
    ("id", "str", False, None),
    ("user_id", "str", False, None),
)
EXPECTED_HOLDING_FIELDS = (
    ("portfolio_id", "str", False, None),
    ("security_id", "str", False, None),
    ("quantity", "Decimal", False, None),
    ("currency", "str", True, "'USD'"),
    ("exchange", "str | None", True, "None"),
    ("symbol_suffix", "str | None", True, "None"),
)
EXPECTED_TRANSACTION_FIELDS = (
    ("portfolio_id", "str", False, None),
    ("kind", "str", False, None),
    ("amount", "float", False, None),
)


def _annotation_to_string(annotation) -> str:
    """Best-effort conversion of a dataclass field annotation into a
    human-readable string for comparison. We deliberately use a
    simple `getattr(annotation, "__name__", str(annotation))` rather
    than parsing the AST -- annotations may be real types (str,
    Decimal, float) or string-form annotations."""
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    # PEP 604 unions: int | None has no __name__; render its repr.
    return repr(annotation)


def _field_signature_tuple(dc_field) -> tuple:
    """Build a (name, annotation_str, has_default, default_repr) tuple
    for one dataclass field so we can compare against EXPECTED_*_FIELDS.

    On a real dataclass Field, the `default` attribute is the sentinel
    `MISSING` if no default was set, and `default_factory` is similarly
    MISSING if no factory was set. (Both attributes always exist as
    attributes on a Field instance -- they default to MISSING.)"""
    default_repr = None
    has_default = False
    if dc_field.default is not MISSING:
        has_default = True
        default_repr = repr(dc_field.default)
    elif dc_field.default_factory is not MISSING:
        has_default = True
        default_repr = "<default_factory>"
    return (
        dc_field.name,
        _annotation_to_string(dc_field.type),
        has_default,
        default_repr,
    )


def _assert_dataclass_fields(dc_class, expected_fields, *, class_name):
    """Verify a dataclass has the exact field list/annotations/defaults."""
    actual_fields = tuple(_field_signature_tuple(f) for f in fields(dc_class))
    # Compare lengths first -- catches both additions and removals.
    assert len(actual_fields) == len(expected_fields), (
        f"{class_name} has {len(actual_fields)} fields {actual_fields!r}, "
        f"expected {len(expected_fields)} {expected_fields!r}"
    )
    for actual, expected in zip(actual_fields, expected_fields):
        name_ok = actual[0] == expected[0]
        annotation_ok = actual[1] == expected[1]
        default_ok = actual[2] == expected[2] and actual[3] == expected[3]
        if not (name_ok and annotation_ok and default_ok):
            raise AssertionError(
                f"{class_name} field mismatch: actual={actual!r}, expected={expected!r}"
            )


def test_user_dataclass_fields_match_recon_doc():
    domain_module, _c01_module = _load_modules()
    _assert_dataclass_fields(domain_module.User, EXPECTED_USER_FIELDS, class_name="User")


def test_portfolio_dataclass_fields_match_recon_doc():
    domain_module, _c01_module = _load_modules()
    _assert_dataclass_fields(domain_module.Portfolio, EXPECTED_PORTFOLIO_FIELDS, class_name="Portfolio")


def test_holding_dataclass_has_no_silently_added_id_field():
    """Acceptance criterion: 'No id field was added to Holding or Transaction
    if they did not already have one.' Holding's fields are exactly
    portfolio_id, security_id, quantity, currency, exchange, symbol_suffix."""
    domain_module, _c01_module = _load_modules()
    holding_field_names = {f.name for f in fields(domain_module.Holding)}
    assert "id" not in holding_field_names, (
        f"Holding must not have an id field (it did not pre-STORY-2); got fields={holding_field_names!r}"
    )
    # And the full field list must match exactly.
    _assert_dataclass_fields(domain_module.Holding, EXPECTED_HOLDING_FIELDS, class_name="Holding")


def test_transaction_dataclass_has_no_silently_added_id_field():
    """Acceptance criterion: 'No id field was added to Holding or Transaction
    if they did not already have one.' Transaction's fields are exactly
    portfolio_id, kind, amount."""
    domain_module, _c01_module = _load_modules()
    transaction_field_names = {f.name for f in fields(domain_module.Transaction)}
    assert "id" not in transaction_field_names, (
        f"Transaction must not have an id field (it did not pre-STORY-2); got fields={transaction_field_names!r}"
    )
    _assert_dataclass_fields(domain_module.Transaction, EXPECTED_TRANSACTION_FIELDS, class_name="Transaction")


# ---------- 3. domain.py imports ONLY standard-library modules ------------

ALLOWED_DOMAIN_IMPORTS = {"dataclasses", "decimal", "re", "typing", "uuid", "datetime", "enum"}


def test_domain_module_imports_only_stdlib():
    """Acceptance criterion: 'src/domain.py imports only standard-library modules --
    it contains no import of src.infrastructure, any src.components module,
    or any third-party package.'"""
    src_path = Path("src/domain.py")
    assert src_path.exists(), "src/domain.py must exist"
    source = src_path.read_text()
    tree = ast.parse(source)

    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_DOMAIN_IMPORTS:
                    forbidden.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            # Check the top-level package: 'src.infrastructure' -> 'src'.
            top = node.module.split(".")[0]
            if top not in ALLOWED_DOMAIN_IMPORTS:
                forbidden.append(f"line {node.lineno}: from {node.module} import ...")

    assert not forbidden, (
        f"src/domain.py imports non-stdlib modules: {forbidden!r}"
    )


# ---------- 4. c01 has NO duplicate definitions of the 8 names -------------

def test_c01_has_no_module_level_definitions_of_the_four_dataclasses():
    """Acceptance criterion: c01 must no longer define the four dataclasses
    at module level. (The re-export block brings them in, but a future
    regression that re-introduces a class statement must be caught.)"""
    src_path = Path("src/components/c01_user_portfolio.py")
    source = src_path.read_text()
    tree = ast.parse(source)

    forbidden = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in (
            "User", "Portfolio", "Holding", "Transaction",
        ):
            forbidden.append(f"line {node.lineno}: class {node.name}")

    assert not forbidden, (
        f"src/components/c01_user_portfolio.py must not define these dataclasses "
        f"at module level (they now live in src/domain.py): {forbidden!r}"
    )


def test_c01_has_no_module_level_assignments_of_the_four_table_constants():
    """Acceptance criterion: c01 must no longer define the four table
    constants at module level. (A future regression that re-introduces
    a module-level assignment for any of them must be caught.)"""
    src_path = Path("src/components/c01_user_portfolio.py")
    source = src_path.read_text()
    tree = ast.parse(source)

    forbidden = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "USERS_TABLE", "PORTFOLIOS_TABLE",
                    "HOLDINGS_TABLE", "TRANSACTIONS_TABLE",
                ):
                    forbidden.append(f"line {node.lineno}: assignment to {target.id}")

    assert not forbidden, (
        f"src/components/c01_user_portfolio.py must not assign these table-name "
        f"constants at module level (they now live in src/domain.py): {forbidden!r}"
    )


# ---------- 5. validate_stock_symbol re-export for backward compatibility ---

def test_validate_stock_symbol_is_importable_from_c01_and_is_the_domain_function():
    """The existing tests/components/test_user_portfolio.py imports
    `validate_stock_symbol` from `components.c01_user_portfolio`.
    STORY-2 requires that file to pass with zero modifications. So
    c01 must re-export `validate_stock_symbol` as the same object
    as in domain, and it must be callable -- otherwise the import
    statement in the existing test file fails at collection time."""
    domain_module, c01_module = _load_modules()

    c01_has_it = hasattr(c01_module, "validate_stock_symbol")
    assert c01_has_it, (
        "c01 must re-export `validate_stock_symbol` so that "
        "tests/components/test_user_portfolio.py's "
        "`from components.c01_user_portfolio import validate_stock_symbol` "
        "keeps working (the story requires that file to pass with "
        "zero modifications)."
    )

    c01_val = c01_module.validate_stock_symbol
    domain_val = domain_module.validate_stock_symbol
    assert c01_val is domain_val, (
        f"c01.validate_stock_symbol is not the same object as domain's: "
        f"c01={c01_val!r} domain={domain_val!r}"
    )

    # Callable sanity-check: must be a real function we can call with
    # a string and have it accept the input.
    c01_val("AAPL")  # must not raise


# ---------- 6. The existing c01 test file collects without ImportError -----

def test_existing_c01_test_file_collects_without_import_errors():
    """Acceptance criterion: 'The existing c01 test file passes with zero
    modifications to that file.' Run pytest --collect-only on
    tests/components/test_user_portfolio.py and assert it produces
    zero collection errors."""
    import subprocess
    result = subprocess.run(
        ["pytest", "--collect-only", "-q",
         "tests/components/test_user_portfolio.py"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert "ImportError" not in output, (
        f"Existing c01 test file failed to collect (ImportError must not appear).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ERROR" not in output or "no tests ran" not in output, (
        f"Existing c01 test file had collection-time errors.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    # Confirm at least one test was actually collected.
    assert "tests collected" in output or "test collected" in output, (
        f"Expected pytest to report at least one test collected.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )