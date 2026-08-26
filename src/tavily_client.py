"""Tavily client — resolves ADR-0034's corrective-retrieval external
search provider gap (ADR-0047), the same "one small module per external
vendor" shape `src/llm.py`, `src/mem0_embedder.py`, and
`src/alpha_vantage_client.py` already established.

This module is deliberately the *only* place in the codebase that reads
`TAVILY_API_KEY` or talks to Tavily's `/search` endpoint.
`src/components/c05_retrieval_context.py`'s `TavilySearchProvider` calls
`search_tavily` from here rather than reimplementing any part of this.

`TAVILY_API_KEY` is read at call time, inside `get_api_key()` — never at
import time — so this module stays importable, and importing it never
touches the environment or the filesystem.
"""

import os
from pathlib import Path

import requests

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_REQUEST_TIMEOUT_SECONDS = 30
_DEFAULT_MAX_RESULTS = 5
_ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"


class MissingTavilyAPIKeyError(RuntimeError):
    """Raised by `search_tavily` when `TAVILY_API_KEY` is not set in the
    environment at call time. A specific, named exception — the same
    posture `src/llm.py`'s `MissingOpenRouterAPIKeyError` and
    `src/alpha_vantage_client.py`'s `MissingAlphaVantageAPIKeyError`
    already take — so a caller that genuinely wanted the real search
    provider gets an unambiguous signal, rather than a generic error or
    a silent fallback to placeholder-shaped output."""


def _load_dotenv_into_environ() -> None:
    """Manual `.env` parsing, not `python-dotenv` — same reasoning and
    same shape as `src/llm.py`'s and `src/alpha_vantage_client.py`'s own
    `_load_dotenv_into_environ`: the only format this repo's `.env` ever
    needs is single-line `KEY=VALUE`. Never overwrites a variable
    already set in the real process environment."""
    if not _ENV_FILE_PATH.exists():
        return
    for line in _ENV_FILE_PATH.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, _, value = stripped_line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str | None:
    """Reads `TAVILY_API_KEY` from the environment or a `.env` file at
    the repo root, at call time. Returns `None` when it isn't configured
    — callers (`get_external_search_provider`, below) use that `None` to
    decide whether to construct a real provider or fall back to a
    placeholder, the same selection shape `get_reason_fn`/
    `get_source_fetcher` already established."""
    _load_dotenv_into_environ()
    return os.environ.get("TAVILY_API_KEY")


def search_tavily(query: str, max_results: int = _DEFAULT_MAX_RESULTS) -> list[dict]:
    """One real HTTP POST to Tavily's `/search` endpoint. Returns
    Tavily's own `results` list verbatim — a list of dicts, each
    already shaped the way `ExternalSearchProvider.search`'s contract
    expects (`c05_retrieval_context.py`) — deliberately not
    reshaped or reinterpreted here; that's `DefaultCorrectiveRetriever`'s
    job (tagging provenance), not this module's.

    Raises `MissingTavilyAPIKeyError` if no key is configured at call
    time, and raises on a non-2xx response (`raise_for_status`) — both
    real failures, not silently degraded here. The caller
    (`TavilySearchProvider`) decides what, if anything, to do about
    either."""
    api_key = get_api_key()
    if not api_key:
        raise MissingTavilyAPIKeyError(
            "TAVILY_API_KEY is not set. search_tavily requires a real Tavily API key at call "
            "time (from the environment or a .env file at the repo root) — construct the "
            "caller's own placeholder provider instead if no key is configured."
        )
    response = requests.post(
        TAVILY_SEARCH_URL,
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("results", [])
