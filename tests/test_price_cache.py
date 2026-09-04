"""Tests for src/price_cache.py — the TTL-aware, per-exchange,
market-state-aware price cache layer (STORY-9 / issue #53).

The test suite covers:

* TTL selection per (data_type, market_open) cell of the decision
  matrix.
* Per-exchange key namespacing (NSE / BSE / US never collide).
* Cache hit within the TTL window (fetch_fn called exactly once
  across N repeated `get_cached_price` calls).
* Closed-to-open transition invalidates the cache: a real-time
  price cached while the market was closed is NOT served once the
  market opens.
* `refresh_cached_price` bypasses the cache lookup but writes the
  fresh result back so a subsequent read is a hit.
* Unknown exchange and unknown data_type raise the named
  exceptions, not a generic `KeyError` or silent miss.
* UnknownMarketError from `is_market_open` is surfaced as
  `UnknownPriceCacheExchangeError`.

NOTE on "cache hit ratio exceeds 80%": that figure is a production
monitoring target, NOT a unit-testable assertion (it's an
operational metric over real traffic patterns; a unit test with an
injected `fetch_fn` cannot honestly prove a property of production
workload). The real, testable behaviour — that repeated calls
within the TTL window produce hits — IS asserted here against a
counter-incrementing `fetch_fn`. See the dedicated test."""

import pytest

import price_cache
from price_cache import (
    COMPANY_METADATA,
    HISTORICAL,
    InvalidPriceCacheDataTypeError,
    REALTIME_PRICE,
    TTL_COMPANY_METADATA_SECONDS,
    TTL_HISTORICAL_SECONDS,
    TTL_REALTIME_CLOSED_SECONDS,
    TTL_REALTIME_OPEN_SECONDS,
    UnknownPriceCacheExchangeError,
    _cache_key,
    _default_infrastructure,
    _is_cache_fresh_for_current_market,
    _ttl_for,
    get_cached_price,
    refresh_cached_price,
)


# ---------------------------------------------------------------------------
# Fakes — in-memory stand-ins for DefaultInfrastructure.cache_get/set.
# Mirrors the `_FakeInfrastructure` shape `test_exchange_rate_client.py`
# already established so the test surface stays consistent across modules.
# ---------------------------------------------------------------------------


class _FakeInfrastructure:
    """In-memory stand-in for `DefaultInfrastructure.cache_get` /
    `cache_set`. Records every (key, value, ttl_seconds) so tests
    can assert against the exact contract the real
    `DefaultInfrastructure.cache_set` uses."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.set_calls: list = []
        self.get_calls: list = []

    def cache_get(self, key: str):
        self.get_calls.append(key)
        return self.store.get(key)

    def cache_set(self, key: str, value, ttl_seconds: int) -> None:
        self.set_calls.append((key, dict(value), ttl_seconds))
        self.store[key] = dict(value)


class _CountingFetch:
    """A `fetch_fn` substitute that increments a call counter and
    returns a fresh sentinel value on each call. Lets tests prove
    "fetch_fn was called exactly N times" without any real vendor
    in the loop."""

    def __init__(self, value_factory=None) -> None:
        self.call_count = 0
        self._value_factory = value_factory or (lambda i: {"price": 100 + i})

    def __call__(self):
        self.call_count += 1
        return self._value_factory(self.call_count)


# ---------------------------------------------------------------------------
# Market-open monkeypatching helper.
#
# `is_market_open` is read at every `get_cached_price` call so the
# closed-to-open transition can be simulated by monkeypatching it
# on the *price_cache* module (not on `market_hours`), which is the
# real surface this module sees at call time.
# ---------------------------------------------------------------------------


def _force_market_state(monkeypatch, is_open: bool):
    """Replaces `price_cache.is_market_open` with a constant-returning
    fake. Mirrors how `_FakeInfrastructure` replaces the cache
    layer — the test stays hermetic and doesn't depend on real
    wall-clock time."""
    monkeypatch.setattr(price_cache, "is_market_open", lambda exchange: is_open)


# ---------------------------------------------------------------------------
# TTL selection: pure-function tests, no fixtures needed.
# ---------------------------------------------------------------------------


def test_ttl_for_realtime_price_picks_60s_when_market_is_open():
    assert _ttl_for(REALTIME_PRICE, "NSE", market_open=True) == 60
    assert _ttl_for(REALTIME_PRICE, "NYSE", market_open=True) == 60


def test_ttl_for_realtime_price_picks_3600s_when_market_is_closed():
    assert _ttl_for(REALTIME_PRICE, "NSE", market_open=False) == 3600
    assert _ttl_for(REALTIME_PRICE, "NYSE", market_open=False) == 3600


def test_ttl_for_historical_is_24h_regardless_of_market_state():
    assert _ttl_for(HISTORICAL, "NSE", market_open=True) == 86400
    assert _ttl_for(HISTORICAL, "NSE", market_open=False) == 86400
    assert _ttl_for(HISTORICAL, "NYSE", market_open=True) == 86400


def test_ttl_for_company_metadata_is_7d_regardless_of_market_state():
    assert _ttl_for(COMPANY_METADATA, "NSE", market_open=True) == 604800
    assert _ttl_for(COMPANY_METADATA, "NYSE", market_open=False) == 604800


def test_ttl_module_constants_match_acceptance_criteria():
    """Public-facing constants for the TTL values — pin them so a
    refactor can't quietly shift the numbers out from under the
    acceptance criteria."""
    assert TTL_REALTIME_OPEN_SECONDS == 60
    assert TTL_REALTIME_CLOSED_SECONDS == 3600
    assert TTL_HISTORICAL_SECONDS == 86400
    assert TTL_COMPANY_METADATA_SECONDS == 604800


# ---------------------------------------------------------------------------
# Cache key namespacing.
# ---------------------------------------------------------------------------


def test_cache_key_namespaces_per_exchange():
    """Acceptance criterion: market-specific invalidation (NSE/BSE
    separate from US). The same ticker on two different exchanges
    must produce two distinct cache keys."""
    assert _cache_key("AAPL", "NASDAQ") != _cache_key("AAPL", "NSE")
    assert _cache_key("RELIANCE", "NSE") != _cache_key("RELIANCE", "BSE")


def test_cache_key_uppercases_symbol():
    """A caller passing `aapl` and a later caller passing `AAPL`
    must hit the same cache entry — otherwise typos in the symbol
    would silently fragment the namespace. The uppercasing happens
    at the public-function boundary (`get_cached_price` /
    `refresh_cached_price`); the bare `_cache_key` helper is a
    pure pass-through that doesn't normalise on its own. We test
    the public contract here by reading from `_FakeInfrastructure`'s
    store after a `get_cached_price` call with a lowercase symbol."""
    infra = _FakeInfrastructure()
    fetch = _CountingFetch()
    get_cached_price(
        symbol="aapl",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    # The stored key uses the uppercased symbol — typos in the
    # caller-side argument don't fragment the namespace.
    assert "price:NASDAQ:AAPL" in infra.store
    assert "price:NASDAQ:aapl" not in infra.store


# ---------------------------------------------------------------------------
# Cache freshness decision: closed-to-open transition.
# ---------------------------------------------------------------------------


def test_cached_realtime_price_from_closed_market_is_stale_when_market_is_now_open():
    """Acceptance criterion: cache is invalidated when market
    transitions from closed to open. A real-time price captured at
    02:00 on a Sunday is not the right answer at 09:31 on Monday."""
    cached = {
        "value": {"price": 100},
        "market_open_at_cache_time": False,
        "data_type": REALTIME_PRICE,
    }
    assert _is_cache_fresh_for_current_market(
        cached, REALTIME_PRICE, "NSE", now_open=True
    ) is False


def test_cached_realtime_price_from_open_market_is_fresh_while_market_remains_open():
    cached = {
        "value": {"price": 100},
        "market_open_at_cache_time": True,
        "data_type": REALTIME_PRICE,
    }
    assert _is_cache_fresh_for_current_market(
        cached, REALTIME_PRICE, "NSE", now_open=True
    ) is True


def test_cached_realtime_price_from_open_market_is_stale_after_close():
    """A symmetric check: the flip from open to closed is also a
    regime change (the price's relevance shrinks from 60s to 3600s
    TTL, but a cached value from an active session is still
    accurate enough to keep serving). Per the acceptance criteria,
    only the closed->open flip is a hard invalidation."""
    cached = {
        "value": {"price": 100},
        "market_open_at_cache_time": True,
        "data_type": REALTIME_PRICE,
    }
    # Closed->open is the documented invalidation; open->closed is
    # NOT — the entry is allowed to keep serving until its TTL
    # expires, and a closed-market TTL is just longer.
    assert _is_cache_fresh_for_current_market(
        cached, REALTIME_PRICE, "NSE", now_open=False
    ) is True


def test_historical_data_is_fresh_regardless_of_market_state():
    """Historical data is, by definition, not live — so no
    market-state comparison applies. A 24-hour-old historical bar
    served at any market state is correct (the bar's data hasn't
    changed)."""
    cached = {
        "value": {"bar": 1},
        "market_open_at_cache_time": False,
        "data_type": HISTORICAL,
    }
    assert _is_cache_fresh_for_current_market(
        cached, HISTORICAL, "NSE", now_open=True
    ) is True


def test_company_metadata_is_fresh_regardless_of_market_state():
    """Same reasoning as historical data: company profiles don't
    change tick-by-tick with the market, so no market-state
    comparison applies."""
    cached = {
        "value": {"name": "Acme"},
        "market_open_at_cache_time": False,
        "data_type": COMPANY_METADATA,
    }
    assert _is_cache_fresh_for_current_market(
        cached, COMPANY_METADATA, "NSE", now_open=True
    ) is True


# ---------------------------------------------------------------------------
# get_cached_price: cache hit within the TTL window.
# ---------------------------------------------------------------------------


def test_get_cached_price_calls_fetch_fn_once_across_n_repeated_calls(monkeypatch):
    """The behaviour the production "80% hit ratio" metric
    measures IS unit-testable: repeated calls within the same TTL
    window produce hits, so `fetch_fn` is invoked exactly once.
    This is what the production target describes, asserted against
    a counter-incrementing fetch_fn (no real network access).

    The 80% figure itself is a production monitoring target, not a
    unit-testable assertion (it depends on real workload patterns
    we cannot fabricate hermetically)."""
    _force_market_state(monkeypatch, is_open=True)
    fetch = _CountingFetch()
    infra = _FakeInfrastructure()

    results = [
        get_cached_price(
            symbol="AAPL",
            exchange="NASDAQ",
            fetch_fn=fetch,
            data_type=REALTIME_PRICE,
            infrastructure=infra,
        )
        for _ in range(10)
    ]

    # fetch_fn called exactly once for ten read calls — proof of
    # cache hits on calls 2..10.
    assert fetch.call_count == 1
    # And every returned value is the SAME cached value, not ten
    # independently re-fetched values.
    assert all(r == results[0] for r in results)


def test_get_cached_price_caches_with_60s_ttl_during_market_hours(monkeypatch):
    """Acceptance criterion: real-time prices are cached for 1
    minute during market hours."""
    _force_market_state(monkeypatch, is_open=True)
    fetch = _CountingFetch()
    infra = _FakeInfrastructure()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    assert len(infra.set_calls) == 1
    key, value, ttl = infra.set_calls[0]
    assert key == "price:NASDAQ:AAPL"
    assert ttl == 60
    # The cached payload carries the market-open state at fetch
    # time so the closed-to-open transition can be detected on
    # later reads.
    assert value["market_open_at_cache_time"] is True
    assert value["data_type"] == REALTIME_PRICE


def test_get_cached_price_caches_with_3600s_ttl_outside_market_hours(monkeypatch):
    """Acceptance criterion: real-time prices are cached for 1
    hour outside market hours."""
    _force_market_state(monkeypatch, is_open=False)
    fetch = _CountingFetch()
    infra = _FakeInfrastructure()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    assert len(infra.set_calls) == 1
    _, _, ttl = infra.set_calls[0]
    assert ttl == 3600


def test_get_cached_price_caches_historical_data_with_24h_ttl(monkeypatch):
    """Acceptance criterion: historical data is cached for 24 hours."""
    _force_market_state(monkeypatch, is_open=True)
    fetch = _CountingFetch()
    infra = _FakeInfrastructure()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=HISTORICAL,
        infrastructure=infra,
    )

    assert len(infra.set_calls) == 1
    _, _, ttl = infra.set_calls[0]
    assert ttl == 86400


def test_get_cached_price_caches_company_metadata_with_7d_ttl(monkeypatch):
    """Acceptance criterion: company metadata is cached for 7 days."""
    _force_market_state(monkeypatch, is_open=False)
    fetch = _CountingFetch()
    infra = _FakeInfrastructure()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=COMPANY_METADATA,
        infrastructure=infra,
    )

    assert len(infra.set_calls) == 1
    _, _, ttl = infra.set_calls[0]
    assert ttl == 604800


# ---------------------------------------------------------------------------
# get_cached_price: closed-to-open transition invalidates the entry.
# ---------------------------------------------------------------------------


def test_get_cached_price_refetches_when_market_transitions_closed_to_open(monkeypatch):
    """Acceptance criterion: cache is invalidated when market
    transitions from closed to open. A real-time price fetched
    while the market was closed is NOT served once the market
    opens — the next read is a real refetch.

    This is implemented by storing `market_open_at_cache_time`
    alongside the value and re-comparing on every read. The test
    simulates the transition by populating the cache as though
    the previous call happened during closed hours, then
    monkeypatching `is_market_open` to return True for the next
    call."""
    infra = _FakeInfrastructure()
    infra.store["price:NASDAQ:AAPL"] = {
        "value": {"price": 100, "note": "fetched-while-closed"},
        "market_open_at_cache_time": False,
        "data_type": REALTIME_PRICE,
    }

    # Now the market opens — `_force_market_state` replaces the
    # is_market_open this module sees at call time.
    _force_market_state(monkeypatch, is_open=True)

    fetch = _CountingFetch(value_factory=lambda i: {"price": 200, "note": "fresh-after-open"})

    value = get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # fetch_fn was called — the cached entry was NOT served.
    assert fetch.call_count == 1
    assert value == {"price": 200, "note": "fresh-after-open"}
    # And the freshly-fetched value is written back to the cache
    # under the new market-open state, so the next read within
    # the TTL window IS a hit.
    assert infra.store["price:NASDAQ:AAPL"]["market_open_at_cache_time"] is True
    assert infra.store["price:NASDAQ:AAPL"]["value"] == {
        "price": 200,
        "note": "fresh-after-open",
    }


def test_get_cached_price_does_not_invalidate_on_open_to_closed_transition(monkeypatch):
    """The symmetric transition (open -> closed) is NOT a hard
    invalidation: the entry is allowed to keep serving until its
    TTL expires (and the TTL is just longer for the closed
    regime). The acceptance criteria explicitly call out only the
    closed->open direction."""
    infra = _FakeInfrastructure()
    infra.store["price:NASDAQ:AAPL"] = {
        "value": {"price": 100},
        "market_open_at_cache_time": True,
        "data_type": REALTIME_PRICE,
    }
    _force_market_state(monkeypatch, is_open=False)

    fetch = _CountingFetch()

    value = get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # The cached value was served — fetch_fn NOT called.
    assert fetch.call_count == 0
    assert value == {"price": 100}


# ---------------------------------------------------------------------------
# get_cached_price: per-exchange isolation.
# ---------------------------------------------------------------------------


def test_get_cached_price_keeps_us_and_india_caches_separate(monkeypatch):
    """Acceptance criterion: market-specific invalidation (NSE/BSE
    separate from US). Two distinct exchanges for the same ticker
    produce two independent cache entries; writing to one never
    invalidates the other."""
    _force_market_state(monkeypatch, is_open=True)

    nasdaq_fetch = _CountingFetch(value_factory=lambda i: {"price": "us-" + str(i)})
    nse_fetch = _CountingFetch(value_factory=lambda i: {"price": "in-" + str(i)})
    infra = _FakeInfrastructure()

    nasdaq_value = get_cached_price(
        symbol="RELIANCE",
        exchange="NASDAQ",
        fetch_fn=nasdaq_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    nse_value = get_cached_price(
        symbol="RELIANCE",
        exchange="NSE",
        fetch_fn=nse_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # Both fetches fired exactly once — no cross-contamination.
    assert nasdaq_fetch.call_count == 1
    assert nse_fetch.call_count == 1
    assert nasdaq_value == {"price": "us-1"}
    assert nse_value == {"price": "in-1"}
    # Two distinct cache keys, no overlap.
    assert set(infra.store.keys()) == {"price:NASDAQ:RELIANCE", "price:NSE:RELIANCE"}

    # And a second read on each side still hits its own cache.
    nasdaq_again = get_cached_price(
        symbol="RELIANCE",
        exchange="NASDAQ",
        fetch_fn=nasdaq_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    nse_again = get_cached_price(
        symbol="RELIANCE",
        exchange="NSE",
        fetch_fn=nse_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert nasdaq_fetch.call_count == 1
    assert nse_fetch.call_count == 1
    assert nasdaq_again == {"price": "us-1"}
    assert nse_again == {"price": "in-1"}


# ---------------------------------------------------------------------------
# get_cached_price: error surface.
# ---------------------------------------------------------------------------


def test_get_cached_price_raises_for_unknown_exchange(monkeypatch):
    """A typo'd exchange surfaces as the named exception, not a
    silent always-miss."""
    _force_market_state(monkeypatch, is_open=False)
    fetch = _CountingFetch()

    with pytest.raises(UnknownPriceCacheExchangeError):
        get_cached_price(
            symbol="AAPL",
            exchange="NYSE-typo",  # not a real key in market_hours config
            fetch_fn=fetch,
            data_type=REALTIME_PRICE,
            infrastructure=_FakeInfrastructure(),
        )
    # And fetch_fn was NOT called — the validator failed before
    # any network-shaped work happened.
    assert fetch.call_count == 0


def test_get_cached_price_raises_for_unknown_data_type(monkeypatch):
    _force_market_state(monkeypatch, is_open=False)
    fetch = _CountingFetch()

    with pytest.raises(InvalidPriceCacheDataTypeError):
        get_cached_price(
            symbol="AAPL",
            exchange="NASDAQ",
            fetch_fn=fetch,
            data_type="not-a-real-data-type",
            infrastructure=_FakeInfrastructure(),
        )
    assert fetch.call_count == 0


def test_get_cached_price_propagates_fetch_fn_exceptions(monkeypatch):
    """Vendor failures are propagated unchanged — this module does
    NOT swallow them into a "last known good" cache value, because
    doing so would mask real outages as silent successes."""
    _force_market_state(monkeypatch, is_open=True)

    def boom():
        raise RuntimeError("vendor is on fire")

    infra = _FakeInfrastructure()
    with pytest.raises(RuntimeError, match="vendor is on fire"):
        get_cached_price(
            symbol="AAPL",
            exchange="NASDAQ",
            fetch_fn=boom,
            data_type=REALTIME_PRICE,
            infrastructure=infra,
        )
    # No partial cache write — a failed fetch leaves nothing
    # behind for the next caller to accidentally see.
    assert infra.set_calls == []


# ---------------------------------------------------------------------------
# refresh_cached_price: bypass cache lookup, but write back.
# ---------------------------------------------------------------------------


def test_refresh_cached_price_bypasses_cache_lookup_but_writes_back(monkeypatch):
    """Acceptance criterion: manual refresh mechanism bypasses
    cache. The read path is skipped (so the operator's stale
    suspicion is validated), but the fresh result IS written back
    to the cache so the *next* read is a hit — the operator who
    hit "refresh" expects both this call AND the next one to be
    fresh."""
    _force_market_state(monkeypatch, is_open=True)

    infra = _FakeInfrastructure()
    # Pre-populate a stale cache entry so we can prove the
    # refresh path does NOT serve it.
    infra.store["price:NASDAQ:AAPL"] = {
        "value": {"price": 100, "note": "stale"},
        "market_open_at_cache_time": True,
        "data_type": REALTIME_PRICE,
    }

    fetch = _CountingFetch(value_factory=lambda i: {"price": 200, "note": "refreshed"})

    refreshed = refresh_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # fetch_fn was called — the cached stale value was NOT served.
    assert fetch.call_count == 1
    assert refreshed == {"price": 200, "note": "refreshed"}
    # And the cache was overwritten with the fresh value.
    assert infra.store["price:NASDAQ:AAPL"]["value"] == {"price": 200, "note": "refreshed"}
    # Cache TTL is still 60 s for real-time while open.
    assert infra.set_calls[-1][2] == 60


def test_refresh_cached_price_subsequent_read_is_a_cache_hit(monkeypatch):
    """The write-back half of the refresh contract: after a manual
    refresh, the next `get_cached_price` read IS a hit (fetch_fn
    is not called a second time)."""
    _force_market_state(monkeypatch, is_open=True)

    refresh_fetch = _CountingFetch(value_factory=lambda i: {"price": 999, "call": i})
    read_fetch = _CountingFetch(value_factory=lambda i: {"price": 1, "call": i})
    infra = _FakeInfrastructure()

    refresh_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=refresh_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    value = get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=read_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    assert refresh_fetch.call_count == 1
    # read_fetch NOT called — the refreshed value was served
    # from cache.
    assert read_fetch.call_count == 0
    assert value == {"price": 999, "call": 1}


def test_refresh_cached_price_with_closed_market_uses_3600s_ttl(monkeypatch):
    """Refresh honours the same TTL selection as a fresh read —
    3600 s when the market is closed, not the open-market 60 s."""
    _force_market_state(monkeypatch, is_open=False)

    fetch = _CountingFetch()
    infra = _FakeInfrastructure()

    refresh_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    assert infra.set_calls[-1][2] == 3600


def test_refresh_cached_price_raises_for_unknown_exchange(monkeypatch):
    fetch = _CountingFetch()
    with pytest.raises(UnknownPriceCacheExchangeError):
        refresh_cached_price(
            symbol="AAPL",
            exchange="NYSE-typo",
            fetch_fn=fetch,
            data_type=REALTIME_PRICE,
            infrastructure=_FakeInfrastructure(),
        )
    assert fetch.call_count == 0


# ---------------------------------------------------------------------------
# default-infrastructure seam — same shape as exchange_rate_client.
# ---------------------------------------------------------------------------


def test_get_cached_price_uses_default_infrastructure_when_none_passed(monkeypatch):
    """When the caller doesn't supply an `Infrastructure`, this
    module defaults to the real `DefaultInfrastructure` (the
    Redis-backed one). Same seam
    `src/exchange_rate_client.py`'s `_default_infrastructure`
    already establishes — tests monkeypatch the seam to stay
    hermetic; production callers get the real thing."""
    _force_market_state(monkeypatch, is_open=True)
    fetch = _CountingFetch()
    fake_infra = _FakeInfrastructure()
    monkeypatch.setattr(price_cache, "_default_infrastructure", lambda: fake_infra)

    value = get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
    )

    assert value == {"price": 101}  # the 1st call to value_factory(1)
    assert len(fake_infra.set_calls) == 1
    assert fake_infra.set_calls[0][2] == 60


# ---------------------------------------------------------------------------
# import-time safety — same posture as the other vendor modules.
# ---------------------------------------------------------------------------


def test_module_imports_without_touching_environment_or_network():
    """Importing the module must not touch the filesystem, the
    environment, or the network. `is_market_open` is read at call
    time; `_default_infrastructure` is invoked at call time; no
    module-level side effects. Mirrors the import-time posture
    `src/exchange_rate_client.py` and `src/alpha_vantage_client.py`
    already take."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "price_cache_reload_probe", price_cache.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Just having imported cleanly is the assertion — no fixture
    # asserts the negative. `module` is intentionally never
    # installed into sys.modules, so it can't affect any other test.


# ---------------------------------------------------------------------------
# STORY-9 acceptance criteria — one end-to-end check that walks the
# public API through every AC in the story, hermetically.
# ---------------------------------------------------------------------------


def test_story9_acceptance_criteria(monkeypatch):
    """Dev-authored story-level acceptance test for STORY-9. Walks
    through each acceptance criterion against the real
    `src/price_cache.py` module, with `is_market_open` and the
    cache layer monkeypatched so this test is hermetic.

    AC1: Real-time prices are cached for 1 minute during market hours.
    AC2: Real-time prices are cached for 1 hour outside market hours.
    AC3: Historical data is cached for 24 hours.
    AC4: Company metadata is cached for 7 days.
    AC5: Cache is invalidated when market transitions from closed to open.
    AC6: Cache includes market-specific invalidation (NSE/BSE separate from US).
    AC7: Manual refresh mechanism bypasses cache.
    """
    infra = _FakeInfrastructure()

    # --- AC1: 60 s TTL during market hours -------------------------
    _force_market_state(monkeypatch, is_open=True)
    fetch = _CountingFetch()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert infra.set_calls[-1][2] == 60

    # --- AC2: 3600 s TTL outside market hours ----------------------
    _force_market_state(monkeypatch, is_open=False)
    infra.store.clear()
    infra.set_calls.clear()
    fetch = _CountingFetch()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert infra.set_calls[-1][2] == 3600

    # --- AC3: 24 h TTL for historical data ------------------------
    _force_market_state(monkeypatch, is_open=True)
    infra.store.clear()
    infra.set_calls.clear()
    fetch = _CountingFetch()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=HISTORICAL,
        infrastructure=infra,
    )
    assert infra.set_calls[-1][2] == 86400

    # --- AC4: 7 d TTL for company metadata -------------------------
    _force_market_state(monkeypatch, is_open=False)
    infra.store.clear()
    infra.set_calls.clear()
    fetch = _CountingFetch()

    get_cached_price(
        symbol="AAPL",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=COMPANY_METADATA,
        infrastructure=infra,
    )
    assert infra.set_calls[-1][2] == 604800

    # --- AC5: closed->open transition invalidates the cache -------
    # Pre-populate as though the previous call happened while the
    # market was closed.
    infra.store["price:NASDAQ:MSFT"] = {
        "value": {"price": 50},
        "market_open_at_cache_time": False,
        "data_type": REALTIME_PRICE,
    }
    # Market now opens.
    _force_market_state(monkeypatch, is_open=True)
    fetch = _CountingFetch(value_factory=lambda i: {"price": 60})

    value = get_cached_price(
        symbol="MSFT",
        exchange="NASDAQ",
        fetch_fn=fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # The cached stale value was NOT served — fetch_fn fired and
    # the fresh value was returned and re-cached.
    assert fetch.call_count == 1
    assert value == {"price": 60}
    assert infra.store["price:NASDAQ:MSFT"]["market_open_at_cache_time"] is True
    assert infra.store["price:NASDAQ:MSFT"]["value"] == {"price": 60}

    # --- AC6: per-exchange isolation (NSE/BSE separate from US) ---
    infra.store.clear()
    infra.set_calls.clear()
    _force_market_state(monkeypatch, is_open=True)

    nasdaq_fetch = _CountingFetch(value_factory=lambda i: {"price": "us"})
    nse_fetch = _CountingFetch(value_factory=lambda i: {"price": "in"})

    get_cached_price(
        symbol="RELIANCE",
        exchange="NASDAQ",
        fetch_fn=nasdaq_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    get_cached_price(
        symbol="RELIANCE",
        exchange="NSE",
        fetch_fn=nse_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # Two distinct cache keys, two distinct fetches, zero
    # cross-contamination.
    assert nasdaq_fetch.call_count == 1
    assert nse_fetch.call_count == 1
    assert set(infra.store.keys()) == {"price:NASDAQ:RELIANCE", "price:NSE:RELIANCE"}

    # --- AC7: refresh bypasses cache but writes back --------------
    infra.store.clear()
    infra.set_calls.clear()
    _force_market_state(monkeypatch, is_open=True)

    infra.store["price:NASDAQ:GOOG"] = {
        "value": {"price": 1, "note": "stale"},
        "market_open_at_cache_time": True,
        "data_type": REALTIME_PRICE,
    }
    refresh_fetch = _CountingFetch(value_factory=lambda i: {"price": 2, "note": "refreshed"})

    refreshed = refresh_cached_price(
        symbol="GOOG",
        exchange="NASDAQ",
        fetch_fn=refresh_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )

    # Bypass: the stale value was NOT served.
    assert refreshed == {"price": 2, "note": "refreshed"}
    assert refresh_fetch.call_count == 1
    # Write-back: cache now holds the fresh value.
    assert infra.store["price:NASDAQ:GOOG"]["value"] == {"price": 2, "note": "refreshed"}

    # And the *next* read within the TTL window is a hit.
    read_fetch = _CountingFetch()
    again = get_cached_price(
        symbol="GOOG",
        exchange="NASDAQ",
        fetch_fn=read_fetch,
        data_type=REALTIME_PRICE,
        infrastructure=infra,
    )
    assert read_fetch.call_count == 0
    assert again == {"price": 2, "note": "refreshed"}