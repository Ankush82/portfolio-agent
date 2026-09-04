"""QA verification tests for STORY-6: currency auto-derivation from
exchange in Holding.__post_init__.

Two of STORY-6's criteria are already satisfied by earlier stories
(STORY-1's 4-decimal-place storage via _coerce_quantity_to_decimal's
.quantize(Decimal('0.0001')), and banker's rounding which is Python
decimal's own default rounding mode) -- these tests verify those
behaviors with concrete assertions rather than reimplementing them.
The genuinely new behavior this story adds is currency auto-derivation:
NSE/BSE -> 'INR' (overriding whatever the caller passed), every other
exchange (NYSE/NASDAQ/None) -> caller's value is preserved (preserves
the existing 'USD' default for US holdings, no new US-specific rule).

These tests target STORY-6's own acceptance criteria with independent
assertions -- they are not a re-run of the existing test suite.

Acceptance criteria being verified here:

  AC1: NSE/BSE exchange -> currency='INR' auto-assigned in __post_init__
  AC2: NYSE/NASDAQ/None exchange -> caller's currency preserved as-is
       (existing 'USD' default unchanged, no new US-specific rule)
  AC3: Currency is auto-derived from exchange, not left to the caller
       to set correctly by hand (caller-passed currency is overridden
       for Indian exchanges, not silently honored)
  AC4: 4-decimal-place storage (already done by
       _coerce_quantity_to_decimal -- verified here with a real test)
  AC5: Banker's rounding / round-half-to-even (already Python
       decimal's default -- verified here with a real test, not
       reimplemented)
"""

from decimal import Decimal

import pytest

from components.c01_user_portfolio import (
    Holding,
    _coerce_quantity_to_decimal,
)


# ---------- AC1: NSE/BSE exchange -> currency='INR' auto-assigned ----------

def test_ac1_nse_exchange_auto_assigns_inr_when_currency_not_passed():
    """AC1 + AC3: caller passes only exchange='NSE' and the Holding
    constructs with currency='INR' automatically -- no separate
    manual step required, and the caller didn't have to know to set
    currency='INR' by hand."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="RELIANCE",
        quantity=Decimal("10"),
        exchange="NSE",
    )
    assert h.exchange == "NSE"
    assert h.currency == "INR"


def test_ac1_bse_exchange_auto_assigns_inr_when_currency_not_passed():
    """AC1: same for BSE."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="500325",
        quantity=Decimal("10"),
        exchange="BSE",
    )
    assert h.exchange == "BSE"
    assert h.currency == "INR"


def test_ac1_ns_suffix_path_results_in_inr_via_exchange_auto_derivation():
    """AC1 + AC3 end-to-end: caller passes symbol_suffix='.NS' (no
    exchange, no currency). The suffix auto-derives exchange='NSE',
    then the exchange auto-derives currency='INR'. This is the full
    end-to-end Indian-stock path: one user-facing field (.NS suffix)
    drives both exchange and currency, no manual currency setting
    needed."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="RELIANCE",
        quantity=Decimal("10"),
        symbol_suffix=".NS",
    )
    assert h.exchange == "NSE", "suffix must auto-derive exchange first"
    assert h.currency == "INR", "exchange must then auto-derive currency"


def test_ac1_bo_suffix_path_results_in_inr_via_exchange_auto_derivation():
    """AC1 + AC3 end-to-end: same for .BO/BSE."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="500325",
        quantity=Decimal("10"),
        symbol_suffix=".BO",
    )
    assert h.exchange == "BSE", "suffix must auto-derive exchange first"
    assert h.currency == "INR", "exchange must then auto-derive currency"


