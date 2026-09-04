"""Exchange rate client — resolves the INR/USD exchange-rate fetch
slice of Data & Sources' open vendor gaps (ADR-0027), the same "one
small module per external vendor" shape `src/alpha_vantage_client.py`,
`src/tavily_client.py`, and `src/yahoo_finance_client.py` already
established.

Unlike `yahoo_finance_client.py` (keyless, verified live against its
public chart endpoint), this client genuinely needs a real API key for
both of its vendors — `exchangerate-api.com` for the primary source
and `fixer.io` for the fallback — because neither exposes an
unauthenticated, abuse-tolerant FX endpoint of the kind Yahoo Finance's
chart endpoint happens to. That is why this module is structured as a
key-gated "Real-vs-Placeholder seam, never a silent no-op" exactly
matching `src/alpha_vantage_client.py`'s posture: when neither key is
configured, callers construct their own honest placeholder rather than
this module fabricating a rate. ADR-0027's "open" vendor gap is
already partially closed for the MARKET_DATA/NEWS/EARNINGS slice via
Alpha Vantage (ADR-0046); the FX feed is the next, narrower slice this
project still needs a credential for before it can be resolved for real.

This module is deliberately the *only* place in the codebase that reads
`EXCHANGE_RATE_API_KEY` / `EXCHANGE_RATE_FALLBACK_API_KEY` or talks to
either vendor's `/latest` endpoint. Caching is delegated to
`DefaultInfrastructure.cache_get`/`cache_set` (Redis-backed) rather than
rebuilt here — the same shared-infrastructure pattern
`src/components/c02_data_sources.py` already uses for source-document
storage — so a rate, once fetched, lives in the same cache layer the
rest of the system already trusts.

`EXCHANGE_RATE_API_KEY`, `EXCHANGE_RATE_FALLBACK_API_KEY`,
`EXCHANGE_RATE_API_URL`, and `EXCHANGE_RATE_FALLBACK_URL` are all read
at call time, inside `get_primary_api_key()`/`get_fallback_api_key()` —
never at import time — so this module stays importable, and importing
it never touches the environment or the filesystem.
"""

import logging
import os
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import requests

from infrastructure import Infrastructure

# `DefaultInfrastructure` is intentionally NOT imported at module top:
# `infrastructure_postgres` transitively imports `psycopg` at collection
# time, which would couple this module's importability to a database
# driver that isn't needed for the env/key/URL getters or for callers
# who inject their own `Infrastructure`. It is resolved lazily inside
# `fetch_exchange_rate` only when the caller did not inject one — the
# same "env reads / vendor URLs / import-time safety, no driver
# coupling" posture `src/alpha_vantage_client.py` already takes (it
# does not import `DefaultInfrastructure` at all; it leaves that to
# its caller).


def _default_infrastructure() -> Infrastructure:
    """Lazy constructor for the real, Redis-backed `DefaultInfrastructure`.

    Indirected through a module-level callable so tests can
    `monkeypatch.setattr(exchange_rate_client, "_default_infrastructure",
    lambda: fake_infra)` and stay hermetic — directly monkeypatching
    the imported `DefaultInfrastructure` symbol wouldn't work because
    the import itself is what triggers `psycopg` at module load.
    """
    from infrastructure_postgres import DefaultInfrastructure

    return DefaultInfrastructure()

# Sensible real defaults so the module works out-of-the-box for any
# caller that hasn't set the URL env vars — these are the documented
# `/latest` endpoints both vendors actually expose.
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
EXCHANGE_RATE_FALLBACK_URL = "https://data.fixer.io/api/latest"

_REQUEST_TIMEOUT_SECONDS = 30
_CACHE_TTL_SECONDS = 3600  # 1 hour, per acceptance criteria
_CACHE_KEY = "exchange_rate:inr_usd"
_ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"

# Decimal precision for the stored rate — matches this project's
# established `quantize(Decimal("0.0001"))` pattern from
# `_coerce_quantity_to_decimal` (`src/components/c01_user_portfolio.py`),
# not an arbitrary precision choice.
_RATE_QUANTUM = Decimal("0.0001")

_LOGGER = logging.getLogger(__name__)


