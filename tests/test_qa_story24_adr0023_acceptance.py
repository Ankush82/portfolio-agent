"""QA tests for STORY-24: ADR-0023 acceptance, ADR-0022 note, and the
manual live-validation runbook at docs/runbooks/upstox-live-validation.md.

This is a documentation-only story -- there is no code under test, only
markdown / ADR files. The right verifications are:

  1. ADR-0023 status is `Accepted` and contains the decision text and
     all four consequence points listed in the story.
  2. ADR-0022 contains the appended note naming
     `DefaultUpstoxBrokerConnector` as the first real implementation.
  3. `docs/runbooks/upstox-live-validation.md` exists with a numbered,
     executable-by-a-human checklist including the specific spot-checks
     (holdings count, sample trade_ids/quantities/prices,
     total_records vs imported row count) and the explicit statement
     that automated tests must not hit sandbox or production Upstox.
  4. The three open questions (rate limits, expires_in/refresh_token,
     background/async import) are recorded in ADR-0023 as open, not
     silently resolved.
  5. No code changes in this story; docs link to the relevant modules
     and env vars by exact name.

These assertions read the real files from disk and check them against
the story's exact wording. Each test asserts one acceptance criterion
or a tight cluster of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_0022_PATH = REPO_ROOT / "adr" / "0022-user-portfolio-broker-connector-interface.md"
ADR_0023_PATH = REPO_ROOT / "adr" / "0023-user-portfolio-broker-api-choice-interim.md"
RUNBOOK_PATH = REPO_ROOT / "docs" / "runbooks" / "upstox-live-validation.md"


@pytest.fixture(scope="module")
def adr_0022_text() -> str:
    assert ADR_0022_PATH.exists(), f"ADR-0022 missing at {ADR_0022_PATH}"
    return ADR_0022_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adr_0023_text() -> str:
    assert ADR_0023_PATH.exists(), f"ADR-0023 missing at {ADR_0023_PATH}"
    return ADR_0023_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK_PATH.exists(), f"runbook missing at {RUNBOOK_PATH}"
    return RUNBOOK_PATH.read_text(encoding="utf-8")


def _section(text: str, heading_pattern: str) -> str:
    """Return the text of a markdown section whose H2/H3 heading matches
    `heading_pattern`. Raises if not found."""
    match = re.search(
        r"^(##+)\s+" + heading_pattern + r"\s*$([\s\S]*?)(?=^\##+ |\Z)",
        text,
        flags=re.MULTILINE,
    )
    assert match, f"section matching {heading_pattern!r} not found"
    return match.group(2)


# ---------------------------------------------------------------------------
# AC #1: ADR-0023 status is Accepted and contains the decision text +
# all four consequence points listed in the story.
# ---------------------------------------------------------------------------


def test_adr_0023_status_is_accepted(adr_0023_text: str):
    # The story requires status == Accepted.
    status_match = re.search(
        r"^\*\*Status:\*\*\s*([^\n]+)", adr_0023_text, flags=re.MULTILINE
    )
    assert status_match, "ADR-0023 missing '**Status:**' line"
    status = status_match.group(1).strip()
    # Tolerate trailing date annotations like "Accepted — 2026-08-26"
    # but the word must be present and not "Proposed" / "Superseded" etc.
    assert re.match(r"Accepted\b", status), (
        f"ADR-0023 status must be 'Accepted', got: {status!r}"
    )
    assert "Proposed" not in status.split("Accepted")[0], (
        f"ADR-0023 status contains 'Proposed' before 'Accepted': {status!r}"
    )


def test_adr_0023_decision_text_present(adr_0023_text: str):
    # The decision section must contain the required phrasing, key
    # phrases being:
    #   - "Upstox is the first and currently only supported broker"
    #   - "Zerodha and Groww remain named-but-undecided future options"
    #   - "Default* implementations behind the same BrokerConnector
    #     Protocol (ADR-0022)"
    decision = _section(adr_0023_text, r"Decision")
    assert "Upstox is the first and currently only supported broker" in decision, (
        "ADR-0023 Decision missing required 'Upstox is the first ...' phrase"
    )
    assert "Zerodha and Groww remain named-but-undecided" in decision, (
        "ADR-0023 Decision missing required 'Zerodha and Groww remain "
        "named-but-undecided future options' phrase"
    )
    # Protocol reference must mention the ADR-0022 cross-link.
    assert re.search(r"BrokerConnector.*Protocol.*ADR-0022", decision), (
        "ADR-0023 Decision must reference the BrokerConnector Protocol and ADR-0022"
    )
    # Future broker additions must be framed as Default* implementations.
    # The story wording uses the Markdown `` `Default*` `` form; accept
    # either bare or backtick-wrapped form.
    assert re.search(
        r"`?Default\*`?\s+implementations", decision
    ), (
        "ADR-0023 Decision must mention future Default* implementations"
    )


def test_adr_0023_consequence_sandbox_gap(adr_0023_text: str):
    # AC #1 consequence point 1: sandbox.upstox.com covers ONLY order
    # placement/modify/cancel and does NOT cover long-term-holdings or
    # charges/historical-trades; therefore correctness is established
    # only against hand-authored fixtures.
    consequences = _section(adr_0023_text, r"Consequences")
    # Required phrases from the story:
    assert "sandbox.upstox.com" in consequences, (
        "ADR-0023 Consequences must reference sandbox.upstox.com"
    )
    assert "order placement/modify/cancel" in consequences, (
        "ADR-0023 Consequences must state the sandbox only covers "
        "order placement/modify/cancel"
    )
    # Must state the sandbox does not cover the read endpoints.
    assert re.search(
        r"long-term-holdings|charges/historical-trades", consequences
    ), (
        "ADR-0023 Consequences must name the read endpoints the sandbox "
        "does NOT cover (long-term-holdings or charges/historical-trades)"
    )
    # Must state correctness is established only against hand-authored
    # fixtures, not softened away.
    assert re.search(
        r"hand-authored\s+fixtures|hand-?authored\s+fixture", consequences, re.IGNORECASE
    ), (
        "ADR-0023 Consequences must state correctness is established "
        "only against hand-authored fixtures"
    )


def test_adr_0023_consequence_credential_dependency(adr_0023_text: str):
    # AC #1 consequence point 2: external credential dependency
    # UPSTOX_CLIENT_ID / UPSTOX_CLIENT_SECRET / UPSTOX_REDIRECT_URI,
    # of the same shape as OPENROUTER_API_KEY / ALPHA_VANTAGE_API_KEY.
    consequences = _section(adr_0023_text, r"Consequences")
    for env_var in (
        "UPSTOX_CLIENT_ID",
        "UPSTOX_CLIENT_SECRET",
        "UPSTOX_REDIRECT_URI",
        "OPENROUTER_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
    ):
        assert env_var in consequences, (
            f"ADR-0023 Consequences missing env var {env_var!r}"
        )


def test_adr_0023_consequence_transaction_history_depth(adr_0023_text: str):
    # AC #1 consequence point 3: transaction history bounded by
    # Upstox to the last 3 financial years.
    consequences = _section(adr_0023_text, r"Consequences")
    assert "3 financial years" in consequences, (
        "ADR-0023 Consequences must state transaction history is bounded "
        "to the last 3 financial years"
    )


def test_adr_0023_consequence_no_silent_softening(adr_0023_text: str):
    # The story explicitly says the consequences must state the gaps
    # "without softening". Sanity check the prose is honest: the
    # sandbox-gap consequence must NOT paper over the gap with
    # weasel words. At least one of the required negative phrases
    # must be present.
    consequences = _section(adr_0023_text, r"Consequences")
    # The story says the sandbox "does not appear to cover" -- the
    # honest phrasing in the consequence section should explicitly
    # state there is no sandbox path to validate end-to-end.
    assert re.search(
        r"no sandbox path|no end-to-end\s+sandbox|cannot be validated.*sandbox",
        consequences,
        re.IGNORECASE,
    ), (
        "ADR-0023 Consequences must honestly state there is no sandbox "
        "path to validate the real read flow end-to-end"
    )


# ---------------------------------------------------------------------------
# AC #2: ADR-0022 contains the appended note naming
# DefaultUpstoxBrokerConnector as the first real implementation.
# ---------------------------------------------------------------------------


def test_adr_0022_contains_appended_note(adr_0022_text: str):
    # The story requires a note appended to ADR-0022 naming
    # DefaultUpstoxBrokerConnector as the first real implementation,
    # and noting that Protocol signatures were finalised with
    # broker-agnostic types only.
    assert re.search(
        r"^##\s+Note\s*\(added\s+at\s+ADR-0023\s+acceptance\)",
        adr_0022_text,
        flags=re.MULTILINE | re.IGNORECASE,
    ), (
        "ADR-0022 must contain a '## Note (added at ADR-0023 acceptance)' section"
    )

    # Locate the appended Note section.
    note_section = _section(
        adr_0022_text, r"Note\s*\(added\s+at\s+ADR-0023\s+acceptance\)"
    )
    assert "DefaultUpstoxBrokerConnector" in note_section, (
        "ADR-0022 appended note must name DefaultUpstoxBrokerConnector"
    )
    assert "first real implementation" in note_section, (
        "ADR-0022 appended note must explicitly call "
        "DefaultUpstoxBrokerConnector the 'first real implementation'"
    )
    # Protocol-signatures-finalised-with-broker-agnostic-types requirement.
    assert re.search(
        r"broker-?agnostic", note_section, re.IGNORECASE
    ), (
        "ADR-0022 appended note must mention broker-agnostic types"
    )


# ---------------------------------------------------------------------------
# AC #3: docs/runbooks/upstox-live-validation.md exists with a numbered,
# executable-by-a-human checklist including the specific spot-checks and
# the explicit statement that automated tests must not hit Upstox.
# ---------------------------------------------------------------------------


def test_runbook_exists():
    assert RUNBOOK_PATH.exists(), (
        f"docs/runbooks/upstox-live-validation.md must exist at {RUNBOOK_PATH}"
    )


def test_runbook_has_numbered_executable_checklist(runbook_text: str):
    # The story requires a "numbered, executable-by-a-human checklist".
    # Locate the checklist section and assert it uses numbered list
    # items (Markdown `1.`, `2.`, ...) -- not just `- [ ]` unnumbered.
    # The runbook uses '1. [ ]' style numbered checklist items.
    checklist_match = re.search(
        r"^##\s+9\.\s+The checklist\b([\s\S]*?)(?=^##\s+|\Z)",
        runbook_text,
        flags=re.MULTILINE,
    )
    assert checklist_match, (
        "runbook must contain a '## 9. The checklist' section "
        "with a numbered checklist"
    )
    checklist = checklist_match.group(1)
    # Must contain numbered list items.
    numbered_items = re.findall(r"(?m)^\s*\d+\.\s+\[[ xX]\]", checklist)
    assert len(numbered_items) >= 10, (
        f"checklist must have >= 10 numbered tickbox items, found {len(numbered_items)}"
    )


def test_runbook_lists_specific_spot_checks(runbook_text: str):
    # The story calls out three specific spot-checks:
    #   - holdings count vs Upstox web portal
    #   - sample trade_ids / quantities / prices
    #   - total_records vs imported row count (multi-page paging)
    # All three must appear in the runbook.
    assert "holdings count" in runbook_text, (
        "runbook must include the holdings count spot-check"
    )
    assert "trade_id" in runbook_text, (
        "runbook must mention trade_id for the sample-trade spot-check"
    )
    assert "quantity" in runbook_text, (
        "runbook must mention quantity for the sample spot-check"
    )
    assert "price" in runbook_text, (
        "runbook must mention price for the sample spot-check"
    )
    assert "total_records" in runbook_text, (
        "runbook must mention total_records for the multi-page paging spot-check"
    )


def test_runbook_states_automated_tests_must_not_hit_upstox(runbook_text: str):
    # The story requires: "the explicit statement that automated tests
    # must not hit sandbox or production Upstox". Must appear verbatim
    # enough to be unmistakable -- e.g. "Automated tests must never call
    # the real or sandbox Upstox API" OR equivalent phrasing that
    # names both "automated tests" / "automated" + "sandbox" + "real".
    # Match a permissive regex that captures either phrasing.
    has_phrase = bool(
        re.search(
            r"[Aa]utomated\s+tests?\s+(must|should|may\s+never)\s+(never\s+|not\s+)?"
            r"(call|hit|reach|touch|invoke)\s+(the\s+)?(real|production|live)?"
            r"\s*(or|/)?\s*sandbox\s+Upstox",
            runbook_text,
        )
    )
    if not has_phrase:
        # Looser fallback: must contain at least one of the canonical
        # phrases either verbatim or as a clear equivalent.
        has_phrase = (
            "Automated tests must never call the real or sandbox Upstox API"
            in runbook_text
            or "automated tests must not hit sandbox or production Upstox"
            in runbook_text.lower()
            or re.search(
                r"automated\s+tests?\s+must\s+never.*sandbox\s+upstox",
                runbook_text,
                re.IGNORECASE | re.DOTALL,
            )
            is not None
        )
    assert has_phrase, (
        "runbook must contain the explicit statement that automated "
        "tests must not call the real or sandbox Upstox API"
    )


def test_runbook_documents_env_vars_and_redirect_uri(runbook_text: str):
    # The story calls out: "register app with the exact redirect URI,
    # set env vars". The runbook must mention all four env vars and
    # the exact redirect URI.
    assert "UPSTOX_CLIENT_ID" in runbook_text
    assert "UPSTOX_CLIENT_SECRET" in runbook_text
    assert "UPSTOX_REDIRECT_URI" in runbook_text
    assert "BROKER_TOKEN_ENCRYPTION_KEY" in runbook_text
    # Exact redirect URI must appear, not just the env var name.
    assert "http://localhost:8000/brokers/upstox/callback" in runbook_text, (
        "runbook must include the exact redirect URI "
        "http://localhost:8000/brokers/upstox/callback"
    )


def test_runbook_includes_connect_flow_check(runbook_text: str):
    # Acceptance criterion from the story: "run connect flow, confirm
    # a CONNECTED row". The runbook must mention CONNECTED state for
    # the broker_connections table.
    assert "CONNECTED" in runbook_text, (
        "runbook must mention the CONNECTED state"
    )
    assert "broker_connections" in runbook_text, (
        "runbook must reference the broker_connections table"
    )


def test_runbook_includes_revoke_disconnect_step(runbook_text: str):
    # The story calls out: "then revoke/disconnect". Runbook must
    # include a revoke/disconnect step.
    assert re.search(r"[Rr]evoke", runbook_text), (
        "runbook must include a revoke step"
    )
    assert re.search(r"[Dd]isconnect", runbook_text), (
        "runbook must include a disconnect step"
    )


# ---------------------------------------------------------------------------
# AC #4: The three open questions are recorded in ADR-0023 as open,
# not silently resolved.
# ---------------------------------------------------------------------------


def _open_questions_section(text: str) -> str:
    match = re.search(
        r"^##\s+Open questions[^\n]*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        flags=re.MULTILINE,
    )
    assert match, (
        "ADR-0023 must contain a '## Open questions' section"
    )
    return match.group(1)


def test_adr_0023_has_open_questions_section(adr_0023_text: str):
    # Must be present and named exactly "Open questions" (or close).
    section = _open_questions_section(adr_0023_text)
    # Must be non-trivial.
    assert len(section.strip()) > 200, (
        "ADR-0023 Open questions section must contain real content"
    )


def test_adr_0023_records_rate_limits_question(adr_0023_text: str):
    section = _open_questions_section(adr_0023_text)
    # Must explicitly state Upstox rate limits are undocumented AND
    # the current policy is a conservative guess.
    assert re.search(r"rate\s+limits?", section, re.IGNORECASE), (
        "ADR-0023 Open questions must mention Upstox rate limits"
    )
    assert re.search(
        r"undocumented|not\s+documented|no\s+documented",
        section,
        re.IGNORECASE,
    ), (
        "ADR-0023 Open questions must state Upstox rate limits are undocumented"
    )
    assert re.search(
        r"conservative\s+guess|guess", section, re.IGNORECASE
    ), (
        "ADR-0023 Open questions must call out that the retry policy is "
        "a conservative guess, not a documented limit"
    )


def test_adr_0023_records_expires_in_refresh_token_question(adr_0023_text: str):
    section = _open_questions_section(adr_0023_text)
    # Must explicitly raise the expires_in / refresh_token question
    # and state they are currently handled as absent.
    assert "expires_in" in section, (
        "ADR-0023 Open questions must mention 'expires_in'"
    )
    assert "refresh_token" in section, (
        "ADR-0023 Open questions must mention 'refresh_token'"
    )
    assert re.search(r"\babsent\b", section, re.IGNORECASE), (
        "ADR-0023 Open questions must say these fields are handled as absent"
    )


def test_adr_0023_records_background_async_import_question(adr_0023_text: str):
    section = _open_questions_section(adr_0023_text)
    # Must explicitly raise the background/async import question.
    assert re.search(
        r"background[/ ]async\s+import\s+job|background\s+job|async\s+import",
        section,
        re.IGNORECASE,
    ), (
        "ADR-0023 Open questions must raise the background/async import job question"
    )
    # Must say it's still unknown -- not silently decided.
    assert re.search(
        r"unknown|not\s+known|do(es)?n['']t\s+know|until\s+real",
        section,
        re.IGNORECASE,
    ), (
        "ADR-0023 Open questions must say the background-import question "
        "is still unknown / pending real portfolio sizes"
    )


# ---------------------------------------------------------------------------
# AC #5: No code changes in this story; docs link to the relevant
# modules and env vars by exact name.
# ---------------------------------------------------------------------------


def test_runbook_links_to_real_modules(runbook_text: str):
    # The story says: "docs link to the relevant modules and env vars
    # by exact name". Each module the runbook references must exist in
    # the repo, and the runbook should mention it by exact path.
    module_paths = [
        "src/components/c01_user_portfolio.py",
        "src/upstox_http.py",
        "src/upstox_config.py",
        "src/broker_token_crypto.py",
    ]
    for module in module_paths:
        assert module in runbook_text, (
            f"runbook must reference module {module!r} by exact path"
        )
        assert (REPO_ROOT / module).exists(), (
            f"runbook references {module} but it does not exist in repo"
        )


def test_adr_0023_links_to_real_modules(adr_0023_text: str):
    # Same cross-check for ADR-0023.
    for module in (
        "src/components/c01_user_portfolio.py",
        "src/upstox_http.py",
        "src/upstox_config.py",
        "src/broker_token_crypto.py",
    ):
        assert module in adr_0023_text, (
            f"ADR-0023 must reference module {module!r} by exact path"
        )
        assert (REPO_ROOT / module).exists(), (
            f"ADR-0023 references {module} but it does not exist in repo"
        )


def test_adr_0023_links_to_env_vars_by_name(adr_0023_text: str):
    # The story says: "docs link to the relevant ... env vars by exact
    # name". Must use the same variable names the .env.example uses.
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for env_var in (
        "UPSTOX_CLIENT_ID",
        "UPSTOX_CLIENT_SECRET",
        "UPSTOX_REDIRECT_URI",
        "BROKER_TOKEN_ENCRYPTION_KEY",
    ):
        assert env_var in adr_0023_text, (
            f"ADR-0023 must reference env var {env_var!r} by exact name"
        )
        # And the env var must actually be documented in .env.example
        # -- guards against invented names.
        assert env_var in env_example, (
            f"ADR-0023 references env var {env_var!r} but .env.example "
            f"does not document it"
        )


def test_runbook_links_to_fixtures(runbook_text: str):
    # Story: docs link to the relevant modules and env vars by exact
    # name. The runbook talks about hand-authored fixtures; those must
    # actually exist on disk.
    fixtures_dir = REPO_ROOT / "tests" / "fixtures" / "upstox"
    if fixtures_dir.exists():
        # If fixtures exist, the runbook must reference at least one
        # of them by name.
        referenced_fixtures = [
            "long_term_holdings_empty.json",
            "long_term_holdings_success.json",
        ]
        for fixture in referenced_fixtures:
            if fixture in runbook_text:
                assert (fixtures_dir / fixture).exists(), (
                    f"runbook references {fixture} but it does not exist"
                )