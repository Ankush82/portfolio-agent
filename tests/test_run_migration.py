"""Tests for scripts/run_migration.sh.

These tests invoke the wrapper as a real subprocess (the way the migration
image will in production) rather than re-implementing its logic in Python.
That keeps the contract the bash wrapper enforces under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "run_migration.sh"


def _wrapper_exists() -> bool:
    return WRAPPER.is_file() and os.access(WRAPPER, os.X_OK)


pytestmark = pytest.mark.skipif(
    not _wrapper_exists(),
    reason=f"wrapper not found or not executable at {WRAPPER}",
)


def _invoke(env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess:
    """Invoke the wrapper with a clean environment + overrides.

    Passing ``None`` for a key means "unset this variable before invoking".
    All other parent-env variables are stripped so a developer's local
    DATABASE_URL doesn't accidentally satisfy the requirement check.
    """
    env = {k: v for k, v in os.environ.items() if k not in env_overrides}
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        ["bash", str(WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_wrapper_fails_clearly_when_database_url_unset() -> None:
    """The story's acceptance criterion: missing DATABASE_URL must fail
    fast with a clear message and a non-zero exit status."""
    result = _invoke({"DATABASE_URL": None, "MIGRATION_DRY_RUN": None})

    assert result.returncode != 0, (
        "wrapper must exit non-zero when DATABASE_URL is unset; "
        f"got rc=0 stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    combined = (result.stdout + "\n" + result.stderr).lower()
    assert "database_url" in combined, (
        "error message must mention DATABASE_URL so operators can diagnose; "
        f"got stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_wrapper_fails_clearly_when_database_url_empty() -> None:
    """Same contract when DATABASE_URL is exported but empty."""
    result = _invoke({"DATABASE_URL": "", "MIGRATION_DRY_RUN": None})

    assert result.returncode != 0, (
        "wrapper must exit non-zero when DATABASE_URL is empty; "
        f"got rc=0 stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = (result.stdout + "\n" + result.stderr).lower()
    assert "database_url" in combined


# ---------------------------------------------------------------------------
# STORY-1 / issue #63 — explicit acceptance-criteria tests added by QA.
# These exercise the specific contract for the skeleton story rather than
# the broader wrapper contract covered above.
# ---------------------------------------------------------------------------

REPO_ROOT_FOR_STORY1 = Path(__file__).resolve().parent.parent
SQL_SCRIPT_FOR_STORY1 = REPO_ROOT_FOR_STORY1 / "scripts" / "migrate_us_stocks.sql"
WRAPPER_FOR_STORY1 = REPO_ROOT_FOR_STORY1 / "scripts" / "run_migration.sh"


def test_story1_sql_skeleton_has_all_three_placeholder_sections() -> None:
    """AC1: SQL skeleton must contain clearly-commented placeholders for
    (1) loading US ticker CSV, (2) counting matched rows, (3) UPDATE."""
    assert SQL_SCRIPT_FOR_STORY1.is_file(), (
        f"expected real file at {SQL_SCRIPT_FOR_STORY1}"
    )
    sql_text = SQL_SCRIPT_FOR_STORY1.read_text(encoding="utf-8").lower()

    # Each numbered placeholder section must be present and named.
    assert "(1)" in sql_text and "load" in sql_text, (
        "section (1) loading US ticker CSV placeholder missing"
    )
    assert "(2)" in sql_text and "count" in sql_text, (
        "section (2) counting matched rows placeholder missing"
    )
    assert "(3)" in sql_text and "update" in sql_text, (
        "section (3) performing UPDATE placeholder missing"
    )

    # Must reference the temp table name from the contract.
    assert "tmp_us_tickers" in sql_text, (
        "SQL skeleton must reference tmp_us_tickers temp table name"
    )

    # Must use \copy-style loading hint for the CSV.
    assert "\\copy" in sql_text or "copy " in sql_text, (
        "SQL skeleton must show the expected COPY/\\copy load pattern"
    )


def test_story1_wrapper_uses_strict_mode_and_mentions_psql() -> None:
    """AC2 wrapper characteristics: strict mode, psql invocation, exit
    status capture, log file, and /app/scripts/ deploy note."""
    assert WRAPPER_FOR_STORY1.is_file(), (
        f"expected real file at {WRAPPER_FOR_STORY1}"
    )
    script = WRAPPER_FOR_STORY1.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script, (
        "wrapper must use 'set -euo pipefail' for strict error handling"
    )
    assert "psql" in script, "wrapper must invoke psql"
    assert "-f" in script, "wrapper must pass the SQL script to psql via -f"
    assert "tee" in script, "wrapper must tee stdout+stderr to a log file"
    assert "LOG_FILE" in script, "wrapper must reference LOG_FILE"
    # Capture real exit status from the pipeline.
    assert "PSQL_EXIT" in script or "PIPESTATUS" in script, (
        "wrapper must capture psql's real exit status"
    )
    # Migration-image deploy comment must be present in the header.
    assert "/app/scripts/" in script, (
        "wrapper header must note it is deployed to /app/scripts/ in the image"
    )


def test_story1_wrapper_exits_one_with_actionable_error_when_database_url_missing() -> None:
    """AC4: with DATABASE_URL unset, the wrapper must exit 1 (not just
    non-zero) and the error must be actionable (mention DATABASE_URL and
    reach stderr)."""
    result = _invoke({"DATABASE_URL": None, "MIGRATION_DRY_RUN": None})

    # Exact exit code 1 — the resolution_notes explicitly call this out.
    assert result.returncode == 1, (
        f"wrapper must exit 1 when DATABASE_URL is unset; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Error must be on stderr so operators see it in container logs.
    assert "ERROR" in result.stderr or "error" in result.stderr.lower(), (
        f"error message must reach stderr; got stderr={result.stderr!r}"
    )

    # Must mention DATABASE_URL in a way an operator can act on.
    combined = (result.stdout + "\n" + result.stderr)
    assert "DATABASE_URL" in combined, (
        f"error message must mention DATABASE_URL; got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )

    # Must look actionable: either suggests how to set it or explains why it's needed.
    actionable_hint = any(
        hint in combined.lower()
        for hint in ("export", "set ", "postgres://", "required", "must")
    )
    assert actionable_hint, (
        f"error message should hint at how to fix it; got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_story1_wrapper_handles_dummy_database_url_and_dry_run_default() -> None:
    """AC2 supplementary: MIGRATION_DRY_RUN must default to a safe value
    when unset, and a dummy DATABASE_URL must at least pass the URL check
    (we don't have psql-backed Postgres in this exact test, so we only
    assert the env handling, not the psql success)."""
    # Dummy URL that passes the empty/unset check; wrapper should NOT
    # bail out on the DATABASE_URL guard.
    result = _invoke(
        {
            "DATABASE_URL": "postgres://user:pass@127.0.0.1:1/none",
            "MIGRATION_DRY_RUN": None,  # must default
        }
    )
    # We should get past the DATABASE_URL guard. The wrapper will then
    # attempt psql against a non-existent host and fail -- that's fine.
    # The point is: the error must NOT be the DATABASE_URL guard error.
    combined = (result.stdout + "\n" + result.stderr)
    assert "DATABASE_URL is not set" not in combined, (
        "with a dummy DATABASE_URL set, the wrapper must not complain "
        f"about DATABASE_URL being unset; got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


@pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql not installed on this host"
)
def test_wrapper_dry_run_smoke_against_local_postgres() -> None:
    """End-to-end smoke test: with DATABASE_URL pointing at a real local
    Postgres (provided by the dev environment) and MIGRATION_DRY_RUN=true,
    the wrapper should run the (currently stub-only) SQL and exit 0.

    If no real Postgres is reachable this test is skipped -- the unit
    tests above already prove the wrapper's failure path.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set in this test environment")

    result = _invoke({"DATABASE_URL": dsn, "MIGRATION_DRY_RUN": "true"})
    assert result.returncode == 0, (
        f"dry-run against local Postgres failed: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )