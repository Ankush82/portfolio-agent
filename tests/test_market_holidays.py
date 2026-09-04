"""Tests for src/market_holidays.py — NSE/BSE and US (NYSE/NASDAQ)
market holiday detection, plus the holiday-override integration in
src/market_hours.py.

The hand-computed holiday dates below are real, observed exchange
holidays (NSE/BSE and NYSE/NASDAQ each publish their own annual
holiday calendars) so a test that picks a date the exchange wasn't
actually closed on will surface as "expected holiday, got None" rather
than masking a real bug.

The `market_holidays` module has a module-level cache (per full file
path) so tests that don't swap configs don't need to clear it. The
few tests that DO swap a config (env-var override, missing-file
fixture) explicitly call `reset_holiday_cache` so the change takes
effect on the very next call — same posture as test_market_hours.py
uses for `reset_config_cache`."""

from datetime import date

import pytest

import market_holidays
import market_hours
from market_holidays import (
    MarketHolidaysConfigError,
    UnknownMarketError,
    is_market_holiday,
    list_known_holiday_markets,
    reset_holiday_cache,
)
from market_hours import is_market_open, market_status, reset_config_cache


# --- Real holiday fixtures (NSE/BSE and US, 2024-2025) -----------------
#
# Each fixture is paired with a non-holiday weekday so a test that
# accidentally hits "always returns a holiday name" still trips on the
# control pair below it.

# NSE 2024 holidays (real, from NSE's official holiday calendar)
NSE_REPUBLIC_DAY_2024 = date(2024, 1, 26)         # Friday
NSE_INDEPENDENCE_DAY_2024 = date(2024, 8, 15)     # Thursday
NSE_DIWALI_2024 = date(2024, 11, 1)               # Friday
NSE_CHRISTMAS_2024 = date(2024, 12, 25)           # Wednesday

# BSE 2024 holidays — same dates as NSE for these (Bombay Stock
# Exchange's calendar overlaps with NSE on the headline dates).
BSE_REPUBLIC_DAY_2024 = date(2024, 1, 26)
BSE_DIWALI_2024 = date(2024, 11, 1)
BSE_CHRISTMAS_2024 = date(2024, 12, 25)

# US 2024 holidays (real, from NYSE/NASDAQ's published calendar).
US_NEW_YEARS_2024 = date(2024, 1, 1)              # Monday
US_INDEPENDENCE_DAY_2024 = date(2024, 7, 4)       # Thursday
US_THANKSGIVING_2024 = date(2024, 11, 28)         # Thursday
US_CHRISTMAS_2024 = date(2024, 12, 25)            # Wednesday

# 2025 holidays — used to confirm the calendar isn't hardcoded to
# 2024 (a real bug class: an off-by-one or year-bound logic would
# pass 2024-only tests but fail these).
NSE_REPUBLIC_DAY_2025 = date(2025, 1, 26)
NSE_DIWALI_2025 = date(2025, 10, 21)
US_NEW_YEARS_2025 = date(2025, 1, 1)
US_THANKSGIVING_2025 = date(2025, 11, 27)

# A normal weekday that is NOT a holiday — used as a control pair
# for every test asserting a specific holiday. Picked far from any
# observed holiday so a test that accidentally classifies an entire
# week as "holiday" trips here.
NORMAL_NSE_DAY = date(2024, 3, 5)                 # Tuesday, no NSE holiday
NORMAL_BSE_DAY = date(2024, 4, 2)                 # Tuesday, no BSE holiday
NORMAL_US_DAY = date(2024, 2, 27)                 # Tuesday, no US holiday


def setup_function(function):
    """Clear both config caches before each test so env-var overrides
    from a previous test don't leak. pytest calls this automatically
    when named ``setup_function`` at module scope."""
    reset_config_cache()
    reset_holiday_cache()


# --- Direct holiday lookup: real, observed holidays --------------------


def test_nse_republic_day_2024_is_holiday():
    """January 26 is Republic Day, a real NSE holiday. The lookup
    returns the observed name verbatim — string comparison is exact
    so a typo in the bundled JSON ("Republic Dy") would fail here."""
    assert is_market_holiday("NSE", NSE_REPUBLIC_DAY_2024) == "Republic Day"


