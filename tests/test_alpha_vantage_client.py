"""Tests for src/alpha_vantage_client.py — the shared Alpha Vantage
provider `AlphaVantageSourceFetcher` (c02_data_sources.py, ADR-0046)
resolves through.

Almost everything here runs with no network access: `.env` loading,
`get_api_key`'s selection logic, and the missing-key error path are all
pure logic once `requests.get` is mocked out. Real network access is
real money on a rate-limited free tier, so exactly one test —
`test_live_alpha_vantage_call_global_quote` — actually calls Alpha
Vantage, and it skips cleanly with a clear reason when
`ALPHA_VANTAGE_API_KEY` isn't set, the same pattern `tests/test_llm.py`
and `tests/test_infrastructure_postgres.py` already use.
"""

import json
import os

import pytest
import requests

import alpha_vantage_client
from alpha_vantage_client import (
    ALPHA_VANTAGE_BASE_URL,
    MissingAlphaVantageAPIKeyError,
    fetch_alpha_vantage,
    get_api_key,
)


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"fake status {self.status_code}")


def _no_env_key(monkeypatch) -> None:
    """Same isolation posture as tests/test_llm.py's own `_no_env_key`:
    points `_ENV_FILE_PATH` at a file that doesn't exist rather than
    assuming the ambient environment (and this repo's own real `.env`)
    is clean."""
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.setattr(alpha_vantage_client, "_ENV_FILE_PATH", alpha_vantage_client._ENV_FILE_PATH.parent / "does-not-exist.env")


# --- get_api_key --------------------------------------------------------


def test_get_api_key_returns_none_when_unset(monkeypatch):
    _no_env_key(monkeypatch)

    assert get_api_key() is None


def test_get_api_key_returns_value_from_process_env(monkeypatch):
    _no_env_key(monkeypatch)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "sk-real-key")

    assert get_api_key() == "sk-real-key"


def test_get_api_key_reads_from_dotenv_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('ALPHA_VANTAGE_API_KEY="sk-from-dotenv"\n')
    monkeypatch.setattr(alpha_vantage_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)

    try:
        assert get_api_key() == "sk-from-dotenv"
    finally:
        os.environ.pop("ALPHA_VANTAGE_API_KEY", None)


def test_get_api_key_dotenv_never_overwrites_real_env_var(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ALPHA_VANTAGE_API_KEY=sk-from-dotenv-should-be-ignored\n")
    monkeypatch.setattr(alpha_vantage_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "sk-real-process-env")

    assert get_api_key() == "sk-real-process-env"


# --- fetch_alpha_vantage: missing-key error path, no network -----------


def test_fetch_alpha_vantage_raises_specific_error_when_key_missing(monkeypatch):
    _no_env_key(monkeypatch)

    with pytest.raises(MissingAlphaVantageAPIKeyError):
        fetch_alpha_vantage("GLOBAL_QUOTE", symbol="AAPL")


# --- fetch_alpha_vantage: HTTP call, mocked -----------------------------


def test_fetch_alpha_vantage_sends_function_symbol_and_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "demo-key")
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(alpha_vantage_client.requests, "get", fake_get)

    content = fetch_alpha_vantage("GLOBAL_QUOTE", symbol="AAPL")

    assert content == b'{"ok": true}'
    assert captured["url"] == ALPHA_VANTAGE_BASE_URL
    assert captured["params"] == {"function": "GLOBAL_QUOTE", "apikey": "demo-key", "symbol": "AAPL"}
    assert captured["timeout"] == alpha_vantage_client._REQUEST_TIMEOUT_SECONDS


def test_fetch_alpha_vantage_raises_on_http_error_status(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "demo-key")
    monkeypatch.setattr(alpha_vantage_client.requests, "get", lambda *a, **k: _FakeResponse(b"", status_code=429))

    with pytest.raises(requests.exceptions.HTTPError):
        fetch_alpha_vantage("GLOBAL_QUOTE", symbol="AAPL")


# --- one real, live call -------------------------------------------------


def _alpha_vantage_key_available() -> bool:
    alpha_vantage_client._load_dotenv_into_environ()
    return bool(os.environ.get("ALPHA_VANTAGE_API_KEY"))


requires_alpha_vantage_key = pytest.mark.skipif(
    not _alpha_vantage_key_available(),
    reason="no ALPHA_VANTAGE_API_KEY set in the environment or .env — set a real key for live coverage",
)


@requires_alpha_vantage_key
def test_live_alpha_vantage_call_global_quote():
    """The one deliberately real, live network test in this suite — a
    single GLOBAL_QUOTE call against the real Alpha Vantage API, so
    this integration is proven to actually work end to end at least
    once, not just against mocks. Skips cleanly (see
    `requires_alpha_vantage_key` above) when no real key is configured."""
    content = fetch_alpha_vantage("GLOBAL_QUOTE", symbol="AAPL")

    parsed = json.loads(content)
    assert "Global Quote" in parsed
    assert parsed["Global Quote"]["01. symbol"] == "AAPL"
