"""Tests for src/oauth_state (STORY-12)."""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from unittest import mock

import psycopg
import pytest

# ---------------------------------------------------------------------------
# Ensure broker_token_crypto has a key before infrastructure_postgres imports it
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "BROKER_TOKEN_ENCRYPTION_KEY",
    secrets.token_urlsafe(32),
)

from src.oauth_state import (
    configure,
    issue_state,
    consume_state,
    purge_expired_states,
    InvalidOAuthStateError,
    OAuthStateReplayError,
    OAuthStateExpiredError,
    _utc_now,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def _oauth_dsn(postgres_dsn: str) -> str:
    """Configure the module with a clean (truncated) oauth_states table."""
    configure(postgres_dsn)
    conn = psycopg.connect(postgres_dsn, autocommit=True)
    with conn.cursor() as cursor:
        cursor.execute("TRUNCATE TABLE oauth_states RESTART IDENTITY CASCADE")
    conn.close()
    yield postgres_dsn


# ---------------------------------------------------------------------------
# Tests: issue_state
# ---------------------------------------------------------------------------

class TestIssueState:
    def test_returns_urlsafe_string_of_at_least_32_bytes(self, _oauth_dsn):
        state = issue_state("u1", "broker_a")
        # secrets.token_urlsafe(32) produces a 43-char string
        assert len(state) >= 43
        assert re.fullmatch(r"[A-Za-z0-9_-]+", state)

    def test_two_calls_never_return_the_same_value(self, _oauth_dsn):
        s1 = issue_state("u1", "broker_a")
        s2 = issue_state("u1", "broker_a")
        assert s1 != s2

    def test_returns_distinct_tokens_for_different_users(self, _oauth_dsn):
        s1 = issue_state("u1", "broker_a")
        s2 = issue_state("u2", "broker_a")
        assert s1 != s2

    def test_returns_distinct_tokens_for_different_brokers(self, _oauth_dsn):
        s1 = issue_state("u1", "broker_a")
        s2 = issue_state("u1", "broker_b")
        assert s1 != s2


# ---------------------------------------------------------------------------
# Tests: consume_state
# ---------------------------------------------------------------------------

class TestConsumeState:
    def test_fresh_state_returns_user_id_and_broker_id(self, _oauth_dsn):
        state = issue_state("uid_42", "broker_xyz")
        user_id, broker_id = consume_state(state)
        assert user_id == "uid_42"
        assert broker_id == "broker_xyz"

    def test_second_consume_raises_replay_error(self, _oauth_dsn):
        state = issue_state("u1", "broker_a")
        consume_state(state)
        with pytest.raises(OAuthStateReplayError):
            consume_state(state)

    def test_unknown_state_raises_invalid_error(self, _oauth_dsn):
        fake_state = secrets.token_urlsafe(32)
        with pytest.raises(InvalidOAuthStateError):
            consume_state(fake_state)

    def test_expired_state_raises_expired_error(self, _oauth_dsn):
        """Patch _utc_now to return a time past the TTL window."""
        state = issue_state("u1", "broker_a")

        expired_time = datetime.now(timezone.utc) - timedelta(minutes=20)

        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            with pytest.raises(OAuthStateExpiredError):
                consume_state(state)


# ---------------------------------------------------------------------------
# Tests: purge_expired_states
# ---------------------------------------------------------------------------

class TestPurgeExpiredStates:
    def test_deletes_row_created_more_than_10_minutes_ago(self, _oauth_dsn):
        """A never-consumed row past its TTL window is deleted."""
        state = issue_state("u1", "broker_a")

        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)

        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()

        assert deleted == 1

        # State is gone; consuming it raises InvalidOAuthStateError
        with pytest.raises(InvalidOAuthStateError):
            consume_state(state)

    def test_deletes_consumed_row_once_its_ttl_window_has_passed(self, _oauth_dsn):
        """A consumed row that is past its TTL window is also deleted."""
        state = issue_state("u1", "broker_a")
        consume_state(state)

        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)

        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()

        assert deleted == 1

    def test_never_consumed_row_still_within_ttl_is_not_deleted(self, _oauth_dsn):
        """A fresh (unconsumed) row inside the TTL window survives purge."""
        state = issue_state("u1", "broker_a")

        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        with mock.patch("src.oauth_state._utc_now", return_value=recent_time):
            deleted = purge_expired_states()

        assert deleted == 0

        # State is still valid and consumable
        user_id, broker_id = consume_state(state)
        assert user_id == "u1"
        assert broker_id == "broker_a"

    def test_consumed_row_still_within_ttl_is_not_deleted(self, _oauth_dsn):
        """A consumed row inside the TTL window also survives purge."""
        state = issue_state("u1", "broker_a")
        consume_state(state)

        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        with mock.patch("src.oauth_state._utc_now", return_value=recent_time):
            deleted = purge_expired_states()

        assert deleted == 0

    def test_purge_returns_row_count(self, _oauth_dsn):
        """Multiple expired rows are all deleted and the count is correct."""
        issue_state("u1", "broker_a")
        issue_state("u2", "broker_b")

        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)

        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()

        assert deleted == 2

    def test_purge_only_deletes_expired_rows(self, _oauth_dsn):
        """Mix of expired and fresh rows: only the expired ones go."""
        # Fresh rows
        issue_state("u1", "broker_a")
        issue_state("u2", "broker_b")
        # Expired row
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            issue_state("u3", "broker_c")

        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()

        assert deleted == 1  # only the one expired row


# ---------------------------------------------------------------------------
# Tests: module contains no broker-specific strings
# ---------------------------------------------------------------------------

class TestBrokerAgnostic:
    def test_module_source_contains_no_upstox(self):
        import src.oauth_state
        source = src.oauth_state.__file__
        assert source is not None
        with open(source) as fh:
            content = fh.read()
        assert "upstox" not in content.lower(), (
            "oauth_state module must not contain broker-specific strings"
        )