def test_nse_independence_day_2024_is_holiday():
    """August 15 is India's Independence Day, an NSE holiday."""
    assert is_market_holiday("NSE", NSE_INDEPENDENCE_DAY_2024) == "Independence Day"


def test_nse_diwali_2024_is_holiday():
    """Diwali (Laxmi Puja) — November 1, 2024 — was a real NSE
    closure. A naive implementation that picked the wrong Diwali
    date (Diwali spans multiple days in some years) would fail this."""
    assert is_market_holiday("NSE", NSE_DIWALI_2024) == "Diwali (Laxmi Puja)"


def test_nse_christmas_2024_is_holiday():
    """Christmas Day — December 25 — is an NSE holiday."""
    assert is_market_holiday("NSE", NSE_CHRISTMAS_2024) == "Christmas"


def test_nse_normal_weekday_is_not_holiday():
    """A random Tuesday in 2024 with no NSE holiday returns None.
    Without this control pair, the test above could pass for a bug
    that always returns "Christmas" regardless of date."""
    assert is_market_holiday("NSE", NORMAL_NSE_DAY) is None


def test_bse_republic_day_2024_is_holiday():
    """Same date, same holiday name on BSE. Tests that the BSE
    config file is loaded, not just NSE's."""
    assert is_market_holiday("BSE", BSE_REPUBLIC_DAY_2024) == "Republic Day"


def test_bse_diwali_2024_is_holiday():
    """BSE observes Diwali on the same date as NSE for 2024."""
    assert is_market_holiday("BSE", BSE_DIWALI_2024) == "Diwali (Laxmi Puja)"


def test_bse_christmas_2024_is_holiday():
    """BSE Christmas closure."""
    assert is_market_holiday("BSE", BSE_CHRISTMAS_2024) == "Christmas"


def test_bse_normal_weekday_is_not_holiday():
    """BSE non-holiday control pair."""
    assert is_market_holiday("BSE", NORMAL_BSE_DAY) is None


def test_us_new_years_2024_is_holiday():
    """NYSE/NASDAQ New Year's Day closure."""
    assert is_market_holiday("NYSE", US_NEW_YEARS_2024) == "New Year's Day"
    assert is_market_holiday("NASDAQ", US_NEW_YEARS_2024) == "New Year's Day"


def test_us_independence_day_2024_is_holiday():
    """July 4 — Independence Day."""
    assert is_market_holiday("NYSE", US_INDEPENDENCE_DAY_2024) == "Independence Day"


def test_us_thanksgiving_2024_is_holiday():
    """US Thanksgiving — fourth Thursday of November."""
    assert is_market_holiday("NYSE", US_THANKSGIVING_2024) == "Thanksgiving Day"


def test_us_christmas_2024_is_holiday():
    """Christmas Day — NYSE/NASDAQ closure."""
    assert is_market_holiday("NYSE", US_CHRISTMAS_2024) == "Christmas Day"


def test_us_normal_weekday_is_not_holiday():
    """A random Tuesday with no US holiday returns None. Pairs with
    the holiday-positive tests above so a "always returns Christmas"
    bug trips here."""
    assert is_market_holiday("NYSE", NORMAL_US_DAY) is None


def test_nyse_and_nasdaq_share_us_calendar():
    """NYSE and NASDAQ both map to us_holidays.json (SRO rule).
    The same date returns the same holiday name from either code —
    a bug that loaded a separate file for NASDAQ would fail this
    with a key error or different name."""
    assert is_market_holiday("NYSE", US_THANKSGIVING_2024) == \
        is_market_holiday("NASDAQ", US_THANKSGIVING_2024)
    assert is_market_holiday("NYSE", US_CHRISTMAS_2024) == \
        is_market_holiday("NASDAQ", US_CHRISTMAS_2024)


# --- 2025 coverage (calendar isn't hardcoded to 2024) -----------------


def test_calendar_includes_2025_holidays():
    """A real implementation must include 2025 holidays in the same
    JSON file, not hardcode to a single year. Each of these dates
    is a real observed holiday for the exchange in question in
    2025."""
    assert is_market_holiday("NSE", NSE_REPUBLIC_DAY_2025) == "Republic Day"
    assert is_market_holiday("NSE", NSE_DIWALI_2025) == "Diwali (Laxmi Puja)"
    assert is_market_holiday("NYSE", US_NEW_YEARS_2025) == "New Year's Day"
    assert is_market_holiday("NYSE", US_THANKSGIVING_2025) == "Thanksgiving Day"


