"""Tests for src/exchange_rate_client.py — the INR/USD exchange-rate
vendor client that resolves through `fetch_exchange_rate` (with its
`PlaceholderExchangeRateClient` counterpart when neither API key is
configured).

Almost everything here runs with no network access: `.env` loading,
`get_primary_api_key`/`get_fallback_api_key`'s selection logic, the
missing-key error path, the 4-decimal-place quantization, the
primary-then-fallback selection logic, and the cache hit / cache miss
paths are all pure logic once `requests.get` and the cache layer are
mocked out. Real network access is real usage against rate-limited
free tiers, so there is no deliberately-live test here — the same
`tests/test_alpha_vantage_client.py` / `tests/test_tavily_client.py`
pattern of one optional live test gated on a real key being
configured would apply if a key existed in this repo's `.env`, which
this story's premise explicitly assumes it does not.
"""

import json
import os
from decimal import Decimal

import pytest
import requests

import exchange_rate_client
from exchange_rate_client import (
    EXCHANGE_RATE_API_URL,
    EXCHANGE_RATE_FALLBACK_URL,
    ExchangeRateFetchError,
    MissingExchangeRateAPIKeyError,
    PlaceholderExchangeRateClient,
    _CACHE_KEY,
    _CACHE_TTL_SECONDS,
    fetch_exchange_rate,
    get_fallback_api_key,
    get_fallback_api_url,
    get_primary_api_key,
    get_primary_api_url,
)


class _FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"fake status {self.status_code}")

    def json(self) -> dict:
        return self._body


class _FakeInfrastructure:
    """In-memory stand-in for `DefaultInfrastructure.cache_get` /
    `cache_set`. Tracks what was stored so tests can assert against
    the real (rate, fetched_at) shape and the real 1-hour TTL."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.set_calls: list[tuple] = []

    def cache_get(self, key: str):
        return self.store.get(key)

    def cache_set(self, key: str, value, ttl_seconds: int) -> None:
        self.set_calls.append((key, value, ttl_seconds))
        self.store[key] = value


def _no_env_keys(monkeypatch) -> None:
    """Same isolation posture as the other vendor-client test helpers
    (e.g. `tests/test_alpha_vantage_client.py`'s `_no_env_key`):
    points `_ENV_FILE_PATH` at a file that doesn't exist and clears
    both key env vars so the real `.env` (if any) and the ambient
    process env can't leak into the test."""
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )


def _primary_only(monkeypatch) -> None:
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )


# --- get_*_api_key / get_*_api_url --------------------------------------


def test_get_primary_api_key_returns_none_when_unset(monkeypatch):
    _no_env_keys(monkeypatch)

    assert get_primary_api_key() is None


def test_get_primary_api_key_returns_value_from_process_env(monkeypatch):
    _no_env_keys(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-real-key")

    assert get_primary_api_key() == "primary-real-key"


def test_get_primary_api_key_reads_from_dotenv_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('EXCHANGE_RATE_API_KEY="primary-from-dotenv"\n')
    monkeypatch.setattr(exchange_rate_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)

    try:
        assert get_primary_api_key() == "primary-from-dotenv"
    finally:
        os.environ.pop("EXCHANGE_RATE_API_KEY", None)


def test_get_primary_api_key_treats_empty_string_as_unset(monkeypatch):
    """Empty-string env vars (`EXCHANGE_RATE_API_KEY=`) are treated the
    same as unconfigured — the `if value` guard in `_read_env` ensures
    that, mirroring how `get_api_key` in other vendor clients
    distinguishes "unconfigured" from "configured-to-empty"."""
    _no_env_keys(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "")

    assert get_primary_api_key() is None


def test_get_fallback_api_key_returns_none_when_unset(monkeypatch):
    _no_env_keys(monkeypatch)

    assert get_fallback_api_key() is None


def test_get_fallback_api_key_returns_value_from_process_env(monkeypatch):
    _no_env_keys(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-real-key")

    assert get_fallback_api_key() == "fallback-real-key"


def test_get_primary_api_url_returns_documented_default(monkeypatch):
    _no_env_keys(monkeypatch)
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)

    assert get_primary_api_url() == EXCHANGE_RATE_API_URL
    assert get_primary_api_url() == "https://open.er-api.com/v6/latest/USD"


def test_get_primary_api_url_overridden_by_env_var(monkeypatch):
    _no_env_keys(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_URL", "https://example.com/custom-endpoint")

    assert get_primary_api_url() == "https://example.com/custom-endpoint"


def test_get_fallback_api_url_returns_documented_default(monkeypatch):
    _no_env_keys(monkeypatch)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)

    assert get_fallback_api_url() == EXCHANGE_RATE_FALLBACK_URL
    assert get_fallback_api_url() == "https://data.fixer.io/api/latest"


def test_get_fallback_api_url_overridden_by_env_var(monkeypatch):
    _no_env_keys(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_URL", "https://example.com/custom-fallback")

    assert get_fallback_api_url() == "https://example.com/custom-fallback"


def test_module_level_constants_match_documented_values():
    """Acceptance criterion #4: API URLs are read from env vars, not
    hardcoded — but the documented defaults ARE the real vendor
    endpoints, exposed as module-level constants for tests / callers
    that want to reference them without re-reading the env."""
    assert EXCHANGE_RATE_API_URL == "https://open.er-api.com/v6/latest/USD"
    assert EXCHANGE_RATE_FALLBACK_URL == "https://data.fixer.io/api/latest"
    assert _CACHE_TTL_SECONDS == 3600  # 1 hour, per acceptance criteria
    assert _CACHE_KEY == "exchange_rate:inr_usd"


# --- fetch_exchange_rate: placeholder path (no keys configured) --------


def test_fetch_exchange_rate_raises_missing_key_error_when_neither_key_configured(monkeypatch):
    """The repo's real, current state: no exchange-rate keys
    configured. `fetch_exchange_rate` raises
    `MissingExchangeRateAPIKeyError` so a caller can pick a
    placeholder rather than this module fabricating a rate — the
    README's "Real-vs-Placeholder seam, never a silent no-op"
    principle in code."""
    _no_env_keys(monkeypatch)

    with pytest.raises(MissingExchangeRateAPIKeyError):
        fetch_exchange_rate(infrastructure=_FakeInfrastructure())


def test_placeholder_exchange_rate_client_returns_none():
    """When neither key is configured, callers construct a
    `PlaceholderExchangeRateClient` and ask it for the rate — it
    returns `None` honestly, never fabricating a number."""
    placeholder = PlaceholderExchangeRateClient()

    assert placeholder.get_rate() is None


# --- fetch_exchange_rate: primary happy path ----------------------------


def test_fetch_exchange_rate_returns_rate_from_primary_source(monkeypatch):
    """With EXCHANGE_RATE_API_KEY set, the primary source is tried
    first and its rate returned — quantized to 4 decimal places,
    matching the `_coerce_quantity_to_decimal` pattern in
    `c01_user_portfolio.py`."""
    _primary_only(monkeypatch)

    captured: dict = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "result": "success",
                "base_code": "USD",
                "rates": {"INR": 83.12345, "EUR": 0.92},
            }
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    infra = _FakeInfrastructure()
    rate = fetch_exchange_rate(infrastructure=infra)

    assert rate == Decimal("83.1235")  # quantized to 4 decimal places
    assert captured["url"] == EXCHANGE_RATE_API_URL
    assert captured["params"] == {"apikey": "primary-key"}
    assert captured["timeout"] == exchange_rate_client._REQUEST_TIMEOUT_SECONDS


def test_fetch_exchange_rate_caches_rate_with_fetched_at_and_1h_ttl(monkeypatch):
    """Acceptance criterion #3: 1-hour Redis-backed cache with a real
    fetched-at timestamp stored alongside the rate. The cache layer
    is the real `DefaultInfrastructure.cache_set` shape (the
    `_FakeInfrastructure` here mirrors it byte-for-byte so a real
    `DefaultInfrastructure` would behave identically)."""
    _primary_only(monkeypatch)
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _FakeResponse({"rates": {"INR": 83.5}}),
    )

    infra = _FakeInfrastructure()
    fetch_exchange_rate(infrastructure=infra)

    assert len(infra.set_calls) == 1
    key, value, ttl = infra.set_calls[0]
    assert key == _CACHE_KEY
    assert ttl == 3600  # 1 hour, per acceptance criteria
    # The cached value carries the rate as a string (so JSON
    # round-tripping doesn't lose Decimal precision) plus a real
    # fetched-at timestamp.
    assert value["rate"] == "83.5000"
    assert isinstance(value["fetched_at"], str)
    assert value["fetched_at"]  # non-empty


def test_fetch_exchange_rate_returns_cached_rate_on_cache_hit(monkeypatch):
    """Acceptance criterion #3 again: on a cache hit, no HTTP call is
    made and the cached rate is returned. This is what makes the
    1-hour TTL meaningful — second-and-later calls within the TTL
    pay zero network cost."""
    _primary_only(monkeypatch)

    def explode(*a, **k):
        raise AssertionError(
            "requests.get was called on a cache hit — should be a no-op"
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", explode)

    infra = _FakeInfrastructure()
    # Pre-populate the cache as though a previous (mocked) fetch had
    # already stored the rate with a real fetched-at timestamp.
    infra.store[_CACHE_KEY] = {"rate": "82.9900", "fetched_at": "2026-01-01T00:00:00"}

    rate = fetch_exchange_rate(infrastructure=infra)

    assert rate == Decimal("82.9900")
    # No new cache_set call — the cached value was just read.
    assert infra.set_calls == []


# --- fetch_exchange_rate: fallback on primary failure -------------------


def test_fetch_exchange_rate_falls_back_to_secondary_when_primary_raises_5xx(monkeypatch):
    """Acceptance criterion #2: on a real primary failure (non-2xx
    response here), the fallback vendor is consulted. The rate that
    comes back is returned; the original primary error is logged but
    not raised — the fallback is a *real* recovery, not a warning."""
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )

    call_log: list[str] = []

    def fake_get(url, params=None, timeout=None):
        call_log.append(url)
        if "open.er-api.com" in url:
            return _FakeResponse({}, status_code=500)
        # fixer.io fallback: derive INR/USD from EUR-base rates.
        return _FakeResponse(
            {
                "success": True,
                "base": "EUR",
                "rates": {"USD": 1.08, "INR": 90.0},
            }
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_FakeInfrastructure())

    # Both vendors were tried, in the documented order, and the
    # fallback-derived rate is returned, quantized to 4 dp.
    assert call_log == [EXCHANGE_RATE_API_URL, EXCHANGE_RATE_FALLBACK_URL]
    assert rate == Decimal("83.3333")  # 90.0 / 1.08, quantized


def test_fetch_exchange_rate_falls_back_on_primary_network_error(monkeypatch):
    """Network errors (timeouts, DNS failures, connection resets) are
    also real primary failures that trigger the fallback path — not
    just non-2xx responses."""
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )

    def fake_get(url, params=None, timeout=None):
        if "open.er-api.com" in url:
            raise requests.exceptions.ConnectionError("simulated network failure")
        return _FakeResponse({"rates": {"USD": 1.0, "INR": 83.0}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_FakeInfrastructure())

    assert rate == Decimal("83.0000")


def test_fetch_exchange_rate_falls_back_on_primary_missing_inr(monkeypatch):
    """A 200 OK with a body that lacks an INR rate is a real primary
    failure: the vendor returned successfully but didn't surface the
    field we asked for. Fallback kicks in just the same."""
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )

    def fake_get(url, params=None, timeout=None):
        if "open.er-api.com" in url:
            return _FakeResponse({"rates": {"EUR": 0.92}})  # no INR
        return _FakeResponse({"rates": {"USD": 1.0, "INR": 83.0}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_FakeInfrastructure())

    assert rate == Decimal("83.0000")


def test_fetch_exchange_rate_falls_back_on_primary_missing_key_when_fallback_is_set(monkeypatch):
    """If only the *fallback* key is configured, `fetch_exchange_rate`
    still tries the primary source's HTTP call first (which raises
    because the primary key is missing) and then recovers via the
    fallback. The `MissingExchangeRateAPIKeyError` from the primary is
    caught the same as any other real primary failure — the fallback
    is the recovery path."""
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"rates": {"USD": 1.0, "INR": 83.0}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_FakeInfrastructure())

    assert rate == Decimal("83.0000")


def test_fetch_exchange_rate_raises_clear_error_when_both_sources_fail(monkeypatch):
    """Acceptance criterion #5: real API failures (both sources) raise
    a clear, real exception and are logged, never silently fabricated.
    The exception is the module-specific `ExchangeRateFetchError`,
    with the original primary *and* fallback exceptions chained via
    `__cause__` / message so debugging has the real trace."""
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({}, status_code=500)

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    infra = _FakeInfrastructure()
    with pytest.raises(ExchangeRateFetchError) as exc_info:
        fetch_exchange_rate(infrastructure=infra)

    # Both vendors' failures are named in the message — a real
    # debugging signal, not a generic exception.
    message = str(exc_info.value)
    assert "primary" in message
    assert "fallback" in message
    # And no rate got cached — the failure was honest about producing
    # nothing, not silently storing a fabricated value.
    assert infra.set_calls == []


def test_fetch_exchange_rate_skips_primary_when_only_fallback_key_is_set(monkeypatch):
    """When only the fallback key is configured, _fetch_primary's own
    upfront MissingExchangeRateAPIKeyError short-circuit fires BEFORE
    any real HTTP call is made -- there's no point spending a real
    network round-trip on a request we already know is missing its
    key. fetch_exchange_rate still catches that as a real primary
    failure and recovers via the fallback, so only the fallback URL is
    ever actually visited."""
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )

    captured: dict = {}

    def fake_get(url, params=None, timeout=None):
        captured.setdefault("urls", []).append(url)
        return _FakeResponse({"rates": {"USD": 1.0, "INR": 83.0}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_FakeInfrastructure())

    # Only the fallback URL is actually visited -- the primary short-
    # circuits on the missing key without a real HTTP call.
    assert captured["urls"] == [EXCHANGE_RATE_FALLBACK_URL]
    assert rate == Decimal("83.0000")


# --- fetch_exchange_rate: cache uses real DefaultInfrastructure --------


def test_fetch_exchange_rate_uses_default_infrastructure_when_none_passed(monkeypatch):
    """When the caller doesn't supply an `Infrastructure`, this module
    defaults to `DefaultInfrastructure` (the real, Redis-backed one
    from `src/infrastructure_postgres.py`) — never a fabricated
    in-memory cache. This is the README's "Real-vs-Placeholder seam,
    never a silent no-op" principle applied to caching too."""
    _primary_only(monkeypatch)
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _FakeResponse({"rates": {"INR": 83.5}}),
    )

    # `_FakeInfrastructure` will be substituted in via monkeypatch
    # so the test stays hermetic and doesn't depend on a live Redis.
    fake_infra = _FakeInfrastructure()
    monkeypatch.setattr(
        exchange_rate_client, "_default_infrastructure", lambda: fake_infra
    )

    rate = fetch_exchange_rate()

    assert rate == Decimal("83.5000")
    # The cache layer's `cache_set` was called once with the real
    # (rate, fetched_at) payload and a 1-hour TTL — exactly the
    # DefaultInfrastructure.cache_set contract.
    assert fake_infra.set_calls and fake_infra.set_calls[0][2] == 3600


# --- fetch_exchange_rate: error paths (no fabricated fallback) ---------


def test_fetch_exchange_rate_raises_on_non_2xx_primary_with_no_fallback_key(monkeypatch):
    """If the primary fails and no fallback key is configured, this
    raises — never silently degrades to a placeholder-shaped value."""
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _FakeResponse({}, status_code=503),
    )

    with pytest.raises(ExchangeRateFetchError):
        fetch_exchange_rate(infrastructure=_FakeInfrastructure())


# --- STORY-7 acceptance-criteria test (dev-authored) -------------------


def test_story7_acceptance_criteria(monkeypatch):
    """Dev-authored story-level acceptance test for STORY-7. Walks
    through each of the five acceptance criteria and asserts the real
    behaviour of `fetch_exchange_rate` against the real
    `src/exchange_rate_client.py` module, with `requests.get` and the
    cache layer monkeypatched so this test is hermetic.

    AC1: With EXCHANGE_RATE_API_KEY configured, the primary source
         (exchangerate-api.com) is consulted first and its rate
         returned, quantized to 4 decimal places.
    AC2: On a real primary failure, the fallback (fixer.io) is
         consulted and its rate returned.
    AC3: Rates are cached with a 1-hour TTL via the real
         DefaultInfrastructure.cache_get/cache_set shape, with a real
         fetched-at timestamp stored alongside the rate.
    AC4: API URLs and keys are read from env vars, not hardcoded —
         env overrides take effect, defaults exist for when they
         aren't set.
    AC5: Both sources failing raises a clear, named
         ExchangeRateFetchError, never a fabricated fallback value.
    """
    fake_infra = _FakeInfrastructure()
    monkeypatch.setattr(exchange_rate_client, "_default_infrastructure", lambda: fake_infra)

    # --- AC4: env vars override defaults, but defaults exist -------
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "does-not-exist.env",
    )
    monkeypatch.setenv("EXCHANGE_RATE_API_URL", "https://primary.example.com/v6/latest/USD")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_URL", "https://fallback.example.com/api/latest")

    assert get_primary_api_url() == "https://primary.example.com/v6/latest/USD"
    assert get_fallback_api_url() == "https://fallback.example.com/api/latest"

    # Reset to documented defaults for the rest of the test.
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)

    # --- AC1: primary path returns the quantized rate ---------------
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "fallback-key")

    captured: dict = {"urls": []}

    def fake_get(url, params=None, timeout=None):
        captured["urls"].append(url)
        if "open.er-api.com" in url:
            return _FakeResponse(
                {"result": "success", "base_code": "USD", "rates": {"INR": 83.12345}}
            )
        # Fallback path (EUR base, derive INR/USD via division).
        return _FakeResponse({"success": True, "rates": {"USD": 1.08, "INR": 90.0}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=fake_infra)

    # --- AC1: quantized to 4 decimal places -------------------------
    assert rate == Decimal("83.1235")

    # --- AC3: 1-hour TTL with real fetched-at timestamp -------------
    assert len(fake_infra.set_calls) == 1
    key, value, ttl = fake_infra.set_calls[0]
    assert key == "exchange_rate:inr_usd"
    assert ttl == 3600
    assert value["rate"] == "83.1235"
    assert isinstance(value["fetched_at"], str) and value["fetched_at"]

    # --- AC2: cache hit returns cached value, no HTTP call ---------
    cached_before = fake_infra.set_calls[0]
    captured["urls"] = []
    second = fetch_exchange_rate(infrastructure=fake_infra)

    assert second == Decimal("83.1235")
    assert captured["urls"] == []  # no second HTTP call on cache hit
    assert fake_infra.set_calls == [cached_before]  # no second cache_set either

    # --- AC2: cache miss after eviction falls through to vendors ---
    fake_infra.store.clear()
    fake_infra.set_calls.clear()
    captured["urls"] = []

    # Make the primary vendor fail this time so the fallback path
    # is the one that produces the rate.
    def fake_get_primary_fails_then_fallback_succeeds(url, params=None, timeout=None):
        captured["urls"].append(url)
        if "open.er-api.com" in url:
            return _FakeResponse({}, status_code=503)
        return _FakeResponse({"rates": {"USD": 1.0, "INR": 84.0}})

    monkeypatch.setattr(
        exchange_rate_client.requests, "get", fake_get_primary_fails_then_fallback_succeeds
    )

    recovered = fetch_exchange_rate(infrastructure=fake_infra)

    # AC2: fallback was tried after primary failed.
    assert captured["urls"] == [EXCHANGE_RATE_API_URL, EXCHANGE_RATE_FALLBACK_URL]
    assert recovered == Decimal("84.0000")
    # AC3: the recovered rate also gets cached with the same shape.
    assert fake_infra.set_calls and fake_infra.set_calls[0][2] == 3600
    assert fake_infra.set_calls[0][1]["rate"] == "84.0000"

    # --- AC5: both sources failing raises the named exception ------
    fake_infra.store.clear()
    fake_infra.set_calls.clear()
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _FakeResponse({}, status_code=500),
    )

    with pytest.raises(ExchangeRateFetchError) as exc_info:
        fetch_exchange_rate(infrastructure=fake_infra)

    # AC5: the exception is module-specific (not a bare requests /
    # RuntimeError), and the original failure reasons are chained.
    assert isinstance(exc_info.value, ExchangeRateFetchError)
    message = str(exc_info.value)
    assert "primary" in message
    assert "fallback" in message
    # AC5: nothing was cached on the failing path — a fabricated
    # cache entry would defeat the whole principle.
    assert fake_infra.set_calls == []


# --- supplemental: json round-trip preserves Decimal precision ---------


