"""Tests for src/upstox_config.py."""

import os

import pytest

from upstox_config import BrokerConfigError, UpstoxConfig


def test_upstox_config_from_env_returns_populated_when_all_set(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/callback")

    config = UpstoxConfig.from_env()

    assert config.client_id == "test_client_id"
    assert config.client_secret == "test_client_secret"
    assert config.redirect_uri == "https://example.com/callback"


def test_upstox_config_from_env_raises_when_client_id_missing(monkeypatch):
    monkeypatch.delenv("UPSTOX_CLIENT_ID", raising=False)
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/callback")

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    assert "UPSTOX_CLIENT_ID" in str(excinfo.value)
    assert "UPSTOX_CLIENT_SECRET" not in str(excinfo.value)
    assert "UPSTOX_REDIRECT_URI" not in str(excinfo.value)
    assert "register an app at https://upstox.com/developer/api-documentation/" in str(excinfo.value)
    assert "set these variables in your .env file" in str(excinfo.value)


def test_upstox_config_from_env_raises_when_client_secret_missing(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.delenv("UPSTOX_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/callback")

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    assert "UPSTOX_CLIENT_SECRET" in str(excinfo.value)
    assert "UPSTOX_CLIENT_ID" not in str(excinfo.value)
    assert "UPSTOX_REDIRECT_URI" not in str(excinfo.value)
    assert "register an app at https://upstox.com/developer/api-documentation/" in str(excinfo.value)
    assert "set these variables in your .env file" in str(excinfo.value)


def test_upstox_config_from_env_raises_when_redirect_uri_missing(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret")
    monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    assert "UPSTOX_REDIRECT_URI" in str(excinfo.value)
    assert "UPSTOX_CLIENT_ID" not in str(excinfo.value)
    assert "UPSTOX_CLIENT_SECRET" not in str(excinfo.value)
    assert "register an app at https://upstox.com/developer/api-documentation/" in str(excinfo.value)
    assert "set these variables in your .env file" in str(excinfo.value)


def test_upstox_config_from_env_raises_when_multiple_missing(monkeypatch):
    monkeypatch.delenv("UPSTOX_CLIENT_ID", raising=False)
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret")
    monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    error_msg = str(excinfo.value)
    assert "UPSTOX_CLIENT_ID" in error_msg
    assert "UPSTOX_REDIRECT_URI" in error_msg
    assert "UPSTOX_CLIENT_SECRET" not in error_msg
    assert "register an app at https://upstox.com/developer/api-documentation/" in error_msg
    assert "set these variables in your .env file" in error_msg


def test_upstox_config_from_env_raises_when_client_id_blank(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "   ")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/callback")

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    assert "UPSTOX_CLIENT_ID" in str(excinfo.value)
    assert "UPSTOX_CLIENT_SECRET" not in str(excinfo.value)
    assert "UPSTOX_REDIRECT_URI" not in str(excinfo.value)


def test_upstox_config_from_env_raises_when_client_secret_blank(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "   \t\n   ")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/callback")

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    assert "UPSTOX_CLIENT_SECRET" in str(excinfo.value)
    assert "UPSTOX_CLIENT_ID" not in str(excinfo.value)
    assert "UPSTOX_REDIRECT_URI" not in str(excinfo.value)


def test_upstox_config_from_env_raises_when_redirect_uri_blank(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "   ")

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    assert "UPSTOX_REDIRECT_URI" in str(excinfo.value)
    assert "UPSTOX_CLIENT_ID" not in str(excinfo.value)
    assert "UPSTOX_CLIENT_SECRET" not in str(excinfo.value)


def test_upstox_config_from_env_strips_whitespace(monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "  test_id  ")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "  test_secret  ")
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "  https://example.com/callback  ")

    config = UpstoxConfig.from_env()

    assert config.client_id == "test_id"
    assert config.client_secret == "test_secret"
    assert config.redirect_uri == "https://example.com/callback"


def test_upstox_config_from_env_error_message_no_secrets(monkeypatch):
    monkeypatch.delenv("UPSTOX_CLIENT_ID", raising=False)
    monkeypatch.delenv("UPSTOX_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "https://example.com/callback")

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    error_msg = str(excinfo.value)
    # Ensure no secret values appear in the error message
    assert "test_secret" not in error_msg
    assert "test_id" not in error_msg
    assert "https://example.com/callback" not in error_msg


def test_upstox_config_from_env_raises_when_all_three_missing(monkeypatch):
    monkeypatch.delenv("UPSTOX_CLIENT_ID", raising=False)
    monkeypatch.delenv("UPSTOX_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)

    with pytest.raises(BrokerConfigError) as excinfo:
        UpstoxConfig.from_env()

    error_msg = str(excinfo.value)
    # Check that all three variable names are in the error message
    assert "UPSTOX_CLIENT_ID" in error_msg
    assert "UPSTOX_CLIENT_SECRET" in error_msg
    assert "UPSTOX_REDIRECT_URI" in error_msg
    # Check the instruction part
    assert "register an app at https://upstox.com/developer/api-documentation/" in error_msg
    assert "set these variables in your .env file" in error_msg