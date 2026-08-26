"""Tests for src/resend_client.py — the shared Resend provider
`ResendNotificationChannel` (c13_interaction_notification.py, ADR-0048)
resolves through.

Deliberately no live-network test here, unlike tests/test_llm.py,
tests/test_alpha_vantage_client.py, and tests/test_tavily_client.py:
those each make one real, read-only (or side-effect-free) call. Sending
a real email is not side-effect-free — it lands in a real inbox — so a
"runs on every `pytest tests/`" live test here would send a real email
every time the suite runs, indefinitely. Real end-to-end delivery was
verified once, manually, outside the checked-in test suite (see
ADR-0048's Consequences); everything below runs with no network access,
`requests.post` mocked out.
"""

import os

import pytest
import requests

import resend_client
from resend_client import (
    DEFAULT_FROM_ADDRESS,
    RESEND_SEND_URL,
    MissingResendAPIKeyError,
    get_api_key,
    send_email,
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
    """Same isolation posture as every other vendor module's own
    `_no_env_key` helper: points `_ENV_FILE_PATH` at a file that
    doesn't exist rather than assuming the ambient environment (and
    this repo's own real `.env`) is clean."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(resend_client, "_ENV_FILE_PATH", resend_client._ENV_FILE_PATH.parent / "does-not-exist.env")


# --- get_api_key --------------------------------------------------------


def test_get_api_key_returns_none_when_unset(monkeypatch):
    _no_env_key(monkeypatch)

    assert get_api_key() is None


def test_get_api_key_returns_value_from_process_env(monkeypatch):
    _no_env_key(monkeypatch)
    monkeypatch.setenv("RESEND_API_KEY", "re_real_key")

    assert get_api_key() == "re_real_key"


def test_get_api_key_reads_from_dotenv_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('RESEND_API_KEY="re_from_dotenv"\n')
    monkeypatch.setattr(resend_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    try:
        assert get_api_key() == "re_from_dotenv"
    finally:
        os.environ.pop("RESEND_API_KEY", None)


def test_get_api_key_dotenv_never_overwrites_real_env_var(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("RESEND_API_KEY=re_from_dotenv_should_be_ignored\n")
    monkeypatch.setattr(resend_client, "_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("RESEND_API_KEY", "re_real_process_env")

    assert get_api_key() == "re_real_process_env"


# --- send_email: missing-key error path, no network ----------------------


def test_send_email_raises_specific_error_when_key_missing(monkeypatch):
    _no_env_key(monkeypatch)

    with pytest.raises(MissingResendAPIKeyError):
        send_email(to="user@example.com", subject="s", text="t")


# --- send_email: HTTP call, mocked ---------------------------------------


def test_send_email_sends_bearer_auth_default_from_and_body(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_demo_key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse({"id": "msg-123"})

    monkeypatch.setattr(resend_client.requests, "post", fake_post)

    result = send_email(to="user@example.com", subject="Alert", text="AAPL beat earnings")

    assert captured["url"] == RESEND_SEND_URL
    assert captured["headers"]["Authorization"] == "Bearer re_demo_key"
    assert captured["json"] == {
        "from": DEFAULT_FROM_ADDRESS,
        "to": ["user@example.com"],
        "subject": "Alert",
        "text": "AAPL beat earnings",
    }
    assert result == {"id": "msg-123"}


def test_send_email_accepts_a_custom_from_address(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_demo_key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse({"id": "msg-123"})

    monkeypatch.setattr(resend_client.requests, "post", fake_post)

    send_email(to="user@example.com", subject="s", text="t", from_address="alerts@myportfolioagent.dev")

    assert captured["json"]["from"] == "alerts@myportfolioagent.dev"


def test_send_email_raises_on_http_error_status(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_demo_key")
    monkeypatch.setattr(resend_client.requests, "post", lambda *a, **k: _FakeResponse({}, status_code=422))

    with pytest.raises(requests.exceptions.HTTPError):
        send_email(to="user@example.com", subject="s", text="t")
