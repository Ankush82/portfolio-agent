"""Price data caching — TTL-aware, per-exchange, market-state-aware
cache wrapper around the real `DefaultInfrastructure.cache_get` /
`cache_set` (Redis-backed), the same shared-infrastructure pattern
`src/exchange_rate_client.py` already uses for its own 1-hour cache.

This is the focused module issue #53 (STORY-9) asks for: a real,
small, single-responsibility cache layer for vendor-fetched price
data, sitting between any caller (the data-sources component, a
component-level price fetch, an ad-hoc script) and the vendor client
they would otherwise hit on every call. It does not own a single
external concern beyond caching — no vendor URLs, no API keys, no
network calls of its own. The fetch is supplied by the caller as a
real injected callable (`fetch_fn`), so:

* the same module works for `alpha_vantage_client.py`,
  `yahoo_finance_client.py`, or a private internal feed — there is
  no implicit dependency on a particular vendor.
* tests do not need a network connection (a counter-incrementing
  lambda stands in for the real vendor call).

Design notes:

* **TTL selection is data-type aware, not call-site aware.** A
  caller expresses *what kind of data they want* — real-time
  price, historical bar, company profile — and this module picks
  the right TTL based on (a) the data type and (b) whether the
  market is currently open. A real-time price fetched at 09:30 ET
  is only meaningful until 09:31 ET; the same symbol's price
  fetched at 02:00 on a Sunday is meaningful until Monday 09:30 ET
  — and the cache should reflect that distinction, which is what
  the TTL chosen by this module does.

* **Per-exchange keys.** Cache keys are namespaced by exchange, so
  `AAPL` on NASDAQ and `AAPL` on NSE (hypothetically) never collide,
  and the US and India caches are entirely separate. This is the
  concrete realisation of acceptance criterion "market-specific
  invalidation (NSE/BSE separate from US)".

* **Closed-to-open transition invalidates the cache.** The cached
  payload stores *the market state at the time the value was
  fetched* alongside the value. On every read, this module compares
  that stored state to the market's current state: if the value was
  cached while the market was closed and the market is now open,
  the entry is treated as a miss and re-fetched. A closed-market
  price from yesterday is genuinely stale once the market opens, so
  serving it would be a real correctness bug, not just a freshness
  nit.

* **Manual refresh bypasses lookup, not write-back.** The "refresh"
  path always calls `fetch_fn` (no cache hit on the way in) but
  still writes the fresh result back through the cache layer, so a
  subsequent read within the same TTL window is a hit. Skipping the
  write-back would mean a manual refresh leaves no trace, which is
  rarely what an operator actually wants — they refresh because they
  suspect the cached value is wrong, and they want the *next* read
  to reflect the fresh value too, not just this one call.

* **Operational metrics are not unit-testable assertions.** A
  production target like "cache hit ratio exceeds 80%" is a real
  monitoring concern, not something a unit test can honestly
  prove — `fetch_fn` is injected, so a test that wires a 100%
  hit-yielding fake would prove nothing about production traffic.
  This module is structured so that the actual *behaviour* the
  metric measures (cache hits happen when the TTL window is
  respected) IS unit-testable: a test that calls `get_cached_price`
  N times in a row with a counter-incrementing `fetch_fn` and
  asserts the counter stays at 1 is asserting the real
  behaviour. The 80% figure belongs in production monitoring,
  not in the test suite.
"""

import logging
from typing import Any, Callable, Literal

from infrastructure import Infrastructure

from market_hours import is_market_open

# `is_market_open` is the only thing this module imports from
# `market_hours`, on purpose: `market_status`'s dict shape isn't
# needed for the cache decision (open/closed is binary), so we keep
# the dependency surface minimal. `list_known_markets` is used at
# validation time so a typo'd exchange name surfaces as the same
# `UnknownMarketError` the rest of the codebase already raises,
# rather than as a silent "always cache miss" surprise.
from market_hours import list_known_markets

_LOGGER = logging.getLogger(__name__)

# TTL constants, expressed in seconds. Defined as module-level
# constants (rather than buried inside the function) so a test can
# import them and assert against them without re-deriving them
# from the rule of thumb. Matches the acceptance criteria verbatim.
_TTL_REALTIME_OPEN_SECONDS = 60          # 1 minute during market hours
_TTL_REALTIME_CLOSED_SECONDS = 3600     # 1 hour outside market hours
_TTL_HISTORICAL_SECONDS = 86400         # 24 hours for historical bars
_TTL_COMPANY_METADATA_SECONDS = 604800  # 7 days for company metadata

