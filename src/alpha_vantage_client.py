"""Alpha Vantage client — resolves the MARKET_DATA/NEWS/EARNINGS slice of
ADR-0027's data-fetch-provider gap (ADR-0046), the same "one small module
per external vendor" shape `src/llm.py` and `src/mem0_embedder.py` already
established for OpenRouter and mem0ai's fastembed embedder respectively.

This module is deliberately the *only* place in the codebase that reads
`ALPHA_VANTAGE_API_KEY` or talks to Alpha Vantage's `query` endpoint.
`src/components/c02_data_sources.py`'s `AlphaVantageSourceFetcher` calls
`fetch_alpha_vantage` from here rather than reimplementing any part of
this — the same split `c10_agent_runtime.py`/`c08_analysis_reasoning.py`
already use with `src/llm.py`.

`ALPHA_VANTAGE_API_KEY` is read at call time, inside `get_api_key()` —
never at import time — so this module stays importable, and importing it
never touches the environment or the filesystem.
"""

import os
from pathlib import Path

import requests

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"

_REQUEST_TIMEOUT_SECONDS = 30
_ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"


class MissingAlphaVantageAPIKeyError(RuntimeError):
    """Raised by `fetch_alpha_vantage` when `ALPHA_VANTAGE_API_KEY` is not
    set in the environment at call time. A specific, named exception —
    the same posture `src/llm.py`'s `MissingOpenRouterAPIKeyError` already
    takes — so a caller that genuinely wanted the real fetcher gets an
    unambiguous signal, rather than a generic error or a silent fallback
    to placeholder-shaped output."""


def _load_dotenv_into_environ() -> None:
    """Manual `.env` parsing, not `python-dotenv` — same reasoning and
    same shape as `src/llm.py`'s own `_load_dotenv_into_environ`: the
    only format this repo's `.env` ever needs is single-line
    `KEY=VALUE`, so a dependency for that is not worth adding. Never
    overwrites a variable already set in the real process environment."""
    if not _ENV_FILE_PATH.exists():
        return
    for line in _ENV_FILE_PATH.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, _, value = stripped_line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str | None:
    """Reads `ALPHA_VANTAGE_API_KEY` from the environment or a `.env`
    file at the repo root, at call time. Returns `None` when it isn't
    configured — callers (`get_source_fetcher`, below) use that `None`
    to decide whether to construct a real fetcher or fall back to a
    placeholder, the same selection shape `src/llm.py`'s `get_reason_fn`
    already established."""
    _load_dotenv_into_environ()
    return os.environ.get("ALPHA_VANTAGE_API_KEY")


def fetch_alpha_vantage(function: str, **params: str) -> bytes:
    """One real HTTP GET call to Alpha Vantage's `query` endpoint.
    `function` is Alpha Vantage's own endpoint-selector parameter (e.g.
    `"GLOBAL_QUOTE"`, `"NEWS_SENTIMENT"`, `"EARNINGS"`); `**params` are
    that function's other real parameters (e.g. `symbol="AAPL"`).

    Returns the raw response body as bytes — deliberately not parsed or
    interpreted here. `SourceDocument.content` (`c02_data_sources.py`)
    is raw bytes by contract, and structurally parsing fetched content
    is Data Processing & Quality's job (component 03, ADR-0032), not
    this module's — this function's only job is getting real bytes for
    a real source.

    Raises `MissingAlphaVantageAPIKeyError` if no key is configured at
    call time, and raises on a non-2xx response (`raise_for_status`) —
    both real failures, not silently degraded here. The caller
    (`AlphaVantageSourceFetcher`) decides what, if anything, to do about
    either."""
    api_key = get_api_key()
    if not api_key:
        raise MissingAlphaVantageAPIKeyError(
            "ALPHA_VANTAGE_API_KEY is not set. fetch_alpha_vantage requires a real Alpha "
            "Vantage API key at call time (from the environment or a .env file at the repo "
            "root) — construct the caller's own placeholder fetcher instead if no key is "
            "configured."
        )
    response = requests.get(
        ALPHA_VANTAGE_BASE_URL,
        params={"function": function, "apikey": api_key, **params},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.content