def test_ac3_caller_passed_currency_is_overridden_to_inr_for_nse():
    """AC3: currency is auto-derived from exchange, not left to the
    caller. Even if a caller explicitly passes currency='USD' (or
    any other caller-passed value) with exchange='NSE', the
    auto-derivation rule overrides it to 'INR' -- the same
    "suffix/exchange is authoritative" pattern STORY-4 already uses
    for exchange-from-suffix."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="RELIANCE",
        quantity=Decimal("10"),
        exchange="NSE",
        currency="USD",  # deliberately wrong -- exchange wins
    )
    assert h.exchange == "NSE"
    assert h.currency == "INR", "NSE must override caller-passed currency to INR"


def test_ac3_caller_passed_currency_is_overridden_to_inr_for_bse():
    """AC3: same for BSE."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="500325",
        quantity=Decimal("10"),
        exchange="BSE",
        currency="USD",  # deliberately wrong -- exchange wins
    )
    assert h.exchange == "BSE"
    assert h.currency == "INR", "BSE must override caller-passed currency to INR"


# ---------- AC2: NYSE/NASDAQ/None -> existing US currency behavior preserved ----------

def test_ac2_nyse_exchange_preserves_callers_usd_default():
    """AC2: existing US behavior preserved -- caller passes only
    exchange='NYSE', currency defaults to 'USD' and stays 'USD'. No
    new US-specific rule is invented."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="IBM",
        quantity=Decimal("10"),
        exchange="NYSE",
    )
    assert h.exchange == "NYSE"
    assert h.currency == "USD"


def test_ac2_nasdaq_exchange_preserves_callers_usd_default():
    """AC2: same for NASDAQ."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="AMZN",
        quantity=Decimal("10"),
        exchange="NASDAQ",
    )
    assert h.exchange == "NASDAQ"
    assert h.currency == "USD"


def test_ac2_no_exchange_preserves_callers_usd_default():
    """AC2: an imported holding whose broker payload omits the
    exchange (the existing None case, explicitly allowed by STORY-1)
    keeps the caller's currency unchanged -- the default 'USD' is
    not overridden by an auto-derivation rule."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="AAPL",
        quantity=Decimal("10"),
        exchange=None,
    )
    assert h.exchange is None
    assert h.currency == "USD"


def test_ac2_no_exchange_no_currency_specified_keeps_default_usd():
    """AC2 + the pre-existing default behavior: caller passes
    nothing -- no exchange, no currency -- and the holding still
    constructs with the existing 'USD' default. This is the
    pre-STORY-6 behavior, which the story explicitly says must
    stay unchanged for US holdings."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="AAPL",
        quantity=Decimal("10"),
    )
    assert h.exchange is None
    assert h.currency == "USD"