# `DataType` is the caller's expression of *what kind of data
# they're fetching*. Not a vendor identifier — this module does not
# care whether the price came from Alpha Vantage, Yahoo Finance, or
# an internal ledger. Just the data shape.
DataType = Literal["realtime_price", "historical", "company_metadata"]


# Public alias for callers that want to import the literal string
# directly without restating it. Same posture as the rest of this
# codebase, where public constants are surfaced by name.
REALTIME_PRICE = "realtime_price"
HISTORICAL = "historical"
COMPANY_METADATA = "company_metadata"

#: Public alias for the "realtime-price while market is open" TTL.
#: Useful for tests that want to assert "yes, the open-market path
#: really picked 60 seconds, not 3600".
TTL_REALTIME_OPEN_SECONDS = _TTL_REALTIME_OPEN_SECONDS
TTL_REALTIME_CLOSED_SECONDS = _TTL_REALTIME_CLOSED_SECONDS
TTL_HISTORICAL_SECONDS = _TTL_HISTORICAL_SECONDS
TTL_COMPANY_METADATA_SECONDS = _TTL_COMPANY_METADATA_SECONDS


class UnknownPriceCacheExchangeError(ValueError):
    """Raised when `get_cached_price` is called with an `exchange`
    that isn't one of the markets `market_hours.list_known_markets`
    knows about. Mirrors `market_hours.UnknownMarketError`'s
    posture: a named exception so callers get an unambiguous
    signal that the exchange they asked about isn't configured,
    rather than a generic `KeyError` or a silent "always cache
    miss". Re-raised here from `is_market_open`'s internal
    `UnknownMarketError` for symmetry with the rest of this
    module's public API."""


class InvalidPriceCacheDataTypeError(ValueError):
    """Raised when `data_type` is not one of the three values this
    module recognises (`realtime_price`, `historical`,
    `company_metadata`). Mirrors the rest of this module's
    validation surface — a named exception so a caller passing a
    typo'd data type gets a clear signal."""


def _known_exchange(exchange: str) -> None:
    """Validates that `exchange` is one of the keys
    `market_hours.list_known_markets` knows about, raising
    `UnknownPriceCacheExchangeError` if it isn't. Defensive check
    at the public-function boundary; the per-exchange cache key
    namespace relies on this being a small, finite set of strings
    (so the namespace is predictable and can't accidentally
    fragment by typos)."""
    if exchange not in list_known_markets():
        raise UnknownPriceCacheExchangeError(
            f"Unknown exchange {exchange!r} for price cache. Known exchanges: "
            f"{list_known_markets()}. Add it to config/market_hours.json to "
            f"cache prices for it."
        )


def _validate_data_type(data_type: str) -> None:
    """Defensive check at the public-function boundary; the TTL
    selection below is keyed off `data_type` so a typo'd value
    here would silently fall through to the historical-data TTL
    (the implicit default in the elif chain), which would be a
    real correctness bug rather than a clean error."""
    if data_type not in (REALTIME_PRICE, HISTORICAL, COMPANY_METADATA):
        raise InvalidPriceCacheDataTypeError(
            f"Unknown data_type {data_type!r}. Must be one of "
            f"{REALTIME_PRICE!r}, {HISTORICAL!r}, {COMPANY_METADATA!r}."
        )


def _ttl_for(data_type: str, exchange: str, market_open: bool) -> int:
    """Picks the right TTL for the (data_type, exchange, market-open)
    triple, per the acceptance criteria:

    * realtime_price + market open  -> 60 s (1 minute)
    * realtime_price + market closed -> 3600 s (1 hour)
    * historical (any market state) -> 86400 s (24 hours)
    * company_metadata (any market state) -> 604800 s (7 days)

    The function is pure (no I/O) so tests can assert against
    every cell of the decision matrix without standing up any
    external state."""
    if data_type == REALTIME_PRICE:
        return (
            _TTL_REALTIME_OPEN_SECONDS if market_open else _TTL_REALTIME_CLOSED_SECONDS
        )
    if data_type == HISTORICAL:
        return _TTL_HISTORICAL_SECONDS
    # COMPANY_METADATA is the only remaining valid value; the
    # validator at the public-function boundary guarantees we never
    # reach this branch with anything else.
    return _TTL_COMPANY_METADATA_SECONDS