def test_cache_roundtrip_preserves_decimal_precision(monkeypatch):
    """The cache stores the rate as a string (not a float) precisely
    because JSON round-tripping a Decimal as a number would lose
    precision. This test pins that behaviour: cache_get reads back a
    string and the caller re-coerces it to Decimal via
    `_quantize_rate`."""
    _primary_only(monkeypatch)
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _FakeResponse({"rates": {"INR": 83.12345}}),
    )

    infra = _FakeInfrastructure()
    fetch_exchange_rate(infrastructure=infra)

    cached = infra.store[_CACHE_KEY]
    # Stored as a string — JSON-round-trip-safe.
    assert isinstance(cached["rate"], str)
    assert cached["rate"] == "83.1235"

    # And the same cache_get returns it as-is, with the caller
    # re-quantizing on read so cache contents can't smuggle in an
    # arbitrary precision value.
    retrieved = infra.cache_get(_CACHE_KEY)
    assert json.loads(json.dumps(retrieved)) == cached


# --- supplemental: import-time safety -----------------------------------


def test_module_imports_without_touching_environment_or_network():
    """All env reads in this module are gated behind
    `_load_dotenv_into_environ`, which is called inside
    `get_primary_api_key`/`get_fallback_api_key`. Importing the
    module must not read `.env`, hit the network, or require a real
    key — the same import-time posture
    `src/alpha_vantage_client.py` and `src/tavily_client.py` already
    take.

    Real bug, found live: `importlib.reload(exchange_rate_client)`
    replaces the SHARED module object every other test in this file
    (and this one, via its own top-of-file `from exchange_rate_client
    import ...`) already holds references into -- every class
    (MissingExchangeRateAPIKeyError, ExchangeRateFetchError, ...)
    becomes a NEW object with the same name but a different identity.
    A later test's `pytest.raises(MissingExchangeRateAPIKeyError)`
    then silently fails to catch an exception the (reloaded) module
    actually raises, since they're no longer the same class. Loading
    a fresh copy under a throwaway module name via importlib.util
    (never touching `sys.modules['exchange_rate_client']`) proves the
    same "import alone doesn't touch env/network" property without
    corrupting shared module identity for every test that runs after
    this one in the same session."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "exchange_rate_client_reload_probe", exchange_rate_client.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Just having imported cleanly without raising is the assertion;
    # no fixture asserts the negative. `module` is intentionally never
    # installed into sys.modules, so it can't affect any other test.


# -------------------------------------------------------------------------
# QA verification suite for STORY-7.
#
# Independent of the dev-authored tests above: each function below targets
# ONE acceptance criterion from the story and makes assertions that are
# fresh and direct. We don't re-prove the suite; we re-prove each AC.
# -------------------------------------------------------------------------


class _QAFakeResponse:
    """QA's own FakeResponse (separate from the dev's `_FakeResponse`
    so a bug in one helper doesn't mask a bug in the implementation)."""

    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(
                f"{self.status_code} simulated"
            )
            err.response = self
            raise err

    def json(self) -> dict:
        return self._body


class _QAFakeInfrastructure:
    """QA's own in-memory cache stand-in. Records every cache_set so
    tests can assert against the EXACT (key, value, ttl_seconds)
    signature the real `DefaultInfrastructure.cache_set` uses."""

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


def _qa_no_env(monkeypatch) -> None:
    """QA helper: zero out both key env vars and repoint .env at a
    missing file so the real one (if any) can't leak in."""
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "qa-does-not-exist.env",
    )


def test_qa_ac1_primary_source_consulted_when_primary_key_set(monkeypatch):
    """AC1: System fetches INR/USD exchange rates from
    exchangerate-api.com as primary source when EXCHANGE_RATE_API_KEY
    is configured."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")

    visited_urls: list = []

    def fake_get(url, params=None, timeout=None):
        visited_urls.append((url, params))
        # Resemble exchangerate-api.com's documented response shape.
        return _QAFakeResponse(
            {"result": "success", "base_code": "USD", "rates": {"INR": 84.5678}}
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    infra = _QAFakeInfrastructure()
    rate = fetch_exchange_rate(infrastructure=infra)

    # The primary URL was the one and only call — no fallback URL.
    assert len(visited_urls) == 1
    assert visited_urls[0][0] == "https://open.er-api.com/v6/latest/USD"
    assert visited_urls[0][1] == {"apikey": "qa-primary-key"}
    assert rate == Decimal("84.5678")


def test_qa_ac2_fallback_on_primary_5xx_when_fallback_key_set(monkeypatch):
    """AC2: System falls back to fixer.io on any real primary-source
    failure, when EXCHANGE_RATE_FALLBACK_API_KEY is configured."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "qa-fallback-key")

    visited_urls: list = []

    def fake_get(url, params=None, timeout=None):
        visited_urls.append((url, params))
        if "open.er-api.com" in url:
            return _QAFakeResponse({}, status_code=500)
        # fixer.io shape (EUR base).
        return _QAFakeResponse(
            {"success": True, "base": "EUR", "rates": {"USD": 1.05, "INR": 88.2}}
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_QAFakeInfrastructure())

    # Both URLs were visited in order; primary first.
    assert len(visited_urls) == 2
    assert visited_urls[0][0] == "https://open.er-api.com/v6/latest/USD"
    assert visited_urls[1][0] == "https://data.fixer.io/api/latest"
    # Fallback params use `access_key`, not `apikey`.
    assert visited_urls[1][1] == {"access_key": "qa-fallback-key"}
    # 88.2 / 1.05 = 84.0 (4 dp).
    assert rate == Decimal("84.0000")


