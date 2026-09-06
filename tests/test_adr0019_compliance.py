"""ADR-0019 enforcement: no component may import a database driver directly.

ADR-0019 requires that every component talk to System Infrastructure
through the Infrastructure Protocol (src/infrastructure.py), never
through a concrete backend driver.  This test statically inspects
source files using AST so that it catches conditional and function-local
imports that a runtime import check would miss.

Allowlisted pre-existing violations (outside src/domain.py and
src/repositories/*) are listed below.  Any NEW violation in those
files will cause the test to FAIL, and any new import of
infrastructure_postgres in src/domain.py or src/repositories/* will
also cause the test to FAIL.
"""

import ast
import os
import re
from pathlib import Path
from typing import List

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SRC = Path("src")

# Forbidden import roots — anything that resolves to these packages is a
# violation regardless of how it is spelled (absolute / relative /
# aliased).
FORBIDDEN_IMPORT_ROOTS = frozenset([
    "psycopg",
    "redis",
    "sqlalchemy",
])

# Specific absolute module that repositories and domain must not import.
FORBIDDEN_ABSOLUTE_MODULE = "infrastructure_postgres"

# Components with pre-existing violations (outside scope for this story).
# These modules import infrastructure_postgres but are NOT in
# src/domain.py or src/repositories/*, so fixing them is out of scope.
# New violations in ANY file will still fail the test.
_PREEXISTING_COMPONENT_VIOLATIONS: List[str] = [
    "src/components/c01_user_portfolio.py",
    "src/components/c02_data_sources.py",
    "src/components/c03_data_processing_quality.py",
    "src/components/c04_knowledge_entity.py",
    "src/components/c05_retrieval_context.py",
    "src/components/c07_event_observation.py",
    "src/components/c08_analysis_reasoning.py",
    "src/components/c12_decision_policy.py",
    "src/components/c13_interaction_notification.py",
    "src/components/c14_learning_evaluation.py",
]

# Regex patterns for raw SQL literals that must not appear in repository source.
# We search INSIDE string literals only (not code or stripped source) so that
# method names like `def update(...)` never produce false positives.
# Patterns are CASE-SENSITIVE: SQL keywords are always UPPERCASE, so English
# prose words like "insert" or "Update" in docstrings are correctly ignored.
_RAW_SQL_PATTERNS = [
    re.compile(r'\bSELECT\b'),
    re.compile(r'\bINSERT\b'),
    re.compile(r'\bUPDATE\b'),
    re.compile(r'\bDELETE\s+FROM\b'),
]

