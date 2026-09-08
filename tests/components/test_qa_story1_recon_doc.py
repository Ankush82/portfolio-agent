"""Tests verifying the STORY-1 recon document (docs/repo-layer-recon.md).

STORY-1 is a read-only recon story whose deliverable is one markdown file.
Its acceptance criteria are about the *content and structure* of that file,
not about any application code, so the natural test surface is "read the
real file and assert against its real contents".

Each test below asserts one specific acceptance criterion from STORY-1's
PRD against the actual file on disk. They intentionally do NOT parse the
document's prose or attempt to verify the factual correctness of the
*quoted code snippets* (those are already checked at code-review time by
a human reading the doc against the source files).
"""

from __future__ import annotations

import re
from pathlib import Path

DOC_PATH = Path("docs/repo-layer-recon.md")
SECTION_HEADERS_REQUIRED = [
    "V-pre",
    "## V1:",
    "## V2:",
    "## V3:",
    "## V4:",
    "## V5:",
    "## V6:",
    "## V7:",
    "## V8:",
]


def _read_doc() -> str:
    """Read the recon document from disk (the real file the story produced)."""
    assert DOC_PATH.exists(), (
        f"recon document does not exist at {DOC_PATH}"
    )
    return DOC_PATH.read_text(encoding="utf-8")


def test_story1_recon_doc_exists_with_all_required_section_headers():
    """Acceptance criterion: 'one clearly headed section per item V-pre and V1 through V8'."""
    text = _read_doc()
    missing = [h for h in SECTION_HEADERS_REQUIRED if h not in text]
    assert not missing, (
        f"docs/repo-layer-recon.md is missing required section headers: "
        f"{missing}; full headers required: {SECTION_HEADERS_REQUIRED}"
    )


def test_story1_recon_doc_each_section_cites_a_real_file_path():
    """Acceptance criterion: 'Every section cites at least one concrete file path'."""
    text = _read_doc()
    # Split on the section headers so we can check each section
    # individually. We use the V1..V8 + V-pre headers as anchors.
    section_starts = []
    for header in ["V-pre", "V1:", "V2:", "V3:", "V4:", "V5:", "V6:", "V7:", "V8:"]:
        m = re.search(rf"^##? .*{re.escape(header)}.*$", text, flags=re.MULTILINE)
        assert m, f"could not find section header containing {header!r}"
        section_starts.append((header, m.start()))
    section_starts.sort(key=lambda x: x[1])

    failures = []
    for i, (header, start) in enumerate(section_starts):
        end = section_starts[i + 1][1] if i + 1 < len(section_starts) else len(text)
        body = text[start:end]
        # A "concrete file path" here means a string matching at least one
        # of these real-repo path prefixes — these are the kinds of paths
        # every section must cite.
        path_patterns = [
            r"src/infrastructure\.py",
            r"src/infrastructure_postgres\.py",
            r"src/components/c01_user_portfolio\.py",
            r"tests/components/test_user_portfolio\.py",
            r"scripts/migrate_us_stocks\.(sql|py)",
            r"scripts/run_migration\.sh",
            r"scripts/verify_migration\.(sql|py)",
            r"docs/migrations/",
        ]
        if not any(re.search(p, body) for p in path_patterns):
            failures.append(
                f"section {header} cites no concrete file path from the "
                f"expected set {path_patterns}"
            )
    assert not failures, "; ".join(failures)


def test_story1_recon_doc_quotes_protocol_signatures_verbatim_and_indicates_sync():
    """Acceptance criterion: 'The exact signatures of store, retrieve, and query are copied verbatim from src/infrastructure.py, including whether they are async'."""
    text = _read_doc()
    # The Protocol declarations (verbatim).
    expected = [
        "def store(self, table: str, record: dict) -> str:",
        '"""Write a record. Returns its id."""',
        "def retrieve(self, table: str, id_: str) -> dict | None:",
        '"""Read a record by id."""',
        "def query(self, table: str, filters: dict) -> list[dict]:",
        '"""Read records matching filters."""',
    ]
    for snippet in expected:
        assert snippet in text, (
            f"expected verbatim Protocol signature snippet not found in recon doc: {snippet!r}"
        )
    # The doc must explicitly state the methods are sync (NOT async).
    # Per the PRD: "including whether they are async".
    assert re.search(r"\bdef\b", text) and "async" in text, (
        "recon doc must explicitly state whether the Protocol methods are async"
    )
    # And the explicit verdict must be synchronous.
    sync_verdict_patterns = [
        r"synchronous",
        r"\bnot async\b",
        r"\bnot\s+`?async`?\b",
        r"`def`, not `async def`",
    ]
    assert any(re.search(p, text) for p in sync_verdict_patterns), (
        "recon doc must explicitly state that store/retrieve/query are "
        "synchronous (the PRD asks whether they are async)"
    )