def test_qa_ac2_fallback_on_primary_network_error(monkeypatch):
    """AC2 (continued): a network error from the primary is also a
    real primary-source failure that must trigger the fallback."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "qa-fallback-key")

    visited_urls: list = []

    def fake_get(url, params=None, timeout=None):
        visited_urls.append(url)
        if "open.er-api.com" in url:
            raise requests.exceptions.ConnectionError("qa simulated network failure")
        return _QAFakeResponse({"rates": {"USD": 1.0, "INR": 82.5}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_QAFakeInfrastructure())

    assert visited_urls == [
        "https://open.er-api.com/v6/latest/USD",
        "https://data.fixer.io/api/latest",
    ]
    assert rate == Decimal("82.5000")


def test_qa_ac3_cache_uses_infrastructure_cache_get_cache_set_with_1h_ttl(monkeypatch):
    """AC3: Exchange rate data is cached for up to 1 hour using the
    real, existing DefaultInfrastructure.cache_get/cache_set
    (Redis-backed), with a real fetched-at timestamp stored alongside
    the rate."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")

    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _QAFakeResponse({"rates": {"INR": 83.7}}),
    )

    infra = _QAFakeInfrastructure()

    # Call once to populate cache.
    rate = fetch_exchange_rate(infrastructure=infra)

    assert rate == Decimal("83.7000")

    # Exactly one cache_set happened with a 1-hour TTL.
    assert len(infra.set_calls) == 1
    key, value, ttl = infra.set_calls[0]
    assert key == "exchange_rate:inr_usd"
    assert ttl == 3600  # 1 hour, per AC3

    # The cached value carries a real fetched-at timestamp AND the rate.
    assert "fetched_at" in value, "AC3 requires fetched_at stored alongside the rate"
    assert "rate" in value
    fetched_at = value["fetched_at"]
    assert isinstance(fetched_at, str)
    assert len(fetched_at) >= 10  # at least a YYYY-MM-DD
    # And the rate round-trips cleanly.
    assert value["rate"] == "83.7000"

    # A second call should consult cache_get and not call cache_set again.
    set_count_before = len(infra.set_calls)
    get_count_before = len(infra.get_calls)
    rate2 = fetch_exchange_rate(infrastructure=infra)
    assert rate2 == Decimal("83.7000")
    assert len(infra.get_calls) > get_count_before, "second call must hit cache_get"
    assert len(infra.set_calls) == set_count_before, "second call must not write through"


def test_qa_ac3_default_infrastructure_used_when_none_passed(monkeypatch):
    """AC3 (continued): When no infrastructure is injected,
    `fetch_exchange_rate` must default to the REAL
    `DefaultInfrastructure` (Redis-backed), not to a fabricated
    in-memory cache. We monkeypatch the DefaultInfrastructure symbol
    in the module's namespace with a recording fake and assert the
    module reaches for that default."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _QAFakeResponse({"rates": {"INR": 83.7}}),
    )

    real_default_called: dict = {"calls": 0}
    fake = _QAFakeInfrastructure()

    def fake_default_infra_constructor():
        real_default_called["calls"] += 1
        return fake

    monkeypatch.setattr(
        exchange_rate_client, "_default_infrastructure", fake_default_infra_constructor
    )

    # No `infrastructure=` kwarg -> must use the default.
    rate = fetch_exchange_rate()

    assert real_default_called["calls"] == 1, (
        "DefaultInfrastructure must be instantiated when no infrastructure is passed"
    )
    assert rate == Decimal("83.7000")
    assert len(fake.set_calls) == 1
    assert fake.set_calls[0][2] == 3600


def test_qa_ac4_env_var_overrides_hardcoded(monkeypatch):
    """AC4: API URLs/keys are read from EXCHANGE_RATE_API_URL,
    EXCHANGE_RATE_API_KEY, EXCHANGE_RATE_FALLBACK_URL,
    EXCHANGE_RATE_FALLBACK_API_KEY environment variables, not
    hardcoded."""
    _qa_no_env(monkeypatch)
    # Set all four env vars to non-default values.
    monkeypatch.setenv("EXCHANGE_RATE_API_URL", "https://qa-primary.test/v6/latest/USD")
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-env-primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_URL", "https://qa-fallback.test/api/latest")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "qa-env-fallback-key")

    # URL getters reflect env vars.
    assert get_primary_api_url() == "https://qa-primary.test/v6/latest/USD"
    assert get_fallback_api_url() == "https://qa-fallback.test/api/latest"

    # Key getters reflect env vars.
    assert get_primary_api_key() == "qa-env-primary-key"
    assert get_fallback_api_key() == "qa-env-fallback-key"

    # Now also verify the env vars reach the actual HTTP calls.
    visited_urls: list = []
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda url, params=None, timeout=None: (
            visited_urls.append((url, params))
            or _QAFakeResponse(
                {"rates": {"INR": 83.7}} if "qa-primary" in url
                else {"rates": {"USD": 1.0, "INR": 83.0}}
            )
        ),
    )

    rate = fetch_exchange_rate(infrastructure=_QAFakeInfrastructure())
    assert rate == Decimal("83.7000")
    assert visited_urls[0][0] == "https://qa-primary.test/v6/latest/USD"
    assert visited_urls[0][1] == {"apikey": "qa-env-primary-key"}


def test_qa_ac4_defaults_used_when_env_vars_absent(monkeypatch):
    """AC4 (continued): the documented real defaults are returned when
    the env vars aren't set — `EXCHANGE_RATE_API_URL` defaults to
    exchangerate-api.com's /v6/latest/USD, `EXCHANGE_RATE_FALLBACK_URL`
    defaults to fixer.io's /api/latest."""
    _qa_no_env(monkeypatch)

    assert get_primary_api_url() == "https://open.er-api.com/v6/latest/USD"
    assert get_fallback_api_url() == "https://data.fixer.io/api/latest"
    # And the keys are None (unconfigured), not "" or 0.
    assert get_primary_api_key() is None
    assert get_fallback_api_key() is None


