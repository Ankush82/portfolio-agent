"""QA verification tests for STORY-3: stock symbol format validation
for NSE and BSE.

These tests are written from scratch specifically to verify the
acceptance criteria of STORY-3 (server-side symbol validation in
src/components/c01_user_portfolio.py). They are NOT a re-run of the
existing test suite; each test directly exercises one of the story's
own valid/invalid examples with a concrete assertion.

Acceptance criteria being verified here:

  AC1: NSE symbols: 1-20 chars (letters, numbers, &, -) + .NS suffix
  AC2: BSE symbols: exactly 6 numeric digits + .BO suffix
  AC3: US symbols: existing format without suffix (no new US rules)
  AC4: Suffixes are case-sensitive; lowercase suffixes rejected with
       a clear error message
  AC5: Valid examples (RELIANCE.NS, M&M.NS, 500325.BO) pass and
       invalid examples (REL@IANCE.NS, 12345.BO, reliance.ns) fail
"""

from decimal import Decimal

import pytest

from components.c01_user_portfolio import (
    Holding,
    validate_stock_symbol,
)


# ---------- AC1: NSE symbols (1-20 chars [A-Z0-9&-] + .NS) ----------

def test_ac1_nse_reliance_ns_is_valid():
    """AC1 + AC5: RELIANCE.NS is the canonical NSE example from the story."""
    assert validate_stock_symbol("RELIANCE.NS") is None


def test_ac1_nse_m_and_m_ns_is_valid():
    """AC1 + AC5: M&M.NS uses the ampersand character, which the story
    explicitly lists as a permitted character in the NSE body."""
    assert validate_stock_symbol("M&M.NS") is None


def test_ac1_nse_accepts_dash_in_body():
    """AC1: the dash '-' must be a permitted character in the NSE body."""
    assert validate_stock_symbol("M&M-FIN.NS") is None


def test_ac1_nse_accepts_digits_in_body():
    """AC1: digits are permitted in the NSE body."""
    assert validate_stock_symbol("RELIANCE123.NS") is None


def test_ac1_nse_accepts_1_char_body_edge():
    """AC1: minimum length is 1 character before .NS."""
    assert validate_stock_symbol("A.NS") is None


def test_ac1_nse_accepts_20_char_body_edge():
    """AC1: maximum length is 20 characters before .NS."""
    assert validate_stock_symbol("A" * 20 + ".NS") is None


# ---------- AC2: BSE symbols (exactly 6 digits + .BO) ----------

def test_ac2_bse_500325_bo_is_valid():
    """AC2 + AC5: 500325.BO is the canonical BSE example from the story."""
    assert validate_stock_symbol("500325.BO") is None


def test_ac2_bse_accepts_another_6_digit_body():
    """AC2: another 6-digit BSE body must also be valid."""
    assert validate_stock_symbol("100000.BO") is None


# ---------- AC3: US symbols (existing format, no new rules) ----------

def test_ac3_us_format_aapl_is_accepted_unchanged():
    """AC3: AAPL with no suffix must be accepted -- the story says
    existing US-format symbols continue to pass."""
    assert validate_stock_symbol("AAPL") is None


def test_ac3_us_format_brk_b_with_dot_is_accepted_unchanged():
    """AC3: a US-format symbol with a dot (BRK.B) is accepted -- no
    new US-specific rules are invented; whatever passed before passes
    now."""
    assert validate_stock_symbol("BRK.B") is None


def test_ac3_us_format_pure_digit_string_is_accepted_unchanged():
    """AC3: pure-digit US 'symbol' (like '123') is accepted -- no
    new US rule rejects it."""
    assert validate_stock_symbol("123") is None


def test_ac3_us_format_long_ticker_is_accepted_unchanged():
    """AC3: a long ticker without .NS/.BO suffix is accepted as US."""
    assert validate_stock_symbol("VERYLONGTICKERXYZ") is None


# ---------- AC4: Case-sensitive suffixes ----------

def test_ac4_lowercase_ns_suffix_is_rejected_with_case_sensitive_message():
    """AC4 + AC5: reliance.ns (lowercase suffix) must be rejected with
    a clear message that mentions case-sensitivity or lowercase."""
    with pytest.raises(ValueError) as exc_info:
        validate_stock_symbol("reliance.ns")
    msg = str(exc_info.value).lower()
    assert "lowercase" in msg or "case-sensitive" in msg, (
        f"Expected a case-sensitive/lowercase error message, got: {exc_info.value!r}"
    )


def test_ac4_lowercase_bo_suffix_is_rejected_with_case_sensitive_message():
    """AC4: a BSE symbol with lowercase .bo suffix must also be
    rejected with a clear case-sensitive error."""
    with pytest.raises(ValueError) as exc_info:
        validate_stock_symbol("500325.bo")
    msg = str(exc_info.value).lower()
    assert "lowercase" in msg or "case-sensitive" in msg, (
        f"Expected a case-sensitive/lowercase error message, got: {exc_info.value!r}"
    )


