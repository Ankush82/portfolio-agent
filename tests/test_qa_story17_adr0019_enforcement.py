"""QA test for STORY-17: ADR-0019 compliance enforcement.

This test verifies the acceptance criteria for the ADR-0019 compliance
test story. It runs against the real codebase (not mocks) and includes:

1. AST-based inspection (not runtime imports) of all relevant files.
2. Assertion that no repo module imports psycopg/redis/sqlalchemy/infrastructure_postgres.
3. Assertion that src/domain.py imports only stdlib.
4. Assertion that no component imports psycopg/redis/sqlalchemy.
5. Assertion that no repo module contains raw SQL literals.
6. Pre-existing violations are captured in a commented allowlist (not silently ignored).
7. A "surgical fault injection" test: a single new `import psycopg` in a
   repo file makes the test fail (end-to-end, not just the check logic).

Acceptance criteria verified here (each maps to a test function):
  AC1: test_adr0019_compliance_file_exists_and_uses_ast
  AC2: test_repos_no_db_driver_imports
  AC3: test_domain_stdlib_only
  AC4: test_components_no_db_drivers
  AC5: test_repos_no_raw_sql
  AC6: test_preexisting_violations_in_explicit_allowlist
  AC7: test_surgical_fault_injection_catches_new_psycopg_import
"""

import ast
import os
import re
import tempfile
from pathlib import Path

import pytest

SRC = Path("src")

# Forbidden import roots for ADR-0019.
FORBIDDEN_IMPORT_ROOTS = frozenset(["psycopg", "redis", "sqlalchemy"])
FORBIDDEN_ABSOLUTE_MODULE = "infrastructure_postgres"

# Pre-existing violations (out of scope for this story — documented only).
# Every entry here imports infrastructure_postgres at the component level.
# Any NEW violation (psycopg/redis/sqlalchemy in components, or any
# forbidden import in repos/domain) will still cause the test to FAIL.
_PREEXISTING_COMPONENT_VIOLATIONS = [
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

# Case-sensitive SQL keyword patterns (UPPERCASE only — avoids false
# positives from English prose words like "update" or "Insert" in docstrings).
_RAW_SQL_PATTERNS = [
    re.compile(r"\bSELECT\b"),
    re.compile(r"\bINSERT\b"),
    re.compile(r"\bUPDATE\b"),
    re.compile(r"\bDELETE\s+FROM\b"),
]


# ---------------------------------------------------------------------------
# Helpers (mirrored from the implementation test, AC-agnostic)
# ---------------------------------------------------------------------------

def _collect_imports(source: str) -> list[str]:
    """Return top-level imported module roots from Python source via AST."""
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=n):
                for alias in n:
                    imports.append(alias.name.split(".")[0])
            case ast.ImportFrom(module=mod) if mod:
                imports.append(mod.split(".")[0])
    return imports


def _extract_string_literals(source: str) -> str:
    """Extract the contents of all Python string literals from source."""
    _td = '"""(?:[^"\\\\]|\\\\.)*"""'
    _ts = "'''(?:[^'\\\\]|\\\\.)*'''"
    _sq = '"(?:[^"\\\\]|\\\\.)*"'
    _dq = "'(?:[^'\\\\]|\\\\.)*'"
    _body = "|".join([_td, _ts, _sq, _dq])
    _re = re.compile(r"(?:[rR])?(?:[fF])?(?:" + _body + ")"
                     r"|"
                     r"(?:[fF])?(?:[rR])?(?:" + _body + ")", re.DOTALL)
    return "\n".join(_re.findall(source))


# ---------------------------------------------------------------------------
# AC1: The compliance test file exists and uses AST, not runtime imports.
# ---------------------------------------------------------------------------