def test_qa_ac5_4_decimal_places_quantization(monkeypatch):
    """AC5: Exchange rates are stored with 4 decimal places."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _QAFakeResponse({"rates": {"INR": 83.123456789}}),
    )

    infra = _QAFakeInfrastructure()
    rate = fetch_exchange_rate(infrastructure=infra)

    # Returned value quantized to 4 decimal places.
    assert rate == Decimal("83.1235")
    assert str(rate) == "83.1235"
    # Stored in cache quantized to 4 decimal places too.
    assert Decimal(infra.store["exchange_rate:inr_usd"]["rate"]) == Decimal("83.1235")


def test_qa_ac5_real_exception_on_both_sources_failing(monkeypatch):
    """AC5 (continued): real API failures (both sources) raise a
    clear, real exception and are logged, never silently fabricated."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "qa-fallback-key")
    monkeypatch.setattr(
        exchange_rate_client.requests,
        "get",
        lambda *a, **k: _QAFakeResponse({}, status_code=500),
    )

    infra = _QAFakeInfrastructure()

    with pytest.raises(ExchangeRateFetchError) as exc_info:
        fetch_exchange_rate(infrastructure=infra)

    # The exception is the module-specific type, not a generic one.
    assert type(exc_info.value) is ExchangeRateFetchError
    # The message names BOTH sources' failures so debugging has real
    # information (per AC5: "clear, real exception").
    msg = str(exc_info.value)
    assert "primary" in msg
    assert "fallback" in msg
    # And nothing got cached — a fabricated cache entry would defeat
    # the whole principle.
    assert infra.set_calls == [], (
        "Both sources failed — nothing should be cached, ever"
    )


def test_qa_ac5_placeholder_never_fabricates(monkeypatch):
    """AC5 (continued): When neither key is configured, the
    placeholder path returns None — never a fabricated rate."""
    _qa_no_env(monkeypatch)

    # The placeholder path: PlaceholderExchangeRateClient returns None.
    assert PlaceholderExchangeRateClient().get_rate() is None

    # And `fetch_exchange_rate` itself raises (doesn't fabricate) when
    # no keys are configured — so a caller MUST use the placeholder,
    # not get a fake value back.
    with pytest.raises(MissingExchangeRateAPIKeyError):
        fetch_exchange_rate(infrastructure=_QAFakeInfrastructure())


# -------------------------------------------------------------------------
# QA verification test for STORY-7 (independent, hermetic, fresh).
#
# This is a NEW independent test exercising THIS story's own
# acceptance criteria end-to-end. It is deliberately independent of
# the dev's helpers / QA's earlier helpers — it uses only the public
# surface of `src/exchange_rate_client.py` and `requests`. It asserts
# against the real, fresh behaviour:
#   - AC1: real primary URL hit with primary key, real rate returned
#   - AC2: real fallback URL hit on real primary RequestException
#   - AC3: real Infrastructure.cache_get/cache_set used with 1h TTL
#          AND a real fetched_at timestamp in the cached value
#   - AC4: real env-var overrides reach the HTTP call, real defaults
#          are used when env vars are absent
#   - AC5: real 4-decimal quantization (ROUND_HALF_UP), real exception
#          when both sources fail, no cache write on failure,
#          PlaceholderExchangeRateClient returns None (no fabrication)
# -------------------------------------------------------------------------


class _Story7IndependentInfrastructure:
    """Independent in-memory Infrastructure stand-in for this story's
    verification test. Mirrors the (key) -> value / (key, value,
    ttl_seconds) -> None signatures of `DefaultInfrastructure.cache_get`
    / `cache_set` exactly."""

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


def test_story7_end_to_end_acceptance_criteria(monkeypatch):
    """End-to-end fresh verification of STORY-7's five acceptance
    criteria, using only public surface of
    `src/exchange_rate_client.py`. Independent of the dev's test
    helpers and the QA suite's earlier helpers."""
    # Isolate from any real .env / process env.
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "story7-independent-does-not-exist.env",
    )

    # ----- AC4 (env-var override reaches the actual HTTP call) ----
    # Set non-default env vars and verify they reach requests.get.
    monkeypatch.setenv(
        "EXCHANGE_RATE_API_URL", "https://story7-primary.test/v6/latest/USD"
    )
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "story7-primary-key")
    monkeypatch.setenv(
        "EXCHANGE_RATE_FALLBACK_URL", "https://story7-fallback.test/api/latest"
    )
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "story7-fallback-key")

    assert (
        get_primary_api_url() == "https://story7-primary.test/v6/latest/USD"
    ), "AC4: env var must override default URL"
    assert (
        get_fallback_api_url() == "https://story7-fallback.test/api/latest"
    ), "AC4: env var must override default fallback URL"
    assert get_primary_api_key() == "story7-primary-key"
    assert get_fallback_api_key() == "story7-fallback-key"

    # AC4 (defaults): when env vars are absent, the real documented
    # defaults are returned.
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)
    assert get_primary_api_url() == "https://open.er-api.com/v6/latest/USD", (
        "AC4: documented real default for primary URL"
    )
    assert get_fallback_api_url() == "https://data.fixer.io/api/latest", (
        "AC4: documented real default for fallback URL"
    )

    # ----- AC1 (primary source consulted when primary key set) -----
    # Re-set primary key (we just unset it), leave URLs at defaults.
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "story7-primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "story7-fallback-key")

    visited: list = []

    def fake_get_ac1(url, params=None, timeout=None):
        visited.append((url, params, timeout))
        # Primary succeeds, fallback never reached on AC1.
        return _QAFakeResponse(
            {"result": "success", "base_code": "USD", "rates": {"INR": 83.12345}}
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get_ac1)

    infra_ac1 = _Story7IndependentInfrastructure()
    rate_ac1 = fetch_exchange_rate(infrastructure=infra_ac1)

    assert len(visited) == 1, "AC1: only primary URL should be hit"
    assert visited[0][0] == "https://open.er-api.com/v6/latest/USD"
    assert visited[0][1] == {"apikey": "story7-primary-key"}
    # AC5 (4 dp quantization): 83.12345 -> 83.1235 (ROUND_HALF_UP).
    assert rate_ac1 == Decimal("83.1235"), "AC5: rate must be 4 dp quantized"
    assert str(rate_ac1) == "83.1235"

    # AC3 (cache: 1h TTL, fetched_at, rate key/value shape).
    assert len(infra_ac1.set_calls) == 1
    key, value, ttl = infra_ac1.set_calls[0]
    assert key == "exchange_rate:inr_usd", "AC3: real cache key"
    assert ttl == 3600, "AC3: 1-hour TTL"
    assert value["rate"] == "83.1235", "AC3: rate stored as 4-dp string"
    assert isinstance(value["fetched_at"], str), "AC3: real fetched_at timestamp"
    assert len(value["fetched_at"]) >= 10, "AC3: fetched_at is a real ISO timestamp"

    # AC3 (cache hit: no second HTTP call, no second cache_set).
    get_calls_before = len(infra_ac1.get_calls)
    visited.clear()
    rate_cached = fetch_exchange_rate(infrastructure=infra_ac1)
    assert rate_cached == Decimal("83.1235")
    assert visited == [], "AC3: cache hit must not hit HTTP"
    assert len(infra_ac1.get_calls) > get_calls_before, "AC3: cache_get must be consulted"
    assert len(infra_ac1.set_calls) == 1, "AC3: cache hit must not write through"

    # ----- AC2 (fallback on real primary network failure) ----------
    # Reset cache and simulate a real network failure on primary
    # (requests.exceptions.Timeout is a subclass of RequestException),
    # then a real fallback success.
    infra_ac1.store.clear()
    infra_ac1.set_calls.clear()
    visited.clear()

    def fake_get_ac2(url, params=None, timeout=None):
        visited.append((url, params))
        if "open.er-api.com" in url:
            # Real RequestException (subclass), not just a non-2xx.
            raise requests.exceptions.Timeout(
                "story7 simulated primary timeout"
            )
        # Fallback: fixer.io EUR-base shape.
        return _QAFakeResponse(
            {"success": True, "base": "EUR", "rates": {"USD": 1.0, "INR": 83.0}}
        )

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get_ac2)
    rate_ac2 = fetch_exchange_rate(infrastructure=infra_ac1)
    assert visited[0][0] == "https://open.er-api.com/v6/latest/USD"
    assert visited[1][0] == "https://data.fixer.io/api/latest"
    assert visited[1][1] == {"access_key": "story7-fallback-key"}, (
        "AC2: fallback must use `access_key`, not `apikey`"
    )
    assert rate_ac2 == Decimal("83.0000"), (
        "AC2: fallback-derived rate (INR/USD from EUR-base)"
    )
    # AC3 (fallback rate also cached with 1h TTL + fetched_at).
    assert len(infra_ac1.set_calls) == 1
    assert infra_ac1.set_calls[0][2] == 3600
    assert infra_ac1.set_calls[0][1]["rate"] == "83.0000"
    assert "fetched_at" in infra_ac1.set_calls[0][1]

    # ----- AC5 (real exception on both sources failing; no cache) --
    infra_ac1.store.clear()
    infra_ac1.set_calls.clear()

    def fake_get_ac5(url, params=None, timeout=None):
        return _QAFakeResponse({}, status_code=500)

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get_ac5)

    with pytest.raises(ExchangeRateFetchError) as exc_info:
        fetch_exchange_rate(infrastructure=infra_ac1)
    assert type(exc_info.value) is ExchangeRateFetchError, (
        "AC5: must be the module-specific exception, not a generic one"
    )
    msg = str(exc_info.value)
    assert "primary" in msg and "fallback" in msg, (
        "AC5: exception message must name both sources' failures"
    )
    assert infra_ac1.set_calls == [], (
        "AC5: failure must not write to cache (no fabrication)"
    )

    # ----- AC5 (placeholder path: never fabricates a rate) ----------
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    assert (
        PlaceholderExchangeRateClient().get_rate() is None
    ), "AC5: placeholder must return None, never fabricate"
    with pytest.raises(MissingExchangeRateAPIKeyError):
        fetch_exchange_rate(infrastructure=_Story7IndependentInfrastructure()), (
            "AC5: with no keys, must raise MissingExchangeRateAPIKeyError"
        )