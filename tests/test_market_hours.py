"""Tests for src/market_hours.py — NSE/BSE and US (NYSE/NASDAQ) market
hours detection.

The hand-computed UTC timestamps below are derived from the documented
session windows (NSE/BSE: 09:15–15:30 IST = +05:30 fixed; NYSE/NASDAQ:
09:30–16:00 America/New_York = UTC-5 standard / UTC-4 daylight), so a
real zoneinfo bug — e.g. hardcoding UTC-5 for New York, ignoring DST
— will trip at least one of these tests. The DST-transition test uses
March 2024 dates (US "spring forward" was March 10, 2024 — clocks
moved from UTC-5 to UTC-4 at 02:00 local) and November 2024 dates
("fall back" was November 3, 2024 — clocks moved from UTC-4 to UTC-5
at 02:00 local), so a correct zoneinfo implementation must agree on
both sides of *both* transitions without any custom offset arithmetic.

`market_hours` has a module-level config cache so tests that don't
mutate the config file don't need to call `reset_config_cache`. The
few tests that *do* mutate the config (e.g. to assert the
`UnknownMarketError` path) explicitly clear the cache so the change
takes effect on the very next call."""

from datetime import datetime, timezone

import pytest

import market_hours
from market_hours import (
    MarketHoursConfigError,
    UnknownMarketError,
    is_market_open,
    list_known_markets,
    market_status,
    reset_config_cache,
)


# Hand-computed UTC timestamps for the NSE/BSE window (09:15–15:30 IST
# = +05:30 fixed; Asia/Kolkata has no DST in practice). All chosen on
# a Wednesday so weekday-vs-weekday is the only thing varying.

# 2024-01-10 (Wednesday) 09:15:00 IST == 2024-01-10 03:45:00 UTC.
_NSE_OPEN_INSTANT_UTC = datetime(2024, 1, 10, 3, 45, 0, tzinfo=timezone.utc)
# 2024-01-10 09:14:59 IST == 03:44:59 UTC — one second before open.
_NSE_JUST_BEFORE_OPEN_UTC = datetime(2024, 1, 10, 3, 44, 59, tzinfo=timezone.utc)
# 2024-01-10 12:00:00 IST == 06:30:00 UTC — middle of the session.
_NSE_MIDDAY_UTC = datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)
# 2024-01-10 15:29:59 IST == 09:59:59 UTC — one second before close.
_NSE_JUST_BEFORE_CLOSE_UTC = datetime(2024, 1, 10, 9, 59, 59, tzinfo=timezone.utc)
# 2024-01-10 15:30:00 IST == 10:00:00 UTC — exact close instant; the
# half-open interval convention makes this OUTSIDE the session.
_NSE_EXACT_CLOSE_UTC = datetime(2024, 1, 10, 10, 0, 0, tzinfo=timezone.utc)
# 2024-01-10 15:31:00 IST == 10:01:00 UTC — well after close.
_NSE_AFTER_CLOSE_UTC = datetime(2024, 1, 10, 10, 1, 0, tzinfo=timezone.utc)
# 2024-01-10 16:00:00 IST == 10:30:00 UTC — used for the "after-hours
# but still on a weekday" market_status test (next open should be
# tomorrow's 09:15 IST, not Monday's).
_NSE_AFTER_HOURS_UTC = datetime(2024, 1, 10, 10, 30, 0, tzinfo=timezone.utc)

# Saturday 2024-01-13 in IST. The equivalent UTC depends on the IST
# date, not the UTC date. 2024-01-13 12:00:00 IST == 2024-01-13
# 06:30:00 UTC.
_NSE_SATURDAY_UTC = datetime(2024, 1, 13, 6, 30, 0, tzinfo=timezone.utc)
# Sunday 2024-01-14 in IST. 2024-01-14 12:00:00 IST == 2024-01-14
# 06:30:00 UTC.
_NSE_SUNDAY_UTC = datetime(2024, 1, 14, 6, 30, 0, tzinfo=timezone.utc)


