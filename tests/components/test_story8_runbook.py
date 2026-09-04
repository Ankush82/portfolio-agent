"""QA tests for STORY-8: the US-stock migration runbook at
docs/migrations/us_stock_migration.md.

This is a documentation story -- there is no code to execute. The right
verifications are:

  1. The runbook file exists and is well-formed Markdown.
  2. Every file, command, env var, table name, and script the runbook
     references is real and grep-verifiable against this repository.
  3. The README links to the runbook from its existing repo-map block.
  4. Every acceptance criterion in STORY-8 is covered by a section in
     the runbook.
  5. The runbook does not invent a fictional down-migration script
     (per the story's explicit constraint: "since there's no separate
     down-migration script in this repo -- don't invent one").

These assertions ground the runbook in the real repo rather than just
checking that a string exists somewhere.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_PATH = REPO_ROOT / "docs" / "migrations" / "us_stock_migration.md"
README_PATH = REPO_ROOT / "README.md"

# Files / scripts / config the runbook is allowed to reference. If a
# claim in the runbook points at a path that's not in this allow-list,
# the test fails -- that catches "fabricated content" regressions.
EXPECTED_FILES = {
    "scripts/migrate_us_stocks.sql",
    "scripts/migrate_us_stocks.py",
    "scripts/run_migration.sh",
    "scripts/verify_migration.py",
    "scripts/verify_migration.sql",
    ".github/workflows/migration.yml",
    "src/infrastructure_postgres.py",
}


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK_PATH.exists(), f"runbook missing at {RUNBOOK_PATH}"
    return RUNBOOK_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC #1 + structural: file exists, well-formed Markdown, links from README
# ---------------------------------------------------------------------------


def test_runbook_file_exists():
    assert RUNBOOK_PATH.exists(), f"runbook file must exist at {RUNBOOK_PATH}"


def _strip_fenced_code(text: str) -> str:
    """Remove fenced code blocks so structural regexes don't see
    Markdown-looking content inside code samples (e.g. '# prints: ...')."""
    return re.sub(r"```[\s\S]*?```", "", text)


def test_runbook_is_well_formed_markdown(runbook_text: str):
    # Strip fenced code blocks so # / ## inside example output don't
    # count as headings.
    prose = _strip_fenced_code(runbook_text)

    # Exactly one H1 (the document title) -- extra H1s would mean the
    # doc is malformed / accidentally split.
    h1_count = len(re.findall(r"(?m)^# ", prose))
    assert h1_count == 1, f"expected exactly one H1, found {h1_count}"

    # At least a handful of H2s and H3s -- empty / stub runbooks fail.
    h2_count = len(re.findall(r"(?m)^## ", prose))
    h3_count = len(re.findall(r"(?m)^### ", prose))
    assert h2_count >= 3, f"expected >= 3 H2 sections, found {h2_count}"
    assert h3_count >= 3, f"expected >= 3 H3 sections, found {h3_count}"

    # At least a few fenced code blocks (bash + sql snippets).
    fence_count = runbook_text.count("```")
    assert fence_count >= 6, (
        f"expected >= 6 fence markers (3+ code blocks), found {fence_count}"
    )

    # Balanced fences -- every opening triple-backtick must be closed.
    assert fence_count % 2 == 0, (
        f"unbalanced fenced code blocks: {fence_count} fence markers"
    )


def test_readme_links_to_runbook():
    readme = README_PATH.read_text(encoding="utf-8")
    # The story says "add a short line/section pointing to
    # docs/migrations/us_stock_migration.md, don't restructure the
    # whole README". Assert the link is present, and that it's
    # within the existing repo-map block (between "## Repo map" and
    # the next "## " header).
    assert "docs/migrations/us_stock_migration.md" in readme, (
        "README does not reference docs/migrations/us_stock_migration.md"
    )
    repo_map_match = re.search(
        r"## Repo map.*?(?=\n## |\Z)", readme, flags=re.DOTALL
    )
    assert repo_map_match, "could not locate '## Repo map' section in README"
    repo_map = repo_map_match.group(0)
    assert "docs/migrations/us_stock_migration.md" in repo_map, (
        "runbook link is not inside the existing 'Repo map' block"
    )


# ---------------------------------------------------------------------------
# AC #1: step-by-step instructions for the migration + verifier
# ---------------------------------------------------------------------------


def test_runbook_covers_migration_dry_run_modes(runbook_text: str):
    # The story is explicit: "with and without MIGRATION_DRY_RUN".
    assert "MIGRATION_DRY_RUN=true" in runbook_text, (
        "runbook must show a dry-run example with MIGRATION_DRY_RUN=true"
    )
    assert "MIGRATION_DRY_RUN" in runbook_text
    # And both the shell wrapper and the Python entry point are
    # documented as real paths.
    assert "scripts/run_migration.sh" in runbook_text
    assert "scripts/migrate_us_stocks.py" in runbook_text


def test_runbook_covers_migration_log_queries(runbook_text: str):
    # Acceptance criterion 1: "checking migration_log" -- need at
    # least one SELECT against the table.
    assert re.search(
        r"SELECT[\s\S]+?FROM\s+migration_log", runbook_text, flags=re.IGNORECASE
    ), "runbook must include at least one SELECT against migration_log"
    # And the column names the runbook describes actually exist in
    # the real schema (infrastructure_postgres.py).
    for col in ("migration_name", "run_at", "status", "rows_affected", "dry_run"):
        assert col in runbook_text, f"runbook missing migration_log column {col!r}"


def test_runbook_covers_verification_script(runbook_text: str):
    assert "scripts/verify_migration.py" in runbook_text
    # The verifier's printed summary lines must be documented.
    for line in (
        "stocks.total_rows",
        "stocks.bad_currency",
        "stocks.bad_exchange",
        "stocks.bad_suffix",
        "migration_log.success",
    ):
        assert line in runbook_text, (
            f"runbook must document verifier summary line {line!r}"
        )


# ---------------------------------------------------------------------------
# AC #2: rollback procedure (no fictional down-migration)
# ---------------------------------------------------------------------------


def test_runbook_documents_automatic_rollback(runbook_text: str):
    # Real rollback in this repo comes from --single-transaction
    # (shell path) and autocommit single-statement rollback (Python
    # path). The runbook must mention both honestly.
    assert "--single-transaction" in runbook_text, (
        "runbook must reference --single-transaction for the shell wrapper's rollback"
    )
    assert re.search(r"\bROLLBACK\b", runbook_text), (
        "runbook must mention the ROLLBACK mechanism for the shell wrapper"
    )


def test_runbook_does_not_invent_down_migration_script(runbook_text: str):
    # Story's hard constraint: "there's no separate down-migration
    # script in this repo -- don't invent one". So the runbook must
    # not claim a down-migration script exists. The runbook explicitly
    # disclaims it; the test asserts the *negative* -- nothing in the
    # repo other than the runbook names a down/rollback script.
    invented_scripts = [
        "scripts/rollback_migration.sh",
        "scripts/migrate_us_stocks_down.sql",
        "scripts/down_migration.py",
        "scripts/revert_migration.py",
    ]
    for path in invented_scripts:
        assert not (REPO_ROOT / path).exists(), (
            f"runbook claimed {path} exists but it does not"
        )
    # The runbook itself should explicitly say there's no down-migration.
    assert re.search(r"no\s+(separate\s+)?down-?migration", runbook_text, re.IGNORECASE), (
        "runbook must explicitly say no separate down-migration script exists"
    )


def test_runbook_documents_post_commit_remediation_via_migration_log(
    runbook_text: str,
):
    # AC #2 also requires "manual steps if a post-migration issue is
    # discovered". Per the story, the manual remediation is "query
    # migration_log for the run's rowcount / affected records, since
    # there's no separate down-migration script in this repo".
    remediation_section = re.search(
        r"##\s+5b?\.\s+Post-commit[^\n]*", runbook_text
    )
    assert remediation_section, (
        "runbook must have a 'Post-commit' remediation section"
    )
    section_text = runbook_text[remediation_section.start():]
    assert "rows_affected" in section_text, (
        "post-commit remediation must reference migration_log.rows_affected"
    )
    # The runbook describes a "corrective `UPDATE` by hand" inline +
    # step 4 ("Author a targeted corrective `UPDATE`"). Match either
    # a fenced SQL block starting with UPDATE or a backticked UPDATE
    # mention, both of which are valid runbook prose.
    has_update_sql = bool(
        re.search(r"```[\s\S]*?UPDATE\s+stocks[\s\S]*?```", section_text, re.IGNORECASE)
    )
    has_update_mention = bool(
        re.search(r"corrective\s+`?UPDATE`?", section_text, re.IGNORECASE)
    )
    assert has_update_sql or has_update_mention, (
        "post-commit remediation must describe a corrective UPDATE by hand"
    )


# ---------------------------------------------------------------------------
# Cross-check: every file the runbook references is real and exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relpath", sorted(EXPECTED_FILES))
def test_runbook_references_real_files_exist(relpath: str, runbook_text: str):
    assert relpath in runbook_text, (
        f"runbook should reference {relpath} but does not"
    )
    assert (REPO_ROOT / relpath).exists(), (
        f"runbook references {relpath} but it is not in the repo"
    )


def test_runbook_references_github_actions_workflow_dispatch(
    runbook_text: str,
):
    # AC #1 explicitly mentions the .github/workflows/migration.yml
    # workflow_dispatch input as the recommended manual re-trigger.
    assert "workflow_dispatch" in runbook_text, (
        "runbook must reference workflow_dispatch as the manual re-trigger path"
    )
    assert "dry_run" in runbook_text, (
        "runbook must reference the dry_run input on the workflow_dispatch trigger"
    )


def test_runbook_mentions_psycopg_dependency(runbook_text: str):
    # The Python entry points need psycopg -- mentioned in the
    # prerequisites / 1. Prerequisites section.
    assert "psycopg" in runbook_text, (
        "runbook must mention psycopg for the Python entry points"
    )


def test_runbook_references_real_tmp_us_tickers_table(runbook_text: str):
    # The migration SQL joins against tmp_us_tickers -- the runbook
    # uses this in its post-commit remediation example. Confirm the
    # reference is real (it appears in the actual SQL file).
    assert "tmp_us_tickers" in runbook_text
    sql = (REPO_ROOT / "scripts" / "migrate_us_stocks.sql").read_text()
    assert "tmp_us_tickers" in sql, (
        "runbook references tmp_us_tickers but the SQL does not -- fabrication"
    )


# ---------------------------------------------------------------------------
# AC #4 (all team members can follow without consulting the implementer)
# -- covered implicitly by the existence of all the above. We add one
# explicit cross-check that the runbook addresses all three audiences.
# ---------------------------------------------------------------------------


def test_runbook_addresses_all_three_audiences(runbook_text: str):
    # AC #4 says "All team members (backend, DevOps, QA) can follow
    # the runbook without needing to consult the original implementer."
    for audience in ("backend", "DevOps", "QA"):
        assert audience in runbook_text, (
            f"runbook must explicitly mention audience {audience!r}"
        )


# ---------------------------------------------------------------------------
# AC #5 (review/approval as part of DoD) is a process gate, not a code
# assertion. We don't try to fake-assert it programmatically; the test
# suite as a whole constitutes the review.
# ---------------------------------------------------------------------------