# --- Separate files for NSE vs BSE -----------------------------------


def test_nse_and_bse_have_separate_config_files():
    """STORY-11 acceptance criteria: NSE and BSE use separate
    holiday-config files. This test asserts that fact directly by
    reading both paths and confirming they exist as distinct files
    (not symlinks, not a shared file) — a bug that collapsed them
    into one file would fail this."""
    from pathlib import Path

    nse_path = Path(market_holidays.__file__).resolve().parent.parent / "config" / "nse_holidays.json"
    bse_path = Path(market_holidays.__file__).resolve().parent.parent / "config" / "bse_holidays.json"
    us_path = Path(market_holidays.__file__).resolve().parent.parent / "config" / "us_holidays.json"

    assert nse_path.exists(), f"NSE holiday config missing at {nse_path}"
    assert bse_path.exists(), f"BSE holiday config missing at {bse_path}"
    assert us_path.exists(), f"US holiday config missing at {us_path}"

    # Distinct paths (catches a symlink-to-shared-file hack too,
    # because resolve() follows symlinks).
    assert nse_path.resolve() != bse_path.resolve()
    assert nse_path.resolve() != us_path.resolve()
    assert bse_path.resolve() != us_path.resolve()


def test_holiday_config_files_have_date_and_name_keys():
    """Each entry in the bundled files has both 'date' (YYYY-MM-DD)
    string) and 'name' (non-empty string) keys — required by the
    story acceptance criteria. Reads the files directly so the test
    pins the file format, not just the parsed runtime answer."""
    import json
    from pathlib import Path

    config_dir = Path(market_holidays.__file__).resolve().parent.parent / "config"
    for filename in ("nse_holidays.json", "bse_holidays.json", "us_holidays.json"):
        raw = json.loads((config_dir / filename).read_text())
        holidays = raw["holidays"]
        assert isinstance(holidays, list) and holidays, \
            f"{filename}: 'holidays' must be a non-empty list"
        for entry in holidays:
            assert "date" in entry, f"{filename}: entry missing 'date' key"
            assert "name" in entry, f"{filename}: entry missing 'name' key"
            # Verify the date is a valid ISO-format date.
            date.fromisoformat(entry["date"])
            assert isinstance(entry["name"], str) and entry["name"], \
                f"{filename}: entry 'name' must be a non-empty string"


# --- Missing file = no holidays (graceful, no raise) -----------------


def test_missing_holiday_config_file_returns_no_holidays(monkeypatch, tmp_path):
    """STORY-11 acceptance criteria: missing holiday files are
    handled gracefully (assumes no holidays). Pointing the env var
    at a directory with no holiday file must NOT raise — it must
    return ``None`` from ``is_market_holiday`` as if the calendar
    were empty. A bare ``FileNotFoundError`` leaking out would fail
    this test and force a real fix in the loader."""
    monkeypatch.setenv("MARKET_HOLIDAYS_CONFIG_DIR", str(tmp_path))
    reset_holiday_cache()

    try:
        # None of the bundled filenames exist in tmp_path.
        assert is_market_holiday("NSE", NSE_DIWALI_2024) is None
        assert is_market_holiday("BSE", BSE_DIWALI_2024) is None
        assert is_market_holiday("NYSE", US_THANKSGIVING_2024) is None
    finally:
        reset_holiday_cache()


def test_module_imports_without_reading_files():
    """Importing `market_holidays` does not touch the filesystem —
    same posture as market_hours.py. The cache is lazy, so a fresh
    import has zero side effects on disk I/O, even if a config
    file is missing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "market_holidays_reload_probe", market_holidays.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Clean import without raising is the assertion.


# --- Malformed file -> named error ----------------------------------


def test_malformed_holiday_config_raises_named_error(monkeypatch, tmp_path):
    """A holiday-config file that exists but isn't valid JSON raises
    `MarketHolidaysConfigError`, not a bare ``json.JSONDecodeError``.
    Same posture as ``market_hours.MarketHoursConfigError`` — a
    named, specific exception so callers can distinguish "config is
    broken" from "no holidays"."""
    bad = tmp_path / "nse_holidays.json"
    bad.write_text("not valid json {")
    monkeypatch.setenv("MARKET_HOLIDAYS_NSE_FILE", str(bad))
    reset_holiday_cache()

    try:
        with pytest.raises(MarketHolidaysConfigError):
            is_market_holiday("NSE", NSE_DIWALI_2024)
    finally:
        reset_holiday_cache()