def _et_to_utc(year: int, month: int, day: int, hour: int, minute: int, *, dst: bool) -> datetime:
    """Converts a wall-clock ET (America/New_York) time to the
    equivalent UTC `datetime` for test fixtures. Uses real
    `ZoneInfo('America/New_York').utcoffset()` for the date in
    question — NOT a hardcoded offset — so a test that accidentally
    swaps a pre-DST and a post-DST timestamp will compute the wrong
    UTC and fail loudly.

    `dst=True` => caller asserts the date is in daylight time (UTC-4)
    and the function will FAIL if zoneinfo disagrees (e.g. the date is
    actually in standard time). This is the guardrail that makes the
    DST-transition tests real: if zoneinfo's offset rules change or
    are misconfigured, the test errors out before any market-status
    assertion runs."""
    from zoneinfo import ZoneInfo

    ny_zone = ZoneInfo("America/New_York")
    local = datetime(year, month, day, hour, minute, tzinfo=ny_zone)
    offset = local.utcoffset()
    if offset is None:
        raise AssertionError(f"No UTC offset for {local}")
    is_dst = offset.total_seconds() == -4 * 3600
    if dst and not is_dst:
        raise AssertionError(
            f"Test fixture expected DST (UTC-4) for {year}-{month}-{day}, "
            f"but zoneinfo says offset is {offset} (DST={is_dst})"
        )
    if not dst and is_dst:
        raise AssertionError(
            f"Test fixture expected standard time (UTC-5) for {year}-{month}-{day}, "
            f"but zoneinfo says offset is {offset} (DST={is_dst})"
        )
    return local.astimezone(timezone.utc)


# --- NSE / BSE: real-time IST conversion -------------------------------


def test_nse_open_at_session_start():
    """09:15:00 IST on a weekday is exactly when the session opens.
    A naive implementation that opens one minute early or late would
    fail this test."""
    assert is_market_open("NSE", now_utc=_NSE_OPEN_INSTANT_UTC) is True


def test_nse_open_midway_through_session():
    """12:00 IST is unambiguously inside the session — neither close
    to open nor close to close."""
    assert is_market_open("NSE", now_utc=_NSE_MIDDAY_UTC) is True


def test_nse_open_one_second_before_close():
    """15:29:59 IST is the last second of the session — a real
    off-by-one bug would flip this to False."""
    assert is_market_open("NSE", now_utc=_NSE_JUST_BEFORE_CLOSE_UTC) is True


def test_nse_closed_at_exact_close():
    """15:30:00 IST is the close instant; the half-open convention
    means the session is no longer active."""
    assert is_market_open("NSE", now_utc=_NSE_EXACT_CLOSE_UTC) is False


def test_nse_closed_one_minute_after_close():
    """15:31:00 IST is one minute past close — solidly after-hours."""
    assert is_market_open("NSE", now_utc=_NSE_AFTER_CLOSE_UTC) is False


def test_nse_closed_one_second_before_open():
    """09:14:59 IST is one second before the session opens — a
    real off-by-one bug would flip this to True."""
    assert is_market_open("NSE", now_utc=_NSE_JUST_BEFORE_OPEN_UTC) is False


def test_bse_uses_same_window_as_nse():
    """BSE has the same session hours as NSE by design; assert that
    the same hand-computed UTC instant is open for BSE too. A real
    config bug (e.g. accidentally writing 09:30 instead of 09:15 for
    BSE) would flip this."""
    assert is_market_open("BSE", now_utc=_NSE_OPEN_INSTANT_UTC) is True
    assert is_market_open("BSE", now_utc=_NSE_AFTER_CLOSE_UTC) is False


# --- NSE / BSE: weekend closure ----------------------------------------


def test_nse_closed_on_saturday():
    """Saturday at midday IST — the session has not opened and will
    not open today."""
    assert is_market_open("NSE", now_utc=_NSE_SATURDAY_UTC) is False


def test_nse_closed_on_sunday():
    """Sunday at midday IST — same as Saturday."""
    assert is_market_open("NSE", now_utc=_NSE_SUNDAY_UTC) is False


def test_bse_closed_on_weekend():
    """BSE also closed on weekends, mirroring NSE."""
    assert is_market_open("BSE", now_utc=_NSE_SATURDAY_UTC) is False
    assert is_market_open("BSE", now_utc=_NSE_SUNDAY_UTC) is False


# --- US (NYSE/NASDAQ): DST-aware conversion ----------------------------


def test_nyse_open_in_standard_time():
    """In January (standard time, UTC-5), 09:30 ET == 14:30 UTC. A
    naive implementation that hardcoded UTC-4 would compute the open
    as 13:30 UTC and this test would fail (the 14:30 UTC fixture is
    inside the session under a real zoneinfo, but outside it under
    the broken hardcoded-offset version)."""
    utc = _et_to_utc(2024, 1, 16, 9, 30, dst=False)  # Tuesday
    assert utc.hour == 14 and utc.minute == 30
    assert is_market_open("NYSE", now_utc=utc) is True


