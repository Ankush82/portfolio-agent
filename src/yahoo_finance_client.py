"""Yahoo Finance client — resolves the same "one small module per
external vendor" slice `src/alpha_vantage_client.py` already established
for Alpha Vantage, for the NSE/BSE market-data path (`RELIANCE.NS`,
`TCS.BO`, ...). Mirrored from `src/alpha_vantage_client.py`'s shape and
docstring conventions on purpose, so the two vendor clients read
identically:

  - a module-level `YAHOO_FINANCE_BASE_URL` constant,
  - a `_REQUEST_TIMEOUT_SECONDS` constant,
  - a single public fetch function that returns the parsed result, and
  - a clear, named, module-specific exception class for failures.

Yahoo Finance's public `query1.finance.yahoo.com` chart endpoint
(verified live against `RELIANCE.NS`) does not require an API key —
unlike Alpha Vantage — so this module has no `get_api_key` /
`MissingYahooFinanceAPIKeyError` pair: there is no key to load. The
docstring on `fetch_yahoo_finance_quote` calls that contrast out
explicitly.

Symbols are passed in already carrying `.NS` / `.BO` — this project's
`validate_stock_symbol` (`src/components/c01_user_portfolio.py`) is the
single, canonical enforcement point for that format, and this client
deliberately does not re-validate it. Anything this client gets from a
caller is used verbatim as the URL path segment.

This module is the *only* place in the codebase that talks to Yahoo
Finance's chart endpoint.

STORY-12: real exponential-backoff retry (1s, 2s, 4s -- 3 retries,
total possible wait = 7s) on rate-limit (HTTP 429) and
`requests.exceptions.Timeout`, delegated to
`src/api_error_logging.http_get_with_retry`. Invalid-symbol errors
(yielded by Yahoo Finance as `meta.currency is None` or as HTTP 404)
surface a specific `YahooFinanceError` message that names both the
symbol and the exchange (`.NS` → `NSE`, `.BO` → `BSE`), never a
generic "API error". Every API failure is logged via the stdlib
`logging` module under the `YAHOO_*` error-code taxonomy; the real
underlying exception is preserved on the raised `YahooFinanceError`
via `__cause__` so debugging has the real trace.
"""

import requests

from api_error_logging import get_logger, http_get_with_retry

YAHOO_FINANCE_BASE_URL = "https://query1.finance.yahoo.com"

_REQUEST_TIMEOUT_SECONDS = 5

_CHART_PATH_TEMPLATE = "/v8/finance/chart/{symbol}"

_LOGGER = get_logger(__name__)

# Mapping from `.NS` / `.BO` symbol suffixes to their exchange names,
# used to make invalid-symbol error messages specific to the symbol
# AND exchange (the AC's "specific message string that includes both
# the symbol and exchange"), without re-validating the symbol format
# upstream of this client.
_SYMBOL_SUFFIX_TO_EXCHANGE: dict[str, str] = {
    ".NS": "NSE",
    ".BO": "BSE",
}


def _derive_exchange_from_symbol(symbol: str) -> str:
    """Returns the canonical exchange name for a symbol's `.NS` /
    `.BO` suffix, or an empty string if the suffix is unrecognised.
    Used purely to enrich error messages — this client still uses
    `symbol` verbatim as the URL path segment and does not enforce
    the suffix format itself."""
    for suffix, exchange in _SYMBOL_SUFFIX_TO_EXCHANGE.items():
        if symbol.endswith(suffix):
            return exchange
    return ""


class YahooFinanceError(RuntimeError):
    """Raised by `fetch_yahoo_finance_quote` on a real HTTP or parse
    failure — non-2xx response, malformed JSON body, missing required
    fields, missing price for the requested symbol, etc. A specific,
    named exception class mirroring `src/alpha_vantage_client.py`'s
    `MissingAlphaVantageAPIKeyError` posture, so callers that wanted
    the real fetcher get an unambiguous signal instead of a generic
    exception or a silently fabricated fallback value. The caller
    decides what, if anything, to do about the failure.

    STORY-12: the exception's message is intentionally user-friendly
    (no raw stack trace, no vendor JSON dumped into the message).
    Where the failure has a known shape (invalid symbol, missing
    fields, HTTP 404), the message names both the requested symbol
    and the exchange. The real underlying exception is chained via
    `__cause__` for debugging."""


