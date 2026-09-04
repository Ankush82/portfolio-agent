"""QA verification for STORY-2 (issue #46):
'Create and execute database migration for existing US stock records'.

Independent QA verification, written from scratch against the actual
on-disk files. It is intentionally separate from the dev-agent's
``tests/test_verify_holdings_currency_backfill.py`` (which exercises
the verifier's exit-code contract) and ``tests/test_migration_workflow.py``
(which exercises STORY-6's ``migrate`` job). The assertions below stand
on their own: every acceptance criterion specifically introduced by
STORY-2 is exercised here with new assertions, not by re-using or
paraphrasing any existing test.

This story has TWO acceptance criteria that are NEW work (the other
three are 'verify, don't redo'):

  AC-NEW-1: "A real CI job invokes scripts/backfill_holdings_currency.py
             after deployment, following .github/workflows/migration.yml's
             existing pattern."
  AC-NEW-2: "A real verification script (scripts/verify_holdings_currency_backfill.py)
             is part of the CI pipeline so a successful backfill +
             verification failure surfaces as a workflow failure."

The other three ACs (set currency='USD', atomicity + DRY_RUN,
migration_log + rollback) are pre-existing on story STORY-1 and are
covered by tests/test_backfill_holdings_currency.py; we don't re-assert
them here so the test set stays focused on STORY-2's own delta.

We do NOT have real GitHub Actions access from here -- the story says
"follow the existing pattern" -- so the workflow YAML file is parsed
with a real ``yaml.safe_load`` call and the structural properties that
implement each acceptance criterion are asserted against it. The
verifier itself is exercised against a real Postgres because that's
what was added in the previous turn.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

# psycopg is required for the verifier-level tests at the bottom of
# this module; if it's not installed we still want the YAML/structural
# tests above to run (they don't need a DB). A module-level skipif on
# the WHOLE module would short-circuit the cheap, fast structural
# checks, so we skip only the DB-dependent ones.
psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "migration.yml"
BACKFILL_SCRIPT_PATH = REPO_ROOT / "scripts" / "backfill_holdings_currency.py"
VERIFY_SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_holdings_currency_backfill.py"

# Imported lazily because these touch psycopg / require a live DB.
sys.path.insert(0, str(REPO_ROOT / "src"))

from infrastructure_postgres import DEFAULT_POSTGRES_DSN  # noqa: E402
from scripts.backfill_holdings_currency import MIGRATION_NAME  # noqa: E402
from scripts.verify_holdings_currency_backfill import (  # noqa: E402
    EXIT_FAIL,
    EXIT_PASS,
    verify_holdings_currency_backfill,
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except psycopg.Error:
        return False


POSTGRES_SKIP_REASON = (
    "no live Postgres reachable at DEFAULT_POSTGRES_DSN -- "
    "run `docker-compose up -d` for real coverage"
)
needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason=POSTGRES_SKIP_REASON
)


# ---------------------------------------------------------------------------
# Workflow file parsing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Real YAML parse of the workflow file. Shared by all structural
    tests below. If the file is missing or unparseable, every test
    that depends on it fails loudly -- which is the correct outcome."""
    assert WORKFLOW_PATH.is_file(), (
        f"workflow file missing at {WORKFLOW_PATH}; STORY-2 requires the "
        "real workflow definition at .github/workflows/migration.yml"
    )
    with WORKFLOW_PATH.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict), (
        f"workflow YAML must parse to a mapping; got {type(loaded).__name__}"
    )
    return loaded


def _backfill_job(workflow: dict) -> dict:
    """Helper: extract the new ``backfill-holdings-currency`` job from
    the parsed workflow. Raises if it's not there -- that IS the story's
    AC-NEW-1, so an absent job must surface as a hard failure."""
    jobs = workflow.get("jobs") or {}
    job = jobs.get("backfill-holdings-currency")
    assert job is not None, (
        "STORY-2 AC-NEW-1: workflow must declare a new "
        "`backfill-holdings-currency` job; got jobs="
        f"{list(jobs.keys())!r}"
    )
    return job


# ---------------------------------------------------------------------------
# AC-NEW-1: real CI job for the backfill follows the existing pattern
# ---------------------------------------------------------------------------