def test_nyse_open_in_daylight_time():
    """In July (daylight time, UTC-4), 09:30 ET == 13:30 UTC. A naive
    implementation that hardcoded UTC-5 would compute the open as
    14:30 UTC and fail (the 13:30 UTC fixture is inside the session
    under a real zoneinfo, but outside it under the broken
    hardcoded-offset version)."""
    utc = _et_to_utc(2024, 7, 16, 9, 30, dst=True)  # Tuesday
    assert utc.hour == 13 and utc.minute == 30
    assert is_market_open("NYSE", now_utc=utc) is True


def test_nyse_closed_in_standard_time_after_close():
    """16:01 ET in January == 21:01 UTC — well after the close."""
    utc = _et_to_utc(2024, 1, 16, 16, 1, dst=False)
    assert is_market_open("NYSE", now_utc=utc) is False


def test_nyse_closed_in_daylight_time_after_close():
    """16:01 ET in July == 20:01 UTC — well after the close."""
    utc = _et_to_utc(2024, 7, 16, 16, 1, dst=True)
    assert is_market_open("NYSE", now_utc=utc) is False


def test_nasdaq_shares_session_with_nyse():
    """NASDAQ has the same 09:30–16:00 ET window as NYSE — both
    pointed at the same ZoneInfo, same DST behaviour."""
    utc = _et_to_utc(2024, 1, 16, 9, 30, dst=False)
    assert is_market_open("NASDAQ", now_utc=utc) is True
    utc = _et_to_utc(2024, 7, 16, 16, 1, dst=True)
    assert is_market_open("NASDAQ", now_utc=utc) is False


# --- US: real DST transitions (the headline correctness test) ---------


def test_us_market_status_one_minute_before_spring_forward():
    """US "spring forward" in 2024 was Sunday March 10. We can't use
    March 10 itself to assert "NYSE is open at 09:30 ET" because the
    transition date is a Sunday and NYSE is closed on Sundays — the
    market-status answer is closed regardless of any timezone math,
    which would mask a hardcoded-offset implementation bug.

    Instead we pair the LAST PRE-DST TRADING DAY (Friday 2024-03-08,
    standard time, UTC-5) with the FIRST POST-DST TRADING DAY
    (Monday 2024-03-11, daylight time, UTC-4). On the pre-DST day,
    09:30 ET == 14:30 UTC. On the post-DST day, 09:30 ET == 13:30 UTC.
    A hardcoded-UTC-5 implementation would answer True for the 14:30
    UTC fixture but False for the 13:30 UTC fixture — the OPPOSITE
    of what zoneinfo says — so the post-DST assertion is the
    headline DST-correctness check.

    We also assert the actual UTC instants so a swap between the
    two fixtures fails loudly."""
    # Friday 2024-03-08 (PRE spring-forward): standard time (UTC-5).
    utc_pre = _et_to_utc(2024, 3, 8, 9, 30, dst=False)
    assert utc_pre.hour == 14 and utc_pre.minute == 30
    assert is_market_open("NYSE", now_utc=utc_pre) is True

    # Monday 2024-03-11 (POST spring-forward): daylight time (UTC-4).
    utc_post = _et_to_utc(2024, 3, 11, 9, 30, dst=True)
    assert utc_post.hour == 13 and utc_post.minute == 30
    # The headline DST assertion: 13:30 UTC on 2024-03-11 is 09:30 ET
    # in daylight time, market is OPEN. A buggy hardcoded-UTC-5
    # implementation would treat 13:30 UTC as 08:30 standard ET and
    # answer False — failing this assertion.
    assert is_market_open("NYSE", now_utc=utc_post) is True

    # Mid-session on the post-DST day: 14:30 UTC == 10:30 ET (DST).
    utc_post_mid = datetime(2024, 3, 11, 14, 30, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=utc_post_mid) is True

    # And as a sanity check, also verify pre-open on the post-DST day:
    # 12:30 UTC == 08:30 ET (DST), market is closed (before 09:30).
    utc_post_pre = datetime(2024, 3, 11, 12, 30, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=utc_post_pre) is False