def test_story1_recon_doc_copies_dataclass_fields_verbatim_and_states_id_field():
    """Acceptance criterion: 'The four dataclasses' fields, annotations, and defaults are copied verbatim from src/components/c01_user_portfolio.py, and the doc explicitly states yes/no on whether Holding and Transaction have an id field'."""
    text = _read_doc()
    expected_dataclass_snippets = [
        # User
        "    id: str\n    preferences: dict = field(default_factory=dict)\n    email: str = \"\"",
        # Portfolio
        "    id: str\n    user_id: str",
        # Holding
        "    portfolio_id: str\n    security_id: str\n    quantity: Decimal\n    currency: str = \"USD\"\n    exchange: str | None = None\n    symbol_suffix: str | None = None",
        # Transaction
        "    portfolio_id: str\n    kind: str\n    amount: float",
    ]
    for snippet in expected_dataclass_snippets:
        assert snippet in text, (
            f"expected verbatim dataclass snippet not found in recon doc: {snippet!r}"
        )
    # Explicit yes/no on id field for both Holding and Transaction.
    # The doc uses the exact phrasing "*`Holding` has an `id` field: NO.*"
    # in V4's "Explicit yes/no on `id` field" subsection.
    assert "Holding` has an `id` field: NO" in text, (
        "recon doc must explicitly state 'Holding has an id field: NO'"
    )
    assert "Transaction` has an `id` field: NO" in text, (
        "recon doc must explicitly state 'Transaction has an id field: NO'"
    )


def test_story1_recon_doc_states_whether_fake_infrastructure_exists_with_path():
    """Acceptance criterion: 'The doc states explicitly whether an existing fake/in-memory Infrastructure was found, and if so its file path and which Protocol methods it implements'."""
    text = _read_doc()
    # Section V2 must explicitly answer YES or NO; per the real code it
    # must say YES and cite the file path.
    assert "tests/components/test_user_portfolio.py" in text, (
        "recon doc must cite tests/components/test_user_portfolio.py as the location of the fake Infrastructure"
    )
    # Coverage table must enumerate Protocol methods (store/retrieve/query at minimum).
    for method in ["store(table, record)", "retrieve(table, id_)", "query(table, filters)"]:
        assert method in text, (
            f"recon doc must enumerate Protocol method {method!r} in the V2 coverage table"
        )


def test_story1_recon_doc_contains_grep_output_for_table_name_constants_and_literals():
    """Acceptance criterion: 'The doc contains the full grep -rn output for the four table-name constants and the four bare table-name string literals across src/ and tests/'."""
    text = _read_doc()
    # The four constants and four bare literals must appear in V5.
    for constant in ["USERS_TABLE", "PORTFOLIOS_TABLE", "HOLDINGS_TABLE", "TRANSACTIONS_TABLE"]:
        assert constant in text, (
            f"recon doc V5 section missing table-name constant {constant!r}"
        )
    for literal in ['"users"', '"portfolios"', '"holdings"', '"transactions"']:
        assert literal in text, (
            f"recon doc V5 section missing bare-literal table name {literal!r}"
        )
    # The grep must span both src/ and tests/ — at least one match for each tree.
    assert "src/components/c01_user_portfolio.py" in text, (
        "recon doc V5 section must cite a src/ hit"
    )
    assert "tests/components/test_user_portfolio.py" in text, (
        "recon doc V5 section must cite a tests/ hit"
    )


def test_story1_recon_doc_states_us_stock_migration_path_and_no_rollback():
    """Acceptance criterion: 'The doc states the exact path and filename of the existing us_stock .sql migration and whether a down/rollback file exists'."""
    text = _read_doc()
    assert "scripts/migrate_us_stocks.sql" in text, (
        "recon doc V6 section must state the exact path of the us_stock .sql migration"
    )
    # The doc must explicitly state whether a rollback/down file exists.
    # Per the real repo there is none, so the doc must say so clearly.
    rollback_section = text[text.index("V6"):text.index("V7")]
    assert re.search(
        r"\bno\s+(separate\s+)?(down[- ]?migration|rollback|down)\b",
        rollback_section,
        flags=re.IGNORECASE,
    ), (
        "recon doc V6 section must explicitly state that no down/rollback file exists"
    )


def test_story1_recon_doc_lists_postgres_and_redis_env_vars_for_default_and_run_migration():
    """Acceptance criterion: 'The doc lists the exact Postgres and Redis env var names used by DefaultInfrastructure and by run_migration.sh'."""
    text = _read_doc()
    # V7 must enumerate the env vars read by run_migration.sh.
    for var in ["DATABASE_URL", "MIGRATION_DRY_RUN", "LOG_FILE"]:
        assert var in text, (
            f"recon doc V7 section missing env var {var!r} read by run_migration.sh"
        )
    # V7 must also enumerate the constructor-default DSN constants used by DefaultInfrastructure.
    assert "DEFAULT_POSTGRES_DSN" in text, (
        "recon doc V7 section must name the DefaultInfrastructure Postgres DSN constant"
    )
    assert "DEFAULT_REDIS_URL" in text, (
        "recon doc V7 section must name the DefaultInfrastructure Redis URL constant"
    )


def test_story1_recon_doc_every_section_ends_with_a_decision_line():
    """Acceptance criterion: 'Each of the nine sections ends with a one-line Decision: statement applying the PRD's rule for that item'.

    Each section in the recon doc is followed by a `---` horizontal rule,
    so "ends with" is interpreted as: the section body contains exactly
    one paragraph that opens with **Decision:**, and that paragraph is
    the last paragraph in the section before the `---` rule and the
    next heading. A paragraph is one or more consecutive non-blank lines
    (a blank line ends the paragraph), so a Decision statement that
    wraps onto a second physical line is still one paragraph.
    """
    text = _read_doc()
    section_anchors = []
    for header in ["V-pre", "V1:", "V2:", "V3:", "V4:", "V5:", "V6:", "V7:", "V8:"]:
        m = re.search(rf"^##? .*{re.escape(header)}.*$", text, flags=re.MULTILINE)
        assert m, f"section header for {header!r} not found"
        section_anchors.append((header, m.start()))
    section_anchors.sort(key=lambda x: x[1])

    failures = []
    for i, (header, start) in enumerate(section_anchors):
        end = section_anchors[i + 1][1] if i + 1 < len(section_anchors) else len(text)
        body = text[start:end]

        # Split the section body into paragraphs separated by blank lines.
        paragraphs: list[list[str]] = []
        current: list[str] = []
        for ln in body.splitlines():
            if ln.strip() == "":
                if current:
                    paragraphs.append(current)
                    current = []
            else:
                current.append(ln)
        if current:
            paragraphs.append(current)

        # Drop trailing `---` paragraphs (Markdown horizontal rules used
        # as section separators, not real content) before checking the
        # Decision position.
        paragraphs = [p for p in paragraphs if not (len(p) == 1 and p[0].strip() == "---")]

        def _is_decision_paragraph(p: list[str]) -> bool:
            return p[0].lstrip().startswith("**Decision:**")

        decision_paragraphs = [p for p in paragraphs if _is_decision_paragraph(p)]
        if not decision_paragraphs:
            failures.append(f"section {header} contains no **Decision:** paragraph")
            continue
        if len(decision_paragraphs) > 1:
            failures.append(
                f"section {header} contains {len(decision_paragraphs)} "
                f"**Decision:** paragraphs; the PRD requires exactly one"
            )
            continue

        # The Decision paragraph must be the last paragraph in the section
        # (before the trailing `---` separator and the next heading).
        if paragraphs[-1] is not decision_paragraphs[0]:
            failures.append(
                f"section {header}: the **Decision:** paragraph is not the "
                f"last paragraph in the section; last paragraph starts "
                f"with {paragraphs[-1][0]!r}"
            )
    assert not failures, "; ".join(failures)


def test_story1_recon_doc_no_changes_to_src_or_scripts_in_git_status():
    """Acceptance criterion: 'git status shows no changes to any file under src/ or scripts/'.

    The story's only deliverable is docs/repo-layer-recon.md, so any
    uncommitted change under src/ or scripts/ on this branch would mean
    the story leaked application code changes into its read-only recon.
    """
    import subprocess

    completed = subprocess.run(
        ["git", "status", "--porcelain", "--", "src/", "scripts/"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "", (
        "git status shows uncommitted changes under src/ or scripts/ — "
        "STORY-1 is a read-only recon and must not touch them:\n"
        + completed.stdout
    )


def test_story1_recon_doc_is_tracked_in_git_and_on_a_real_commit():
    """The recon doc must be a committed file on this branch (not just untracked on disk)."""
    import subprocess

    # File must be tracked by git (the prior attempt only created the
    # file on disk and never committed it; this guards against that).
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "docs/repo-layer-recon.md"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        "docs/repo-layer-recon.md is not tracked by git — STORY-1's "
        "deliverable was the recon commit, not an untracked file on disk.\n"
        f"stderr: {completed.stderr}"
    )
    # There must be a commit touching exactly this file.
    completed = subprocess.run(
        ["git", "log", "--oneline", "--", "docs/repo-layer-recon.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip(), (
        "no git commit touches docs/repo-layer-recon.md"
    )