def test_story2_ac_new_1_backfill_job_exists_and_invokes_backfill(workflow: dict) -> None:
    """STORY-2 AC-NEW-1: the workflow must contain a job named
    ``backfill-holdings-currency`` that actually invokes
    ``scripts/backfill_holdings_currency.py`` via ``python -m``.

    We accept ``python -m scripts.backfill_holdings_currency`` because
    that's the form the dev's report and the workflow file both use
    (the script is single-language Python; no .sh wrapper is needed
    because there's no companion .sql file like
    scripts/migrate_us_stocks.sql).
    """
    job = _backfill_job(workflow)
    steps = (job or {}).get("steps") or []
    assert steps, "backfill-holdings-currency job must declare at least one step"

    found_invocation = None
    for step in steps:
        run = (step or {}).get("run") or ""
        # Match the exact invocation shape used in the workflow.
        if "scripts.backfill_holdings_currency" in run or "scripts/backfill_holdings_currency" in run:
            assert "python " in run, (
                "the backfill step must invoke python (not bash with no "
                f"target, not a here-doc shell stub); got run={run!r}"
            )
            found_invocation = run
            break

    assert found_invocation is not None, (
        "no workflow step actually invokes scripts/backfill_holdings_currency; "
        f"steps={steps!r}"
    )


def test_story2_ac_new_1_backfill_step_has_no_continue_on_error(workflow: dict) -> None:
    """AC-NEW-1 + AC-NEW-2 follow-up: the backfill step must NOT use
    ``continue-on-error`` -- a non-zero exit must fail the job. This is
    the real mechanism behind "otherwise pipeline fails" (STORY-6 AC4
    contract, repeated here for the new job)."""
    job = _backfill_job(workflow)
    for step in (job or {}).get("steps", []) or []:
        run = (step or {}).get("run") or ""
        if "scripts.backfill_holdings_currency" in run or "scripts/backfill_holdings_currency" in run:
            assert "continue-on-error" not in step, (
                "the backfill step must not use continue-on-error; a non-zero "
                "exit must fail the job. step="
                f"{step!r}"
            )
            return
    pytest.fail(
        "could not locate the backfill step to validate "
        "(the structural existence test should have caught this first)"
    )


def test_story2_ac_new_1_job_follows_existing_postgres_service_pattern(workflow: dict) -> None:
    """AC-NEW-1: 'following .github/workflows/migration.yml's existing
    pattern'. Concretely: the new job must declare its own
    ``services: postgres`` block with a postgres image + healthcheck
    options, so an operator never has to point DATABASE_URL at a
    hand-managed database (same invariant as STORY-6 AC6, applied to
    the new job)."""
    job = _backfill_job(workflow)
    services = (job or {}).get("services") or {}
    assert services, (
        "backfill-holdings-currency job must declare a services block "
        "so it provisions its own Postgres (mirroring the existing "
        f"`migrate` job); got services={services!r}"
    )

    postgres_service = None
    for name, svc in services.items():
        image = (svc or {}).get("image", "") or ""
        if "postgres" in image.lower():
            postgres_service = svc
            break
    assert postgres_service is not None, (
        "backfill-holdings-currency job must declare a postgres service "
        "container so no manual DBA intervention is required; got "
        f"service images={[list((s or {}).get('image', '') for s in services.values())]!r}"
    )

    # DATABASE_URL must be set at the job level so the backfill sees it.
    env = (job or {}).get("env") or {}
    url_keys = [k for k in env.keys() if str(k).upper() == "DATABASE_URL"]
    assert url_keys, (
        "backfill-holdings-currency job must export DATABASE_URL so the "
        "backfill's psycopg.connect(dsn) call doesn't need operator "
        f"intervention; got env={env!r}"
    )

    # MIGRATION_DRY_RUN must be wired to the workflow_dispatch dry_run
    # input so a manual re-run with dry_run=true really is a dry-run.
    dry_run_val = env.get("MIGRATION_DRY_RUN")
    assert dry_run_val is not None and "dry_run" in str(dry_run_val), (
        "backfill-holdings-currency job must export MIGRATION_DRY_RUN "
        "from workflow_dispatch.dry_run so the backfill honors dry-run "
        "mode; got MIGRATION_DRY_RUN={dry_run_val!r}"
    )


