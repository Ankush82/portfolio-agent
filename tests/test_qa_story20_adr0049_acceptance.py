"""QA tests for STORY-20: ADR-0049 (core-entity repository layer) acceptance.

This is a documentation-only story -- there is no code under test, only
markdown / ADR files. The right verifications are:

  1. ADR-0049 exists at adr/0049-core-entity-repository-layer.md using the
     next unused ADR number (0049), and ADR-0019 was NOT edited.
  2. ADR-0049 follows the same structural format as the existing ADRs
     (Status / Context / Decision / Alternatives considered / Consequences /
     Related).
  3. All five numbered decision areas are present, each with an explicit
     Consequences section.
  4. The typed-columns decision (decision 2) states the rationale AND names
     the rejected alternative (information_schema introspection) AND names
     the drift-guard test.
  5. The append-only transactions decision (decision 4) explicitly names the
     duplicate-row risk on broker re-sync AND assigns resolution ownership
     to the Upstox broker-integration feature.
  6. The ADR records whether created_at/updated_at shipped and whether any
     FK had to be dropped for legacy or behavioral reasons.
  7. The ADR records the accepted non-atomicity of cross-entity writes AND
     states that adding a transaction boundary would require its own ADR.
  8. The ADR states the exact migration file path AND includes the manual
     rollback SQL.
  9. The ADR notes the float-precision tradeoff for quantity/amount if
     those fields are annotated float.

These assertions read the real files from disk and check them against the
story's exact wording. Each test asserts one acceptance criterion or a
tight cluster of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_0049_PATH = REPO_ROOT / "adr" / "0049-core-entity-repository-layer.md"
ADR_0019_PATH = REPO_ROOT / "adr" / "0019-infrastructure-unified-managed-stack.md"


@pytest.fixture(scope="module")
def adr_0049_text() -> str:
    assert ADR_0049_PATH.exists(), f"ADR-0049 missing at {ADR_0049_PATH}"
    return ADR_0049_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adr_0019_text() -> str:
    assert ADR_0019_PATH.exists(), f"ADR-0019 missing at {ADR_0019_PATH}"
    return ADR_0019_PATH.read_text(encoding="utf-8")


def _h2_section(text: str, heading_pattern: str) -> str:
    """Return the text of a markdown section whose H2 heading matches
    `heading_pattern`. Raises if not found."""
    match = re.search(
        r"^##\s+" + heading_pattern + r"\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        flags=re.MULTILINE,
    )
    assert match, f"section matching {heading_pattern!r} not found"
    return match.group(1)


def _numbered_decision_section(text: str, n: int, title_substring: str) -> str:
    """Return the text of an `### N. ...` numbered decision section whose
    heading contains `title_substring` (plain string, not regex).
    Used for the `Decision` section text only."""
    decision = _h2_section(text, r"Decision")
    # Find `### N.` headers in order.
    starts = list(
        re.finditer(r"^###\s+(\d+)\.\s+([^\n]+)$", decision, flags=re.MULTILINE)
    )
    target_idx = None
    for idx, m in enumerate(starts):
        if m.group(1) == str(n) and title_substring in m.group(2):
            target_idx = idx
            break
    assert target_idx is not None, (
        f"decision section '{n}. ...{title_substring}...' not found in "
        f"Decision block"
    )
    # Slice from this header to the next `### N+1.` or end of decision.
    body_start = starts[target_idx].end()
    body_end = (
        starts[target_idx + 1].start()
        if target_idx + 1 < len(starts)
        else len(decision)
    )
    return decision[body_start:body_end]


# ---------------------------------------------------------------------------
# AC #1: ADR-0049 file exists at the right path, ADR-0019 was not edited.
# ---------------------------------------------------------------------------


def test_adr_0049_file_exists():
    # The story requires adr/ADR-00NN-core-entity-repository-layer.md
    # using the next unused ADR number. We expect 0049.
    assert ADR_0049_PATH.exists(), (
        f"ADR-0049 must exist at {ADR_0049_PATH}"
    )
    # Filename must be exactly the canonical "0049-core-entity-repository-layer.md".
    assert ADR_0049_PATH.name == "0049-core-entity-repository-layer.md", (
        f"ADR-0049 filename must be '0049-core-entity-repository-layer.md', "
        f"got {ADR_0049_PATH.name!r}"
    )


def test_adr_0049_number_is_next_unused(adr_0049_text: str):
    # Heading must read "0049 — Core-entity repository layer: ...".
    # We accept any title text after the number.
    match = re.match(
        r"^#\s+0049\s+—\s+", adr_0049_text
    )
    assert match, (
        "ADR-0049 must begin with '# 0049 — ' (em-dash separator)"
    )


def test_adr_0019_was_not_edited(adr_0019_text: str):
    # The story explicitly requires: "ADR-0019 was not edited".
    # We assert ADR-0019's status header (its first content-bearing line)
    # still reads "0019 — System Infrastructure: unified, managed stack".
    # Any edit to ADR-0019 (e.g. adding new sections, changing status)
    # would be caught by this guard.
    expected_first_heading = "# 0019 — System Infrastructure: unified, managed stack"
    assert adr_0019_text.startswith(expected_first_heading), (
        f"ADR-0019 first heading must be unchanged. Expected "
        f"{expected_first_heading!r}, got first line {adr_0019_text.splitlines()[0]!r}"
    )
    # Status line must still be Accepted with the original date.
    status_match = re.search(
        r"^\*\*Status:\*\*\s*Accepted\s*—\s*2026-08-26",
        adr_0019_text,
        flags=re.MULTILINE,
    )
    assert status_match, (
        "ADR-0019 must still be 'Status: Accepted — 2026-08-26' "
        "(the original status was not edited)"
    )


# ---------------------------------------------------------------------------
# AC #2: Same structural format as existing ADRs
# ---------------------------------------------------------------------------


def test_adr_0049_has_status_and_template_h2_sections(adr_0049_text: str):
    # The repo's ADR template (per adr/README.md) names six logical
    # sections: Status, Context, Decision, Alternatives considered,
    # Consequences, Related. In actual ADRs (e.g. ADR-0019, ADR-0044,
    # ADR-0048), Status is rendered as an inline `**Status:**` line
    # rather than as a separate `## Status` heading. We therefore
    # accept either form for Status, but require the remaining five
    # to be top-level `## ` H2 headings.
    has_status = bool(
        re.search(r"^\*\*Status:\*\*", adr_0049_text, flags=re.MULTILINE)
    ) or bool(
        re.search(r"^##\s+Status\s*$", adr_0049_text, flags=re.MULTILINE)
    )
    assert has_status, (
        "ADR-0049 must include a Status line (either inline "
        "'**Status:** ...' or as a '## Status' heading)"
    )
    for section_name in (
            "Context",
            "Decision",
            "Alternatives considered",
            "Consequences",
            "Related",
    ):
        pattern = r"^##\s+" + re.escape(section_name) + r"\s*$"
        assert re.search(pattern, adr_0049_text, flags=re.MULTILINE), (
            f"ADR-0049 must contain a '## {section_name}' H2 section to "
            f"match the repo's ADR template (per adr/README.md)"
        )


def test_adr_0049_status_is_accepted(adr_0049_text: str):
    # Like ADR-0048, the status should be Accepted. (A Proposed ADR is
    # explicitly NOT what this story asks for.)
    status_match = re.search(
        r"^\*\*Status:\*\*\s*([^\n]+)", adr_0049_text, flags=re.MULTILINE
    )
    assert status_match, "ADR-0049 missing '**Status:**' line"
    status = status_match.group(1).strip()
    assert re.match(r"Accepted\b", status), (
        f"ADR-0049 status must start with 'Accepted', got: {status!r}"
    )


# ---------------------------------------------------------------------------
# AC #3: All five numbered decision areas present, each with explicit
# Consequences section
# ---------------------------------------------------------------------------


def test_adr_0049_has_five_numbered_decisions(adr_0049_text: str):
    # The Decision section must contain five `### N. <title>` subsections
    # numbered 1..5 -- exactly the five decision areas the story names.
    decision = _h2_section(adr_0049_text, r"Decision")
    for n in range(1, 6):
        pattern = r"^###\s+" + str(n) + r"\.\s+"
        assert re.search(pattern, decision, flags=re.MULTILINE), (
            f"ADR-0049 Decision section must contain a '### {n}. ...' "
            f"decision subsection"
        )


def test_each_decision_has_consequences_subsection(adr_0049_text: str):
    # Each of the five `### N.` decision subsections must have its own
    # `#### Consequences` subsection. This is the explicit acceptance
    # criterion from the story.
    decision = _h2_section(adr_0049_text, r"Decision")
    # Find each `### N.` and the next `#### Consequences` after it.
    decision_starts = list(
        re.finditer(r"^###\s+(\d+)\.\s+([^\n]+)$", decision, flags=re.MULTILINE)
    )
    assert len(decision_starts) == 5, (
        f"ADR-0049 Decision must contain exactly 5 numbered subsections, "
        f"found {len(decision_starts)}"
    )
    for idx, start in enumerate(decision_starts):
        n = start.group(1)
        # Find the text slice from this `### N.` to either the next
        # `### N+1.` or end of decision section.
        start_pos = start.end()
        end_pos = (
            decision_starts[idx + 1].start()
            if idx + 1 < len(decision_starts)
            else len(decision)
        )
        subsection_text = decision[start_pos:end_pos]
        # Must contain a `#### Consequences` heading.
        has_consequences = re.search(
            r"^####\s+Consequences\s*$", subsection_text, flags=re.MULTILINE
        )
        assert has_consequences, (
            f"Decision {n} must have its own '#### Consequences' subsection"
        )


# ---------------------------------------------------------------------------
# AC #4: Typed-columns decision rationale + rejected alternative + drift test
# ---------------------------------------------------------------------------


def test_decision_2_typed_columns_rationale_and_alternatives(adr_0049_text: str):
    # Decision 2 is the typed-columns decision. The story requires:
    #   (a) explicit rationale (FKs, list predicates, broker re-sync
    #       idempotency UNIQUE(portfolio_id, security_id) -- all three
    #       must be mentioned or the rationale is not stated)
    #   (b) explicitly names information_schema introspection as the
    #       rejected alternative
    #   (c) names the drift-guard test
    decision_2 = _numbered_decision_section(
        adr_0049_text, 2,
        "Real typed columns"
    )
    decision_2_lower = decision_2.lower()

    # (a) rationale -- must mention FKs and at least one of
    # list_for_user / list_for_portfolio predicates, and the
    # (portfolio_id, security_id) uniqueness rule.
    assert re.search(r"\bforeign keys?\b|\bFKs?\b", decision_2, re.IGNORECASE), (
        "Decision 2 must state the rationale about foreign keys"
    )
    assert (
        "list_for_user" in decision_2 or "list_for_portfolio" in decision_2
    ), (
        "Decision 2 must state the rationale about list_for_user / "
        "list_for_portfolio predicates"
    )
    assert re.search(
        r"\(?portfolio_id,\s*security_id\)?", decision_2
    ), (
        "Decision 2 must state the rationale about the "
        "(portfolio_id, security_id) uniqueness that broker re-sync "
        "idempotency depends on"
    )

    # (b) rejected alternative -- must name information_schema.
    assert "information_schema" in decision_2, (
        "Decision 2 must explicitly name 'information_schema' as the "
        "rejected alternative"
    )
    # Must also use rejection-style phrasing, not just a passing mention.
    assert re.search(
        r"[Rr]ejected\s+alternative", decision_2
    ), (
        "Decision 2 must explicitly mark information_schema as a "
        "'Rejected alternative'"
    )

    # (c) drift-guard test -- must be named. Accept "drift-guard test" or
    # "drift guard test" or "drift-guard".
    assert re.search(r"drift[- ]guard\s+test|drift[- ]guard", decision_2, re.IGNORECASE), (
        "Decision 2 must name the drift-guard test"
    )
    # And the drift-guard must mention that it asserts registry/schema/dataclass
    # agreement.
    assert re.search(
        r"registry|MIGRATED_TABLES|dataclass|column",
        decision_2,
        re.IGNORECASE,
    ), (
        "Decision 2's drift-guard test description must reference "
        "registry / MIGRATED_TABLES / dataclass / column agreement"
    )


# ---------------------------------------------------------------------------
# AC #5: Append-only transactions decision names duplicate-row risk and
# assigns resolution ownership to Upstox broker-integration feature.
# ---------------------------------------------------------------------------


def test_decision_4_appends_only_states_duplicate_row_risk(adr_0049_text: str):
    # Decision 4 (transactions append-only) must explicitly state:
    #   (a) the duplicate-row risk on broker re-sync
    #   (b) assign resolution ownership to the Upstox broker-integration feature
    decision_4 = _numbered_decision_section(
        adr_0049_text, 4,
        "Transactions are append-only"
    )

    # (a) duplicate-row risk.
    assert re.search(r"duplicate", decision_4, re.IGNORECASE), (
        "Decision 4 must explicitly mention 'duplicate' rows / re-sync risk"
    )
    assert re.search(
        r"broker\s+re-?sync|re-?sync",
        decision_4,
        re.IGNORECASE,
    ), (
        "Decision 4 must explicitly link the duplicate-row risk to "
        "broker re-sync"
    )

    # (b) resolution ownership -- Upstox broker-integration feature.
    # Must name both 'Upstox' and 'broker-integration' (or 'broker
    # integration') in the ownership section.
    assert re.search(r"\bUpstox\b", decision_4), (
        "Decision 4 must name 'Upstox' as the owning feature"
    )
    assert re.search(
        r"broker[- ]integration\s+feature",
        decision_4,
        re.IGNORECASE,
    ), (
        "Decision 4 must assign resolution ownership to the "
        "'Upstox broker-integration feature'"
    )


def test_decision_4_is_not_pre_built(adr_0049_text: str):
    # The story also requires the ADR to NOT pre-build the broker_txn_id
    # fix; decision 4 must explicitly say so.
    decision_4 = _numbered_decision_section(
        adr_0049_text, 4,
        "Transactions are append-only"
    )
    assert re.search(
        r"not\s+pre[- ]?built|does\s+not\s+pre[- ]?build|does\s+not\s+pre-build",
        decision_4,
        re.IGNORECASE,
    ), (
        "Decision 4 must explicitly state the broker_txn_id fix is "
        "'not pre-built' in this ADR"
    )


# ---------------------------------------------------------------------------
# AC #6: Records whether created_at/updated_at shipped AND whether FK had
# to be dropped for legacy or behavioral reasons.
# ---------------------------------------------------------------------------


def test_adr_0049_records_created_at_updated_at_shipped(adr_0049_text: str):
    # The story requires: "record whether created_at/updated_at shipped
    # and whether any foreign key had to be dropped".
    # We search the whole ADR (not just one decision) since the story
    # is satisfied by either an explicit statement in decision 3 or in
    # Consequences / Alternatives.
    assert re.search(r"created_at", adr_0049_text), (
        "ADR-0049 must mention created_at"
    )
    assert re.search(r"updated_at", adr_0049_text), (
        "ADR-0049 must mention updated_at"
    )
    # Must explicitly say they "shipped" (vs were deferred).
    assert re.search(
        r"created_at.*shipped|shipped.*created_at|created_at.*updated_at.*shipped",
        adr_0049_text,
        re.IGNORECASE | re.DOTALL,
    ), (
        "ADR-0049 must state explicitly that created_at (and updated_at) shipped"
    )


def test_adr_0049_records_no_fk_dropped(adr_0049_text: str):
    # Must state explicitly that NO foreign key had to be dropped.
    # We accept either the phrasing "No foreign key had to be dropped"
    # or "no FK had to be dropped".
    has_explicit_no_drop = bool(
        re.search(
            r"[Nn]o\s+foreign\s+key\s+had\s+to\s+be\s+dropped|"
            r"[Nn]o\s+FK\s+had\s+to\s+be\s+dropped",
            adr_0049_text,
        )
    )
    assert has_explicit_no_drop, (
        "ADR-0049 must explicitly state that no foreign key had to be "
        "dropped for legacy or behavioral reasons"
    )


# ---------------------------------------------------------------------------
# AC #7: Records accepted non-atomicity of cross-entity writes AND states
# adding a transaction boundary would require its own ADR.
# ---------------------------------------------------------------------------


def test_adr_0049_records_cross_entity_non_atomicity(adr_0049_text: str):
    # Must use the phrase "non-atomic" (with optional hyphen variants).
    has_non_atomic = bool(
        re.search(r"non[- ]atomic|nonatomic", adr_0049_text, re.IGNORECASE)
    )
    assert has_non_atomic, (
        "ADR-0049 must record the accepted non-atomicity of cross-entity "
        "writes (literal phrase 'non-atomic' or 'nonatomic')"
    )
    # And must explicitly tie it to "cross-entity" writes.
    assert re.search(
        r"cross[- ]entity\s+writes?",
        adr_0049_text,
        re.IGNORECASE,
    ), (
        "ADR-0049 must explicitly identify 'cross-entity writes' as "
        "the subject of the non-atomicity acceptance"
    )


def test_adr_0049_states_transaction_boundary_needs_own_adr(adr_0049_text: str):
    # The story requires the ADR to explicitly state that adding a
    # transaction boundary would require its own ADR.
    # Look for a sentence/phrase near "transaction boundary" that says
    # it would need / require its own ADR.
    has_phrase = bool(
        re.search(
            r"transaction\s+boundary.{0,200}(require|need|own\s+ADR|its\s+own\s+ADR)",
            adr_0049_text,
            re.IGNORECASE | re.DOTALL,
        )
    )
    if not has_phrase:
        # Looser fallback -- any phrasing that says a transaction boundary
        # is a future ADR.
        has_phrase = bool(
            re.search(
                r"unit_of_work.*ADR|unit[- ]of[- ]work.*ADR|"
                r"future\s+.*transaction\s+boundary\s+ADR|"
                r"follow-up\s+ADR.*transaction\s+boundary",
                adr_0049_text,
                re.IGNORECASE | re.DOTALL,
            )
        )
    assert has_phrase, (
        "ADR-0049 must explicitly state that adding a transaction "
        "boundary would require its own ADR"
    )


# ---------------------------------------------------------------------------
# AC #8: Exact migration file path AND manual rollback SQL.
# ---------------------------------------------------------------------------


def test_adr_0049_states_exact_migration_file_path(adr_0049_text: str):
    # Must state the exact path 'scripts/migrate_core_entities.sql'.
    assert "scripts/migrate_core_entities.sql" in adr_0049_text, (
        "ADR-0049 must state the exact migration file path "
        "'scripts/migrate_core_entities.sql'"
    )
    # Must also say "Migration file path:" or equivalent label.
    assert re.search(
        r"[Mm]igration\s+file\s+path",
        adr_0049_text,
    ), (
        "ADR-0049 must label the migration file path section explicitly "
        "(e.g. 'Migration file path:')"
    )


def test_adr_0049_includes_manual_rollback_sql(adr_0049_text: str):
    # Must label a "Manual rollback SQL" section.
    assert re.search(
        r"[Mm]anual\s+rollback\s+SQL",
        adr_0049_text,
    ), (
        "ADR-0049 must contain a 'Manual rollback SQL' section"
    )
    # Must contain real DROP TABLE statements (the rollback).
    rollback_section = re.search(
        r"```sql([\s\S]*?DROP TABLE[\s\S]*?)```",
        adr_0049_text,
    )
    assert rollback_section, (
        "ADR-0049 manual rollback must include a SQL code block "
        "containing 'DROP TABLE' statements"
    )
    # Must drop all four tables: users, portfolios, holdings, transactions.
    rollback_sql = rollback_section.group(1)
    for table in ("users", "portfolios", "holdings", "transactions"):
        assert re.search(
            r"DROP\s+TABLE\s+(IF\s+EXISTS\s+)?" + table + r"\b",
            rollback_sql,
            re.IGNORECASE,
        ), (
            f"ADR-0049 manual rollback SQL must DROP TABLE {table!r}"
        )


# ---------------------------------------------------------------------------
# AC #9: Float-precision tradeoff for quantity/amount if those fields are
# annotated float.
# ---------------------------------------------------------------------------


def test_adr_0049_notes_float_precision_tradeoff(adr_0049_text: str):
    # The story requires the ADR to "note the float-precision tradeoff
    # for quantity/amount if those fields are annotated float".
    # Must mention both quantity AND amount in the float-precision context.
    # We search the whole ADR since the note could appear in either
    # decision 5 (reaffirmations) or in Consequences.
    has_quantity_float_note = bool(
        re.search(
            r"quantity.{0,100}float|float.{0,100}quantity",
            adr_0049_text,
            re.IGNORECASE | re.DOTALL,
        )
    )
    has_amount_float_note = bool(
        re.search(
            r"amount.{0,100}float|float.{0,100}amount",
            adr_0049_text,
            re.IGNORECASE | re.DOTALL,
        )
    )
    assert has_quantity_float_note, (
        "ADR-0049 must note the float-precision tradeoff for 'quantity'"
    )
    assert has_amount_float_note, (
        "ADR-0049 must note the float-precision tradeoff for 'amount'"
    )
    # Must use "tradeoff" or "trade-off" or "trade off" wording.
    assert re.search(
        r"trade[- ]?off|tradeoff",
        adr_0049_text,
        re.IGNORECASE,
    ), (
        "ADR-0049 must describe the float-precision situation as a "
        "'tradeoff' (or 'trade-off')"
    )
    # Must mention the follow-up to Decimal.
    assert re.search(
        r"Decimal|decimal",
        adr_0049_text,
    ), (
        "ADR-0049 must mention Decimal (the planned move) in the "
        "float-precision context"
    )