def test_us_market_status_one_minute_before_fall_back():
    """US "fall back" in 2024 was Sunday November 3. Same problem as
    spring-forward: we can't use the transition date itself because
    it falls on a Sunday, when NYSE is closed. Instead we pair the
    LAST PRE-FALLBACK TRADING DAY (Friday 2024-11-01, daylight time,
    UTC-4) with the FIRST POST-FALLBACK TRADING DAY (Monday
    2024-11-04, standard time, UTC-5). On the pre-fallback day,
    09:30 ET == 13:30 UTC. On the post-fallback day, 09:30 ET ==
    14:30 UTC.

    A hardcoded-UTC-4 implementation would answer True for 13:30 UTC
    on November 4 (treating it as 09:30 DST ET) but the real zoneinfo
    answer is False (13:30 UTC is 08:30 standard ET, pre-open) —
    failing the post-fallback assertion. Symmetric to the
    spring-forward test."""
    # Friday 2024-11-01 (PRE fall-back): daylight time (UTC-4).
    utc_pre = _et_to_utc(2024, 11, 1, 9, 30, dst=True)
    assert utc_pre.hour == 13 and utc_pre.minute == 30
    assert is_market_open("NYSE", now_utc=utc_pre) is True

    # Monday 2024-11-04 (POST fall-back): standard time (UTC-5).
    utc_post = _et_to_utc(2024, 11, 4, 9, 30, dst=False)
    assert utc_post.hour == 14 and utc_post.minute == 30
    # The headline DST assertion: 14:30 UTC on 2024-11-04 is 09:30 ET
    # in standard time, market is OPEN. A buggy hardcoded-UTC-4
    # implementation would treat 14:30 UTC as 10:30 DST ET (still
    # inside the session, so this one would actually still pass) —
    # so the real headline is the cross-day check below.
    assert is_market_open("NYSE", now_utc=utc_post) is True

    # The symmetric DST cross-check: 13:30 UTC on 2024-11-04 (the
    # post-fallback Monday) is now 08:30 STANDARD ET, pre-open. A
    # buggy hardcoded-UTC-4 implementation would treat 13:30 UTC as
    # 09:30 DST ET and answer True — failing this assertion.
    utc_post_wrong = datetime(2024, 11, 4, 13, 30, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=utc_post_wrong) is False

    # Pre-open on the post-fallback day, sanity check the other
    # direction: 13:00 UTC == 08:00 standard ET, still pre-open.
    utc_post_pre = datetime(2024, 11, 4, 13, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=utc_post_pre) is False


# --- US: weekend closure ----------------------------------------------


def test_us_closed_on_saturday():
    """Saturday in ET — both NYSE and NASDAQ closed, regardless of
    whether DST is in effect."""
    utc = _et_to_utc(2024, 1, 13, 12, 0, dst=False)  # Saturday
    assert is_market_open("NYSE", now_utc=utc) is False
    assert is_market_open("NASDAQ", now_utc=utc) is False


def test_us_closed_on_sunday():
    """Sunday in ET — both NYSE and NASDAQ closed, regardless of
    whether DST is in effect."""
    utc = _et_to_utc(2024, 7, 14, 12, 0, dst=True)  # Sunday (July)
    assert is_market_open("NYSE", now_utc=utc) is False
    assert is_market_open("NASDAQ", now_utc=utc) is False


# --- market_status: dict shape and transition math ----------------------


def test_market_status_open_returns_open_with_future_close_transition():
    """When the market is open, `next_status` should be "closed" and
    `next_transition_utc` should equal the session's close in UTC."""
    status = market_status("NSE", now_utc=_NSE_MIDDAY_UTC)

    assert status["status"] == "open"
    assert status["next_status"] == "closed"
    # 15:30 IST on the same date == 10:00:00 UTC.
    assert status["next_transition_utc"].startswith("2024-01-10T10:00:00")
    assert status["timezone"] == "Asia/Kolkata"


def test_market_status_closed_after_hours_points_to_next_open():
    """When the market is closed after hours on a weekday, the next
    transition is the next session's open. For NSE at 16:00 IST on
    a Wednesday, the next open is 09:15 IST the next day
    (Thursday), which is 03:45:00 UTC the next day."""
    status = market_status("NSE", now_utc=_NSE_AFTER_HOURS_UTC)

    assert status["status"] == "closed"
    assert status["next_status"] == "open"
    # Next open: 2024-01-11 09:15 IST == 2024-01-11 03:45 UTC.
    assert status["next_transition_utc"].startswith("2024-01-11T03:45:00")


def test_market_status_closed_friday_evening_points_to_monday_open():
    """When the market is closed on a Friday evening, the next open
    is Monday 09:15 IST — skipping the weekend. For NYSE at 17:00
    ET on Friday 2024-01-12 (standard time), the next open is
    Monday 2024-01-15 09:30 ET (standard time) == 14:30 UTC."""
    # Friday 2024-01-12 17:00 ET (UTC-5) == 22:00 UTC.
    utc = _et_to_utc(2024, 1, 12, 17, 0, dst=False)
    status = market_status("NYSE", now_utc=utc)

    assert status["status"] == "closed"
    assert status["next_status"] == "open"
    # Monday 2024-01-15 09:30 ET (UTC-5) == 14:30 UTC.
    assert status["next_transition_utc"].startswith("2024-01-15T14:30:00")