def _cache_key(symbol: str, exchange: str) -> str:
    """The Redis key for this (symbol, exchange) pair. Per-exchange
    namespacing is the concrete realisation of "market-specific
    invalidation (NSE/BSE separate from US)": the same symbol on
    two different exchanges produces two distinct keys, so writes
    on one can never invalidate reads on the other."""
    return f"price:{exchange}:{symbol}"


def _is_cache_fresh_for_current_market(
    cached: dict, data_type: str, exchange: str, now_open: bool
) -> bool:
    """Returns True iff the cached payload should still be served
    in the current market-state regime.

    For real-time prices, only ONE specific transition is a hard
    invalidation: the flip from *closed* (at cache time) to *open*
    (now). That transition is when stale-cache becomes actively
    misleading — the price was captured when nothing was trading,
    and serving it during the live session would be wrong, not
    just slightly stale.

    The other direction (open -> closed) is NOT a hard
    invalidation: a price captured during the active session is
    still correct after the close, the cache just gets a longer
    TTL going forward. This matches the acceptance criteria
    exactly — the story calls out closed->open, not every state
    flip.

    For historical / company_metadata, market-open-ness is
    irrelevant (the data is by definition not live), so any
    cached value is fine regardless of current market state.

    Stored payload shape:
        {
            "value": <the actual data>,
            "market_open_at_cache_time": <bool>,
            "fetched_at": <ISO-ish string>,
            "data_type": <str>,
        }
    """
    if data_type != REALTIME_PRICE:
        return True
    was_open = bool(cached.get("market_open_at_cache_time"))
    # Only the closed -> open direction is a hard invalidation;
    # open -> closed is fine (and the cache just gets a longer
    # TTL under the new regime).
    if not was_open and now_open:
        return False
    return True


def _default_infrastructure() -> Infrastructure:
    """Lazy constructor for the real, Redis-backed `DefaultInfrastructure`.

    Indirected through a module-level callable (same pattern
    `src/exchange_rate_client.py` and `src/alpha_vantage_client.py`
    already take) so a test can
    `monkeypatch.setattr(price_cache, "_default_infrastructure",
    lambda: fake_infra)` and stay hermetic — directly monkeypatching
    the imported `DefaultInfrastructure` symbol wouldn't work
    because that import is what triggers `psycopg` at module load.
    """
    from infrastructure_postgres import DefaultInfrastructure

    return DefaultInfrastructure()


def get_cached_price(
    symbol: str,
    exchange: str,
    fetch_fn: Callable[[], Any],
    data_type: str = REALTIME_PRICE,
    infrastructure: Infrastructure | None = None,
) -> Any:
    """Returns a price (or historical bar, or company profile) for
    `symbol` on `exchange`, served from the Redis-backed cache when
    fresh and fetched via the injected `fetch_fn` when not.

    Cache behaviour:

    * The cache key is ``f"price:{exchange}:{symbol}"`` so NSE/BSE
      and US caches never collide, even for tickers that exist on
      multiple exchanges.
    * TTL selection honours the acceptance criteria exactly:
      60 s for real-time prices while the market is open, 3600 s
      for real-time prices while the market is closed, 86400 s
      (24 h) for historical data, 604800 s (7 d) for company
      metadata.
    * On every read, the current market state is compared to the
      state at fetch time: a cached real-time price fetched while
      the market was closed is treated as a miss the moment the
      market opens, and the fresh value is fetched and re-cached.
      This is the "cache invalidated when market transitions from
      closed to open" acceptance criterion, implemented as data
      rather than as a separate invalidation hook.
    * `fetch_fn` is a real injected callable (dependency
      injection): no vendor URL, no API key, no network call lives
      in this module. A test substitutes a counter-incrementing
      lambda and asserts the counter stays at 1 across N repeated
      calls within the same TTL window — that IS the test of the
      cache hit behaviour, and is what proves the "80% hit ratio"
      production target is even possible.

    Args:
        symbol: the ticker / identifier being priced (e.g.
            ``"AAPL"``, ``"RELIANCE"``). Uppercased here so a
            caller passing ``"aapl"`` doesn't fragment the cache
            namespace.
        exchange: one of the exchanges in
            `market_hours.list_known_markets()` (e.g.
            ``"NSE"``, ``"BSE"``, ``"NYSE"``, ``"NASDAQ"``).
        fetch_fn: a zero-argument callable that performs the real
            vendor fetch and returns the data. Called at most once
            per TTL window under normal cache-hit conditions.
        data_type: one of ``"realtime_price"`` (default),
            ``"historical"``, ``"company_metadata"``. Drives TTL
            selection.
        infrastructure: optional `Infrastructure` for cache
            access. Defaults to the real
            `DefaultInfrastructure` (Redis-backed) when not
            provided; tests pass a fake.

    Returns:
        Whatever `fetch_fn` returns — a dict, a number, a
        vendor-shaped object. This module is shape-agnostic on
        purpose: the cache layer doesn't know or care whether
        it's wrapping a Yahoo Finance quote dict or an Alpha
        Vantage daily bar.

    Raises:
        `UnknownPriceCacheExchangeError` if `exchange` isn't in
            the configured market set.
        `InvalidPriceCacheDataTypeError` if `data_type` isn't one
            of the three recognised values.
        Anything `fetch_fn` raises is propagated unchanged — this
            module does not swallow vendor failures into a cached
            "last known good" value, because doing so would mask
            real outages as silent successes.
    """
    _known_exchange(exchange)
    _validate_data_type(data_type)

    normalised_symbol = symbol.upper()
    key = _cache_key(normalised_symbol, exchange)

    if infrastructure is None:
        # Lazy: `infrastructure_postgres` pulls in `psycopg`, which
        # is only required at call time when no `Infrastructure`
        # was injected. Same import-time-safety posture
        # `src/exchange_rate_client.py` already takes.
        infrastructure = _default_infrastructure()

    now_open = is_market_open(exchange)
    ttl = _ttl_for(data_type, exchange, now_open)

    cached = infrastructure.cache_get(key)
    if cached is not None and _is_cache_fresh_for_current_market(
        cached, data_type, exchange, now_open
    ):
        return cached["value"]

    if cached is not None and data_type == REALTIME_PRICE:
        _LOGGER.info(
            "Price cache entry for %s on %s was captured while the market was "
            "%s; market is now %s. Refetching.",
            normalised_symbol,
            exchange,
            "open" if cached.get("market_open_at_cache_time") else "closed",
            "open" if now_open else "closed",
        )

    value = fetch_fn()

    infrastructure.cache_set(
        key,
        {
            "value": value,
            "market_open_at_cache_time": now_open,
            "data_type": data_type,
        },
        ttl,
    )
    return value


def refresh_cached_price(
    symbol: str,
    exchange: str,
    fetch_fn: Callable[[], Any],
    data_type: str = REALTIME_PRICE,
    infrastructure: Infrastructure | None = None,
) -> Any:
    """Manual refresh path: bypasses the cache lookup, calls
    `fetch_fn` directly, and writes the fresh result back to the
    cache so subsequent reads within the same TTL window are hits.

    The write-back is intentional: an operator hitting "refresh"
    usually does so because they suspect the cached value is wrong,
    and they want the *next* read (the one served from cache) to
    reflect the fresh value too — not just this one manual call.
    Skipping the write-back would leave the operator's belief
    ("I refreshed, surely the next read is fresh") out of sync
    with reality ("the next read is whatever was there before").
    Same signature as `get_cached_price` for caller ergonomics;
    same exception surface; same per-exchange key.

    NOTE: production "cache hit ratio exceeds 80%" is a monitoring
    metric, not a unit-testable assertion. The behaviour the
    metric measures IS unit-testable via `get_cached_price` with a
    counter-incrementing `fetch_fn` — see the dedicated test.
    """
    _known_exchange(exchange)
    _validate_data_type(data_type)

    normalised_symbol = symbol.upper()
    key = _cache_key(normalised_symbol, exchange)

    if infrastructure is None:
        infrastructure = _default_infrastructure()

    # Bypass the cache lookup entirely: fetch fresh, then write
    # back. The market-state comparison that `get_cached_price`
    # does on read is irrelevant here because we're not reading —
    # we're explicitly invalidating-and-replacing.
    value = fetch_fn()

    now_open = is_market_open(exchange)
    ttl = _ttl_for(data_type, exchange, now_open)
    infrastructure.cache_set(
        key,
        {
            "value": value,
            "market_open_at_cache_time": now_open,
            "data_type": data_type,
        },
        ttl,
    )
    return value