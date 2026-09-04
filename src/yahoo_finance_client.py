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
"""

import requests

YAHOO_FINANCE_BASE_URL = "https://query1.finance.yahoo.com"

_REQUEST_TIMEOUT_SECONDS = 5

_CHART_PATH_TEMPLATE = "/v8/finance/chart/{symbol}"


class YahooFinanceError(RuntimeError):
    """Raised by `fetch_yahoo_finance_quote` on a real HTTP or parse
    failure — non-2xx response, malformed JSON body, missing required
    fields, missing price for the requested symbol, etc. A specific,
    named exception class mirroring `src/alpha_vantage_client.py`'s
    `MissingAlphaVantageAPIKeyError` posture, so callers that wanted
    the real fetcher get an unambiguous signal instead of a generic
    exception or a silently fabricated fallback value. The caller
    decides what, if anything, to do about the failure."""


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
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except requests.exceptions.HTTPError as exc:
        raise YahooFinanceError(
            f"Yahoo Finance chart endpoint returned a non-2xx response for "
            f"symbol {symbol!r}: {exc}"
        ) from exc
    except requests.exceptions.JSONDecodeError as exc:
        raise YahooFinanceError(
            f"Yahoo Finance chart endpoint returned a non-JSON body for "
            f"symbol {symbol!r}: {exc}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise YahooFinanceError(
            f"Yahoo Finance chart endpoint request failed for symbol "
            f"{symbol!r}: {exc}"
        ) from exc

    # The real shape, verified live: {"chart": {"result": [...], "error": null|...}}.
    try:
        chart = body["chart"]
        error_payload = chart.get("error")
        if error_payload:
            raise YahooFinanceError(
                f"Yahoo Finance chart endpoint returned an error payload for "
                f"symbol {symbol!r}: {error_payload}"
            )
        results = chart["result"]
    except (KeyError, TypeError) as exc:
        raise YahooFinanceError(
            f"Yahoo Finance chart response for symbol {symbol!r} is missing "
            f"the expected 'chart.result' structure: {exc}"
        ) from exc

    if not results:
        raise YahooFinanceError(
            f"Yahoo Finance chart response for symbol {symbol!r} has an empty "
            f"'chart.result' array."
        )

    result = results[0]
    try:
        meta = result["meta"]
    except (KeyError, TypeError) as exc:
        raise YahooFinanceError(
            f"Yahoo Finance chart response for symbol {symbol!r} is missing "
            f"the expected 'chart.result[0].meta' object: {exc}"
        ) from exc

    # The real signal Yahoo Finance returns for an unrecognised or
    # non-equity symbol is `meta.currency is None` and no price fields
    # (verified live against an invalid symbol). Refuse to fabricate a
    # price in that case.
    if meta.get("currency") is None or "regularMarketPrice" not in meta:
        raise YahooFinanceError(
            f"Yahoo Finance chart response for symbol {symbol!r} has no usable "
            f"quote data (currency={meta.get('currency')!r}, "
            f"regularMarketPrice present={'regularMarketPrice' in meta})."
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
    }