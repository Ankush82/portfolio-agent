"""Tests for src/tavily_client.py — the shared Tavily provider
`TavilySearchProvider` (c05_retrieval_context.py, ADR-0047) resolves
through.

Almost everything here runs with no network access: `.env` loading,
`get_api_key`'s selection logic, and the missing-key error path are all
pure logic once `requests.post` is mocked out. Real network access is
real usage against a rate-limited free tier, so exactly one test —
`test_live_tavily_search_call` — actually calls Tavily, and it skips
cleanly with a clear reason when `TAVILY_API_KEY` isn't set, the same
pattern `tests/test_llm.py` and `tests/test_alpha_vantage_client.py`
already use.
"""

import os

import pytest
import requests

import tavily_client
from tavily_client import (
    TAVILY_SEARCH_URL,
    MissingTavilyAPIKeyError,
    get_api_key,
    search_tavily,
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


def _no_env_key(monkeypatch) -> None:
    """Same isolation posture as tests/test_llm.py's and
    tests/test_alpha_vantage_client.py's own `_no_env_key` helpers:
    points `_ENV_FILE_PATH` at a file that doesn't exist rather than
    assuming the ambient environment (and this repo's own real `.env`)
    is clean."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(tavily_client, "_ENV_FILE_PATH", tavily_client._ENV_FILE_PATH.parent / "does-not-exist.env")


# --- get_api_key --------------------------------------------------------


def test_get_api_key_returns_none_when_unset(monkeypatch):
    _no_env_key(monkeypatch)

    assert get_api_key() is None


def test_get_api_key_returns_value_from_process_env(monkeypatch):
    _no_env_key(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-real-key")

    assert get_api_key() == "tvly-real-key"


def test_get_api_key_reads_from_dotenv_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('TAVILY_API_KEY="tvly-from-dotenv"\n')
    monkeypatch.setattr(tavily_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    try:
        assert get_api_key() == "tvly-from-dotenv"
    finally:
        os.environ.pop("TAVILY_API_KEY", None)


def test_get_api_key_dotenv_never_overwrites_real_env_var(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API_KEY=tvly-from-dotenv-should-be-ignored\n")
    monkeypatch.setattr(tavily_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-real-process-env")

    assert get_api_key() == "tvly-real-process-env"


# --- search_tavily: missing-key error path, no network ------------------


def test_search_tavily_raises_specific_error_when_key_missing(monkeypatch):
    _no_env_key(monkeypatch)

    with pytest.raises(MissingTavilyAPIKeyError):
        search_tavily("Apple earnings")


# --- search_tavily: HTTP call, mocked -----------------------------------


def test_search_tavily_sends_api_key_query_and_max_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "demo-key")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse({"results": [{"title": "x", "url": "https://example.com", "content": "y"}]})

    monkeypatch.setattr(tavily_client.requests, "post", fake_post)

    results = search_tavily("Apple earnings", max_results=3)

    assert captured["url"] == TAVILY_SEARCH_URL
    assert captured["json"] == {"api_key": "demo-key", "query": "Apple earnings", "max_results": 3}
    assert captured["timeout"] == tavily_client._REQUEST_TIMEOUT_SECONDS
    assert results == [{"title": "x", "url": "https://example.com", "content": "y"}]


def test_search_tavily_returns_empty_list_when_results_key_missing(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "demo-key")
    monkeypatch.setattr(tavily_client.requests, "post", lambda *a, **k: _FakeResponse({}))

    assert search_tavily("obscure query with nothing found") == []


def test_search_tavily_raises_on_http_error_status(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "demo-key")
    monkeypatch.setattr(tavily_client.requests, "post", lambda *a, **k: _FakeResponse({}, status_code=401))

    with pytest.raises(requests.exceptions.HTTPError):
        search_tavily("Apple earnings")


# --- one real, live call -------------------------------------------------


def _tavily_key_available() -> bool:
    tavily_client._load_dotenv_into_environ()
    return bool(os.environ.get("TAVILY_API_KEY"))


requires_tavily_key = pytest.mark.skipif(
    not _tavily_key_available(),
    reason="no TAVILY_API_KEY set in the environment or .env — set a real key for live coverage",
)


@requires_tavily_key
def test_live_tavily_search_call():
    """The one deliberately real, live network test in this suite — a
    single search call against the real Tavily API, so this integration
    is proven to actually work end to end at least once, not just
    against mocks. Skips cleanly (see `requires_tavily_key` above) when
    no real key is configured."""
    results = search_tavily("Apple Inc quarterly earnings", max_results=2)

    assert isinstance(results, list)
    assert len(results) > 0
    assert "url" in results[0]
    assert "content" in results[0]
