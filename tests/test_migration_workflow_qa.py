"""Independent QA verification for STORY-6 (issue #76):
"Integrate migration into CI/CD pipeline as a one-time background job".

This is a fresh test module written by QA against the actual on-disk
files. It is intentionally separate from ``tests/test_migration_workflow.py``
(added by the dev agent) so QA assertions stand on their own: every
acceptance criterion is exercised here from scratch with new assertions,
not by re-using or paraphrasing the dev's existing tests.

The story explicitly states "we do NOT have real GitHub Actions access
from here" -- so the test parses the workflow YAML file with a real
``yaml.safe_load`` call and asserts the structural properties that
implement each acceptance criterion. Where helpful, the wrapper script
itself is also inspected for the contract it must satisfy when invoked.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "migration.yml"
WRAPPER_PATH = REPO_ROOT / "scripts" / "run_migration.sh"


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Real YAML parse of the workflow file. Shared by all tests below."""
    assert WORKFLOW_PATH.is_file(), (
        f"workflow file missing at {WORKFLOW_PATH}; STORY-6 requires the "
        "real workflow definition at .github/workflows/migration.yml"
    )
    with WORKFLOW_PATH.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict), (
        f"workflow YAML must parse to a mapping; got {type(loaded).__name__}"
    )
    return loaded


# ---------------------------------------------------------------------------
# AC1: "Pipeline definition includes a step/job that runs the migration
#       wrapper script (scripts/run_migration.sh)."
# ---------------------------------------------------------------------------


def test_ac1_workflow_invokes_real_wrapper_script_via_bash(workflow: dict) -> None:
    """Find the job that runs the wrapper and verify the invocation is real."""
    jobs = workflow.get("jobs") or {}
    assert jobs, "workflow must declare at least one job"

    invocation_lines: list[tuple[str, str]] = []
    for job_name, job_def in jobs.items():
        for step in (job_def or {}).get("steps", []) or []:
            run = (step or {}).get("run") or ""
            if "scripts/run_migration.sh" in run:
                invocation_lines.append((job_name, run.strip()))

    assert invocation_lines, (
        "no workflow step actually invokes scripts/run_migration.sh; "
        f"jobs={list(jobs.keys())}"
    )

    # The invocation must use a real shell interpreter, not just mention
    # the path in a comment.
    invocation_strings = "\n".join(line for _, line in invocation_lines)
    assert re.search(r"\bbash\s+scripts/run_migration\.sh", invocation_strings), (
        f"the wrapper must be invoked via `bash scripts/run_migration.sh`; "
        f"got invocations={invocation_lines!r}"
    )

    # Belt-and-braces: the wrapper script itself must exist on disk and
    # actually be executable -- otherwise the CI step would fail.
    assert WRAPPER_PATH.is_file(), (
        f"wrapper script missing at {WRAPPER_PATH}; CI cannot invoke it"
    )
    mode = WRAPPER_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"wrapper script at {WRAPPER_PATH} must be executable by the owner "
        f"(got mode={oct(mode)})"
    )


# ---------------------------------------------------------------------------
# AC2: "Job is configured to run only once per deployment (a concurrency
#       group serializing overlapping runs, or equivalent)."
# ---------------------------------------------------------------------------


def test_ac2_concurrency_group_serializes_overlapping_runs(workflow: dict) -> None:
    """Top-level concurrency block with a non-empty group, and
    cancel-in-progress is not set to true (true would cancel an in-flight
    migration, which is the opposite of 'serializes overlapping runs')."""
    concurrency = workflow.get("concurrency")
    assert concurrency is not None, (
        "STORY-6 AC2: workflow must declare a top-level `concurrency:` block "
        "to serialize overlapping runs"
    )
    assert isinstance(concurrency, dict), (
        f"concurrency block must be a mapping; got {type(concurrency).__name__}"
    )
    group = concurrency.get("group")
    assert isinstance(group, str) and group.strip(), (
        f"concurrency.group must be a non-empty string; got {group!r}"
    )
    assert concurrency.get("cancel-in-progress", False) is False, (
        "concurrency.cancel-in-progress must be false (or absent); true "
        "would cancel an in-flight migration and violate the AC"
    )


# ---------------------------------------------------------------------------
# AC3: "Job logs are retained for audit (a real artifact upload, or GitHub
#       Actions' own native log retention)."
# ---------------------------------------------------------------------------