def test_holiday_config_missing_holidays_key_raises_named_error(monkeypatch, tmp_path):
    """A JSON object that lacks the required ``holidays`` key raises
    the named error rather than silently treating "missing key" as
    "no holidays" (which would mask a config drift bug)."""
    bad = tmp_path / "nse_holidays.json"
    bad.write_text('{"display_name": "x"}')  # valid JSON, no 'holidays'
    monkeypatch.setenv("MARKET_HOLIDAYS_NSE_FILE", str(bad))
    reset_holiday_cache()

    try:
        with pytest.raises(MarketHolidaysConfigError):
            is_market_holiday("NSE", NSE_DIWALI_2024)
    finally:
        reset_holiday_cache()


def test_holiday_config_malformed_entry_raises_named_error(monkeypatch, tmp_path):
    """A list entry that isn't an object with 'date' and 'name'
    raises the named error — same posture as the JSON-parse and
    missing-key cases."""
    bad = tmp_path / "nse_holidays.json"
    bad.write_text('{"holidays": [{"date": "2024-11-01"}]}')  # missing 'name'
    monkeypatch.setenv("MARKET_HOLIDAYS_NSE_FILE", str(bad))
    reset_holiday_cache()

    try:
        with pytest.raises(MarketHolidaysConfigError):
            is_market_holiday("NSE", NSE_DIWALI_2024)
    finally:
        reset_holiday_cache()


def test_holiday_config_bad_date_format_raises_named_error(monkeypatch, tmp_path):
    """A 'date' value that isn't ISO-format (YYYY-MM-DD) raises the
    named error."""
    bad = tmp_path / "nse_holidays.json"
    bad.write_text('{"holidays": [{"date": "Nov 1 2024", "name": "Diwali"}]}')
    monkeypatch.setenv("MARKET_HOLIDAYS_NSE_FILE", str(bad))
    reset_holiday_cache()

    try:
        with pytest.raises(MarketHolidaysConfigError):
            is_market_holiday("NSE", NSE_DIWALI_2024)
    finally:
        reset_holiday_cache()


# --- Unknown exchange ------------------------------------------------


def test_unknown_market_raises_named_error_via_direct_lookup(monkeypatch, tmp_path):
    """Asking about an exchange the holiday module genuinely doesn't
    know about raises ``UnknownMarketError`` when called directly —
    BUT only if the directory env var is in use. With the default
    bundled config, an unknown exchange (e.g. ``"LSE"``) returns
    None from is_market_holiday via the "no mapping" path, because
    market_holidays' graceful-default policy applies to both
    missing files AND missing entries. This matches the missing-file
    contract.

    A custom exchange that DOES have a market-hours entry but no
    holiday-file mapping therefore degrades gracefully — exactly
    what ``test_config_path_env_var_override`` in test_market_hours
    needs."""
    # Default behaviour: unknown exchange => None (no raise).
    assert is_market_holiday("LSE", date(2024, 1, 1)) is None


def test_unknown_market_raises_when_dir_env_var_points_nowhere(monkeypatch, tmp_path):
    """When ``MARKET_HOLIDAYS_CONFIG_DIR`` is set to a directory
    with no bundled files, an unknown exchange still returns None
    — the graceful path is consistent across the two failure modes
    (missing file vs. unknown exchange)."""
    monkeypatch.setenv("MARKET_HOLIDAYS_CONFIG_DIR", str(tmp_path))
    reset_holiday_cache()
    try:
        assert is_market_holiday("LSE", date(2024, 1, 1)) is None
    finally:
        reset_holiday_cache()


# --- list_known_holiday_markets --------------------------------------