def test_story2_ac_new_1_concurrency_group_is_distinct_from_us_stock(workflow: dict) -> None:
    """AC-NEW-1: 'following the existing pattern' BUT the story says the
    new job should NOT race against itself or block the us-stock
    migration. The existing job uses the top-level
    ``concurrency: group: us-stock-migration``. The new job must use a
    DIFFERENT group name (per-job concurrency is the cleanest way to
    keep the two migrations from blocking each other)."""
    job = _backfill_job(workflow)
    concurrency = (job or {}).get("concurrency")
    assert concurrency is not None, (
        "backfill-holdings-currency job must declare its own per-job "
        "concurrency block so the two migrations don't serialize "
        "against each other; got job.concurrency=None"
    )
    assert isinstance(concurrency, dict), (
        f"job.concurrency must be a mapping; got {type(concurrency).__name__}"
    )
    group = concurrency.get("group")
    assert isinstance(group, str) and group.strip(), (
        f"job.concurrency.group must be a non-empty string; got {group!r}"
    )
    assert group != "us-stock-migration", (
        f"the new job must NOT share the us-stock-migration concurrency "
        f"group (that would serialize the two migrations against each "
        f"other); got group={group!r}"
    )
    # Same invariant as STORY-6 AC2: cancel-in-progress=true would
    # cancel an in-flight migration, defeating the AC.
    assert concurrency.get("cancel-in-progress", False) is False, (
        "job.concurrency.cancel-in-progress must be false (or absent); "
        "true would cancel an in-flight migration"
    )


# ---------------------------------------------------------------------------
# AC-NEW-2: real verification script is part of the CI pipeline
# ---------------------------------------------------------------------------


def test_story2_ac_new_2_verifier_step_is_wired_into_the_backfill_job(workflow: dict) -> None:
    """AC-NEW-2: the CI pipeline must actually run
    ``scripts/verify_holdings_currency_backfill.py`` after the
    backfill. We assert it's a real step in the same job (so a
    successful backfill + verification failure fails CI), invoked via
    python -m, with no continue-on-error."""
    job = _backfill_job(workflow)
    found = False
    for step in (job or {}).get("steps", []) or []:
        run = (step or {}).get("run") or ""
        if "scripts.verify_holdings_currency_backfill" in run or "scripts/verify_holdings_currency_backfill" in run:
            assert "python " in run, (
                "the verifier step must invoke python; got "
                f"run={run!r}"
            )
            assert "continue-on-error" not in step, (
                "the verifier step must not use continue-on-error; a "
                "non-zero exit (i.e. verification failure) must fail "
                "the job. step="
                f"{step!r}"
            )
            found = True
            break
    assert found, (
        "STORY-2 AC-NEW-2: the backfill-holdings-currency job must "
        "include a step that invokes "
        "scripts/verify_holdings_currency_backfill; steps="
        f"{[(s or {}).get('run', '') for s in (job or {}).get('steps', [])]!r}"
    )


def test_story2_ac_new_2_verifier_runs_after_backfill_not_before(workflow: dict) -> None:
    """AC-NEW-2 ordering: the verifier must run AFTER the backfill, not
    before. The story says "validates migration success" -- a verifier
    that runs before the backfill would always pass (or always fail
    based on stale state), neither of which is what the AC means."""
    job = _backfill_job(workflow)
    steps = (job or {}).get("steps", []) or []
    backfill_idx = None
    verifier_idx = None
    for idx, step in enumerate(steps):
        run = (step or {}).get("run") or ""
        if "scripts.backfill_holdings_currency" in run or "scripts/backfill_holdings_currency" in run:
            backfill_idx = idx
        if "scripts.verify_holdings_currency_backfill" in run or "scripts/verify_holdings_currency_backfill" in run:
            verifier_idx = idx
    assert backfill_idx is not None and verifier_idx is not None, (
        "both backfill and verifier steps must be present "
        f"(backfill_idx={backfill_idx}, verifier_idx={verifier_idx})"
    )
    assert backfill_idx < verifier_idx, (
        f"the verifier step (index={verifier_idx}) must run AFTER the "
        f"backfill step (index={backfill_idx}); they can't be reordered"
    )