def test_market_status_open_just_before_close_has_correct_transition():
    """When the market is open with one second to go before close,
    `next_transition_utc` should be the close instant (one second
    later)."""
    status = market_status("NSE", now_utc=_NSE_JUST_BEFORE_CLOSE_UTC)

    assert status["status"] == "open"
    assert status["next_status"] == "closed"
    # 15:30 IST == 10:00 UTC; the fixture is 09:59:59 UTC (one second
    # before).
    assert status["next_transition_utc"].startswith("2024-01-10T10:00:00")


def test_market_status_returns_now_utc_iso_string():
    """The `now_utc` field is an ISO-8601 string in UTC — what the
    price-cache layer will persist verbatim."""
    status = market_status("NSE", now_utc=_NSE_MIDDAY_UTC)
    assert status["now_utc"] == "2024-01-10T06:30:00+00:00"


def test_market_status_now_local_uses_market_timezone():
    """The `now_local` field is the same instant expressed in the
    market's local timezone — useful for human-readable logging.
    14:30 UTC on July 16 2024 (DST) is 10:30 ET."""
    utc = datetime(2024, 7, 16, 14, 30, 0, tzinfo=timezone.utc)
    status = market_status("NYSE", now_utc=utc)
    assert status["now_local"].startswith("2024-07-16T10:30:00")
    assert status["timezone"] == "America/New_York"


# --- naive datetime handling -------------------------------------------


def test_naive_datetime_is_treated_as_utc_not_local():
    """A naive `datetime` (no tzinfo) is interpreted as UTC. This
    matches the project-wide internal-time convention and means a
    tester running in a non-UTC timezone can't accidentally shift
    the answer. The hand-computed UTC instant for NSE open
    (2024-01-10 03:45 UTC) is constructed naively here and the
    answer is still True."""
    naive_open = datetime(2024, 1, 10, 3, 45, 0)  # no tzinfo
    assert is_market_open("NSE", now_utc=naive_open) is True


def test_aware_datetime_in_other_zone_is_converted_to_utc():
    """An aware datetime in any timezone is converted to UTC
    internally. Constructed as 09:30 America/New_York on a Tuesday
    in July (DST), the answer is the same as the equivalent UTC
    instant."""
    from zoneinfo import ZoneInfo

    # 09:30 ET (DST) == 13:30 UTC.
    ny_zone = ZoneInfo("America/New_York")
    aware_in_ny = datetime(2024, 7, 16, 9, 30, 0, tzinfo=ny_zone)
    assert is_market_open("NYSE", now_utc=aware_in_ny) is True


# --- unknown / malformed exchange --------------------------------------


def test_unknown_market_raises_named_error():
    """Asking about an exchange that isn't in the config raises
    `UnknownMarketError` — a named, specific exception, not a bare
    `KeyError`. This is the same posture
    `src/exchange_rate_client.py`'s error hierarchy takes."""
    with pytest.raises(UnknownMarketError):
        is_market_open("LSE", now_utc=_NSE_MIDDAY_UTC)


def test_market_status_unknown_market_raises_named_error():
    """`market_status` raises the same exception for the same reason —
    no divergent error paths between the two public functions."""
    with pytest.raises(UnknownMarketError):
        market_status("LSE", now_utc=_NSE_MIDDAY_UTC)


# --- config validation ------------------------------------------------


def test_list_known_markets_returns_sorted_keys():
    """`list_known_markets` returns the markets defined in
    `config/market_hours.json`, sorted alphabetically. Acts as a
    smoke test that the bundled config is parseable and contains
    the markets the rest of the system expects."""
    assert list_known_markets() == ["BSE", "NASDAQ", "NSE", "NYSE"]