def fetch_yahoo_finance_quote(symbol: str) -> dict:
    """One real HTTP GET to Yahoo Finance's public chart endpoint for
    `symbol`, returning the parsed daily-quote fields the rest of this
    codebase needs. No API key required (verified live against
    `https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`)
    — this is the documented contrast with `fetch_alpha_vantage`'s
    `ALPHA_VANTAGE_API_KEY` requirement.

    `symbol` is used verbatim as the URL path segment — the caller is
    responsible for any `validate_stock_symbol`-style format check
    (this project's own `validate_stock_symbol` enforces the `.NS` /
    `.BO` suffix upstream of this module).

    Returns a real `dict` of real values parsed from the live JSON
    body, against the *actual* `chart.result[0].meta` keys Yahoo
    Finance's chart endpoint returns (verified live, not assumed):

      - `current_price`         — `meta.regularMarketPrice`
      - `previous_close`        — `meta.previousClose`
                                  (also surfaced as `meta.chartPreviousClose`)
      - `day_high`              — `meta.regularMarketDayHigh`
      - `day_low`               — `meta.regularMarketDayLow`
      - `volume`                — `meta.regularMarketVolume`
      - `fifty_two_week_high`   — `meta.fiftyTwoWeekHigh`
      - `fifty_two_week_low`    — `meta.fiftyTwoWeekLow`
      - `currency`              — `meta.currency`
                                  (e.g. `"INR"` for `RELIANCE.NS`,
                                  verified live — this client does
                                  not assume INR or any other currency)
      - `exchange_name`         — `meta.fullExchangeName`
                                  (e.g. `"NSE"`, verified live)
      - `symbol`                — `meta.symbol`
                                  (echoed back from the live response)
      - `market_cap`            — `None` if absent. The public chart
                                  endpoint does not return a market-cap
                                  field (verified live by walking the
                                  real JSON tree for any
                                  `marketCap` / `market_cap` key —
                                  none exists), so this client surfaces
                                  that absence honestly as `None`
                                  rather than fabricating a value.
      - `tradeable`             — `meta.tradeable` when present
                                  (the real `True` / `False` flag
                                  Yahoo Finance surfaces for a
                                  tradable / delisted-or-suspended
                                  symbol, verified live on real
                                  responses), `None` when absent.
                                  STORY-13: the public chart endpoint
                                  only sometimes includes
                                  `meta.tradeable`; this client
                                  surfaces it honestly as
                                  `True`/`False`/`None` rather than
                                  fabricating a value — `None` is
                                  the signal that the caller should
                                  skip delisting detection for that
                                  symbol without erroring.

    Raises `YahooFinanceError` on a real failure:
      - non-2xx HTTP response (`raise_for_status`),
      - JSON body that does not parse,
      - JSON body whose `chart.result` is empty or whose
        `result[0].meta` is missing `currency` / `regularMarketPrice`
        (the real, observed signal Yahoo Finance returns for an
        unrecognised or non-equity symbol, verified live),
      - any other unexpected JSON shape.

    Every real request uses a 5-second `timeout` — matching the
    acceptance criterion exactly, not the longer timeout
    `fetch_alpha_vantage` uses (Alpha Vantage's free tier is
    noticeably slower)."""
    url = YAHOO_FINANCE_BASE_URL + _CHART_PATH_TEMPLATE.format(symbol=symbol)
    # Derive the exchange label once so every error path can include
    # BOTH the symbol and the exchange (AC1) without each branch
    # recomputing it. Falls back to "(unknown)" for any suffix the
    # helper doesn't recognise -- the message is still specific to
    # the symbol, just honestly so.
    exchange_label = _derive_exchange_from_symbol(symbol) or "(unknown)"

    # The single real HTTP GET, with genuine exponential-backoff
    # retry (1s, 2s, 4s -- 3 retries, total possible wait = 7s) on
    # HTTP 429 and `requests.exceptions.Timeout`. The helper raises
    # `Timeout` / `HTTPError` with a user-friendly message (no raw
    # stack trace, no vendor JSON) after retries are exhausted, and
    # logs the real underlying error under the `YAHOO_*` error-code
    # taxonomy. Other failures (e.g. 404 invalid symbol, 5xx) are
    # returned normally and surfaced by the existing error-handling
    # branches below -- the AC scopes retry to 429/Timeout only.
    try:
        response = http_get_with_retry(
            logger=_LOGGER,
            url=url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
            error_code_prefix="YAHOO",
            extra_fields={"symbol": symbol, "exchange": exchange_label},
        )
        response.raise_for_status()
        body = response.json()
    except requests.exceptions.HTTPError as exc:
        # 404 / other non-2xx responses are real invalid-symbol (or
        # real upstream) failures -- the user-friendly message names
        # BOTH the symbol and the exchange, and never leaks the raw
        # response body or the chained exception's text. The real
        # underlying exception stays available via `__cause__` for
        # debugging.
        _LOGGER.error(
            "Yahoo Finance chart endpoint returned a non-2xx response "
            "for symbol %r on exchange %s.",
            symbol, exchange_label,
            extra={
                "error_code": "YAHOO_HTTP_ERROR",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance does not recognise symbol {symbol!r} on "
            f"{exchange_label} (upstream returned a non-2xx response). "
            f"Please verify the symbol and try again."
        ) from exc
    except requests.exceptions.Timeout as exc:
        # The retry helper already raised a user-friendly Timeout
        # with chained `__cause__`; surface it as the same named
        # YahooFinanceError so callers only need to catch one type,
        # and so the message stays specific to this client
        # (symbol + exchange) rather than generic.
        raise YahooFinanceError(
            f"Yahoo Finance did not respond for symbol {symbol!r} on "
            f"{exchange_label} within {_REQUEST_TIMEOUT_SECONDS}s after "
            f"multiple retries. Please try again shortly."
        ) from exc
    except requests.exceptions.JSONDecodeError as exc:
        _LOGGER.error(
            "Yahoo Finance chart endpoint returned a non-JSON body for "
            "symbol %r on exchange %s.",
            symbol, exchange_label,
            extra={
                "error_code": "YAHOO_PARSE_ERROR",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance returned an unexpected response body for "
            f"symbol {symbol!r} on {exchange_label}. Please try again shortly."
        ) from exc
    except requests.exceptions.RequestException as exc:
        _LOGGER.error(
            "Yahoo Finance chart endpoint request failed for symbol %r "
            "on exchange %s.",
            symbol, exchange_label,
            extra={
                "error_code": "YAHOO_HTTP_ERROR",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance request failed for symbol {symbol!r} on "
            f"{exchange_label}. Please try again shortly."
        ) from exc

    # The real shape, verified live: {"chart": {"result": [...], "error": null|...}}.
    try:
        chart = body["chart"]
        error_payload = chart.get("error")
        if error_payload:
            raise YahooFinanceError(
                f"Yahoo Finance rejected symbol {symbol!r} on "
                f"{exchange_label} (vendor returned an error payload). "
                f"Please verify the symbol and try again."
            )
        results = chart["result"]
    except (KeyError, TypeError) as exc:
        _LOGGER.error(
            "Yahoo Finance chart response for symbol %r on exchange %s "
            "is missing the expected 'chart.result' structure.",
            symbol, exchange_label,
            extra={
                "error_code": "YAHOO_PARSE_ERROR",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance returned an unexpected response shape for "
            f"symbol {symbol!r} on {exchange_label}. Please try again shortly."
        ) from exc

    if not results:
        _LOGGER.error(
            "Yahoo Finance chart response for symbol %r on exchange %s "
            "has an empty 'chart.result' array -- symbol is not recognised.",
            symbol, exchange_label,
            extra={
                "error_code": "YAHOO_INVALID_SYMBOL",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance does not recognise symbol {symbol!r} on "
            f"{exchange_label} (empty result). Please verify the symbol "
            f"and try again."
        )

    result = results[0]
    try:
        meta = result["meta"]
    except (KeyError, TypeError) as exc:
        _LOGGER.error(
            "Yahoo Finance chart response for symbol %r on exchange %s "
            "is missing the expected 'chart.result[0].meta' object.",
            symbol, exchange_label,
            extra={
                "error_code": "YAHOO_PARSE_ERROR",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance returned an unexpected response shape for "
            f"symbol {symbol!r} on {exchange_label}. Please try again shortly."
        ) from exc

    # The real signal Yahoo Finance returns for an unrecognised or
    # non-equity symbol is `meta.currency is None` and no price fields
    # (verified live against an invalid symbol). Refuse to fabricate a
    # price in that case; the message still names both symbol and
    # exchange (AC1).
    if meta.get("currency") is None or "regularMarketPrice" not in meta:
        _LOGGER.error(
            "Yahoo Finance chart response for symbol %r on exchange %s "
            "has no usable quote data (currency=%r, regularMarketPrice "
            "present=%s).",
            symbol, exchange_label,
            meta.get("currency"),
            "regularMarketPrice" in meta,
            extra={
                "error_code": "YAHOO_INVALID_SYMBOL",
                "symbol": symbol,
                "exchange": exchange_label,
            },
        )
        raise YahooFinanceError(
            f"Yahoo Finance does not recognise symbol {symbol!r} on "
            f"{exchange_label} (no usable quote data returned). "
            f"Please verify the symbol and try again."
        )

    return {
        "symbol": meta.get("symbol"),
        "exchange_name": meta.get("fullExchangeName"),
        "currency": meta.get("currency"),
        "current_price": meta.get("regularMarketPrice"),
        "previous_close": meta.get("previousClose"),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
        "market_cap": None,  # documented absence — see module docstring
        # STORY-13: meta.tradeable is sometimes present in the live
        # response (True = still tradable, False = delisted /
        # suspended), sometimes absent. Surface it honestly:
        # True/False when the API gave it, None when the API didn't —
        # never fabricate a value either way.
        "tradeable": meta.get("tradeable"),
    }