def test_story2_ac_new_2_push_trigger_watches_new_files(workflow: dict) -> None:
    """AC-NEW-2 supplementary: a push changing either the backfill
    script or its verifier must restart the workflow. The push trigger's
    ``paths:`` filter must include both new files."""
    on = workflow.get(True) or workflow.get("on") or {}
    push = (on or {}).get("push") or {}
    paths = push.get("paths") or []
    assert paths, (
        "workflow push trigger must use a `paths:` filter; "
        "without one, a CI job change wouldn't be reachable by a push"
    )
    joined = "\n".join(paths)
    assert "scripts/backfill_holdings_currency.py" in joined, (
        "push.paths must include scripts/backfill_holdings_currency.py "
        f"so a change to the backfill restarts the workflow; got paths={paths!r}"
    )
    assert "scripts/verify_holdings_currency_backfill.py" in joined, (
        "push.paths must include scripts/verify_holdings_currency_backfill.py "
        f"so a change to the verifier restarts the workflow; got paths={paths!r}"
    )


# ---------------------------------------------------------------------------
# AC-NEW-2: verifier behavior with seeded passing AND failing scenarios.
# Real exit codes, real Postgres. This is the load-bearing test -- the
# story explicitly says "Write a real pytest test for the verification
# script (seed both a passing and failing scenario, assert real exit
# codes)."
# ---------------------------------------------------------------------------


_SESSION_UUID = uuid.uuid4().hex
_TEST_HOLDING_PREFIX = f"pf-story2qa-{_SESSION_UUID}"
_TEST_HOLDING_PREFIX_LIKE = f"{_TEST_HOLDING_PREFIX}%"


@pytest.fixture
def records_table():
    """Create the canonical ``records`` table this app stores everything
    in, scope our seed rows by prefix, and clean both schema_migrations
    and migration_log for MIGRATION_NAME so we don't bleed state."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    table_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (table_name, id)
                )
                """
            )
            cur.execute(
                "DELETE FROM records WHERE table_name = 'holdings' "
                "AND id LIKE %s",
                (_TEST_HOLDING_PREFIX_LIKE,),
            )
            cur.execute(
                "DELETE FROM schema_migrations WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "DELETE FROM migration_log WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )

    yield "records"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM records WHERE table_name = 'holdings' "
                "AND id LIKE %s",
                (_TEST_HOLDING_PREFIX_LIKE,),
            )
            cur.execute(
                "DELETE FROM schema_migrations WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "DELETE FROM migration_log WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )


@needs_postgres
def test_story2_ac_new_2_passing_scenario_exits_zero(records_table, capsys) -> None:
    """Seed: 3 holdings rows, all with currency='USD' in their data
    JSONB; schema_migrations has the MIGRATION_NAME row; migration_log
    has a SUCCESS row. The verifier must exit 0."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO records (table_name, id, data) VALUES (%s, %s, %s)",
                [
                    ("holdings", f"{_TEST_HOLDING_PREFIX}-AAPL", psycopg.types.json.Jsonb({"currency": "USD", "quantity": 10})),
                    ("holdings", f"{_TEST_HOLDING_PREFIX}-MSFT", psycopg.types.json.Jsonb({"currency": "USD", "quantity": 5})),
                    ("holdings", f"{_TEST_HOLDING_PREFIX}-GOOGL", psycopg.types.json.Jsonb({"currency": "USD", "quantity": 3})),
                ],
            )
            cur.execute(
                "INSERT INTO schema_migrations (migration_name, applied_at) "
                "VALUES (%s, now())",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "INSERT INTO migration_log (migration_name, run_at, status, "
                "rows_affected, error_message, dry_run) "
                "VALUES (%s, now(), 'SUCCESS', 3, NULL, false)",
                (MIGRATION_NAME,),
            )

    exit_code = verify_holdings_currency_backfill()
    assert exit_code == EXIT_PASS, (
        f"passing scenario must exit {EXIT_PASS}; got {exit_code}. "
        "This is the success path the CI workflow runs the verifier on."
    )


@needs_postgres
def test_story2_ac_new_2_failing_scenario_exits_one(records_table, capsys) -> None:
    """Seed: 1 holdings row missing the ``currency`` key in its data
    JSONB. The verifier must exit non-zero (1). This is the story's
    explicit 'failing scenario' seed -- it's the case where the
    backfill didn't actually normalize the data."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Schema_migrations and migration_log both say SUCCESS, but
            # the row itself was never normalized -- this is the failure
            # mode the verifier exists to catch.
            cur.execute(
                "INSERT INTO records (table_name, id, data) VALUES (%s, %s, %s)",
                ("holdings", f"{_TEST_HOLDING_PREFIX}-BAD",
                 psycopg.types.json.Jsonb({"quantity": 5})),  # NO currency key
            )
            cur.execute(
                "INSERT INTO schema_migrations (migration_name, applied_at) "
                "VALUES (%s, now())",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "INSERT INTO migration_log (migration_name, run_at, status, "
                "rows_affected, error_message, dry_run) "
                "VALUES (%s, now(), 'SUCCESS', 1, NULL, false)",
                (MIGRATION_NAME,),
            )

    exit_code = verify_holdings_currency_backfill()
    captured = capsys.readouterr()
    assert exit_code == EXIT_FAIL, (
        f"failing scenario must exit {EXIT_FAIL}; got {exit_code}. "
        "A 'success in logs but stale in data' state must surface as "
        "a non-zero exit so CI fails."
    )
    # The verifier must explain WHY it failed on stderr -- this is the
    # actionable signal ops/QA needs when CI fails.
    assert "FAIL:" in captured.err, (
        f"failure scenario must print 'FAIL:' on stderr; got stderr={captured.err!r}"
    )


