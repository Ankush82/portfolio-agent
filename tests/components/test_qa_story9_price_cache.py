"""QA verification tests for STORY-9: price data caching strategy
implemented in src/price_cache.py.

These tests target STORY-9's own acceptance criteria with
independent assertions -- they are not a re-run of the existing
tests/test_price_cache.py suite. Each test below exercises exactly
one acceptance criterion of this story:

  AC1: Real-time prices are cached for 1 minute during market hours
  AC2: Real-time prices are cached for 1 hour outside market hours
  AC3: Historical data is cached for 24 hours
  AC4: Company metadata is cached for 7 days
  AC5: Cache is invalidated when market transitions from closed to open
  AC6: Cache includes market-specific invalidation (NSE/BSE separate from US)
  AC7: Manual refresh mechanism bypasses cache

The infrastructure story says "do NOT try to literally test or claim
'cache hit ratio exceeds 80%' as an automated unit-test assertion".
This file therefore tests the *behaviour* the metric measures
(cache hits happen when the TTL window is respected) via a
counter-incrementing fetch_fn -- a real, repeatable assertion.
The 80% figure remains a production monitoring target, documented
in a comment but never asserted here.

The fetch_fn is a real injected callable (dependency injection) so
no network access is required; all assertions run against a real
in-memory fake of `infrastructure.cache_get` / `cache_set`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Make `src/` importable when this file is run directly via pytest
# from the repo root. Same pattern other QA-story tests use; harmless
# when pytest already has src/ on its pythonpath via pytest.ini.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from infrastructure import Infrastructure  # noqa: E402

import price_cache  # noqa: E402
from price_cache import (  # noqa: E402
    COMPANY_METADATA,
    HISTORICAL,
    REALTIME_PRICE,
    TTL_COMPANY_METADATA_SECONDS,
    TTL_HISTORICAL_SECONDS,
    TTL_REALTIME_CLOSED_SECONDS,
    TTL_REALTIME_OPEN_SECONDS,
    get_cached_price,
    refresh_cached_price,
)


class _CountingFetch:
    """A real, dependency-injected callable whose `call_count` we can
    assert against. Replaces a vendor client so the test is hermetic
    and proves the cache layer is making the read/hit decision
    itself -- not the vendor."""
    def __init__(self, value: Any = {"price": 42}) -> None:
        self.call_count = 0
        self.value = value

    def __call__(self) -> Any:
        self.call_count += 1
        return self.value


class _FakeInfrastructure(Infrastructure):
    """A real `Infrastructure` subclass whose `cache_get`/`cache_set`
    are backed by an in-memory dict. The cache layer doesn't know
    or care that the backing store isn't Redis -- it sees the same
    public API. TTL is recorded per-write so a test can assert the
    exact TTL chosen without re-deriving it from the rule of thumb."""
    def __init__(self) -> None:
        self.store: dict[str, tuple[Any, int]] = {}

    def cache_get(self, key: str) -> Any | None:
        entry = self.store.get(key)
        if entry is None:
            return None
        return entry[0]

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.store[key] = (value, ttl_seconds)


@pytest.fixture
def infra() -> _FakeInfrastructure:
    """Fresh in-memory cache for each test so writes from one test
    never leak into the next."""
    return _FakeInfrastructure()


# ---------------------------------------------------------------------------
# AC1: Real-time prices are cached for 1 minute (60s) during market hours.
# ---------------------------------------------------------------------------
def test_ac1_realtime_price_ttl_is_60_seconds_when_market_open(infra, monkeypatch):
    """When `is_market_open(exchange)` returns True, a real-time
    price write must be cached for exactly 60 seconds."""
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: True)

    fetch = _CountingFetch(value={"price": 100.0})
    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # Cache was populated.
    assert "price:NASDAQ:AAPL" in infra.store
    # TTL on write is exactly 60 seconds.
    _value, ttl = infra.store["price:NASDAQ:AAPL"]
    assert ttl == TTL_REALTIME_OPEN_SECONDS == 60, (
        f"Expected TTL of 60s for real-time prices while market open, got {ttl}"
    )
    # And the cross-check: TTL is NOT the closed-market TTL.
    assert ttl != TTL_REALTIME_CLOSED_SECONDS


# ---------------------------------------------------------------------------
# AC2: Real-time prices are cached for 1 hour (3600s) outside market hours.
# ---------------------------------------------------------------------------
def test_ac2_realtime_price_ttl_is_3600_seconds_when_market_closed(
    infra, monkeypatch
):
    """When `is_market_open(exchange)` returns False, a real-time
    price write must be cached for exactly 3600 seconds."""
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: False)

    fetch = _CountingFetch(value={"price": 99.5})
    get_cached_price(
        symbol="RELIANCE",
        exchange="NSE",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    assert "price:NSE:RELIANCE" in infra.store
    _value, ttl = infra.store["price:NSE:RELIANCE"]
    assert ttl == TTL_REALTIME_CLOSED_SECONDS == 3600, (
        f"Expected TTL of 3600s for real-time prices outside market hours, got {ttl}"
    )
    # And the cross-check: TTL is NOT the open-market TTL.
    assert ttl != TTL_REALTIME_OPEN_SECONDS


# ---------------------------------------------------------------------------
# AC3: Historical data is cached for 24 hours (86400s).
# ---------------------------------------------------------------------------
def test_ac3_historical_data_ttl_is_86400_seconds_regardless_of_market_state(
    infra, monkeypatch
):
    """Historical-data TTL is 86400s whether the market is open or
    closed -- the data is by definition not live, so market state
    is irrelevant to its TTL."""
    # Market closed case.
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: False)
    fetch = _CountingFetch(value={"bars": []})
    get_cached_price(
        symbol="TCS",
        exchange="NSE",
        fetch_fn=fetch,
        data_type=HISTORICAL,
        infrastructure=infra,
    )
    _value, ttl = infra.store["price:NSE:TCS"]
    assert ttl == TTL_HISTORICAL_SECONDS == 86400, (
        f"Expected TTL of 86400s for historical data while market closed, got {ttl}"
    )

    # Market-open case: same TTL.
    infra2 = _FakeInfrastructure()
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: True)
    fetch2 = _CountingFetch(value={"bars": []})
    get_cached_price(
        symbol="TCS",
        exchange="NSE",
        fetch_fn=fetch2,
        data_type=HISTORICAL,
        infrastructure=infra2,
    )
    _value2, ttl2 = infra2.store["price:NSE:TCS"]
    assert ttl2 == TTL_HISTORICAL_SECONDS == 86400, (
        f"Expected TTL of 86400s for historical data while market open, got {ttl2}"
    )


# ---------------------------------------------------------------------------
# AC4: Company metadata is cached for 7 days (604800s).
# ---------------------------------------------------------------------------
def test_ac4_company_metadata_ttl_is_604800_seconds(infra, monkeypatch):
    """Company metadata is cached for 7 days (604800 seconds), and
    the choice is independent of market-open state."""
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: False)
    fetch = _CountingFetch(value={"name": "Apple Inc."})
    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=COMPANY_METADATA,
        infrastructure=infra,
    )

    _value, ttl = infra.store["price:NASDAQ:AAPL"]
    assert ttl == TTL_COMPANY_METADATA_SECONDS == 604800, (
        f"Expected TTL of 604800s for company metadata, got {ttl}"
    )
    # And not any of the smaller TTLs.
    assert ttl != TTL_REALTIME_OPEN_SECONDS
    assert ttl != TTL_REALTIME_CLOSED_SECONDS
    assert ttl != TTL_HISTORICAL_SECONDS


# ---------------------------------------------------------------------------
# AC5: Cache is invalidated when market transitions from closed to open.
# ---------------------------------------------------------------------------
def test_ac5_closed_to_open_transition_treats_cache_as_miss(infra, monkeypatch):
    """The cached payload stores `market_open_at_cache_time`. If
    that was False (closed) and `is_market_open` now returns True,
    the cache entry must be treated as a miss and the fetch_fn
    called again, even within the TTL window."""
    # First call: cache while market is CLOSED.
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: False)
    fetch = _CountingFetch(value={"price": 50.0})
    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert fetch.call_count == 1
    cached_payload, _ = infra.store["price:NASDAQ:AAPL"]
    assert cached_payload["market_open_at_cache_time"] is False, (
        "Stored payload should remember the market was closed at fetch time"
    )

    # Now market opens. The cache key still has an entry, but the
    # closed-to-open transition must cause a refetch.
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: True)
    fetch2 = _CountingFetch(value={"price": 55.0})
    result = get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch2,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert fetch2.call_count == 1, (
        "Closed->open transition must trigger a refetch; "
        f"fetch_fn was called {fetch2.call_count} times"
    )
    assert result == {"price": 55.0}

    # The stored payload should now reflect the open-market state.
    cached_payload2, _ = infra.store["price:NASDAQ:AAPL"]
    assert cached_payload2["market_open_at_cache_time"] is True


# ---------------------------------------------------------------------------
# AC6: Market-specific invalidation (NSE/BSE separate from US).
# ---------------------------------------------------------------------------
def test_ac6_nse_and_nasdaq_caches_are_separate_for_same_symbol(
    infra, monkeypatch
):
    """The cache key is namespaced by exchange. A write for `AAPL`
    on NASDAQ must not be served by a read for `AAPL` on NSE -- they
    are entirely separate cache entries."""
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: True)

    nasdaq_fetch = _CountingFetch(value={"price": 100.0, "venue": "NASDAQ"})
    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=nasdaq_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # Now ask for the SAME ticker on NSE. The cache must not serve
    # the NASDAQ entry -- it must call fetch again for NSE.
    nse_fetch = _CountingFetch(value={"price": 200.0, "venue": "NSE"})
    result_nse = get_cached_price(
        symbol="AAPL",
        exchange="NSE",
        fetch_fn=nse_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    assert nasdaq_fetch.call_count == 1
    assert nse_fetch.call_count == 1, (
        "NSE lookup for AAPL must not be served from the NASDAQ cache entry; "
        f"fetch_fn called {nse_fetch.call_count} times"
    )
    assert result_nse == {"price": 200.0, "venue": "NSE"}

    # Both keys exist independently with their own values.
    assert "price:NASDAQ:AAPL" in infra.store
    assert "price:NSE:AAPL" in infra.store
    nasdaq_entry, _ = infra.store["price:NASDAQ:AAPL"]
    nse_entry, _ = infra.store["price:NSE:AAPL"]
    assert nasdaq_entry["value"]["venue"] == "NASDAQ"
    assert nse_entry["value"]["venue"] == "NSE"

    # And a repeat read on each venue IS a cache hit (proves they
    # are genuinely separate, not that one was just overwritten).
    repeat_nasdaq = _CountingFetch()
    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=repeat_nasdaq,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    repeat_nse = _CountingFetch()
    get_cached_price(
        symbol="AAPL",
        exchange="NSE",
        fetch_fn=repeat_nse,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert repeat_nasdaq.call_count == 0, "NASDAQ AAPL should hit its own cache"
    assert repeat_nse.call_count == 0, "NSE AAPL should hit its own cache"


# ---------------------------------------------------------------------------
# AC7: Manual refresh mechanism bypasses cache lookup but writes back.
# ---------------------------------------------------------------------------
def test_ac7_refresh_bypasses_lookup_and_writes_back(infra, monkeypatch):
    """`refresh_cached_price` must call fetch_fn unconditionally
    (bypassing the cache lookup) but still write the fresh value
    back so the NEXT read is a cache hit."""
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: True)

    # Prime the cache with a stale value.
    infra.store["price:NASDAQ:GOOG"] = (
        {"value": {"price": 1, "note": "stale"}, "market_open_at_cache_time": True},
        60,
    )

    refresh_fetch = _CountingFetch(value={"price": 2, "note": "refreshed"})
    refreshed = refresh_cached_price(
        symbol="GOOG",
        exchange="NASDAQ",
        fetch_fn=refresh_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # Bypass: refresh always called fetch_fn, even though a cache
    # entry already existed.
    assert refresh_fetch.call_count == 1, (
        "Manual refresh must always call fetch_fn, bypassing cache lookup; "
        f"called {refresh_fetch.call_count} times"
    )
    assert refreshed == {"price": 2, "note": "refreshed"}

    # Write-back: the cache now holds the fresh value.
    stored, _ = infra.store["price:NASDAQ:GOOG"]
    assert stored["value"] == {"price": 2, "note": "refreshed"}

    # Subsequent read within the TTL window IS a cache hit.
    read_fetch = _CountingFetch()
    again = get_cached_price(
        symbol="GOOG",
        exchange="NASDAQ",
        fetch_fn=read_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert read_fetch.call_count == 0, (
        "After a manual refresh, the next read must be a cache hit; "
        f"fetch_fn called {read_fetch.call_count} times"
    )
    assert again == {"price": 2, "note": "refreshed"}


# ---------------------------------------------------------------------------
# Behaviour-the-80%-metric-measures: cache hits happen within a TTL window.
#
# The "cache hit ratio exceeds 80%" figure is a production monitoring
# target, NOT a unit-testable assertion (a test that wires a 100%
# hit-yielding fake would prove nothing about production traffic).
# What IS unit-testable is the *behaviour* the metric measures:
# within a TTL window, repeated reads must hit the cache, not call
# fetch_fn again. This test asserts that real behaviour.
# ---------------------------------------------------------------------------
def test_repeated_reads_within_ttl_window_call_fetch_fn_once(
    infra, monkeypatch
):
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: True)
    fetch = _CountingFetch(value={"price": 42})
    for _ in range(10):
        get_cached_price(
            symbol="AAPL",
            exchange="NASDAQ",
            fetch_fn=fetch,
            data_type=REALTIME_PRICE,
            infrastructure=infra,
        )
    assert fetch.call_count == 1, (
        "Repeated reads within the TTL window must all be cache hits; "
        f"fetch_fn was called {fetch.call_count} times across 10 reads"
    )