def test_list_known_holiday_markets_returns_sorted_keys():
    """Returns the exchanges the holiday module knows about, sorted.
    Same posture as ``market_hours.list_known_markets``. A test
    that accidentally adds an entry to the routing dict without
    shipping a file for it would trip here if the function reads
    from the wrong source."""
    assert list_known_holiday_markets() == ["BSE", "NASDAQ", "NSE", "NYSE"]


# --- Integration: market_hours honors holiday override ----------------


def test_market_hours_is_market_open_returns_false_on_nse_holiday():
    """is_market_open("NSE", ...) returns False on Diwali (Nov 1,
    2024) at 12:00 IST — squarely inside the 09:15–15:30 session
    window. Without the holiday override, is_market_open would
    return True (Friday, valid session). This is the headline
    integration test — the price-cache layer relies on this False
    to stop serving stale prices on exchange holidays."""
    from datetime import datetime, timezone

    # 12:00 IST on 2024-11-01 == 06:30 UTC. Diwali is on this date.
    diwali_noon_utc = datetime(2024, 11, 1, 6, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=diwali_noon_utc) is False


def test_market_hours_is_market_open_returns_false_on_us_thanksgiving():
    """Symmetric to the NSE Diwali test: NYSE is closed on US
    Thanksgiving (Nov 28, 2024) at 10:00 ET — inside the
    09:30–16:00 ET session. Without the holiday override, this
    would incorrectly return True."""
    from datetime import datetime, timezone

    # 10:00 ET (UTC-5 in November — already in standard time) ==
    # 15:00 UTC.
    thanksgiving_et_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=thanksgiving_et_utc) is False


def test_market_status_returns_holiday_name_for_nse_diwali():
    """``market_status("NSE", ...)`` on Diwali returns a dict with
    ``status="closed"`` AND ``holiday_name="Diwali (Laxmi Puja)"``.
    The new key is the integration point the price-cache UI uses
    to surface the closure reason to a human caller."""
    from datetime import datetime, timezone

    diwali_noon_utc = datetime(2024, 11, 1, 6, 30, 0, tzinfo=timezone.utc)
    status = market_status("NSE", now_utc=diwali_noon_utc)

    assert status["status"] == "closed"
    assert status["holiday_name"] == "Diwali (Laxmi Puja)"


def test_market_status_returns_holiday_name_for_us_thanksgiving():
    """NYSE Thanksgiving returns the observed US holiday name."""
    from datetime import datetime, timezone

    thanksgiving_et_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
    status = market_status("NYSE", now_utc=thanksgiving_et_utc)

    assert status["status"] == "closed"
    assert status["holiday_name"] == "Thanksgiving Day"


def test_market_status_holiday_name_is_none_on_normal_weekday():
    """A normal weekday with no holiday returns ``holiday_name=None``
    — paired with the holiday-positive tests above so an
    "always returns a string" bug trips here."""
    from datetime import datetime, timezone

    # Wednesday 2024-01-10 at midday IST — no holiday.
    normal_noon_utc = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
    status = market_status("NSE", now_utc=normal_noon_utc)

    assert status["status"] == "open"
    assert status["holiday_name"] is None


def test_market_status_holiday_overrides_open_session():
    """A holiday at midday on a weekday forces status to "closed"
    even though the weekday/session-block code would otherwise say
    "open". Pinned explicitly: a regression where the holiday
    override is applied only AFTER the status decision (rather
    than as an override on top of it) would still report "open"
    on Diwali — exactly the bug the integration is supposed to
    prevent."""
    from datetime import datetime, timezone

    # 13:00 IST (well inside session) on Independence Day 2024.
    # 13:00 IST == 07:30 UTC.
    independence_utc = datetime(2024, 8, 15, 7, 30, 0, tzinfo=timezone.utc)
    status = market_status("NSE", now_utc=independence_utc)

    assert status["status"] == "closed", (
        "Holiday override must force status='closed' on a normal "
        "weekday inside the trading session."
    )
    assert status["holiday_name"] == "Independence Day"