class MissingExchangeRateAPIKeyError(RuntimeError):
    """Raised by `fetch_exchange_rate` when neither
    `EXCHANGE_RATE_API_KEY` nor `EXCHANGE_RATE_FALLBACK_API_KEY` is set
    in the environment at call time. A specific, named exception — the
    same posture `src/alpha_vantage_client.py`'s
    `MissingAlphaVantageAPIKeyError` already takes — so a caller that
    genuinely wanted the real fetcher gets an unambiguous signal,
    rather than a generic error or a fabricated fallback rate."""


class ExchangeRateFetchError(RuntimeError):
    """Raised by `fetch_exchange_rate` when the primary source *and*
    the fallback source both fail — network errors, non-2xx responses,
    malformed JSON, or a vendor that does not surface a numeric INR
    rate. A specific, named exception class so callers that wanted the
    real fetcher get an unambiguous signal. No silent fallback to a
    fabricated rate — that's the README's "Real-vs-Placeholder seam,
    never a silent no-op" principle in code."""


def _load_dotenv_into_environ() -> None:
    """Manual `.env` parsing, not `python-dotenv` — same reasoning and
    same shape as `src/llm.py`'s, `src/alpha_vantage_client.py`'s, and
    `src/tavily_client.py`'s own `_load_dotenv_into_environ`: the only
    format this repo's `.env` ever needs is single-line `KEY=VALUE`, so
    a dependency for that is not worth adding. Never overwrites a
    variable already set in the real process environment."""
    if not _ENV_FILE_PATH.exists():
        return
    for line in _ENV_FILE_PATH.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, _, value = stripped_line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_env(name: str) -> str | None:
    """Reads a single environment variable, applying the same
    process-env-then-.env layering every other vendor client in this
    codebase already uses. `None` when it isn't configured, so the
    caller can distinguish "unconfigured" from "empty string"."""
    _load_dotenv_into_environ()
    value = os.environ.get(name)
    return value if value else None


def get_primary_api_key() -> str | None:
    """Reads `EXCHANGE_RATE_API_KEY` from the environment or a `.env`
    file at the repo root, at call time. Returns `None` when it isn't
    configured — callers (`fetch_exchange_rate`, below) use that `None`
    to decide whether to attempt the primary source or go straight to
    the fallback / placeholder, the same selection shape
    `src/alpha_vantage_client.py`'s `get_api_key` already established.
    Also returns `None` if `EXCHANGE_RATE_API_KEY` is set to an empty
    string, treating that the same as unconfigured."""
    return _read_env("EXCHANGE_RATE_API_KEY")


def get_fallback_api_key() -> str | None:
    """Reads `EXCHANGE_RATE_FALLBACK_API_KEY` from the environment or a
    `.env` file at the repo root, at call time. Returns `None` when it
    isn't configured. Same posture as `get_primary_api_key`."""
    return _read_env("EXCHANGE_RATE_FALLBACK_API_KEY")


def get_primary_api_url() -> str:
    """Reads `EXCHANGE_RATE_API_URL` from the environment, falling back
    to `EXCHANGE_RATE_API_URL`'s documented real default. Env var
    wins so an operator can override the vendor's endpoint URL
    without editing this module's source."""
    return _read_env("EXCHANGE_RATE_API_URL") or EXCHANGE_RATE_API_URL


def get_fallback_api_url() -> str:
    """Reads `EXCHANGE_RATE_FALLBACK_URL` from the environment, falling
    back to `EXCHANGE_RATE_FALLBACK_URL`'s documented real default.
    Same posture as `get_primary_api_url`."""
    return _read_env("EXCHANGE_RATE_FALLBACK_URL") or EXCHANGE_RATE_FALLBACK_URL


def _quantize_rate(value) -> Decimal:
    """Coerces a parsed JSON rate to a `Decimal` with 4 decimal places
    of precision, matching this project's established
    `Decimal(str(value)).quantize(Decimal("0.0001"))` pattern from
    `_coerce_quantity_to_decimal`
    (`src/components/c01_user_portfolio.py`). Raises `ValueError` on a
    non-numeric input — the rate is genuinely missing or malformed in
    that case, and the caller surfaces that as a real failure rather
    than fabricating a value."""
    try:
        return Decimal(str(value)).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"Exchange rate must be a real number (int/float/Decimal/str); got {value!r}"
        ) from exc