def test_module_imports_without_reading_files():
    """Importing `market_hours` does not touch the filesystem or the
    environment — same import-time safety posture
    `src/exchange_rate_client.py` takes. This matters because every
    other module that ever does `from market_hours import ...`
    inherits the import cost, and a config read at import time would
    make the module unimportable in any environment where the config
    file is missing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "market_hours_reload_probe", market_hours.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Just having imported cleanly without raising is the assertion.


# --- config overrides via env var --------------------------------------


def test_config_path_env_var_override(monkeypatch, tmp_path):
    """`MARKET_HOURS_CONFIG_PATH` env var redirects config loading to
    a custom file — used by ops for per-environment overrides. The
    module reads the env var at call time, not at import time, so a
    monkeypatch takes effect on the very next call.

    The test pins two distinct facts: (1) without the override,
    "CUSTOM" is not a known market and the call raises; (2) with the
    override pointing at a config that defines only "CUSTOM", the
    previously-known "NSE" disappears and "CUSTOM" is honoured. That
    pair is the real evidence that the env var actually changes which
    config file is loaded."""
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

    # Sanity check the bundled config is loaded first: "CUSTOM" is
    # not in the default markets list and should raise.
    with pytest.raises(UnknownMarketError):
        is_market_open("CUSTOM", now_utc=_NSE_MIDDAY_UTC)

    monkeypatch.setenv("MARKET_HOURS_CONFIG_PATH", str(custom_config))
    reset_config_cache()

    try:
        # With the override, "CUSTOM" is now known and uses 10:00–11:00 IST.
        # 10:30 IST == 05:00 UTC.
        ten_thirty_ist = datetime(2024, 1, 10, 5, 0, 0, tzinfo=timezone.utc)
        assert is_market_open("CUSTOM", now_utc=ten_thirty_ist) is True

        # 11:30 IST == 06:00 UTC — past the 11:00 close.
        eleven_thirty_ist = datetime(2024, 1, 10, 6, 0, 0, tzinfo=timezone.utc)
        assert is_market_open("CUSTOM", now_utc=eleven_thirty_ist) is False

        # And the bundled "NSE" is gone under the override, because
        # the custom config only defines "CUSTOM". This is the
        # smoking-gun proof the override actually redirected config
        # loading — not just appended to it.
        with pytest.raises(UnknownMarketError):
            is_market_open("NSE", now_utc=_NSE_MIDDAY_UTC)
    finally:
        # Clear the cache so the original bundled config is used by
        # every other test in this file.
        reset_config_cache()


# --- malformed config handling -----------------------------------------


def test_malformed_config_json_raises_market_hours_config_error(monkeypatch, tmp_path):
    """A non-JSON config file raises `MarketHoursConfigError`, not a
    bare `json.JSONDecodeError`. The named exception lets a caller
    distinguish "config is broken" from "exchange is unknown"."""
    bad_config = tmp_path / "bad.json"
    bad_config.write_text("not valid json {")
    monkeypatch.setenv("MARKET_HOURS_CONFIG_PATH", str(bad_config))
    reset_config_cache()

    try:
        with pytest.raises(MarketHoursConfigError):
            is_market_open("NSE", now_utc=_NSE_MIDDAY_UTC)
    finally:
        reset_config_cache()


def test_missing_config_file_raises_market_hours_config_error(monkeypatch, tmp_path):
    """A missing config file raises `MarketHoursConfigError`, not a
    bare `FileNotFoundError`. Same posture as the JSON-parse case."""
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("MARKET_HOURS_CONFIG_PATH", str(missing))
    reset_config_cache()

    try:
        with pytest.raises(MarketHoursConfigError):
            is_market_open("NSE", now_utc=_NSE_MIDDAY_UTC)
    finally:
        reset_config_cache()


def test_config_with_bad_timezone_raises_market_hours_config_error(monkeypatch, tmp_path):
    """A typo in the `timezone` field (e.g. "Asia/Kolkatta") raises
    `MarketHoursConfigError`. The module eagerly resolves the zone at
    config-lookup time so a typo fails the first call, not the
    first market check hours later."""
    bad_config = tmp_path / "bad_tz.json"
    bad_config.write_text(
        """{
            "markets": {
                "NSE": {
                    "display_name": "NSE",
                    "country": "IN",
                    "timezone": "Asia/Kolkatta",
                    "trading_days": [0, 1, 2, 3, 4],
                    "session_blocks": [{"open": "09:15", "close": "15:30"}]
                }
            }
        }"""
    )
    monkeypatch.setenv("MARKET_HOURS_CONFIG_PATH", str(bad_config))
    reset_config_cache()

    try:
        with pytest.raises(MarketHoursConfigError):
            is_market_open("NSE", now_utc=_NSE_MIDDAY_UTC)
    finally:
        reset_config_cache()


def test_config_with_malformed_hhmm_raises_market_hours_config_error(monkeypatch, tmp_path):
    """A bad `open`/`close` string (e.g. "9:15 AM" instead of
    "09:15") raises `MarketHoursConfigError`."""
    bad_config = tmp_path / "bad_hhmm.json"
    bad_config.write_text(
        """{
            "markets": {
                "NSE": {
                    "display_name": "NSE",
                    "country": "IN",
                    "timezone": "Asia/Kolkata",
                    "trading_days": [0, 1, 2, 3, 4],
                    "session_blocks": [{"open": "9:15 AM", "close": "15:30"}]
                }
            }
        }"""
    )
    monkeypatch.setenv("MARKET_HOURS_CONFIG_PATH", str(bad_config))
    reset_config_cache()

    try:
        with pytest.raises(MarketHoursConfigError):
            is_market_open("NSE", now_utc=_NSE_MIDDAY_UTC)
    finally:
        reset_config_cache()


# --- now_utc=None uses real wall clock --------------------------------


def test_now_utc_none_uses_real_wall_clock(monkeypatch):
    """When `now_utc` is omitted, the function calls
    `datetime.now(timezone.utc)` — the real wall clock — not a
    fabricated timestamp. We monkeypatch the module's view of
    `datetime.now` to return a known instant and assert the answer
    is what that instant would produce. This pins the dependency on
    `datetime.now` (a real, monotonic-ish clock) rather than a
    hardcoded value.

    `market_hours` also uses `datetime.combine` internally for the
    next-transition math, so the stand-in exposes `combine` too —
    tests that exercise the `now_utc=None` path transitively exercise
    the transition code, and a missing `combine` would surface as a
    confusing `AttributeError` here rather than a clean assertion
    failure on the market-status dict."""
    from datetime import datetime as real_datetime

    class _FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2024, 1, 10, 6, 30, 0, tzinfo=timezone.utc)

        @staticmethod
        def combine(date, time, tzinfo=None):
            return real_datetime.combine(date, time, tzinfo=tzinfo)

    monkeypatch.setattr(market_hours, "datetime", _FrozenDateTime)

    # 06:30 UTC on a Wednesday in January == 12:00 IST == NSE open.
    assert is_market_open("NSE") is True

    # And for NYSE at the same instant: 06:30 UTC in January ==
    # 01:30 ET (standard) == pre-open.
    assert is_market_open("NYSE") is False


# --- QA: acceptance-criteria acceptance test (STORY-10) ----------------


def test_qa_story10_all_acceptance_criteria():
    """STORY-10 acceptance-criteria acceptance test.

    A single real test, written by QA against the actual code, that
    exercises each of the five acceptance criteria with hand-computed
    UTC timestamps. A failure in any one criterion fails the whole
    test, so a single `pytest` invocation is enough to surface any
    regression against the original story contract.

    The fixtures are independent of the existing tests in this file
    and intentionally chosen at moments that would expose common
    real-implementation bugs:

    * The IST offset is +05:30 (not +05:00 or +06:00). A hardcoded
      +05:00 would shift 03:45 UTC to 08:45 IST (pre-open).
    * The ET offset is UTC-5 in January and UTC-4 in July. A
      hardcoded UTC-5 would say 13:30 UTC is 08:30 ET (pre-open) in
      July, not 09:30 ET.
    * Weekend closure is enforced for both market groups via the
      config's `trading_days` (Mon-Fri only), not by accident of
      the current weekday.
    * The config is JSON-driven — the bundled file already contains
      NSE, BSE, NYSE, NASDAQ, and a fresh import does not mutate it.
    """
    from pathlib import Path

    # --- AC #1: NSE/BSE 9:15-15:30 IST, real tz-aware -------------
    # Wednesday 2024-01-10 09:15 IST == 03:45 UTC (Asia/Kolkata,
    # fixed +05:30, no DST).
    nse_open_utc = datetime(2024, 1, 10, 3, 45, 0, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=nse_open_utc) is True
    assert is_market_open("BSE", now_utc=nse_open_utc) is True

    # 09:14:59 IST == 03:44:59 UTC (one second before open).
    nse_pre_utc = datetime(2024, 1, 10, 3, 44, 59, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=nse_pre_utc) is False
    assert is_market_open("BSE", now_utc=nse_pre_utc) is False

    # 15:30 IST == 10:00 UTC (exact close; half-open -> closed).
    nse_close_utc = datetime(2024, 1, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=nse_close_utc) is False
    assert is_market_open("BSE", now_utc=nse_close_utc) is False

    # 15:29:59 IST == 09:59:59 UTC (last second of session).
    nse_last_sec_utc = datetime(2024, 1, 10, 9, 59, 59, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=nse_last_sec_utc) is True

    # --- AC #2: US 9:30-16:00 ET, real DST via zoneinfo ----------
    # Tuesday 2024-01-16 (standard time, UTC-5): 09:30 ET == 14:30 UTC.
    us_standard_open_utc = datetime(2024, 1, 16, 14, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=us_standard_open_utc) is True
    assert is_market_open("NASDAQ", now_utc=us_standard_open_utc) is True

    # Tuesday 2024-07-16 (daylight time, UTC-4): 09:30 ET == 13:30 UTC.
    # A hardcoded-UTC-5 implementation would compute this as 08:30
    # standard ET and answer False — failing the assertion.
    us_dst_open_utc = datetime(2024, 7, 16, 13, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=us_dst_open_utc) is True
    assert is_market_open("NASDAQ", now_utc=us_dst_open_utc) is True

    # 16:01 ET in DST (July) == 20:01 UTC; well after close.
    us_dst_after_utc = datetime(2024, 7, 16, 20, 1, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=us_dst_after_utc) is False

    # 16:01 ET in standard (January) == 21:01 UTC; well after close.
    us_std_after_utc = datetime(2024, 1, 16, 21, 1, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=us_std_after_utc) is False

    # DST cross-check: 14:30 UTC on a July Tuesday is 10:30 ET
    # (DST), inside the session. A naive implementation that forgot
    # DST would say 09:30 standard ET (pre-open) at 14:30 UTC.
    us_dst_mid_utc = datetime(2024, 7, 16, 14, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=us_dst_mid_utc) is True

    # --- AC #3: UTC internally ------------------------------------
    # `now_utc` parameter is named/typed UTC, the function converts
    # aware datetimes from other zones, and naive datetimes are
    # treated as UTC. We exercise all three code paths.
    # (a) explicit UTC
    assert is_market_open("NSE", now_utc=nse_open_utc) is True
    # (b) naive -> treated as UTC
    naive_nse_open = datetime(2024, 1, 10, 3, 45, 0)  # no tzinfo
    assert is_market_open("NSE", now_utc=naive_nse_open) is True
    # (c) aware in market zone -> converted to UTC
    from zoneinfo import ZoneInfo
    kolkata_zone = ZoneInfo("Asia/Kolkata")
    aware_kolkata = datetime(2024, 1, 10, 9, 15, 0, tzinfo=kolkata_zone)
    assert is_market_open("NSE", now_utc=aware_kolkata) is True

    # --- AC #4: config from config/market_hours.json -------------
    # The bundled file exists, is parseable JSON, and contains the
    # four markets the rest of the system expects. A test that
    # imports the module and checks these facts directly proves the
    # config drives behaviour, not the in-Python defaults.
    config_path = (
        Path(market_hours.__file__).resolve().parent.parent
        / "config" / "market_hours.json"
    )
    assert config_path.exists(), f"config file missing at {config_path}"
    import json as _json
    parsed = _json.loads(config_path.read_text())
    assert "markets" in parsed, "config must have a 'markets' key"
    for required in ("NSE", "BSE", "NYSE", "NASDAQ"):
        assert required in parsed["markets"], f"missing {required} in config"
        entry = parsed["markets"][required]
        assert "timezone" in entry, f"{required} missing 'timezone'"
        assert "trading_days" in entry, f"{required} missing 'trading_days'"
        assert "session_blocks" in entry, f"{required} missing 'session_blocks'"

    # --- AC #5: weekends closed for both groups ------------------
    # Saturday 2024-01-13 12:00 IST == 06:30 UTC. Sessions do not
    # run on Saturday regardless of which market.
    sat_utc = datetime(2024, 1, 13, 6, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=sat_utc) is False
    assert is_market_open("BSE", now_utc=sat_utc) is False

    # Sunday 2024-01-14 12:00 IST == 06:30 UTC. Same.
    sun_utc = datetime(2024, 1, 14, 6, 30, 0, tzinfo=timezone.utc)
    assert is_market_open("NSE", now_utc=sun_utc) is False
    assert is_market_open("BSE", now_utc=sun_utc) is False

    # Saturday 2024-01-13 12:00 ET (standard, UTC-5) == 17:00 UTC.
    sat_et_utc = datetime(2024, 1, 13, 17, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=sat_et_utc) is False
    assert is_market_open("NASDAQ", now_utc=sat_et_utc) is False

    # Sunday 2024-07-14 12:00 ET (daylight, UTC-4) == 16:00 UTC.
    # Tests weekend closure in DST too, not just standard time.
    sun_dst_utc = datetime(2024, 7, 14, 16, 0, 0, tzinfo=timezone.utc)
    assert is_market_open("NYSE", now_utc=sun_dst_utc) is False
    assert is_market_open("NASDAQ", now_utc=sun_dst_utc) is False