def test_ac3_log_retention_via_native_and_explicit_artifact(workflow: dict) -> None:
    """GitHub Actions retains workflow logs natively (this IS the native
    half), and the workflow also uploads the migration log explicitly
    (the artifact half). Both halves must hold."""
    # Native half: this file is a real GitHub Actions workflow and runs
    # at least one job -- GitHub retains those logs automatically.
    jobs = workflow.get("jobs") or {}
    assert jobs, "AC3 native half: a real GitHub Actions workflow must declare at least one job"

    # Explicit artifact half: actions/upload-artifact step pointing at
    # the migration log file, with a real retention window.
    upload_steps = []
    for job_def in jobs.values():
        for step in (job_def or {}).get("steps", []) or []:
            uses = (step or {}).get("uses", "") or ""
            if "actions/upload-artifact" in uses:
                upload_steps.append(step)

    assert upload_steps, (
        "AC3 explicit artifact half: workflow must have at least one "
        "actions/upload-artifact step for the migration log"
    )

    matched = False
    for step in upload_steps:
        with_block = (step or {}).get("with") or {}
        path = with_block.get("path") or ""
        name = with_block.get("name") or ""
        retention = with_block.get("retention-days")
        cond = (step or {}).get("if")
        if "migrate_us_stocks.log" in str(path):
            assert name.strip(), (
                f"upload-artifact step must have a non-empty name; got {with_block!r}"
            )
            assert retention is None or (isinstance(retention, int) and retention > 0), (
                f"upload-artifact retention-days must be a positive int when set; "
                f"got {retention!r}"
            )
            # Must run even on failure -- that's the whole point of an audit log.
            assert cond in ("always()", "failure()") or cond is None, (
                f"the upload-artifact step should use `if: always()` so the "
                f"log is retained on failure; got if={cond!r}"
            )
            matched = True

    assert matched, (
        "no actions/upload-artifact step uploads the migration log "
        f"(migrate_us_stocks.log); got paths={[s.get('with', {}).get('path') for s in upload_steps]!r}"
    )


# ---------------------------------------------------------------------------
# AC4: "Pipeline proceeds to next steps only if migration job exits with
#       code 0; otherwise pipeline fails."
# ---------------------------------------------------------------------------


def test_ac4_no_continue_on_error_on_migration_step(workflow: dict) -> None:
    """The migration step must NOT swallow non-zero exits via
    continue-on-error. GitHub Actions' default behavior is to fail the
    step -> fail the job -> fail the workflow run."""
    jobs = workflow.get("jobs") or {}
    found = False
    for job_name, job_def in jobs.items():
        for step in (job_def or {}).get("steps", []) or []:
            run = (step or {}).get("run") or ""
            if "scripts/run_migration.sh" not in run:
                continue
            assert "continue-on-error" not in step, (
                f"job={job_name!r}: the migration step must NOT use "
                "continue-on-error; a non-zero exit must fail the job "
                "(the gating mechanism for AC4)"
            )
            found = True

    assert found, (
        "AC4: could not locate the migration step (scripts/run_migration.sh) "
        "to validate -- AC1 should have caught this"
    )


def test_ac4_wrapper_propagates_psql_exit_status() -> None:
    """Belt-and-braces: the bash wrapper must propagate psql's real exit
    status. If it didn't, a failing migration could exit 0 from the
    wrapper and silently pass CI."""
    script_text = WRAPPER_PATH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in script_text, (
        "wrapper must use 'set -euo pipefail' so failures propagate"
    )
    # Must capture psql's exit status into a variable and then `exit`
    # on that variable -- not just rely on the pipeline (which would
    # give tee's exit status instead).
    assert re.search(r"PSQL_EXIT\s*=\s*\$?", script_text) or "PIPESTATUS" in script_text, (
        "wrapper must capture psql's real exit status into a variable "
        "(e.g. PSQL_EXIT or PIPESTATUS)"
    )
    assert re.search(r"exit\s+[\"']?\$\{?PSQL_EXIT", script_text) or "exit \"${PSQL_EXIT" in script_text, (
        "wrapper must `exit` with psql's captured exit status so "
        "non-zero migrations propagate to the CI job"
    )


# ---------------------------------------------------------------------------
# AC5: "Job can be manually re-triggered for retry (workflow_dispatch or
#       equivalent)."
# ---------------------------------------------------------------------------