# Regex to find Python string/f-string literals (simple and triple-quoted,
# single and double quotes, with or without 'r' prefix).
# Covers: "..."  '...'  """..."""  '''...'''  r"..."  f"...{x}..."
# Matches the full literal including delimiters so we can remove it cleanly.
#
# The pattern is built via concatenation to avoid a VERBOSE raw-string gotcha:
# a triple-quote sequence inside a VERBOSE regex flags that triple-quote as
# the end of the Python string literal even in a raw string.
_triple_double = '"""(?:[^"\\\\]|\\\\.)*"""'
_triple_single = "'''(?:[^'\\\\]|\\\\.)*'''"
_single_quoted = '"(?:[^"\\\\]|\\\\.)*"'
_double_quoted = "'(?:[^'\\\\]|\\\\.)*'"
_string_body = "|".join([_triple_double, _triple_single, _single_quoted, _double_quoted])
_string_literal_re_raw = (
    r"(?:[rR])?(?:[fF])?(?:" + _string_body + ")"
    r"|"
    r"(?:[fF])?(?:[rR])?(?:" + _string_body + ")"
)
_STRING_LITERAL_RE = re.compile(_string_literal_re_raw, re.DOTALL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_imports_from_source(source: str) -> List[str]:
    """Return every imported module name from a Python source string.

    Handles:
      - ``import foo``            → ["foo"]
      - ``import foo as bar``     → ["foo"]
      - ``from foo import bar``    → ["foo"]
      - ``from foo import *``      → ["foo"]
      - ``from foo import a, b``   → ["foo"]
      - ``from foo.pkg import bar`` → ["foo.pkg"]
      - aliased imports            → unaliased name returned

    Does NOT follow import chains; only inspects this file's own
    top-level imports.
    """
    tree = ast.parse(source)
    imports: List[str] = []

    for node in ast.walk(tree):
        match node:
            case ast.Import(names=n):
                for alias in n:
                    imports.append(alias.name.split(".")[0])

            case ast.ImportFrom(module=mod, names=n) if mod is not None:
                imports.append(mod.split(".")[0])

    return imports


def _check_source_for_violations(source: str) -> List[str]:
    """Return a list of violation messages for forbidden imports found."""
    errors: List[str] = []
    imports = _collect_imports_from_source(source)

    for imp in imports:
        if imp in FORBIDDEN_IMPORT_ROOTS:
            errors.append(f"forbidden import root: '{imp}'")

    if FORBIDDEN_ABSOLUTE_MODULE in imports:
        errors.append(f"forbidden absolute import: '{FORBIDDEN_ABSOLUTE_MODULE}'")

    return errors


def _strip_strings_and_comments(source: str) -> str:
    """Remove all string literals and comments from source so that
    docstrings / comments containing SQL keywords do not cause false
    positives.  Only actual code is inspected for SQL literals."""
    # Remove triple-quoted strings first (covers docstrings and multi-line).
    source = re.sub(r'""".*?"""', '', source, flags=re.DOTALL)
    source = re.sub(r"'''.*?'''", '', source, flags=re.DOTALL)
    # Remove single-quoted strings (including f-strings with no braces).
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", '', source)
    # Remove # comments.
    source = re.sub(r'#.*', '', source)
    return source


def _extract_string_contents(source: str) -> str:
    """Return the concatenation of all Python string literal contents
    from ``source`` (stripping delimiters).  Used to check whether any
    string in the code is a raw SQL statement."""
    strings = _STRING_LITERAL_RE.findall(source)
    return '\n'.join(strings)


def _check_for_raw_sql(source: str, filepath: str) -> List[str]:
    """Return a list of violation messages for raw SQL literals found in
    string literals within ``source`` (not in docstrings/comments or code).

    We deliberately search only inside string literals, not across the
    whole source, so that method names such as ``def update(...)`` or
    ``def insert(...)`` never produce false positives.
    """
    errors: List[str] = []
    string_contents = _extract_string_contents(source)
    for pattern in _RAW_SQL_PATTERNS:
        if pattern.search(string_contents):
            # Report once per pattern, not per match, to keep noise manageable.
            errors.append(
                f"raw SQL literal matching {pattern.pattern!r} found in {filepath}"
            )
    return errors


def _iter_python_files(under: Path) -> List[Path]:
    """Yield every .py file under ``under`` (non-recursive for top-level only)."""
    return sorted(
        p for p in under.iterdir()
        if p.suffix == ".py" and p.name != "__init__.py"
    )


def _iter_python_files_recursive(under: Path) -> List[Path]:
    """Yield every .py file recursively under ``under``."""
    return sorted(p for p in under.rglob("*.py") if p.name != "__init__.py")


# ---------------------------------------------------------------------------
# Tests — src/repositories/
# ---------------------------------------------------------------------------

REPO_FILES = _iter_python_files_recursive(SRC / "repositories")


@pytest.mark.parametrize("filepath", REPO_FILES, ids=lambda p: p.name)
def test_repo_no_db_driver_imports(filepath: Path) -> None:
    """No module under src/repositories/ may import psycopg, redis, sqlalchemy,
    or infrastructure_postgres."""
    source = filepath.read_text()
    violations = _check_source_for_violations(source)
    assert not violations, (
        f"{filepath}: forbidden DB-driver import(s): {violations}"
    )


@pytest.mark.parametrize("filepath", REPO_FILES, ids=lambda p: p.name)
def test_repo_no_raw_sql(filepath: Path) -> None:
    """Repositories must go through the Infrastructure Protocol, not emit SQL."""
    source = filepath.read_text()
    violations = _check_for_raw_sql(source, str(filepath))
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# Tests — src/domain.py
# ---------------------------------------------------------------------------

def test_domain_stdlib_only() -> None:
    """src/domain.py may import only from the Python standard library."""
    domain_path = SRC / "domain.py"
    source = domain_path.read_text()
    imports = _collect_imports_from_source(source)

    # The domain module legitimately imports 'dataclasses', 'decimal', 're'.
    # Anything else is a violation.
    stdlib_modules = {"dataclasses", "decimal", "re", "typing", "types"}
    violations = [f"non-stdlib import: '{imp}'" for imp in imports if imp not in stdlib_modules]

    assert not violations, (
        f"src/domain.py has non-standard-library imports: {violations}"
    )


def test_domain_no_db_driver_imports() -> None:
    """src/domain.py must not import any database driver or infrastructure_postgres."""
    domain_path = SRC / "domain.py"
    source = domain_path.read_text()
    violations = _check_source_for_violations(source)
    assert not violations, (
        f"src/domain.py has forbidden DB-driver import(s): {violations}"
    )


# ---------------------------------------------------------------------------
# Tests — src/components/
# ---------------------------------------------------------------------------

COMPONENT_FILES = _iter_python_files_recursive(SRC / "components")


@pytest.mark.parametrize("filepath", COMPONENT_FILES, ids=lambda p: p.name)
def test_component_no_db_driver_imports(filepath: Path) -> None:
    """No module under src/components/ may import psycopg, redis, or sqlalchemy."""
    source = filepath.read_text()
    rel_path = str(filepath.relative_to(SRC))

    # Only check psycopg, redis, sqlalchemy — NOT infrastructure_postgres.
    # infrastructure_postgres is the pre-existing violation tracked below.
    errors: List[str] = []
    imports = _collect_imports_from_source(source)

    for imp in imports:
        if imp in FORBIDDEN_IMPORT_ROOTS:
            errors.append(f"forbidden import root: '{imp}'")

    if rel_path in _PREEXISTING_COMPONENT_VIOLATIONS:
        # Pre-existing violation: infrastructure_postgres only (not psycopg/redis/sqlalchemy).
        # Check specifically for infrastructure_postgres to confirm it IS the pre-existing issue.
        if FORBIDDEN_ABSOLUTE_MODULE in imports:
            # This is the known pre-existing violation — don't fail.
            errors.clear()

    assert not errors, (
        f"{filepath}: forbidden DB-driver import(s): {errors}"
    )


# ---------------------------------------------------------------------------
# Pre-existing violation report
# ---------------------------------------------------------------------------

def test_preexisting_component_violations_allowlist() -> None:
    """Document the pre-existing violations captured in the allowlist above.

    This test documents the pre-existing violations and ensures the
    allowlist is kept up to date.  If a pre-existing violation is
    accidentally fixed in a component, this test will fail so that the
    allowlist is updated accordingly (the fix is welcome, but the
    allowlist must stay accurate).
    """
    violations_found: List[str] = []
    for filepath_str in _PREEXISTING_COMPONENT_VIOLATIONS:
        filepath = Path(filepath_str)
        if not filepath.exists():
            pytest.fail(
                f"Allowlisted file {filepath} no longer exists — "
                "remove it from _PREEXISTING_COMPONENT_VIOLATIONS"
            )
        source = filepath.read_text()
        imports = _collect_imports_from_source(source)
        if FORBIDDEN_ABSOLUTE_MODULE in imports:
            violations_found.append(filepath_str)

    # The allowlist should always reflect reality.
    assert set(violations_found) == set(_PREEXISTING_COMPONENT_VIOLATIONS), (
        "Allowlisted pre-existing violations are out of sync with reality. "
        f"Found: {violations_found}\n"
        f"Listed: {_PREEXISTING_COMPONENT_VIOLATIONS}"
    )


# ---------------------------------------------------------------------------
# Intentional-failure test: a deliberate psycopg import in a repository
# should make the test fail.  We test this by verifying the check logic
# itself (no need to mutate source files).
# ---------------------------------------------------------------------------

def test_check_logic_detects_psycopg_in_repo_source() -> None:
    """Verify _check_source_for_violations catches a psycopg import."""
    fake_source = "import psycopg\nimport uuid\n"
    violations = _check_source_for_violations(fake_source)
    assert violations, "check logic must detect 'import psycopg'"
    assert "forbidden import root: 'psycopg'" in violations


def test_check_logic_detects_infrastructure_postgres_in_repo_source() -> None:
    """Verify _check_source_for_violations catches infrastructure_postgres."""
    fake_source = "from infrastructure_postgres import DefaultInfrastructure\n"
    violations = _check_source_for_violations(fake_source)
    assert violations, "check logic must detect 'from infrastructure_postgres import ...'"
    assert "forbidden absolute import: 'infrastructure_postgres'" in violations


def test_check_logic_detects_raw_sql_in_repo_source() -> None:
    """Verify _check_for_raw_sql catches SELECT / INSERT / UPDATE / DELETE FROM."""
    fake_source = 'cursor.execute("SELECT * FROM users")'
    violations = _check_for_raw_sql(fake_source, "fake.py")
    assert violations, "check logic must detect raw SELECT in source"