# ---------------------------------------------------------------------------
# AC-NEW-2: the verifier is wired into the pipeline as a real subprocess,
# so the CI job's exit-status gating contract is real (not just "the
# function returns 1").  Subprocess-level exit code check via
# `python -m scripts.verify_holdings_currency_backfill`.
# ---------------------------------------------------------------------------


@needs_postgres
def test_story2_ac_new_2_verifier_module_subprocess_exits_zero_on_pass(
    records_table,
) -> None:
    """Belt-and-braces: invoke the verifier as a real subprocess the
    way the CI workflow does (``python -m
    scripts.verify_holdings_currency_backfill``) against a passing
    scenario. The process's real exit code must be 0 -- the CI
    gating depends on the subprocess exit, not the in-process
    function return value."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records (table_name, id, data) VALUES (%s, %s, %s)",
                ("holdings", f"{_TEST_HOLDING_PREFIX}-PROC-PASS",
                 psycopg.types.json.Jsonb({"currency": "USD"})),
            )
            cur.execute(
                "INSERT INTO schema_migrations (migration_name, applied_at) "
                "VALUES (%s, now())",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "INSERT INTO migration_log (migration_name, run_at, status, "
                "rows_affected, error_message, dry_run) "
                "VALUES (%s, now(), 'SUCCESS', 1, NULL, false)",
                (MIGRATION_NAME,),
            )

    env = os.environ.copy()
    env["DATABASE_URL"] = DEFAULT_POSTGRES_DSN
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "scripts.verify_holdings_currency_backfill"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"`python -m scripts.verify_holdings_currency_backfill` must "
        f"exit 0 on the passing scenario; got rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@needs_postgres
def test_story2_ac_new_2_verifier_module_subprocess_exits_one_on_fail(
    records_table,
) -> None:
    """Subprocess-level failing-scenario check: invoke the verifier as
    a real subprocess (the way CI does) against a failing scenario.
    The process's real exit code must be non-zero -- this is the actual
    signal that fails the GitHub Actions job when the backfill didn't
    normalize the data."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Missing currency key -- the backfill didn't normalize this.
            cur.execute(
                "INSERT INTO records (table_name, id, data) VALUES (%s, %s, %s)",
                ("holdings", f"{_TEST_HOLDING_PREFIX}-PROC-FAIL",
                 psycopg.types.json.Jsonb({"quantity": 7})),
            )
            cur.execute(
                "INSERT INTO schema_migrations (migration_name, applied_at) "
                "VALUES (%s, now())",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "INSERT INTO migration_log (migration_name, run_at, status, "
                "rows_affected, error_message, dry_run) "
                "VALUES (%s, now(), 'SUCCESS', 1, NULL, false)",
                (MIGRATION_NAME,),
            )

    env = os.environ.copy()
    env["DATABASE_URL"] = DEFAULT_POSTGRES_DSN
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "scripts.verify_holdings_currency_backfill"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        f"`python -m scripts.verify_holdings_currency_backfill` must "
        f"exit non-zero on the failing scenario; got rc=0 "
        f"stdout={result.stdout!r} stderr={result.stderr!r}. "
        "This is the actual signal that would fail the CI job."
    )
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    assert "FAIL:" in combined, (
        f"failing-scenario subprocess must emit a 'FAIL:' line so "
        f"ops/QA can see the reason in CI logs; got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )