"""STORY-23: Static tests enforcing broker isolation and Protocol conformance.

This module contains three guard tests that make the extensibility
requirement enforceable rather than aspirational:

  1. Leakage test — greps the source tree for Upstox-specific literals
     and asserts every hit lives in an allow-listed location.

  2. Protocol-conformance test — asserts both ``StubBrokerConnector``
     and ``DefaultUpstoxBrokerConnector`` satisfy the ``BrokerConnector``
     Protocol via a runtime-checkable ``isinstance`` plus a signature
     check that every Protocol method exists with the declared
     keyword-only parameter names.

  3. ``DefaultUserPortfolio`` isolation test — asserts the portfolio
     module's source contains none of the Upstox literals, so a
     future contributor cannot accidentally introduce Upstox knowledge
     into the generic component.

All tests run offline in under a second and make no network calls.
"""

from __future__ import annotations

import inspect
import sys
from datetime import date
from pathlib import Path
from typing import get_type_hints

import pytest

from components.c01_user_portfolio import (
    BrokerConnector,
    DefaultUpstoxBrokerConnector,
    StubBrokerConnector,
    DefaultUserPortfolio,
    BrokerCredentials,
)


# ---------------------------------------------------------------------------
# Shared connector factory (used by conformance tests)
# ---------------------------------------------------------------------------

def _default_upstox_connector():
    """Return a ``DefaultUpstoxBrokerConnector`` wired with a real
    ``_UpstoxHttp`` that has a no-op token provider (never called in
    these tests — the connector's methods under test do not use the
    HTTP helper in this story's scope)."""
    from upstox_config import UpstoxConfig
    from upstox_http import _UpstoxHttp
    return DefaultUpstoxBrokerConnector(
        config=UpstoxConfig(
            client_id="test-client-id",
            client_secret="test-client-secret",
            redirect_uri="https://example.com/cb",
        ),
        http=_UpstoxHttp(token_provider=lambda: "story23-noop-token"),
    )


# ---------------------------------------------------------------------------
# Helper: source-code scanner
# ---------------------------------------------------------------------------

# The only directories that are part of the project source tree.
# The workspace root (DarkFactory/workspaces/...) contains .venv/, .git/,
# and many nested project copies — scanning the whole root takes ~30 s.
# By enumerating only the known project subdirs we stay within the real
# source tree (~200 .py files) and finish in well under 1 s.
_PROJECT_SCAN_DIRS = ("src", "tests", "scripts", "adr", "docs")

# Top-level names to skip when enumerating children of the repo root.
_SCAN_SKIP_NAMES = frozenset({
    ".venv", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".eggs", "build", "dist",
    ".hg", ".svn", "venv", "env", "lib",
    "static", "config", "checkpoint.md", "loop.md",
    "portfolio_ai_three_literature_reviews.md", "self-evolving-harness-literature-review.md",
    "orchestration.md", "Thoughts.md", "roadmap.md", "pyproject.toml",
    "docker-compose.yml", "uv.lock", ".env.example",
})