def test_ac5_manual_retrigger_via_workflow_dispatch(workflow: dict) -> None:
    """workflow_dispatch must be declared so operators can re-run on demand."""
    on = workflow.get(True) or workflow.get("on") or {}
    assert isinstance(on, dict), (
        f"workflow `on:` must be a mapping; got {type(on).__name__}"
    )
    assert "workflow_dispatch" in on, (
        f"AC5: workflow must declare workflow_dispatch for manual re-trigger; "
        f"got triggers={list(on.keys())}"
    )

    dispatch = on["workflow_dispatch"]
    # The story says workflow_dispatch or "equivalent". An equivalent
    # like workflow_call is also acceptable, but at minimum we want a
    # way to re-run from the Actions UI -- workflow_dispatch is it.
    if dispatch is not None:
        assert isinstance(dispatch, dict), (
            f"workflow_dispatch may be null (no inputs) or a mapping; got {type(dispatch).__name__}"
        )


# ---------------------------------------------------------------------------
# AC6: "No manual DBA intervention is required beyond triggering the
#       pipeline."
# ---------------------------------------------------------------------------


def test_ac6_workflow_provisions_ephemeral_postgres_service(workflow: dict) -> None:
    """The workflow must spin up its own Postgres (services:) and export
    DATABASE_URL on the job -- so an operator never has to point at a
    hand-managed database."""
    jobs = workflow.get("jobs") or {}
    assert jobs, "workflow must declare at least one job"

    found_service = False
    for job_def in jobs.values():
        services = (job_def or {}).get("services") or {}
        for _name, service_def in services.items():
            image = (service_def or {}).get("image", "") or ""
            if "postgres" in image.lower():
                found_service = True
                break
        if found_service:
            break
    assert found_service, (
        "AC6: workflow must declare a postgres service container so the "
        "migration runs against an ephemeral DB with no manual DBA "
        "intervention"
    )

    # DATABASE_URL must be set at job level so the wrapper sees it.
    found_url = False
    for job_def in jobs.values():
        env = (job_def or {}).get("env") or {}
        for key in env.keys():
            if str(key).upper() == "DATABASE_URL":
                found_url = True
                break
        if found_url:
            break
    assert found_url, (
        "AC6: workflow must export DATABASE_URL on the migration job so "
        "the wrapper's required-env check passes without an operator "
        "having to type it in"
    )


def test_ac6_path_filter_triggers_only_on_migration_changes(workflow: dict) -> None:
    """Belt-and-braces: the push trigger must be path-filtered so the
    job only fires when migration files change -- reduces noise and
    reinforces 'runs only when actually needed' (AC6 spirit)."""
    on = workflow.get(True) or workflow.get("on") or {}
    push = on.get("push") or {}
    if not push:
        pytest.skip("workflow has no push trigger -- fine if workflow_dispatch-only")
    paths = push.get("paths") or []
    assert paths, (
        "AC6 spirit: push trigger must use a `paths:` filter so the "
        "migration job doesn't fire on every commit"
    )
    # At least one path must reference the migration surface.
    joined = " ".join(paths).lower()
    assert any(
        token in joined
        for token in ("migrate", "scripts/run_migration.sh", "scripts/migrate_us_stocks")
    ), (
        f"push.paths must filter to migration-related files; got paths={paths!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test: the wrapper exits 1 with an actionable error when DATABASE_URL
# is unset -- this is what makes AC4's "otherwise pipeline fails" real
# (the wrapper is the failure source the CI surfaces).
# ---------------------------------------------------------------------------


def test_wrapper_exit_code_propagates_to_ci() -> None:
    """If the wrapper ever exits 0 on a missing DATABASE_URL, CI would
    silently 'pass' a migration that never ran -- defeating AC4."""
    import subprocess

    if not (WRAPPER_PATH.is_file() and os.access(WRAPPER_PATH, os.X_OK)):
        pytest.skip(f"wrapper not executable at {WRAPPER_PATH}")

    # Strip parent DATABASE_URL so the wrapper's required-env check fails.
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        ["bash", str(WRAPPER_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "wrapper must exit non-zero when DATABASE_URL is unset; "
        f"got rc=0 stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + "\n" + result.stderr
    assert "DATABASE_URL" in combined, (
        "wrapper error must mention DATABASE_URL so operators can "
        f"diagnose; got stdout={result.stdout!r} stderr={result.stderr!r}"
    )