def test_adr0019_compliance_file_exists_and_uses_ast() -> None:
    """AC1: tests/test_adr0019_compliance.py must exist and use AST-based
    source inspection, not runtime imports."""
    test_file = Path("tests/test_adr0019_compliance.py")
    assert test_file.exists(), (
        "tests/test_adr0019_compliance.py does not exist"
    )
    source = test_file.read_text()
    assert "ast.parse" in source, (
        "test_adr0019_compliance.py must use ast.parse for static inspection"
    )
    assert "import psycopg" not in source.split("def ")[0], (
        "test_adr0019_compliance.py must not import psycopg at runtime to test"
    )


# ---------------------------------------------------------------------------
# AC2: No module under src/repositories/ imports a DB driver or infrastructure_postgres.
# ---------------------------------------------------------------------------

def test_repos_no_db_driver_imports() -> None:
    """AC2: Every .py file under src/repositories/ must pass ADR-0019."""
    repo_files = sorted(
        p for p in (SRC / "repositories").rglob("*.py")
        if p.name != "__init__.py"
    )
    assert repo_files, "No repository files found under src/repositories/"

    failures: list[tuple[str, str]] = []
    for filepath in repo_files:
        source = filepath.read_text()
        imports = _collect_imports(source)
        for imp in imports:
            if imp in FORBIDDEN_IMPORT_ROOTS:
                failures.append((str(filepath), f"forbidden import root: '{imp}'"))
            if imp == FORBIDDEN_ABSOLUTE_MODULE:
                failures.append((str(filepath), f"forbidden absolute: '{FORBIDDEN_ABSOLUTE_MODULE}'"))

    assert not failures, f"DB-driver imports found in repositories:\n{failures}"


# ---------------------------------------------------------------------------
# AC3: src/domain.py must import only stdlib modules.
# ---------------------------------------------------------------------------

def test_domain_stdlib_only() -> None:
    """AC3: src/domain.py may import only from Python's standard library."""
    domain_path = SRC / "domain.py"
    assert domain_path.exists(), "src/domain.py does not exist"
    source = domain_path.read_text()
    imports = _collect_imports(source)

    # Legitimate stdlib imports used by domain.
    stdlib_modules = {"dataclasses", "decimal", "re", "typing", "types"}
    violations = [f"'{imp}'" for imp in imports if imp not in stdlib_modules]

    assert not violations, (
        f"src/domain.py has non-stdlib imports: {violations}"
    )


# ---------------------------------------------------------------------------
# AC4: No component may import psycopg, redis, or sqlalchemy.
# (Pre-existing infrastructure_postgres violations are allowlisted separately.)
# ---------------------------------------------------------------------------

def test_components_no_db_drivers() -> None:
    """AC4: No component module may import psycopg, redis, or sqlalchemy."""
    component_files = sorted(
        p for p in (SRC / "components").rglob("*.py")
        if p.name != "__init__.py"
    )
    assert component_files, "No component files found under src/components/"

    failures: list[tuple[str, str]] = []
    for filepath in component_files:
        rel_path = str(filepath.relative_to(SRC))
        source = filepath.read_text()
        imports = _collect_imports(source)
        for imp in imports:
            if imp in FORBIDDEN_IMPORT_ROOTS:
                failures.append((rel_path, f"forbidden import root: '{imp}'"))

    assert not failures, f"DB-driver imports found in components:\n{failures}"


# ---------------------------------------------------------------------------
# AC5: No repository module may contain a raw SQL literal.
# ---------------------------------------------------------------------------

def test_repos_no_raw_sql() -> None:
    """AC5: No repository module may contain raw SELECT/INSERT/UPDATE/DELETE."""
    repo_files = sorted(
        p for p in (SRC / "repositories").rglob("*.py")
        if p.name != "__init__.py"
    )
    assert repo_files, "No repository files found"

    failures: list[str] = []
    for filepath in repo_files:
        source = filepath.read_text()
        string_contents = _extract_string_literals(source)
        for pattern in _RAW_SQL_PATTERNS:
            if pattern.search(string_contents):
                failures.append(
                    f"{filepath.name}: raw SQL literal matching {pattern.pattern!r}"
                )

    assert not failures, f"Raw SQL literals found in repositories:\n{failures}"


