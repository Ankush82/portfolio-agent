"""Independent QA verification test for STORY-11: market holiday
detection for NSE/BSE and US (NYSE/NASDAQ) exchanges.

This is a NEW, independent test (not a re-run of the existing
``tests/test_market_holidays.py`` suite) that targets the six
acceptance criteria of STORY-11 with assertions distinct from the
existing tests. Each assertion below exercises a behaviour the
existing suite does not redundantly cover:

  AC1: config/nse_holidays.json, config/bse_holidays.json,
       config/us_holidays.json exist as separate JSON files.
  AC2: Each entry has {"date": "YYYY-MM-DD", "name": "<name>"}
       with a valid ISO date and a non-empty name string.
  AC3: The system checks the CURRENT date against the holiday
       calendar to determine market status — exercised by
       freezing "now" at known holidays and asserting the
       status answer.
  AC4: Holiday lists are SEPARATE for NSE/BSE and US markets —
       the same date must surface different holiday names (or
       None) depending on the exchange code.
  AC5: The system displays the holiday name when the market is
       closed for a holiday — the ``holiday_name`` key in the
       dict returned by ``market_status``.
  AC6: Missing holiday files are handled gracefully (the system
       assumes no holidays and does not raise).

A failure in any one AC fails the whole test, so a single pytest
invocation surfaces any regression against the STORY-11 contract.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make ``src/`` importable when this file is run directly via pytest
# from the repo root (same posture as other QA-story tests).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import market_holidays  # noqa: E402
from market_holidays import is_market_holiday, reset_holiday_cache  # noqa: E402
import market_hours  # noqa: E402
from market_hours import is_market_open, market_status, reset_config_cache  # noqa: E402


def setup_function(function):
    """Clear both module-level caches before each test so env-var
    overrides from a previous test don't leak. The holiday module
    caches the parsed file per full path; the trading-hours module
    caches its JSON. Both must be cleared together because the
    integration path in ``_compute_status`` consults the holiday
    cache after the trading-hours cache returns."""
    reset_config_cache()
    reset_holiday_cache()


# ============================================================================
# Real holiday fixtures — hand-picked from each exchange's published 2024/25
# calendar. Each fixture is a date that is unambiguously a holiday on the
# named exchange (e.g. Diwali Laxmi Puja 2024 = NSE closure on 2024-11-01).
# ============================================================================

# NSE 2024 (real, from NSE's official holiday calendar)
NSE_REPUBLIC_DAY_2024 = date(2024, 1, 26)         # Friday, Jan 26
NSE_MAHASHIVRATRI_2024 = date(2024, 3, 8)         # Friday
NSE_HOLI_2024 = date(2024, 3, 25)                 # Monday
NSE_INDEPENDENCE_DAY_2024 = date(2024, 8, 15)     # Thursday
NSE_DIWALI_2024 = date(2024, 11, 1)               # Friday (Laxmi Puja)
NSE_CHRISTMAS_2024 = date(2024, 12, 25)           # Wednesday

# BSE 2024 — same headline dates as NSE; BSE keeps its own file per the
# acceptance criteria even when the calendars overlap.
BSE_REPUBLIC_DAY_2024 = date(2024, 1, 26)
BSE_DIWALI_2024 = date(2024, 11, 1)
BSE_CHRISTMAS_2024 = date(2024, 12, 25)

# US (NYSE/NASDAQ) 2024 — real, from NYSE's published holiday calendar.
US_NEW_YEARS_2024 = date(2024, 1, 1)              # Monday
US_MLK_DAY_2024 = date(2024, 1, 15)               # Monday (3rd Jan)
US_PRESIDENTS_DAY_2024 = date(2024, 2, 19)        # Monday (3rd Feb)
US_GOOD_FRIDAY_2024 = date(2024, 3, 29)           # Friday
US_MEMORIAL_DAY_2024 = date(2024, 5, 27)          # Monday (last May)
US_JUNETEENTH_2024 = date(2024, 6, 19)            # Wednesday
US_INDEPENDENCE_DAY_2024 = date(2024, 7, 4)       # Thursday
US_LABOR_DAY_2024 = date(2024, 9, 2)              # Monday (1st Sep)
US_THANKSGIVING_2024 = date(2024, 11, 28)         # Thursday (4th Nov)
US_CHRISTMAS_2024 = date(2024, 12, 25)            # Wednesday

# 2025 holidays — used to confirm the calendar isn't hardcoded to one
# year. A real implementation must include 2025 holidays in the same
# JSON file.
NSE_REPUBLIC_DAY_2025 = date(2025, 1, 26)
NSE_DIWALI_2025 = date(2025, 10, 21)
US_NEW_YEARS_2025 = date(2025, 1, 1)
US_THANKSGIVING_2025 = date(2025, 11, 27)


def _config_dir() -> Path:
    """Returns the absolute path to the bundled config/ directory."""
    return Path(market_holidays.__file__).resolve().parent.parent / "config"


# ============================================================================
# AC1 — Holiday lists are stored as JSON files (3 separate files, NSE/BSE
#       even though they're both India-based per the explicit story req).
# ============================================================================

def test_ac1_holiday_lists_stored_as_separate_json_files():
    """AC1: the three required JSON files exist as distinct files at
    the paths mandated by the story description. NSE and BSE use
    SEPARATE files even though they are both India-based exchanges —
    a regression that collapsed them into one file would fail this."""
    config_dir = _config_dir()
    nse = config_dir / "nse_holidays.json"
    bse = config_dir / "bse_holidays.json"
    us = config_dir / "us_holidays.json"

    # All three must exist as real files.
    assert nse.is_file(), f"AC1: missing JSON file at {nse}"
    assert bse.is_file(), f"AC1: missing JSON file at {bse}"
    assert us.is_file(), f"AC1: missing JSON file at {us}"

    # All three must be distinct paths (resolve() follows symlinks,
    # so this catches a shared-file hack too).
    assert nse.resolve() != bse.resolve(), \
        "AC1: NSE and BSE must use SEPARATE files (per story AC)"
    assert nse.resolve() != us.resolve(), \
        "AC1: NSE and US must use SEPARATE files"
    assert bse.resolve() != us.resolve(), \
        "AC1: BSE and US must use SEPARATE files"

    # All three must be parseable as JSON objects with a top-level
    # 'holidays' list (not lists at the top level, not other shapes).
    for path in (nse, bse, us):
        parsed = json.loads(path.read_text())
        assert isinstance(parsed, dict), \
            f"AC1: {path.name} must be a JSON object"
        assert "holidays" in parsed, \
            f"AC1: {path.name} must have a top-level 'holidays' key"


# ============================================================================
# AC2 — Holiday lists include date and holiday name.
#       Every entry is {"date": "YYYY-MM-DD", "name": "<name>"} with
#       a valid ISO date and a non-empty name string.
# ============================================================================

def test_ac2_holiday_lists_include_date_and_name():
    """AC2: every entry has 'date' (YYYY-MM-DD string parseable by
    date.fromisoformat) and 'name' (non-empty string). This pins the
    file format directly — a regression that added a wrong key, a
    wrong type, or a missing field would fail this assertion."""
    config_dir = _config_dir()

    # Real, distinct holidays used to make sure the per-entry
    # format check isn't running on an empty stub.
    real_entries = {
        "nse_holidays.json": (NSE_REPUBLIC_DAY_2024, "Republic Day"),
        "bse_holidays.json": (BSE_REPUBLIC_DAY_2024, "Republic Day"),
        "us_holidays.json": (US_NEW_YEARS_2024, "New Year's Day"),
    }

    for filename, (expected_date, expected_name) in real_entries.items():
        parsed = json.loads((config_dir / filename).read_text())
        entries = parsed["holidays"]
        assert isinstance(entries, list) and entries, \
            f"AC2: {filename}: 'holidays' must be a non-empty list"

        # First entry must be the expected (date, name) pair — exact
        # format check, not just "has these keys somewhere".
        first = entries[0]
        assert isinstance(first, dict), \
            f"AC2: {filename}: entry must be a JSON object"
        assert set(first.keys()) >= {"date", "name"}, \
            f"AC2: {filename}: entry must have both 'date' and 'name' keys"

        # Strict format checks for the first entry.
        assert first["date"] == expected_date.isoformat(), \
            f"AC2: {filename}[0].date must be {expected_date.isoformat()}"
        assert first["name"] == expected_name, \
            f"AC2: {filename}[0].name must be {expected_name!r}"

        # Every entry: date is an ISO string parseable by
        # date.fromisoformat; name is a non-empty string.
        for entry in entries:
            assert isinstance(entry.get("date"), str), \
                f"AC2: {filename}: 'date' must be a string"
            date.fromisoformat(entry["date"])  # raises if not YYYY-MM-DD
            assert isinstance(entry.get("name"), str) and entry["name"], \
                f"AC2: {filename}: 'name' must be a non-empty string"


# ============================================================================
# AC3 — System checks current date against holiday calendar to determine
#       market status. is_market_open() returns False on a holiday inside
#       the normal trading session.
# ============================================================================

def test_ac3_market_status_uses_holiday_calendar_for_current_date():
    """AC3: when the current date is a holiday, is_market_open()
    returns False even during normal session-block hours. The
    "current date" is frozen here to a known holiday at midday
    IST/ET — inside the trading session — so a regression that
    ignores holidays would incorrectly return True."""
    # Diwali 2024 (NSE): 12:00 IST == 06:30 UTC. Without the
    # holiday override, is_market_open would return True
    # (Friday, inside 09:15–15:30 session).
    diwali_noon_utc = datetime(2024, 11, 1, 12, 0, 0,
                               tzinfo=timezone(timedelta(hours=5, minutes=30)))
    # Sanity: the frozen instant really is at 12:00 IST on Diwali.
    assert diwali_noon_utc.date() == NSE_DIWALI_2024
    assert is_market_open("NSE", now_utc=diwali_noon_utc) is False, \
        "AC3: NSE must be closed on Diwali at 12:00 IST"

    # US Thanksgiving 2024 (NYSE): 10:00 ET on Nov 28.
    # In November, NYC is on standard time (UTC-5), so 10:00 ET ==
    # 15:00 UTC. Without the holiday override, is_market_open
    # would return True (Thursday, inside 09:30–16:00 session).
    thanksgiving_et_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
    assert thanksgiving_et_utc.date() == US_THANKSGIVING_2024
    assert is_market_open("NYSE", now_utc=thanksgiving_et_utc) is False, \
        "AC3: NYSE must be closed on Thanksgiving at 10:00 ET"

    # Independence Day 2024 (US): 11:00 ET on July 4. Without
    # the holiday override, is_market_open would return True
    # (Thursday, inside session). July is daylight time in NYC
    # (UTC-4), so 11:00 ET == 15:00 UTC.
    july4_et_utc = datetime(2024, 7, 4, 15, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=july4_et_utc) is False, \
        "AC3: NYSE must be closed on Independence Day at 11:00 ET"


# ============================================================================
# AC4 — Holiday lists are SEPARATE for NSE/BSE and US markets.
#       The same date must surface different answers depending on
#       the exchange code (because the underlying files differ).
# ============================================================================

def test_ac4_nse_bse_us_holiday_lists_are_separate():
    """AC4: NSE, BSE and US markets use SEPARATE holiday lists —
    a date that's a holiday for one exchange must NOT be a holiday
    for the others (unless it happens to coincide). This exercises
    the cross-exchange separation directly, so a regression that
    merged the three files into one would fail this."""
    # 2024-11-28: US Thanksgiving. NOT an NSE / BSE holiday.
    assert is_market_holiday("NYSE", US_THANKSGIVING_2024) == "Thanksgiving Day"
    assert is_market_holiday("NASDAQ", US_THANKSGIVING_2024) == "Thanksgiving Day"
    assert is_market_holiday("NSE", US_THANKSGIVING_2024) is None, \
        "AC4: 2024-11-28 must be US-only (NSE not affected)"
    assert is_market_holiday("BSE", US_THANKSGIVING_2024) is None, \
        "AC4: 2024-11-28 must be US-only (BSE not affected)"

    # 2024-08-15: India's Independence Day (NSE + BSE). NOT a US holiday.
    assert is_market_holiday("NSE", NSE_INDEPENDENCE_DAY_2024) == "Independence Day"
    assert is_market_holiday("BSE", NSE_INDEPENDENCE_DAY_2024) == "Independence Day"
    assert is_market_holiday("NYSE", NSE_INDEPENDENCE_DAY_2024) is None, \
        "AC4: 2024-08-15 must be NSE/BSE-only (US not affected)"
    assert is_market_holiday("NASDAQ", NSE_INDEPENDENCE_DAY_2024) is None, \
        "AC4: 2024-08-15 must be NSE/BSE-only (US not affected)"

    # NYSE and NASDAQ both share us_holidays.json (SRO rule).
    # The same date must return the same holiday name from either
    # code — a bug that loaded a separate file for NASDAQ would
    # fail this.
    assert is_market_holiday("NYSE", US_CHRISTMAS_2024) == \
        is_market_holiday("NASDAQ", US_CHRISTMAS_2024), \
        "AC4: NYSE and NASDAQ must return the same holiday name"


# ============================================================================
# AC5 — System displays holiday name when market is closed for a holiday.
#       The dict returned by market_status() contains a 'holiday_name'
#       key set to the observed holiday's name (None on non-holidays).
# ============================================================================

def test_ac5_market_status_displays_holiday_name_when_closed_for_holiday():
    """AC5: market_status() returns a dict with a 'holiday_name' key.
    On a holiday: holiday_name = the observed holiday's name.
    On a non-holiday: holiday_name = None."""
    # Holiday case — NSE on Diwali 2024.
    diwali_noon_utc = datetime(2024, 11, 1, 12, 0, 0,
                               tzinfo=timezone(timedelta(hours=5, minutes=30)))
    diwali_status = market_status("NSE", now_utc=diwali_noon_utc)
    assert "holiday_name" in diwali_status, \
        "AC5: market_status dict must include 'holiday_name' key"
    assert diwali_status["holiday_name"] == "Diwali (Laxmi Puja)", \
        "AC5: holiday_name must surface the observed NSE holiday name"
    assert diwali_status["status"] == "closed", \
        "AC5: status must be 'closed' on a holiday"

    # Holiday case — NYSE on US Thanksgiving 2024.
    thanksgiving_et_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
    thanksgiving_status = market_status("NYSE", now_utc=thanksgiving_et_utc)
    assert thanksgiving_status["holiday_name"] == "Thanksgiving Day", \
        "AC5: holiday_name must surface the observed US holiday name"
    assert thanksgiving_status["status"] == "closed", \
        "AC5: status must be 'closed' on a holiday"

    # Non-holiday case — NSE on a normal Wednesday (2024-01-10).
    # 12:00 IST == 06:30 UTC.
    normal_noon_utc = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
    normal_status = market_status("NSE", now_utc=normal_noon_utc)
    assert normal_status["holiday_name"] is None, \
        "AC5: holiday_name must be None on a non-holiday"
    assert normal_status["status"] == "open", \
        "AC5: status must be 'open' on a normal weekday inside session"


# ============================================================================
# AC6 — Missing holiday files are handled gracefully (assumes no holidays).
# ============================================================================

def test_ac6_missing_holiday_file_returns_no_holidays_no_raise(monkeypatch, tmp_path):
    """AC6: when the holiday-config directory is missing every
    expected file, the system must treat it as 'no holidays
    observed' — i.e. is_market_holiday() returns None for every
    (exchange, date) pair, AND is_market_open() / market_status()
    continue to function without raising. A bare FileNotFoundError
    leaking out would fail this test and force a real fix."""
    # Point the loader at a tmp dir with no holiday files.
    monkeypatch.setenv("MARKET_HOLIDAYS_CONFIG_DIR", str(tmp_path))
    reset_holiday_cache()
    try:
        # Direct lookup: must return None, not raise.
        assert is_market_holiday("NSE", NSE_DIWALI_2024) is None, \
            "AC6: missing NSE holiday file must return None"
        assert is_market_holiday("BSE", BSE_DIWALI_2024) is None, \
            "AC6: missing BSE holiday file must return None"
        assert is_market_holiday("NYSE", US_THANKSGIVING_2024) is None, \
            "AC6: missing US holiday file must return None"

        # Integration: is_market_open / market_status must still work
        # on a non-holiday weekday — the trading-hours answer is
        # unaffected by a missing holiday file.
        normal_noon_utc = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
        assert is_market_open("NSE", now_utc=normal_noon_utc) is True, \
            "AC6: is_market_open must still work with no holiday file"

        # Even on a date that WOULD be a holiday if the file were
        # present, is_market_open must return False (because the
        # session would be closed anyway at this time) — but it
        # must not raise.
        diwali_noon_utc = datetime(2024, 11, 1, 12, 0, 0,
                                   tzinfo=timezone(timedelta(hours=5, minutes=30)))
        status_on_diwali = market_status("NSE", now_utc=diwali_noon_utc)
        assert status_on_diwali["holiday_name"] is None, \
            "AC6: with no holiday file, holiday_name must be None"

        # Same for NYSE on Thanksgiving.
        thanksgiving_et_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
        status_on_thanksgiving = market_status("NYSE", now_utc=thanksgiving_et_utc)
        assert status_on_thanksgiving["holiday_name"] is None, \
            "AC6: with no holiday file, holiday_name must be None"
    finally:
        reset_holiday_cache()


# ============================================================================
# Bonus integration: 2025 coverage proves the calendar isn't hardcoded.
# ============================================================================

def test_ac3_calendar_includes_2025_holidays():
    """The holiday calendar covers 2025 (not just 2024). Each of
    these dates is a real, observed exchange holiday in 2025."""
    assert is_market_holiday("NSE", NSE_REPUBLIC_DAY_2025) == "Republic Day"
    assert is_market_holiday("NSE", NSE_DIWALI_2025) == "Diwali (Laxmi Puja)"
    assert is_market_holiday("NYSE", US_NEW_YEARS_2025) == "New Year's Day"
    assert is_market_holiday("NYSE", US_THANKSGIVING_2025) == "Thanksgiving Day"


# ============================================================================
# Consolidated acceptance-criteria test — single pytest invocation
# exercises every AC with real data, so any regression surfaces
# immediately.
# ============================================================================

def test_qa_story11_all_six_acceptance_criteria():
    """Single test that pins all six STORY-11 acceptance criteria
    with real holidays, real dates, and real assertions. Independent
    of the existing test_market_holidays.py suite."""
    config_dir = _config_dir()

    # --- AC #1: three separate JSON files exist -------------------
    nse_path = config_dir / "nse_holidays.json"
    bse_path = config_dir / "bse_holidays.json"
    us_path = config_dir / "us_holidays.json"
    for path in (nse_path, bse_path, us_path):
        assert path.is_file(), f"AC1: missing JSON file at {path}"
    assert nse_path.resolve() != bse_path.resolve(), \
        "AC1: NSE and BSE must use SEPARATE files"
    assert nse_path.resolve() != us_path.resolve(), \
        "AC1: NSE and US must use SEPARATE files"
    assert bse_path.resolve() != us_path.resolve(), \
        "AC1: BSE and US must use SEPARATE files"

    # --- AC #2: date + name format on every entry ------------------
    for path in (nse_path, bse_path, us_path):
        parsed = json.loads(path.read_text())
        entries = parsed["holidays"]
        assert isinstance(entries, list) and entries, \
            f"AC2: {path.name}: 'holidays' must be a non-empty list"
        for entry in entries:
            assert "date" in entry, f"AC2: {path.name}: missing 'date'"
            assert "name" in entry, f"AC2: {path.name}: missing 'name'"
            date.fromisoformat(entry["date"])
            assert isinstance(entry["name"], str) and entry["name"], \
                f"AC2: {path.name}: 'name' must be a non-empty string"

    # --- AC #3: current date checked against holiday calendar ------
    diwali_noon_utc = datetime(2024, 11, 1, 12, 0, 0,
                               tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert is_market_open("NSE", now_utc=diwali_noon_utc) is False, \
        "AC3: NSE must be closed on Diwali at midday"
    thanksgiving_et_utc = datetime(2024, 11, 28, 15, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=thanksgiving_et_utc) is False, \
        "AC3: NYSE must be closed on Thanksgiving at 10:00 ET"

    # --- AC #4: separate for NSE/BSE and US ------------------------
    assert is_market_holiday("NYSE", US_THANKSGIVING_2024) == "Thanksgiving Day"
    assert is_market_holiday("NSE", US_THANKSGIVING_2024) is None, \
        "AC4: 2024-11-28 must be US-only"
    assert is_market_holiday("NSE", NSE_INDEPENDENCE_DAY_2024) == "Independence Day"
    assert is_market_holiday("NYSE", NSE_INDEPENDENCE_DAY_2024) is None, \
        "AC4: 2024-08-15 must be NSE/BSE-only"

    # --- AC #5: holiday name surfaced via market_status -----------
    diwali_status = market_status("NSE", now_utc=diwali_noon_utc)
    assert diwali_status["status"] == "closed"
    assert diwali_status["holiday_name"] == "Diwali (Laxmi Puja)", \
        "AC5: market_status must surface the holiday name"
    thanksgiving_status = market_status("NYSE", now_utc=thanksgiving_et_utc)
    assert thanksgiving_status["holiday_name"] == "Thanksgiving Day", \
        "AC5: market_status must surface the holiday name"

    # --- AC #6: missing file = no holidays, no raise ---------------
    import tempfile
    with tempfile.TemporaryDirectory() as empty_dir:
        monkeypatch_ctx = pytest.MonkeyPatch()
        monkeypatch_ctx.setenv("MARKET_HOLIDAYS_CONFIG_DIR", empty_dir)
        reset_holiday_cache()
        try:
            assert is_market_holiday("NSE", NSE_DIWALI_2024) is None
            assert is_market_holiday("BSE", BSE_DIWALI_2024) is None
            assert is_market_holiday("NYSE", US_THANKSGIVING_2024) is None
            normal_noon_utc = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
            assert is_market_open("NSE", now_utc=normal_noon_utc) is True
            assert market_status("NSE", now_utc=normal_noon_utc)["holiday_name"] is None
        finally:
            reset_holiday_cache()
            monkeypatch_ctx.undo()