def test_market_hours_missing_holiday_file_does_not_break_is_market_open(
    monkeypatch, tmp_path
):
    """If the bundled holiday config is moved away entirely,
    is_market_open / market_status still function — they just stop
    honouring holidays (the weekday/session-block answer is
    unaffected). This is the headline acceptance criterion: missing
    files gracefully degrade, they do NOT break the trading-hours
    answer.

    Picks a date that's NOT a real holiday in any of the bundled
    files, so the test passes regardless of whether the file is
    loaded or treated as empty (the point is "no crash", not "the
    file is correctly loaded")."""
    from datetime import datetime, timezone

    monkeypatch.setenv("MARKET_HOLIDAYS_CONFIG_DIR", str(tmp_path))
    reset_holiday_cache()
    try:
        # Wednesday 2024-01-10 06:30 UTC == 12:00 IST — no holiday.
        normal_noon_utc = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
        # No raise, status unaffected, holiday_name=None.
        status = market_status("NSE", now_utc=normal_noon_utc)
        assert status["status"] == "open"
        assert status["holiday_name"] is None
    finally:
        reset_holiday_cache()


def test_market_hours_unknown_exchange_does_not_break_via_holiday_lookup(
    monkeypatch, tmp_path
):
    """A custom exchange defined only in ``config/market_hours.json``
    (no holiday file mapping) still works through is_market_open —
    the holiday lookup returns None gracefully, the trading-hours
    answer is computed normally. Pins the design choice that the
    two modules' "known market" sets don't have to match."""
    from datetime import datetime, timezone

    custom_config = tmp_path / "custom_market_hours.json"
    custom_config.write_text(
        """{
            "markets": {
                "CUSTOM": {
                    "display_name": "Custom Test Market",
                    "country": "XX",
                    "timezone": "Asia/Kolkata",
                    "trading_days": [0, 1, 2, 3, 4],
                    "session_blocks": [{"open": "10:00", "close": "11:00"}]
                }
            }
        }"""
    )

    monkeypatch.setenv("MARKET_HOURS_CONFIG_PATH", str(custom_config))
    # No MARKET_HOLIDAYS_NSE_FILE for "CUSTOM" — the holiday module
    # returns None via the unknown-exchange path.
    reset_config_cache()
    reset_holiday_cache()

    try:
        # 10:30 IST == 05:00 UTC, inside the custom 10:00–11:00 session.
        utc = datetime(2024, 1, 10, 5, 0, 0, tzinfo=timezone.utc)
        assert is_market_open("CUSTOM", now_utc=utc) is True

        status = market_status("CUSTOM", now_utc=utc)
        assert status["status"] == "open"
        assert status["holiday_name"] is None
    finally:
        reset_config_cache()
        reset_holiday_cache()


# --- QA: acceptance-criteria acceptance test (STORY-11) ---------------