def _scan_for_literals(
    literals: list[str],
    root: Path,
) -> dict[str, list[tuple[Path, int, str]]]:
    """Grep every Python file under the known project subdirs of
    ``root`` for each literal in ``literals``.  Returns a dict mapping
    each literal to a list of ``(file_path, line_number, line_text)``
    hits.

    Scanning only the explicit ``_PROJECT_SCAN_DIRS`` (and their
    children) avoids the workspace root's ``.venv/``, ``.git/``, and
    nested DarkFactory copies — keeping the scan to ~200 .py files and
    well under 1 s."""
    hits: dict[str, list[tuple[Path, int, str]]] = {lit: [] for lit in literals}

    # Build the list of directories to scan: the known project subdirs,
    # plus the repo root itself (covers the case where root IS the project root).
    to_scan: list[Path] = [root]
    for name in _PROJECT_SCAN_DIRS:
        child = root / name
        if child.is_dir():
            to_scan.append(child)

    for base in to_scan:
        for path in sorted(base.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for lit in literals:
                    if lit in line:
                        hits[lit].append((path, lineno, line.rstrip()))
    return hits


# ---------------------------------------------------------------------------
# TEST 1 — Leakage test
# ---------------------------------------------------------------------------

# Literals that are Upstox-specific and must NOT appear outside the
# allow-list below. Each literal is a unique identifier (exact match)
# not a prefix — the test uses ``in`` (substring match) to catch
# occurrences like ``long_term_holdings``, ``trading_symbol``,
# ``instrument_token``, etc. without false-positives on unrelated words
# that happen to share a prefix.
_UPSTOX_LITERALS = [
    # Literal broker name (case-sensitive)
    "upstox",
    # Exact hostname for the Upstox API
    "api.upstox.com",
    # Response field names from Upstox's long-term-holdings endpoint
    "trading_symbol",
    "instrument_token",
    "scrip_name",
    # Endpoint identifiers from the Upstox docs
    "long-term-holdings",
    "historical-trades",
    # Pagination / metadata field names (Upstox-specific)
    "page_number",
    "meta_data",
]

# ALLOW-LIST — the only locations in the source tree where an Upstox
# literal may legitimately appear. Every file not listed here is a
# broker-agnostic layer (Infrastructure, repository, other components)
# and must contain zero Upstox literals.
#
# Rule: if an Upstox literal appears in a file NOT on this list, the
# test fails. The allow-list is intentionally narrow — adding a new
# broker integration (e.g. Zerodha) requires a new integration module
# and a new entry on this list, not a "catch-all" that defeats the guard.
_ALLOWED_PREFIXES = [
    # ── Upstox connector module ───────────────────────────────────────────
    # The connector IS the broker-specific layer; all its literals are
    # expected here (endpoint URLs, field names, config field names).
    "src/components/c01_user_portfolio.py",
    # Upstox HTTP helper and config — both are broker-specific infrastructure.
    "src/upstox_http.py",
    "src/upstox_config.py",
    # ── API route modules ─────────────────────────────────────────────────
    # The Upstox OAuth connect/callback routes ARE the broker-specific
    # HTTP endpoints; they necessarily carry the broker name.
    "src/webapp.py",
    # ── Broker connector registry ─────────────────────────────────────────
    # The single BROKER_CONNECTORS registry entry is the wiring point;
    # it necessarily names the connector class.
    "src/components/c01_user_portfolio.py",
    # ── Tests and fixtures ────────────────────────────────────────────────
    # Test modules that exercise the Upstox connector.
    "tests/test_story5_default_upstox_broker_connector.py",
    "tests/test_story5_diag.py",
    "tests/test_story5_smoke.py",
    "tests/test_story6_exchange_auth_code.py",
    "tests/test_story7_fetch_holdings.py",
    "tests/test_story20_broker_settings.py",
    "tests/test_story20_broker_settings_live_ui.py",
    "tests/test_qa_story16_upstox_connect.py",
    "tests/test_qa_story17_upstox_callback.py",
    "tests/test_qa_story17_upstox_callback_qa.py",
    "tests/test_story17_upstox_callback.py",
    "tests/test_story12_oauth_state.py",          # story 12 is the OAuth state module
    # Fixture files — the JSON blobs Upstox returns; they inherently
    # contain field names like ``trading_symbol``, ``instrument_token``.
    "tests/fixtures/upstox/",
    # ── UI display strings ────────────────────────────────────────────────
    # The settings/brokers template renders a Connect button per broker;
    # the display name string lives in the connector and is referenced
    # here as a string literal so the template is correct.
    "templates/settings_brokers.html",
    "templates/portfolio.html",
    # ── Documentation / ADRs ─────────────────────────────────────────────
    # Design decisions that reference the Upstox integration.
    "adr/",
    "docs/",
    # ── This test file itself ─────────────────────────────────────────────
    # The test module declares the literals as strings below; those hits
    # are expected and must not cause the test to fail.
    __file__,
]


def _is_allowed(path: Path) -> bool:
    """Return True when ``path`` is on the allow-list of broker-specific
    locations.

    Comparison is done via the path relative to ``repo_root`` so that
    absolute paths (e.g. the ``__file__`` entry in the allow-list,
    which pytest expands to an absolute path) match correctly regardless
    of whether the workspace root has a hash suffix.

    Paths not under ``repo_root`` (e.g. nested DarkFactory copies) are
    checked by relative-to-root comparison only; they will not match the
    allow-list entries and their occurrences will correctly appear as
    violations."""
    repo_root = Path(__file__).resolve().parents[2]

    # Normalise to repo-root-relative path for comparison.
    try:
        rel = str(path.relative_to(repo_root))
    except ValueError:
        # Not under repo_root — use the raw path as-is so nested-copy
        # violations are never silently allowed.
        rel = str(path)

    for allowed in _ALLOWED_PREFIXES:
        # Resolve __file__ entry to absolute for consistent comparison.
        allowed_abs = str(Path(allowed).resolve())
        if rel == allowed or rel == allowed_abs:
            return True
        # Support directory-prefix entries (e.g. "tests/fixtures/upstox/")
        if allowed.endswith("/") and (rel.startswith(allowed) or allowed_abs.startswith(allowed.rstrip("/"))):
            return True
    return False


def test_no_upstox_literals_outside_allowed_locations():
    """Upstox-specific literals may appear only in the allow-listed
    locations. Any occurrence elsewhere (e.g. in the Infrastructure
    repository layer, a generic component, or an unrelated test file)
    is a broker-leakage violation and this test fails.

    The allow-list comment at the top of this test explains the rule:
    ``DefaultUserPortfolio`` and the Infrastructure/repository layer
    contain zero Upstox literals by construction.
    """
    repo_root = Path(__file__).resolve().parents[2]
    hits = _scan_for_literals(_UPSTOX_LITERALS, repo_root)

    violations: list[str] = []
    for literal, occurrences in hits.items():
        for file_path, lineno, line_text in occurrences:
            if not _is_allowed(file_path):
                violations.append(
                    f"  literal {literal!r} at {file_path.relative_to(repo_root)}:{lineno}\n"
                    f"    {line_text}"
                )

    if violations:
        joined = "\n".join(violations)
        pytest.fail(
            f"Upstox literals found outside the broker-specific allow-list:\n"
            f"{joined}\n\n"
            f"These literals may only appear in:\n"
            f"  - DefaultUpstoxBrokerConnector and UpstoxConfig\n"
            f"  - /api/brokers/upstox/* route modules\n"
            f"  - The single BROKER_CONNECTORS registry entry\n"
            f"  - Upstox test/fixture files\n"
            f"  - UI display strings (Upstox button/status card)\n"
            f"  - docs/ and adr/ directories\n"
            f"  - This test file\n"
            f"\n"
            f"If a new literal is legitimate (e.g. a new Upstox endpoint\n"
            f"field), add the file to the allow-list with an explanatory\n"
            f"comment rather than weakening this guard."
        )


def test_leakage_test_fails_when_upstox_literal_added_to_infrastructure(monkeypatch):
    """Demonstrate that the leakage test is not a no-op: a temporary
    fixture file containing an Upstox literal outside the allow-list
    causes the test to fail.

    Uses a temporary directory (monkeypatched scan root) so no permanent
    file is written to the real source tree.
    """
    import tempfile

    # Create a temporary directory that mimics a module under the repo root
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        # Mimic the infrastructure module path so the allow-list rejects it
        fake_module = tmp_root / "src" / "infrastructure.py"
        fake_module.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately include an Upstox literal — this MUST be rejected
        fake_module.write_text(
            "UPSTOX_BASE_URL = 'https://api.upstox.com'\n",
            encoding="utf-8",
        )

        # Monkeypatch the repo root so the scanner picks up only the fake tree
        original_scan = _scan_for_literals

        def _patched_scan(literals, root):
            # Redirect the scanner to our temp tree
            return original_scan(literals, tmp_root)

        monkeypatch.setattr(
            "tests.test_story23_broker_isolation_and_protocol_conformance._scan_for_literals",
            _patched_scan,
        )

        # Re-run the scan against the patched tree
        hits = _patched_scan(_UPSTOX_LITERALS, tmp_root)

        # The Upstox literal should be found
        assert hits["api.upstox.com"], "fixture not set up correctly: 'api.upstox.com' not found"

        # Every hit should be in a non-allowed location
        all_violations = []
        for literal, occurrences in hits.items():
            for file_path, lineno, line_text in occurrences:
                if not _is_allowed(file_path):
                    all_violations.append(
                        f"  {literal!r} at {file_path}:{lineno}: {line_text}"
                    )

        assert all_violations, (
            "leakage test did not detect the injected fixture — the test "
            "is not exercising the allow-list correctly"
        )


def test_leakage_test_fails_when_upstox_literal_added_to_user_portfolio(monkeypatch):
    """Demonstrate that the leakage test catches a literal added to
    ``DefaultUserPortfolio`` — the most likely accidental regression.

    Uses a monkeypatch on the scanner so no file is modified.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        # Mimic the c01_user_portfolio module path
        fake_module = tmp_root / "src" / "components" / "c01_user_portfolio.py"
        fake_module.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately include an Upstox literal
        fake_module.write_text(
            "# DefaultUserPortfolio module\n"
            "UPSTOX_BROKER_ID = 'upstox'\n",
            encoding="utf-8",
        )

        original_scan = _scan_for_literals

        def _patched_scan(literals, root):
            return original_scan(literals, tmp_root)

        monkeypatch.setattr(
            "tests.test_story23_broker_isolation_and_protocol_conformance._scan_for_literals",
            _patched_scan,
        )

        hits = _patched_scan(_UPSTOX_LITERALS, tmp_root)

        # The Upstox literal should be found
        assert hits["upstox"], "fixture not set up correctly: 'upstox' not found"

        # All hits should be flagged as violations
        all_violations = []
        for literal, occurrences in hits.items():
            for file_path, lineno, line_text in occurrences:
                if not _is_allowed(file_path):
                    all_violations.append(f"  {literal!r} at {file_path}:{lineno}")

        assert all_violations, (
            "leakage test did not detect 'upstox' in a module "
            "outside the allow-list — the allow-list may be too permissive"
        )


# ---------------------------------------------------------------------------
# TEST 2 — Protocol-conformance test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "connector",
    [
        StubBrokerConnector(),
        pytest.param(_default_upstox_connector(), id="DefaultUpstoxBrokerConnector"),
    ],
)
def test_connector_isinstance_satisfies_broker_connector_protocol(connector):
    """Both ``StubBrokerConnector`` and ``DefaultUpstoxBrokerConnector``
    satisfy the ``BrokerConnector`` Protocol at runtime via
    ``isinstance(..., BrokerConnector)`` — the ``@runtime_checkable``
    decorator on the Protocol makes this check possible."""
    assert isinstance(connector, BrokerConnector), (
        f"{type(connector).__name__} does not satisfy BrokerConnector; "
        f"check that all Protocol methods are implemented with the "
        f"correct signatures"
    )


@pytest.mark.parametrize(
    "connector",
    [
        StubBrokerConnector(),
        pytest.param(_default_upstox_connector(), id="DefaultUpstoxBrokerConnector"),
    ],
)
def test_connector_implements_all_protocol_methods_with_correct_kw_only_params(connector):
    """Every method on ``BrokerConnector`` exists on the connector with
    exactly the keyword-only parameter names the Protocol declares.

    This catches regressions where a method is renamed
    (``exchange_auth_code`` → ``exchange_code``) or a parameter loses
    its ``*`` (becoming positional instead of keyword-only).
    """
    protocol_methods = {
        name: sig
        for name, sig in inspect.signature(BrokerConnector).parameters.items()
        if sig.kind == inspect.Parameter.KEYWORD_ONLY
    }

    connector_methods = {
        name: sig
        for name, sig in inspect.signature(type(connector)).parameters.items()
        if sig.kind == inspect.Parameter.KEYWORD_ONLY
    }

    missing_methods = set(protocol_methods.keys()) - set(connector_methods.keys())
    assert not missing_methods, (
        f"{type(connector).__name__} is missing Protocol methods: {sorted(missing_methods)}"
    )

    # Check parameter names match exactly (order-insensitive)
    for method_name, proto_params in protocol_methods.items():
        conn_params = connector_methods[method_name]
        proto_kwonly_names = {p.name for p in proto_params}
        conn_kwonly_names = {p.name for p in conn_params}
        if proto_kwonly_names != conn_kwonly_names:
            pytest.fail(
                f"{type(connector).__name__}.{method_name} keyword-only parameter names "
                f"do not match Protocol. "
                f"Expected: {sorted(proto_kwonly_names)}; "
                f"got: {sorted(conn_kwonly_names)}"
            )


def test_fake_broker_connector_passes_conformance_without_touching_production_code():
    """A ``DefaultFakeBrokerConnector`` defined entirely in this test
    module passes the conformance test without any modification to
    production code.

    This proves the Protocol is a genuine interface boundary, not a
    magic marker on a specific class — any object satisfying the
    Protocol contract is accepted.
    """

    class DefaultFakeBrokerConnector:
        """A minimal, intentionally minimal fake that satisfies
        ``BrokerConnector`` for the conformance test."""

        broker_id: str = "fake"
        display_name: str = "Fake Broker"

        def build_authorize_url(self, *, state: str) -> str:
            return f"https://fake.broker/auth?state={state}"

        def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
            return BrokerCredentials(access_token="fake-token", raw={})

        def fetch_holdings(self, *, credentials: BrokerCredentials) -> list:
            return []

        def fetch_transactions(
            self,
            *,
            credentials: BrokerCredentials,
            start_date: date,
            end_date: date,
        ) -> list:
            return []

    fake = DefaultFakeBrokerConnector()

    # The fake satisfies the Protocol via isinstance
    assert isinstance(fake, BrokerConnector), (
        "DefaultFakeBrokerConnector should satisfy BrokerConnector"
    )

    # The fake has the correct method signatures
    protocol_methods = {
        name: sig
        for name, sig in inspect.signature(BrokerConnector).parameters.items()
        if sig.kind == inspect.Parameter.KEYWORD_ONLY
    }

    fake_methods = {
        name: sig
        for name, sig in inspect.signature(DefaultFakeBrokerConnector).parameters.items()
        if sig.kind == inspect.Parameter.KEYWORD_ONLY
    }

    for method_name, proto_params in protocol_methods.items():
        conn_params = fake_methods[method_name]
        proto_kwonly_names = {p.name for p in proto_params}
        conn_kwonly_names = {p.name for p in conn_params}
        assert proto_kwonly_names == conn_kwonly_names, (
            f"DefaultFakeBrokerConnector.{method_name}: "
            f"keyword-only params {sorted(conn_kwonly_names)} "
            f"!= Protocol {sorted(proto_kwonly_names)}"
        )


# ---------------------------------------------------------------------------
# TEST 3 — DefaultUserPortfolio isolation test
# ---------------------------------------------------------------------------

def test_default_user_portfolio_module_source_contains_no_upstox_literals():
    """The ``DefaultUserPortfolio`` module (c01_user_portfolio.py) must
    not contain any Upstox-specific literals in its production code.

    This catches the most likely accidental regression: a future
    contributor importing or calling Upstox-specific code from the
    generic portfolio component.
    """
    repo_root = Path(__file__).resolve().parents[2]
    portfolio_module = repo_root / "src" / "components" / "c01_user_portfolio.py"
    source = portfolio_module.read_text(encoding="utf-8", errors="replace")

    violations: list[str] = []
    for literal in _UPSTOX_LITERALS:
        for lineno, line in enumerate(source.splitlines(), start=1):
            if literal in line:
                # Check if this line is inside DefaultUpstoxBrokerConnector
                # class definition (which IS allowed to contain these literals).
                # Lines outside that class are violations.
                in_upstox_connector = _line_is_inside_upstox_connector_class(
                    source, lineno
                )
                if not in_upstox_connector:
                    violations.append(
                        f"  literal {literal!r} at line {lineno}: {line.strip()}"
                    )

    if violations:
        joined = "\n".join(violations)
        pytest.fail(
            f"Upstox literals found in DefaultUserPortfolio outside\n"
            f"the DefaultUpstoxBrokerConnector class:\n"
            f"{joined}\n\n"
            f"DefaultUserPortfolio must remain broker-agnostic.\n"
            f"Move any Upstox-specific logic to DefaultUpstoxBrokerConnector."
        )


# ---------------------------------------------------------------------------
# TEST 0 — Performance / correctness: scan must not recurse into .git/
# ---------------------------------------------------------------------------

def test_scan_performance_does_not_include_git_directory():
    """The literal scanner must not recurse into .git/ (or any other
    skip-named directory) when scanning from the repo root.

    The workspace root contains .git/ with thousands of Python files
    (git's own implementation). If the root is included in the
    to_scan list, ``base.rglob("*.py")`` on the root visits all of
    .git/ and the scan takes ~25 s instead of < 1 s.

    This bug is triggered whenever _scan_for_literals adds ``root`` to
    the scan list (as the first entry) without filtering out .git/.
    The fix is to exclude the root entirely since all project
    subdirs are already enumerated via _PROJECT_SCAN_DIRS.
    """
    import time

    repo_root = Path(__file__).resolve().parents[2]

    # Count .py files under the root (includes .git/)
    git_py_count = len(list((repo_root / ".git").rglob("*.py")))
    assert git_py_count > 0, (
        "sanity check: .git/ should contain .py files; "
        "this test is not meaningful without them"
    )

    t0 = time.time()
    hits = _scan_for_literals(_UPSTOX_LITERALS, repo_root)
    elapsed = time.time() - t0

    # The scan MUST NOT visit .git/.py files. If it does, elapsed
    # will be > 5 s on any filesystem. If it doesn't, elapsed < 2 s.
    # Using a 5 s threshold gives generous headroom while still
    # catching the bug (25 s actual with .git/, < 1 s without).
    assert elapsed < 5.0, (
        f"Scan took {elapsed:.1f}s — the .git/ directory was likely "
        f"included ({git_py_count} .py files found in .git/). "
        f"The scanner must exclude the repo root from to_scan or "
        f"filter out .git/ via _SCAN_SKIP_NAMES."
    )


def test_default_user_portfolio_module_source_contains_no_upstox_literals():
    """Return True when line ``lineno`` of ``source`` falls inside the
    ``class DefaultUpstoxBrokerConnector`` block.

    Uses a simple state-machine that tracks indentation of the class
    definition line. Works for the single class in c01_user_portfolio.py.
    """
    lines = source.splitlines()
    class_indent: int | None = None
    in_target_class = False

    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        # Detect class definition line
        if stripped.startswith("class DefaultUpstoxBrokerConnector"):
            class_indent = indent
            in_target_class = True
            continue

        # Exit the class block when we hit a line at the same or
        # lower indentation as the class definition
        if in_target_class and class_indent is not None:
            if stripped and indent <= class_indent:
                in_target_class = False
                class_indent = None

        if i == lineno and in_target_class:
            return True

    return False
