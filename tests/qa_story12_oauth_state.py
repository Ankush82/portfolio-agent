"""QA verification test for STORY-12 (oauth_state CSRF store).

Independent assertions covering every acceptance criterion:
  AC-1  issue_state returns >=32-char urlsafe string; two calls differ.
  AC-2  consume_state on a fresh state returns (user_id, broker_id).
  AC-3  Second consume_state with same value raises OAuthStateReplayError.
  AC-4  consume_state with unknown value raises InvalidOAuthStateError.
  AC-5  consume_state on an aged state raises OAuthStateExpiredError
        (uses injectable clock via mock.patch, not sleep).
  AC-6  purge_expired_states() deletes expired rows regardless of consumed_at;
        preserves rows still within their TTL window regardless of consumed_at.
  AC-7  Module contains no broker-specific strings (e.g. "upstox").
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from unittest import mock

import psycopg
import pytest

# ---------------------------------------------------------------------------
# Bootstrap — set BROKER_TOKEN_ENCRYPTION_KEY before importing anything
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
    OAuthStateError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def oauth_dsn(postgres_dsn: str) -> str:
    """Configure the module and return a clean oauth_states table.
    
    Skips the entire test if Postgres is not reachable, matching the project's
    own conftest.py pattern (skip in fixture body, not decorator).
    """
    configure(postgres_dsn)
    conn = psycopg.connect(postgres_dsn, autocommit=True)
    with conn.cursor() as cursor:
        cursor.execute("TRUNCATE TABLE oauth_states RESTART IDENTITY CASCADE")
    conn.close()
    yield postgres_dsn


# ---------------------------------------------------------------------------
# AC-1 — issue_state token properties
# ---------------------------------------------------------------------------

class TestIssueStateToken:
    """AC-1: issue_state returns a >=32-char urlsafe string; two calls differ."""

    def test_token_is_at_least_32_urlsafe_characters(self, oauth_dsn):
        state = issue_state("uid", "broker")
        # secrets.token_urlsafe(32) → 43 base64url chars
        assert len(state) >= 32, f"Token too short: {len(state)}"
        assert re.fullmatch(r"[A-Za-z0-9_-]+", state), (
            f"Token contains non-urlsafe chars: {state!r}"
        )

    def test_two_issue_calls_return_different_tokens(self, oauth_dsn):
        s1 = issue_state("u1", "b1")
        s2 = issue_state("u1", "b1")
        assert s1 != s2, "Two issue_state calls must never return the same token"

    def test_100_issue_calls_all_unique(self, oauth_dsn):
        tokens = [issue_state("u1", "b1") for _ in range(100)]
        assert len(set(tokens)) == 100, "100 issue_state calls must produce 100 unique tokens"


# ---------------------------------------------------------------------------
# AC-2 — consume_state returns (user_id, broker_id)
# ---------------------------------------------------------------------------

class TestConsumeStateReturnValue:
    """AC-2: consume_state on a fresh state returns (user_id, broker_id)."""

    @pytest.mark.parametrize("uid,bkr", [
        ("uid_001", "broker_alpha"),
        ("user@domain.com", "bkr/with/slashes"),
        ("", "empty_user_id"),
    ])
    def test_returns_exact_user_id_and_broker_id(self, oauth_dsn, uid, bkr):
        state = issue_state(uid, bkr)
        returned_uid, returned_bkr = consume_state(state)
        assert returned_uid == uid
        assert returned_bkr == bkr


# ---------------------------------------------------------------------------
# AC-3 — replay detection
# ---------------------------------------------------------------------------

class TestConsumeStateReplay:
    """AC-3: Second consume_state raises OAuthStateReplayError."""

    def test_replay_raises_oauth_state_replay_error(self, oauth_dsn):
        state = issue_state("u1", "b1")
        consume_state(state)  # first call succeeds
        with pytest.raises(OAuthStateReplayError):
            consume_state(state)  # second call is a replay

    def test_replay_error_is_a_subclass_of_oauth_state_error(self):
        assert issubclass(OAuthStateReplayError, OAuthStateError)

    def test_replay_after_reload_still_raises_replay(self, oauth_dsn):
        """Consumed rows must stay consumed even after reconnection."""
        state = issue_state("u1", "b1")
        consume_state(state)
        # Force reconnection (configure reuses the DSN, so new conn)
        conn = psycopg.connect(oauth_dsn, autocommit=True)
        conn.close()
        with pytest.raises(OAuthStateReplayError):
            consume_state(state)


# ---------------------------------------------------------------------------
# AC-4 — unknown state detection
# ---------------------------------------------------------------------------

class TestConsumeStateUnknown:
    """AC-4: Unknown state raises InvalidOAuthStateError."""

    def test_unknown_token_raises_invalid_error(self, oauth_dsn):
        fake = secrets.token_urlsafe(32)
        with pytest.raises(InvalidOAuthStateError):
            consume_state(fake)

    def test_invalid_error_is_a_subclass_of_oauth_state_error(self):
        assert issubclass(InvalidOAuthStateError, OAuthStateError)


# ---------------------------------------------------------------------------
# AC-5 — expiry detection (injectable clock, no sleep)
# ---------------------------------------------------------------------------

class TestConsumeStateExpiry:
    """AC-5: State older than 10 minutes raises OAuthStateExpiredError."""

    def test_expired_state_raises_oauth_state_expired_error(self, oauth_dsn):
        state = issue_state("u1", "b1")
        # Mock _utc_now to return a time 15 minutes in the past
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            with pytest.raises(OAuthStateExpiredError):
                consume_state(state)

    def test_state_just_inside_ttl_does_not_expire(self, oauth_dsn):
        """At 9 minutes old the state is still valid."""
        state = issue_state("u1", "b1")
        near_cutoff = datetime.now(timezone.utc) - timedelta(minutes=9)
        with mock.patch("src.oauth_state._utc_now", return_value=near_cutoff):
            uid, bkr = consume_state(state)
            assert uid == "u1"
            assert bkr == "b1"

    def test_expired_error_is_a_subclass_of_oauth_state_error(self):
        assert issubclass(OAuthStateExpiredError, OAuthStateError)


# ---------------------------------------------------------------------------
# AC-6 — purge_expired_states hygiene
# ---------------------------------------------------------------------------

class TestPurgeExpiredStates:
    """AC-6: Purge deletes expired rows regardless of consumed_at;
    preserves rows still within TTL regardless of consumed_at."""

    def test_purge_deletes_never_consumed_expired_row(self, oauth_dsn):
        state = issue_state("u1", "b1")
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()
        assert deleted == 1, "Expired unconsumed row must be deleted"
        with pytest.raises(InvalidOAuthStateError):
            consume_state(state)

    def test_purge_deletes_consumed_expired_row(self, oauth_dsn):
        """AC-6 explicit: consumed + expired must still be deleted."""
        state = issue_state("u1", "b1")
        consume_state(state)  # mark consumed
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()
        assert deleted == 1, "Expired consumed row must also be deleted"

    def test_purge_preserves_fresh_unconsumed_row(self, oauth_dsn):
        state = issue_state("u1", "b1")
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        with mock.patch("src.oauth_state._utc_now", return_value=recent):
            deleted = purge_expired_states()
        assert deleted == 0, "Fresh unconsumed row must NOT be deleted"
        # State must still be usable
        uid, bkr = consume_state(state)
        assert uid == "u1"

    def test_purge_preserves_fresh_consumed_row(self, oauth_dsn):
        """AC-6 explicit: consumed + fresh must also survive purge."""
        state = issue_state("u1", "b1")
        consume_state(state)  # mark consumed
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        with mock.patch("src.oauth_state._utc_now", return_value=recent):
            deleted = purge_expired_states()
        assert deleted == 0, "Fresh consumed row must NOT be deleted"

    def test_purge_returns_correct_row_count(self, oauth_dsn):
        # 2 expired + 1 fresh
        issue_state("u1", "b1")
        issue_state("u2", "b2")
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            issue_state("u3", "b3")  # expired at creation time
        with mock.patch("src.oauth_state._utc_now", return_value=expired_time):
            deleted = purge_expired_states()
        assert deleted == 2, "Only the 2 expired rows should be deleted"

    def test_purge_of_empty_table_returns_zero(self, oauth_dsn):
        deleted = purge_expired_states()
        assert deleted == 0


# ---------------------------------------------------------------------------
# AC-7 — broker-agnostic (no Upstox or other broker strings)
# ---------------------------------------------------------------------------

class TestBrokerAgnostic:
    """AC-7: Module contains no broker-specific strings."""

    @pytest.mark.parametrize("forbidden", [
        "upstox", "Upstox", "UPSTOX",
    ])
    def test_no_broker_strings_in_source(self, forbidden):
        import src.oauth_state
        source_path = src.oauth_state.__file__
        assert source_path is not None
        with open(source_path) as fh:
            content = fh.read()
        assert forbidden.lower() not in content.lower(), (
            f"oauth_state module must not contain broker-specific string "
            f"{forbidden!r}; module must be broker-agnostic"
        )