def _extract_primary_rate(body: dict) -> Decimal:
    """`exchangerate-api.com`'s `/v6/latest/USD` shape, verified
    against the vendor's documented response: `{"result": "success",
    "base_code": "USD", "rates": {"INR": <float>, ...}}`. Returns the
    INR rate quantized to 4 decimal places. Raises `ValueError` on a
    missing or non-numeric rate — caller treats that as a real fetch
    failure (same posture as the rest of this module: never fabricate)."""
    try:
        rates = body["rates"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Primary exchange-rate response is missing 'rates': {exc}"
        ) from exc
    inr = rates.get("INR")
    if inr is None:
        raise ValueError(
            f"Primary exchange-rate response has no 'INR' entry in 'rates'"
        )
    return _quantize_rate(inr)


def _extract_fallback_rate(body: dict) -> Decimal:
    """`fixer.io`'s `/api/latest` shape, verified against the vendor's
    documented response: `{"success": true, "base": "EUR", "rates":
    {"USD": <float>, "INR": <float>, ...}}`. fixer.io's default base
    is EUR, not USD, so this derives INR-per-USD as `INR / USD` rather
    than reading `rates.INR` directly — same precision, just with the
    cross-currency correction the vendor's actual API requires.
    Raises `ValueError` on a missing or non-numeric rate."""
    try:
        rates = body["rates"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Fallback exchange-rate response is missing 'rates': {exc}"
        ) from exc
    usd = rates.get("USD")
    inr = rates.get("INR")
    if usd is None or inr is None:
        raise ValueError(
            f"Fallback exchange-rate response is missing 'USD' or 'INR' "
            f"in 'rates' (USD={usd!r}, INR={inr!r})"
        )
    # Convert both legs to Decimal at the documented quantum before
    # dividing, so the answer inherits the same 4-decimal-place
    # precision as the primary source path.
    usd_decimal = _quantize_rate(usd)
    inr_decimal = _quantize_rate(inr)
    if usd_decimal == 0:
        raise ValueError("Fallback exchange-rate response has USD=0, cannot derive INR/USD")
    return (inr_decimal / usd_decimal).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _fetch_primary() -> Decimal:
    """One real HTTP GET to the primary exchange-rate vendor. Returns
    the parsed, quantized INR/USD rate. Raises on any real failure:
    missing key, network error, non-2xx response, malformed body,
    missing INR rate. The caller (`fetch_exchange_rate`) is responsible
    for deciding whether to fall back to the secondary source on any
    of those — not this function."""
    api_key = get_primary_api_key()
    if not api_key:
        raise MissingExchangeRateAPIKeyError(
            "EXCHANGE_RATE_API_KEY is not set. _fetch_primary requires a real "
            "exchangerate-api.com API key at call time (from the environment or "
            "a .env file at the repo root)."
        )
    url = get_primary_api_url()
    response = requests.get(
        url,
        params={"apikey": api_key},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return _extract_primary_rate(body)


def _fetch_fallback() -> Decimal:
    """One real HTTP GET to the fallback exchange-rate vendor. Returns
    the parsed, quantized INR/USD rate. Raises on any real failure —
    same posture as `_fetch_primary`. The caller treats a fallback
    failure as terminal: both vendors have failed, the rate is not
    fabricable, and the right thing to do is raise the real error."""
    api_key = get_fallback_api_key()
    if not api_key:
        raise MissingExchangeRateAPIKeyError(
            "EXCHANGE_RATE_FALLBACK_API_KEY is not set. _fetch_fallback requires a real "
            "fixer.io API key at call time (from the environment or a .env file at the "
            "repo root)."
        )
    url = get_fallback_api_url()
    response = requests.get(
        url,
        params={"access_key": api_key},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return _extract_fallback_rate(body)


def fetch_exchange_rate(
    infrastructure: Infrastructure | None = None,
) -> Decimal:
    """Returns the real INR/USD exchange rate, fetched fresh from the
    primary source with a one-hour Redis-backed cache, falling back to
    the secondary source on any real primary failure.

    Caching uses `DefaultInfrastructure.cache_get`/`cache_set` directly
    (Redis-backed, JSON-serialized) rather than a bespoke layer — the
    same shared-infrastructure pattern `src/components/c02_data_sources.py`
    already uses for source-document storage. The cached value carries
    a real `fetched_at` timestamp alongside the rate so a downstream
    consumer can audit when the rate was actually captured.

    Returns a `Decimal` quantized to 4 decimal places (matches this
    project's `quantize(Decimal("0.0001"))` pattern from
    `_coerce_quantity_to_decimal`).

    Raises `MissingExchangeRateAPIKeyError` when neither key is
    configured at call time (caller is expected to use a placeholder
    instead — the same selection shape
    `src/alpha_vantage_client.py`'s `get_api_key` enables). Raises
    `ExchangeRateFetchError` when both vendors fail; the original
    exception is chained via `__cause__` so a real debugging
    trace is preserved.

    Every real HTTP request uses a 30-second `timeout`, and the cache
    is keyed `exchange_rate:inr_usd` with a 1-hour TTL — matching the
    acceptance criteria exactly."""
    # Honour the README's "Real-vs-Placeholder seam, never a silent
    # no-op" principle for the case where neither vendor has a key
    # configured at all: don't try either vendor (both will fail with
    # a missing-key error and we'd then raise the more general
    # `ExchangeRateFetchError`), raise the specific, named
    # `MissingExchangeRateAPIKeyError` directly so a caller that
    # genuinely wanted the real fetcher gets an unambiguous signal to
    # use `PlaceholderExchangeRateClient` instead. If at least the
    # fallback key is configured, we still try primary first (and
    # recover via fallback on any real primary failure, missing key
    # included) — only the both-unconfigured case short-circuits.
    primary_key = get_primary_api_key()
    fallback_key = get_fallback_api_key()
    if not primary_key and not fallback_key:
        raise MissingExchangeRateAPIKeyError(
            "Neither EXCHANGE_RATE_API_KEY nor EXCHANGE_RATE_FALLBACK_API_KEY is set. "
            "fetch_exchange_rate requires at least one real exchange-rate vendor key "
            "(from the environment or a .env file at the repo root) — construct "
            "PlaceholderExchangeRateClient instead if no key is configured."
        )

    if infrastructure is None:
        # Lazy: `infrastructure_postgres` pulls in `psycopg`, which is
        # only required at call time when no `Infrastructure` was
        # injected. This keeps the module importable (and the
        # env-key / URL getters usable) without a database driver.
        infrastructure = _default_infrastructure()

    cached = infrastructure.cache_get(_CACHE_KEY)
    if cached is not None:
        rate = _quantize_rate(cached["rate"])
        return rate

    primary_error: Exception | None = None
    try:
        rate = _fetch_primary()
    except (requests.exceptions.RequestException, ValueError, MissingExchangeRateAPIKeyError) as exc:
        primary_error = exc
        _LOGGER.warning(
            "Primary exchange-rate source failed (%s: %s); attempting fallback",
            type(exc).__name__,
            exc,
        )
    else:
        infrastructure.cache_set(
            _CACHE_KEY,
            {"rate": str(rate), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
            _CACHE_TTL_SECONDS,
        )
        return rate

    try:
        rate = _fetch_fallback()
    except (requests.exceptions.RequestException, ValueError, MissingExchangeRateAPIKeyError) as exc:
        _LOGGER.error(
            "Fallback exchange-rate source also failed (%s: %s); primary was: %s",
            type(exc).__name__,
            exc,
            primary_error,
        )
        raise ExchangeRateFetchError(
            "Both exchange-rate sources failed: "
            f"primary ({type(primary_error).__name__}: {primary_error}), "
            f"fallback ({type(exc).__name__}: {exc})"
        ) from exc

    infrastructure.cache_set(
        _CACHE_KEY,
        {"rate": str(rate), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        _CACHE_TTL_SECONDS,
    )
    return rate


class PlaceholderExchangeRateClient:
    """Explicitly NOT a real fetch — mirrors `src/alpha_vantage_client.py`'s
    pattern: when `EXCHANGE_RATE_API_KEY` *and*
    `EXCHANGE_RATE_FALLBACK_API_KEY` are both unconfigured, callers
    (the component that needs an INR/USD rate) construct this
    placeholder rather than `fetch_exchange_rate` fabricating a value.
    Returns `None` from every call — the same "honest, non-cognitive"
    posture `PlaceholderSourceFetcher` already takes for Data &
    Sources (ADR-0027). Never silently invents a rate."""

    def get_rate(self) -> None:
        return None