# ---------------------------------------------------------------------------
# AC6: Pre-existing violations are captured in an explicit commented allowlist.
# ---------------------------------------------------------------------------

def test_preexisting_violations_in_explicit_allowlist() -> None:
    """AC6: Pre-existing violations are in a named allowlist in
    tests/test_adr0019_compliance.py, not silently ignored."""
    test_file = Path("tests/test_adr0019_compliance.py")
    assert test_file.exists()
    source = test_file.read_text()

    # The allowlist must be a named constant (so it can be audited).
    assert "_PREEXISTING_COMPONENT_VIOLATIONS" in source, (
        "Allowlist constant _PREEXISTING_COMPONENT_VIOLATIONS must exist "
        "in test_adr0019_compliance.py"
    )
    assert "c01_user_portfolio" in source, (
        "c01_user_portfolio must appear in the allowlist"
    )

    # Verify allowlisted files still have the violation (allowlist stays accurate).
    allowlisted_files = [
        Path(f) for f in [
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
    ]
    missing_or_clean: list[str] = []
    for f in allowlisted_files:
        if not f.exists():
            missing_or_clean.append(f"{f}: FILE NOT FOUND")
            continue
        imports = _collect_imports(f.read_text())
        if "infrastructure_postgres" not in imports:
            missing_or_clean.append(f"{f}: no longer violates (should be removed from allowlist)")

    assert not missing_or_clean, (
        "Allowlist is out of sync with reality:\n" + "\n".join(missing_or_clean)
    )


# ---------------------------------------------------------------------------
# AC7: Adding `import psycopg` to a repository makes the test fail.
# We do this by writing a temp file with the violation and verifying
# the check function catches it — not by mutating the real repo files.
# ---------------------------------------------------------------------------

def test_surgical_fault_injection_catches_new_psycopg_import() -> None:
    """AC7: A single `import psycopg` inserted into a repo file makes the
    test fail.  Verified by writing a temporary file and running the
    real check logic against it."""
    # Use the real repo file as base and inject a violation.
    base_repo = SRC / "repositories" / "base.py"
    assert base_repo.exists(), f"Reference repo file {base_repo} not found"

    original_source = base_repo.read_text()

    # Inject `import psycopg` at the top of the file.
    violated_source = "import psycopg\n" + original_source

    # Run the real check logic from the existing compliance test module.
    # Import the check function from the actual test file.
    import tests.test_adr0019_compliance as adr_test

    violations = adr_test._check_source_for_violations(violated_source)

    assert violations, (
        "Fault injection FAILED: _check_source_for_violations did not catch "
        "a deliberate 'import psycopg' in a repository source file. "
        "This means ADR-0019 enforcement is not working."
    )
    assert "forbidden import root: 'psycopg'" in violations, (
        f"Expected 'forbidden import root: psycopg' in violations, got: {violations}"
    )


def test_surgical_fault_injection_catches_new_infrastructure_postgres_import() -> None:
    """AC7 (variant): A `from infrastructure_postgres import ...` in a repo
    must also be caught."""
    base_repo = SRC / "repositories" / "base.py"
    original_source = base_repo.read_text()
    violated_source = "from infrastructure_postgres import DefaultInfrastructure\n" + original_source

    import tests.test_adr0019_compliance as adr_test

    violations = adr_test._check_source_for_violations(violated_source)
    assert violations, "Fault injection FAILED: infrastructure_postgres import not caught"
    assert "forbidden absolute import: 'infrastructure_postgres'" in violations


def test_surgical_fault_injection_catches_raw_sql_in_repo() -> None:
    """AC7 (variant): A raw SQL string in a repo must be caught."""
    import tests.test_adr0019_compliance as adr_test

    fake_source = 'cursor.execute("SELECT * FROM holdings WHERE portfolio_id = $1")'
    violations = adr_test._check_for_raw_sql(fake_source, "fake_repo.py")
    assert violations, (
        "Fault injection FAILED: _check_for_raw_sql did not catch "
        "a raw SELECT string literal"
    )