def test_ac4_uppercase_body_with_lowercase_ns_suffix_is_rejected():
    """AC4: RELIANCE.ns (uppercase body, lowercase suffix) is still
    rejected because the *suffix* is what matters for case-sensitivity."""
    with pytest.raises(ValueError) as exc_info:
        validate_stock_symbol("RELIANCE.ns")
    msg = str(exc_info.value).lower()
    assert "lowercase" in msg or "case-sensitive" in msg


# ---------- AC5: Story's invalid examples fail ----------

def test_ac5_invalid_rel_at_iance_ns_is_rejected():
    """AC5: REL@IANCE.NS contains '@' which is not in [A-Z0-9&-]."""
    with pytest.raises(ValueError):
        validate_stock_symbol("REL@IANCE.NS")


def test_ac5_invalid_12345_bo_five_digits_is_rejected():
    """AC5: 12345.BO has only 5 digits; BSE requires exactly 6."""
    with pytest.raises(ValueError):
        validate_stock_symbol("12345.BO")


def test_ac5_invalid_reliance_ns_lowercase_suffix_is_rejected():
    """AC5: reliance.ns is invalid because of the lowercase suffix
    (already covered by AC4, but explicitly named in the story's
    invalid list)."""
    with pytest.raises(ValueError):
        validate_stock_symbol("reliance.ns")


# ---------- Additional edge cases called out by the ACs ----------

def test_ac1_nse_body_longer_than_20_chars_is_rejected():
    """AC1: a 21-char NSE body must be rejected."""
    with pytest.raises(ValueError):
        validate_stock_symbol("A" * 21 + ".NS")


def test_ac1_nse_empty_body_is_rejected():
    """AC1: an empty NSE body ('just .NS') must be rejected."""
    with pytest.raises(ValueError):
        validate_stock_symbol(".NS")


def test_ac2_bse_7_digit_body_is_rejected():
    """AC2: a 7-digit BSE body must be rejected (only exactly 6 allowed)."""
    with pytest.raises(ValueError):
        validate_stock_symbol("1234567.BO")


def test_ac2_bse_letters_in_body_are_rejected():
    """AC2: a BSE body with letters must be rejected (digits only)."""
    with pytest.raises(ValueError):
        validate_stock_symbol("ABC123.BO")


def test_ac1_nse_lowercase_letters_in_body_are_rejected():
    """AC1: NSE body characters are restricted to [A-Z0-9&-] (uppercase
    only); lowercase letters must be rejected."""
    with pytest.raises(ValueError):
        validate_stock_symbol("reliance.NS")


# ---------- Integration: Holding.__post_init__ wires the validator ----------

def test_holding_with_valid_nse_full_symbol_constructs_ok():
    """Integration: A Holding with security_id='RELIANCE' and
    symbol_suffix='.NS' must construct without error -- the
    validate_stock_symbol call inside __post_init__ must accept it."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="RELIANCE",
        quantity=Decimal("10"),
        symbol_suffix=".NS",
    )
    assert h.security_id == "RELIANCE"
    assert h.symbol_suffix == ".NS"


def test_holding_with_valid_bse_full_symbol_constructs_ok():
    """Integration: A Holding with a 6-digit security_id and .BO
    suffix must construct without error."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="500325",
        quantity=Decimal("10"),
        symbol_suffix=".BO",
    )
    assert h.security_id == "500325"
    assert h.symbol_suffix == ".BO"


def test_holding_rejects_invalid_nse_body_via_post_init():
    """Integration: Holding.__post_init__ must raise ValueError when
    the assembled symbol (security_id + suffix) fails validation --
    e.g. REL@IANCE + .NS."""
    with pytest.raises(ValueError) as exc_info:
        Holding(
            portfolio_id="pf-1",
            security_id="REL@IANCE",
            quantity=Decimal("10"),
            symbol_suffix=".NS",
        )
    # The error must come from the new validator, not the existing
    # symbol-suffix check (which would say 'must be one of [...]').
    assert "must be one of" not in str(exc_info.value)
    assert "NSE" in str(exc_info.value) or "nse" in str(exc_info.value).lower()


def test_holding_rejects_invalid_bse_body_via_post_init():
    """Integration: Holding.__post_init__ must raise ValueError when
    the BSE body has only 5 digits (12345 + .BO)."""
    with pytest.raises(ValueError) as exc_info:
        Holding(
            portfolio_id="pf-1",
            security_id="12345",
            quantity=Decimal("10"),
            symbol_suffix=".BO",
        )
    assert "must be one of" not in str(exc_info.value)
    assert "BSE" in str(exc_info.value) or "bse" in str(exc_info.value).lower()


def test_holding_skips_validator_for_us_format_symbols():
    """Integration: A Holding with symbol_suffix=None must skip the
    new validator entirely, regardless of security_id content -- the
    story says 'no new US rules invented'."""
    # 'X' is not a valid NSE or BSE body, but with symbol_suffix=None
    # it must still construct fine.
    h = Holding(
        portfolio_id="pf-1",
        security_id="X",
        quantity=Decimal("10"),
        symbol_suffix=None,
    )
    assert h.symbol_suffix is None
    assert h.security_id == "X"