def test_ac2_caller_passed_inr_with_nyse_is_preserved_not_overridden():
    """AC2: when exchange is not NSE/BSE, the caller's currency
    value is preserved as-is -- including an explicitly passed
    'INR' if they have one. The auto-derivation rule only fires
    for Indian exchanges; it does not invent a new US rule."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="IBM",
        quantity=Decimal("10"),
        exchange="NYSE",
        currency="INR",  # unusual but explicitly passed
    )
    assert h.exchange == "NYSE"
    assert h.currency == "INR", (
        "non-Indian exchange must not invent a rule overriding the caller's "
        "currency; passed value is preserved"
    )


def test_ac2_us_holding_with_no_suffix_keeps_default_usd():
    """AC2: a US-format holding (no suffix, exchange left as default
    None) keeps the existing 'USD' default currency. This is the
    canonical pre-STORY-6 case -- it must keep working verbatim."""
    h = Holding(
        portfolio_id="pf-1",
        security_id="MSFT",
        quantity=Decimal("10"),
    )
    assert h.symbol_suffix is None
    assert h.exchange is None
    assert h.currency == "USD"


# ---------- Ordering: currency derivation runs AFTER exchange is determined ----------

def test_currency_derivation_runs_after_exchange_auto_detection_from_suffix():
    """Ordering guarantee: currency auto-derivation runs AFTER the
    exchange-from-suffix block. We verify this by passing only
    symbol_suffix='.NS' (no exchange, no currency) and confirming
    the final currency is 'INR' -- which it can only be if the
    exchange was first set to 'NSE' (by the suffix block) and then
    the currency block saw 'NSE' and set currency='INR'."""
    # If currency derivation ran BEFORE exchange detection, it would
    # see exchange=None (the dataclass default) and preserve the
    # default 'USD' -- which is wrong for an Indian-stock suffix.
    h = Holding(
        portfolio_id="pf-1",
        security_id="RELIANCE",
        quantity=Decimal("10"),
        symbol_suffix=".NS",
    )
    assert h.exchange == "NSE", (
        f"exchange must be 'NSE' before currency derivation runs; got {h.exchange!r}"
    )
    assert h.currency == "INR", (
        f"currency must be 'INR' after exchange is determined; got {h.currency!r}"
    )


# ---------- AC4: 4-decimal-place storage (verified, already done) ----------

def test_ac4_quantity_is_stored_with_exactly_four_decimal_places_of_precision():
    """AC4 (verify, don't reimplement): STORY-1 already requires
    quantity to be stored as Decimal quantized to 4 decimal places
    via _coerce_quantity_to_decimal. This test confirms it end to
    end through Holding construction: an int input becomes a
    4-decimal-place Decimal, not a raw int or a higher-precision
    Decimal."""
    h_int = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=10)
    assert isinstance(h_int.quantity, Decimal)
    assert h_int.quantity == Decimal("10.0000"), (
        f"int input must quantize to 4 places; got {h_int.quantity}"
    )

    h_float = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=12.5)
    assert isinstance(h_float.quantity, Decimal)
    assert h_float.quantity == Decimal("12.5000"), (
        f"float input must quantize to 4 places; got {h_float.quantity}"
    )

    h_str = Holding(portfolio_id="pf-1", security_id="AAPL", quantity="7.125")
    assert isinstance(h_str.quantity, Decimal)
    assert h_str.quantity == Decimal("7.1250"), (
        f"str input must quantize to 4 places; got {h_str.quantity}"
    )


def test_ac4_helper_quantizes_to_four_decimal_places_independently():
    """AC4 (direct helper check): the helper _coerce_quantity_to_decimal
    -- the function that does the quantization -- is independently
    verifiable on its own. This guards against any future refactor
    that moves the quantization elsewhere without keeping the
    4-decimal-place precision this story requires."""
    assert _coerce_quantity_to_decimal(10) == Decimal("10.0000")
    assert _coerce_quantity_to_decimal(12.5) == Decimal("12.5000")
    assert _coerce_quantity_to_decimal("7.125") == Decimal("7.1250")
    assert _coerce_quantity_to_decimal(Decimal("0.123456")) == Decimal("0.1235")


# ---------- AC5: Banker's rounding (verify, already Python decimal's default) ----------

def test_ac5_banker_rounding_half_at_the_fourth_decimal_rounds_to_even():
    """AC5 (verify, don't reimplement): the story explicitly confirms
    Python decimal's default rounding mode IS banker's rounding
    (round half to even). The story names the two specific cases
    that prove it:

      Decimal('0.00005').quantize(Decimal('0.0001')) == Decimal('0.0000')
        -- 0.00005 has no exact representation; it ties at the 4th
        place between 0.0000 and 0.0001; banker's rounding picks the
        even one (0.0000), NOT the round-half-up one (0.0001).

      Decimal('0.00015').quantize(Decimal('0.0001')) == Decimal('0.0002')
        -- 0.00015 ties at the 4th place between 0.0001 and 0.0002;
        banker's rounding picks the even one (0.0002), not 0.0001.

    These are the live values the story verified. We assert them
    here so any future change that swaps the default rounding mode
    (e.g. to ROUND_HALF_UP) is caught immediately."""
    # The two named story examples
    assert Decimal("0.00005").quantize(Decimal("0.0001")) == Decimal("0.0000"), (
        "0.00005 must round to 0.0000 (banker's rounding to even, "
        "NOT round-half-up to 0.0001)"
    )
    assert Decimal("0.00015").quantize(Decimal("0.0001")) == Decimal("0.0002"), (
        "0.00015 must round to 0.0002 (banker's rounding to even, "
        "NOT round-half-up to 0.0001)"
    )
    # A couple more half-at-the-fourth-decimal cases to confirm
    # the pattern is consistent across the rounding boundary.
    assert Decimal("0.00025").quantize(Decimal("0.0001")) == Decimal("0.0002"), (
        "0.00025 must round to 0.0002 (to even)"
    )
    assert Decimal("0.00035").quantize(Decimal("0.0001")) == Decimal("0.0004"), (
        "0.00035 must round to 0.0004 (to even)"
    )


def test_ac5_banker_rounding_via_the_coercion_helper_end_to_end():
    """AC5 end-to-end: a caller-supplied quantity whose 5th-decimal
    digit ties at exactly 5 must round to the even 4th-decimal
    value through the actual code path (Holding.__post_init__ ->
    _coerce_quantity_to_decimal -> quantize). This confirms the
    banking behavior is what callers actually see, not just what
    Decimal itself happens to do in isolation."""
    # Decimal('0.00005') -> 0.0000 (to even), NOT 0.0001.
    h1 = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=Decimal("0.00005"))
    assert h1.quantity == Decimal("0.0000"), (
        f"0.00005 must round to 0.0000 through the Holding path; got {h1.quantity}"
    )
    # Decimal('0.00015') -> 0.0002 (to even), NOT 0.0001.
    h2 = Holding(portfolio_id="pf-1", security_id="AAPL", quantity=Decimal("0.00015"))
    assert h2.quantity == Decimal("0.0002"), (
        f"0.00015 must round to 0.0002 through the Holding path; got {h2.quantity}"
    )


# ---------- AC round trip: end-to-end combination of all three branches ----------

def test_ac_round_trip_nse_bse_and_no_exchange_all_get_their_documented_currency():
    """A single end-to-end round trip exercising all three branches
    the story names -- Indian-exchange (NSE -> INR, BSE -> INR) and
    US/no-exchange (NYSE/NASDAQ/None -> USD) -- against the Holding
    dataclass itself. This is the load-bearing combination test: if
    any of the three branches regresses, this fails as one
    clearly-named test."""
    h_nse = Holding(
        portfolio_id="pf-1",
        security_id="RELIANCE",
        quantity=Decimal("10"),
        exchange="NSE",
    )
    h_bse = Holding(
        portfolio_id="pf-1",
        security_id="500325",
        quantity=Decimal("10"),
        exchange="BSE",
    )
    h_nyse = Holding(
        portfolio_id="pf-1",
        security_id="IBM",
        quantity=Decimal("10"),
        exchange="NYSE",
    )
    h_nasdaq = Holding(
        portfolio_id="pf-1",
        security_id="AMZN",
        quantity=Decimal("10"),
        exchange="NASDAQ",
    )
    h_no_exchange = Holding(
        portfolio_id="pf-1",
        security_id="AAPL",
        quantity=Decimal("10"),
    )

    assert h_nse.currency == "INR", "NSE must auto-derive INR"
    assert h_bse.currency == "INR", "BSE must auto-derive INR"
    assert h_nyse.currency == "USD", "NYSE must keep existing USD default"
    assert h_nasdaq.currency == "USD", "NASDAQ must keep existing USD default"
    assert h_no_exchange.currency == "USD", "no exchange must keep existing USD default"


# ============================================================================
# QA-verifier-authored tests below this line.
# These tests were written specifically for STORY-6 verification and
# target its acceptance criteria with independent assertions.
# ============================================================================


def test_qa_story6_nse_exchange_with_default_currency_assigns_inr():
    """QA verification of AC1: A Holding constructed with exchange='NSE'
    and no explicit currency must end up with currency='INR' after
    construction. Concrete assertion that currency was auto-derived
    (not 'USD', not the dataclass default)."""
    h = Holding(
        portfolio_id="pf-qa-1",
        security_id="TCS",
        quantity=Decimal("5"),
        exchange="NSE",
    )
    assert h.exchange == "NSE"
    # Core AC1 assertion: currency was set to "INR", not the dataclass default "USD".
    assert h.currency == "INR", (
        f"AC1 FAILED: NSE exchange should auto-derive currency='INR', got {h.currency!r}"
    )


def test_qa_story6_bse_exchange_with_default_currency_assigns_inr():
    """QA verification of AC1 for BSE: A Holding constructed with
    exchange='BSE' and no explicit currency must end up with
    currency='INR' after construction."""
    h = Holding(
        portfolio_id="pf-qa-1",
        security_id="500112",
        quantity=Decimal("5"),
        exchange="BSE",
    )
    assert h.exchange == "BSE"
    assert h.currency == "INR", (
        f"AC1 FAILED: BSE exchange should auto-derive currency='INR', got {h.currency!r}"
    )


def test_qa_story6_nyse_nasdaq_and_none_exchange_keep_default_usd():
    """QA verification of AC2: When exchange is NYSE, NASDAQ, or None,
    the caller's currency must be preserved exactly -- including the
    default 'USD' -- and no new US-specific rule must override it."""
    # NYSE
    h_nyse = Holding(
        portfolio_id="pf-qa-1",
        security_id="IBM",
        quantity=Decimal("1"),
        exchange="NYSE",
    )
    assert h_nyse.currency == "USD", (
        f"AC2 FAILED: NYSE exchange should keep currency='USD', got {h_nyse.currency!r}"
    )

    # NASDAQ
    h_nasdaq = Holding(
        portfolio_id="pf-qa-1",
        security_id="AMZN",
        quantity=Decimal("1"),
        exchange="NASDAQ",
    )
    assert h_nasdaq.currency == "USD", (
        f"AC2 FAILED: NASDAQ exchange should keep currency='USD', got {h_nasdaq.currency!r}"
    )

    # None
    h_none = Holding(
        portfolio_id="pf-qa-1",
        security_id="GOOG",
        quantity=Decimal("1"),
    )
    assert h_none.exchange is None
    assert h_none.currency == "USD", (
        f"AC2 FAILED: no-exchange should keep currency='USD', got {h_none.currency!r}"
    )


def test_qa_story6_currency_is_auto_derived_not_caller_controlled_for_indian_exchanges():
    """QA verification of AC3: Currency is auto-derived from
    exchange, not left to the caller to set correctly by hand. A
    caller passing currency='USD' with an Indian exchange must see
    'INR' on the constructed holding -- the same auto-derivation
    contract STORY-4 already uses for exchange-from-suffix."""
    h_nse = Holding(
        portfolio_id="pf-qa-1",
        security_id="INFY",
        quantity=Decimal("2"),
        exchange="NSE",
        currency="USD",  # deliberately wrong -- exchange wins
    )
    assert h_nse.currency == "INR", (
        f"AC3 FAILED: NSE must override caller-passed 'USD' to 'INR', "
        f"got {h_nse.currency!r}"
    )

    h_bse = Holding(
        portfolio_id="pf-qa-1",
        security_id="500209",
        quantity=Decimal("2"),
        exchange="BSE",
        currency="USD",  # deliberately wrong -- exchange wins
    )
    assert h_bse.currency == "INR", (
        f"AC3 FAILED: BSE must override caller-passed 'USD' to 'INR', "
        f"got {h_bse.currency!r}"
    )


def test_qa_story6_currency_derivation_is_post_init_side_effect_no_extra_call_needed():
    """QA verification of AC3 + AC4: The currency is set as part of
    Holding(...) construction itself (__post_init__), not as a
    separate manual step the caller must invoke. We assert the
    currency is set immediately after Holding(...) returns,
    without invoking any helper method."""
    # Build with only exchange and quantity; never touch h again.
    h_nse = Holding(
        portfolio_id="pf-qa-1",
        security_id="INFY",
        quantity=Decimal("2"),
        exchange="NSE",
    )
    # No h.apply_currency(), h.derive_currency(), h.finalize(), etc.
    # was called. Currency must already be 'INR'.
    assert h_nse.currency == "INR", (
        f"AC3 FAILED: currency should be set during construction; got {h_nse.currency!r}"
    )

    # Same for BSE.
    h_bse = Holding(
        portfolio_id="pf-qa-1",
        security_id="500209",
        quantity=Decimal("2"),
        exchange="BSE",
    )
    assert h_bse.currency == "INR", (
        f"AC3 FAILED: currency should be set during construction; got {h_bse.currency!r}"
    )


def test_qa_story6_quantity_quantizes_to_four_decimal_places():
    """QA verification of AC4: All prices/quantities are stored with
    4 decimal places. This was already done by
    _coerce_quantity_to_decimal -- we verify the actual behavior
    here, not reimplement the rounding."""
    # int
    h_int = Holding(portfolio_id="pf-qa-1", security_id="AAPL", quantity=10)
    assert h_int.quantity == Decimal("10.0000"), (
        f"AC4 FAILED: int quantity must quantize to 4 places; got {h_int.quantity}"
    )
    # float
    h_float = Holding(portfolio_id="pf-qa-1", security_id="AAPL", quantity=12.5)
    assert h_float.quantity == Decimal("12.5000"), (
        f"AC4 FAILED: float quantity must quantize to 4 places; got {h_float.quantity}"
    )
    # str
    h_str = Holding(portfolio_id="pf-qa-1", security_id="AAPL", quantity="7.125")
    assert h_str.quantity == Decimal("7.1250"), (
        f"AC4 FAILED: str quantity must quantize to 4 places; got {h_str.quantity}"
    )
    # over-precision Decimal
    h_over = Holding(portfolio_id="pf-qa-1", security_id="AAPL", quantity=Decimal("0.123456"))
    assert h_over.quantity == Decimal("0.1235"), (
        f"AC4 FAILED: over-precision must quantize to 4 places; got {h_over.quantity}"
    )


def test_qa_story6_banker_rounding_round_half_to_even_is_in_effect():
    """QA verification of AC5: Price calculations use banker's
    rounding / round-half-to-even. This is already Python decimal's
    own default rounding mode -- we assert the two specific named
    cases from the story (0.00005 -> 0.0000, 0.00015 -> 0.0002) so
    any future change that swaps the default rounding mode is caught
    immediately."""
    # 0.00005 rounds DOWN to 0.0000 (to even), NOT UP to 0.0001.
    assert Decimal("0.00005").quantize(Decimal("0.0001")) == Decimal("0.0000"), (
        "AC5 FAILED: 0.00005 must round to 0.0000 (banker's rounding to even), "
        "NOT to 0.0001 (round-half-up)"
    )
    # 0.00015 rounds UP to 0.0002 (to even), NOT DOWN to 0.0001.
    assert Decimal("0.00015").quantize(Decimal("0.0001")) == Decimal("0.0002"), (
        "AC5 FAILED: 0.00015 must round to 0.0002 (banker's rounding to even), "
        "NOT to 0.0001 (round-half-up)"
    )


# ============================================================================
# Independent QA verifier tests (added during STORY-6 verification pass).
# These are independent of the dev's own test block above; they
# specifically exercise STORY-6's acceptance criteria with fresh
# assertions, not retests of pre-existing tests.
# ============================================================================


def test_verifier_story6_independent_nse_bse_currency_auto_derivation():
    """AC1 + AC3 (independent): NSE and BSE exchanges force currency to
    'INR' -- not the dataclass default 'USD', not whatever the caller
    passed. Verified across direct-exchange and end-to-end suffix
    paths in one combined test."""
    # Direct exchange path (caller passes exchange='NSE' explicitly).
    h_direct_nse = Holding(
        portfolio_id="pf-v",
        security_id="TCS",
        quantity=Decimal("1"),
        exchange="NSE",
    )
    assert h_direct_nse.currency == "INR", (
        f"AC1 FAILED (direct NSE): currency should be 'INR', got {h_direct_nse.currency!r}"
    )

    h_direct_bse = Holding(
        portfolio_id="pf-v",
        security_id="500325",
        quantity=Decimal("1"),
        exchange="BSE",
    )
    assert h_direct_bse.currency == "INR", (
        f"AC1 FAILED (direct BSE): currency should be 'INR', got {h_direct_bse.currency!r}"
    )

    # End-to-end suffix path (caller passes symbol_suffix only; both
    # exchange and currency must be derived).
    h_suffix_ns = Holding(
        portfolio_id="pf-v",
        security_id="RELIANCE",
        quantity=Decimal("1"),
        symbol_suffix=".NS",
    )
    assert h_suffix_ns.exchange == "NSE", (
        f"AC1 FAILED (suffix .NS): exchange should auto-derive to 'NSE', got {h_suffix_ns.exchange!r}"
    )
    assert h_suffix_ns.currency == "INR", (
        f"AC1 FAILED (suffix .NS): currency should be 'INR', got {h_suffix_ns.currency!r}"
    )

    h_suffix_bo = Holding(
        portfolio_id="pf-v",
        security_id="500325",
        quantity=Decimal("1"),
        symbol_suffix=".BO",
    )
    assert h_suffix_bo.exchange == "BSE"
    assert h_suffix_bo.currency == "INR", (
        f"AC1 FAILED (suffix .BO): currency should be 'INR', got {h_suffix_bo.currency!r}"
    )

    # Override: caller explicitly passes currency='USD' with NSE -- must be overridden.
    h_override = Holding(
        portfolio_id="pf-v",
        security_id="INFY",
        quantity=Decimal("1"),
        exchange="NSE",
        currency="USD",
    )
    assert h_override.currency == "INR", (
        f"AC3 FAILED (override NSE): caller-passed 'USD' must be overridden to 'INR', "
        f"got {h_override.currency!r}"
    )


def test_verifier_story6_independent_us_currency_preservation_no_new_rule():
    """AC2 (independent): For NYSE/NASDAQ/None exchange, the caller's
    currency must be preserved exactly -- including the default
    'USD'. No new US-specific rule may invent a different default or
    override caller-passed values."""
    # NYSE with default currency -> 'USD'.
    h_nyse = Holding(
        portfolio_id="pf-v",
        security_id="IBM",
        quantity=Decimal("1"),
        exchange="NYSE",
    )
    assert h_nyse.currency == "USD", (
        f"AC2 FAILED (NYSE): currency should remain 'USD', got {h_nyse.currency!r}"
    )

    # NASDAQ with default currency -> 'USD'.
    h_nasdaq = Holding(
        portfolio_id="pf-v",
        security_id="AMZN",
        quantity=Decimal("1"),
        exchange="NASDAQ",
    )
    assert h_nasdaq.currency == "USD", (
        f"AC2 FAILED (NASDAQ): currency should remain 'USD', got {h_nasdaq.currency!r}"
    )

    # No exchange (None) with default currency -> 'USD'.
    h_none = Holding(
        portfolio_id="pf-v",
        security_id="AAPL",
        quantity=Decimal("1"),
    )
    assert h_none.exchange is None
    assert h_none.currency == "USD", (
        f"AC2 FAILED (None exchange): currency should remain 'USD', got {h_none.currency!r}"
    )

    # Caller passes an explicit non-Indian currency with NYSE -- must be preserved.
    # This catches a hypothetical regression where someone adds a US-specific
    # rule that overrides caller-passed currencies to 'USD'.
    h_nyse_explicit = Holding(
        portfolio_id="pf-v",
        security_id="IBM",
        quantity=Decimal("1"),
        exchange="NYSE",
        currency="INR",  # unusual but explicitly passed
    )
    assert h_nyse_explicit.currency == "INR", (
        f"AC2 FAILED (NYSE explicit INR): caller's value must be preserved, "
        f"got {h_nyse_explicit.currency!r}"
    )


def test_verifier_story6_independent_banker_rounding_through_holding_path():
    """AC4 + AC5 (independent, end-to-end through Holding.__post_init__):
    The story names two specific banker's-rounding cases:
        Decimal('0.00005').quantize(Decimal('0.0001')) == Decimal('0.0000')
        Decimal('0.00015').quantize(Decimal('0.0001')) == Decimal('0.0002')
    These are also the exact values that flow through Holding quantity
    coercion -- exercised here through the real Holding code path,
    not just isolated Decimal.quantize calls.

    Also confirms AC4: all stored quantities are exactly 4-decimal-
    place Decimals, regardless of input type (int/float/str/Decimal)."""
    # AC5 story-named cases, through Holding:
    h1 = Holding(portfolio_id="pf-v", security_id="AAPL", quantity=Decimal("0.00005"))
    assert h1.quantity == Decimal("0.0000"), (
        f"AC5 FAILED (Holding path): 0.00005 must round to 0.0000 (banker's), "
        f"got {h1.quantity}"
    )
    h2 = Holding(portfolio_id="pf-v", security_id="AAPL", quantity=Decimal("0.00015"))
    assert h2.quantity == Decimal("0.0002"), (
        f"AC5 FAILED (Holding path): 0.00015 must round to 0.0002 (banker's), "
        f"got {h2.quantity}"
    )

    # AC4: 4-decimal-place storage through Holding with various input types.
    assert Holding(portfolio_id="pf-v", security_id="AAPL", quantity=7).quantity == Decimal("7.0000"), (
        "AC4 FAILED (int): quantity must quantize to 4 places"
    )
    assert Holding(portfolio_id="pf-v", security_id="AAPL", quantity=1.25).quantity == Decimal("1.2500"), (
        "AC4 FAILED (float): quantity must quantize to 4 places"
    )
    assert Holding(portfolio_id="pf-v", security_id="AAPL", quantity="3.5").quantity == Decimal("3.5000"), (
        "AC4 FAILED (str): quantity must quantize to 4 places"
    )
    assert Holding(portfolio_id="pf-v", security_id="AAPL", quantity=Decimal("0.123456")).quantity == Decimal("0.1235"), (
        "AC4 FAILED (over-precision Decimal): quantity must quantize to 4 places"
    )


def test_verifier_story6_independent_ordering_suffix_must_set_exchange_before_currency():
    """Ordering guarantee (independent): The story explicitly requires
    currency derivation to run AFTER exchange is determined. We
    verify this by constructing a Holding with ONLY a .NS suffix
    (no explicit exchange, no explicit currency) and asserting BOTH
    final values are correct:
        - exchange == 'NSE' (must have been set by the suffix block)
        - currency == 'INR' (must have been set by the currency block
          seeing 'NSE')

    If the blocks were reordered (currency-before-exchange), the
    currency block would see exchange=None, the currency block's
    'if self.exchange in ("NSE", "BSE")' check would fail, and the
    final currency would be the dataclass default 'USD' -- which
    would fail this test."""
    h = Holding(
        portfolio_id="pf-v",
        security_id="RELIANCE",
        quantity=Decimal("1"),
        symbol_suffix=".NS",
    )
    assert h.exchange == "NSE", (
        f"ORDERING FAILED: suffix must set exchange to 'NSE' first; got {h.exchange!r}"
    )
    assert h.currency == "INR", (
        f"ORDERING FAILED: currency must derive from the FINAL exchange; "
        f"got {h.currency!r} (would be 'USD' if currency ran before exchange)"
    )