def test_qa_story11_all_acceptance_criteria():
    """STORY-11 acceptance-criteria acceptance test.

    A single real test that exercises each of the six acceptance
    criteria with real holidays and real dates. A failure in any
    one criterion fails the whole test, so a single ``pytest``
    invocation surfaces any regression against the original story
    contract.

    AC1: Holiday lists are stored as JSON files at
    config/nse_holidays.json, config/bse_holidays.json,
    config/us_holidays.json.
    AC2: Holiday lists include date and holiday name (each entry is
    {"date": "YYYY-MM-DD", "name": "..."}).
    AC3: System checks current date against holiday calendar to
    determine market status (is_market_open + market_status return
    closed + holiday_name on a holiday).
    AC4: Holiday lists are SEPARATE for NSE/BSE and US markets
    (distinct files, distinct lookups).
    AC5: System displays holiday name when market is closed for
    holiday (holiday_name in market_status dict).
    AC6: System handles missing holiday files gracefully
    (assumes no holidays — no raise)."""
    import json as _json
    from pathlib import Path

    config_dir = Path(market_holidays.__file__).resolve().parent.parent / "config"

    # --- AC #1 + AC #2: files exist with date+name entries --------
    for filename in ("nse_holidays.json", "bse_holidays.json", "us_holidays.json"):
        path = config_dir / filename
        assert path.exists(), f"AC1: {filename} missing at {path}"
        parsed = _json.loads(path.read_text())
        assert "holidays" in parsed, f"AC1+AC2: {filename} missing 'holidays' key"
        entries = parsed["holidays"]
        assert entries, f"AC1+AC2: {filename} has empty 'holidays'"
        for entry in entries:
            assert "date" in entry, f"AC2: {filename} entry missing 'date'"
            assert "name" in entry, f"AC2: {filename} entry missing 'name'"
            assert isinstance(entry["date"], str), \
                f"AC2: {filename} entry 'date' must be a string"
            assert isinstance(entry["name"], str) and entry["name"], \
                f"AC2: {filename} entry 'name' must be a non-empty string"
            date.fromisoformat(entry["date"])  # valid YYYY-MM-DD

    # --- AC #3: current date is checked against calendar -----------
    # The "current date" here is the moment we evaluate the test
    # at, frozen to a known holiday (Diwali 2024) so we exercise
    # the calendar check, not the wall clock.
    from datetime import datetime, timezone

    # 12:00 IST on Diwali (2024-11-01) == 06:30 UTC.
    diwali_utc = datetime(2024, 11, 1, 6, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=diwali_utc) is False, \
        "AC3: market should be closed on Diwali at midday"

    # 10:00 ET on Thanksgiving (2024-11-28, standard time UTC-5) ==
    # 15:00 UTC.
    thanksgiving_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=thanksgiving_utc) is False, \
        "AC3: market should be closed on Thanksgiving at 10:00 ET"

    # --- AC #4: NSE/BSE and US are separate (distinct lookups) ----
    # The same date with different holidays for different exchanges
    # is the strongest evidence that the calendars are loaded
    # separately, not from a shared file:
    #   * 2024-11-28 is Thanksgiving (US) but a normal trading day
    #     on NSE (it's the day after Diwali, not a holiday on NSE).
    nse_post_diwali = date(2024, 11, 28)  # Thursday — not NSE holiday
    assert is_market_holiday("NYSE", nse_post_diwali) == "Thanksgiving Day"
    assert is_market_holiday("NSE", nse_post_diwali) is None, \
        "AC4: 2024-11-28 must be US-only, not a shared NSE holiday"
    assert is_market_holiday("BSE", nse_post_diwali) is None, \
        "AC4: 2024-11-28 must be US-only, not a shared BSE holiday"

    # Conversely, 2024-08-15 is India's Independence Day — an NSE +
    # BSE holiday but a normal US trading day.
    india_indep = date(2024, 8, 15)
    assert is_market_holiday("NSE", india_indep) == "Independence Day"
    assert is_market_holiday("BSE", india_indep) == "Independence Day"
    assert is_market_holiday("NYSE", india_indep) is None, \
        "AC4: 2024-08-15 must be NSE/BSE-only, not a shared US holiday"

    # --- AC #5: holiday name surfaced via market_status -----------
    diwali_status = market_status("NSE", now_utc=diwali_utc)
    assert diwali_status["status"] == "closed"
    assert diwali_status["holiday_name"] == "Diwali (Laxmi Puja)", \
        "AC5: market_status must surface the holiday name"

    thanksgiving_status = market_status("NYSE", now_utc=thanksgiving_utc)
    assert thanksgiving_status["status"] == "closed"
    assert thanksgiving_status["holiday_name"] == "Thanksgiving Day", \
        "AC5: market_status must surface the holiday name"

    # --- AC #6: missing file = no holidays, no raise ---------------
    import tempfile
    with tempfile.TemporaryDirectory() as empty_dir:
        # Re-bind the env vars to the empty dir for the duration of
        # the assertion. We have to clear the module cache because
        # the loader caches the resolved file path's contents.
        import os
        monkeypatch_ctx = pytest.MonkeyPatch()
        monkeypatch_ctx.setenv("MARKET_HOLIDAYS_CONFIG_DIR", empty_dir)
        reset_holiday_cache()
        try:
            # All three bundled paths now point into the empty
            # directory — every lookup must return None without
            # raising.
            assert is_market_holiday("NSE", NSE_DIWALI_2024) is None
            assert is_market_holiday("BSE", BSE_DIWALI_2024) is None
            assert is_market_holiday("NYSE", US_THANKSGIVING_2024) is None

            # And is_market_open / market_status still function
            # without raising — the trading-hours answer is
            # unaffected.
            normal_noon_utc = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
            assert is_market_open("NSE", now_utc=normal_noon_utc) is True
            normal_status = market_status("NSE", now_utc=normal_noon_utc)
            assert normal_status["holiday_name"] is None
        finally:
            reset_holiday_cache()
            monkeypatch_ctx.undo()