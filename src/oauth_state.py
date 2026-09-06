"""Broker-agnostic OAuth CSRF state store.

Issues single-use state tokens for the OAuth connect flow and provides
atomic consume + purge helpers. Nothing in this module mentions any
specific broker.

Schema: ``oauth_states`` table created lazily (CREATE TABLE IF NOT
EXISTS) the first time a Postgres connection is opened in this
instance's lifetime, consistent with the pattern used by
:mod:`src.infrastructure_postgres`.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import psycopg

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class OAuthStateError(RuntimeError):
    """Base class for all OAuth state errors."""


class InvalidOAuthStateError(OAuthStateError):
    """Raised when the presented state token is not found in the store."""


class OAuthStateReplayError(OAuthStateError):
    """Raised when the presented state token has already been consumed."""


class OAuthStateExpiredError(OAuthStateError):
    """Raised when the presented state token's TTL window has passed."""


# ---------------------------------------------------------------------------
# Internal data record
# ---------------------------------------------------------------------------

_STATE_TTL_MINUTES = 10
_STATE_TTL_DELTA = timedelta(minutes=_STATE_TTL_MINUTES)


@dataclass
class OAuthStateRecord:
    user_id: str
    broker_id: str
    created_at: datetime
    consumed_at: datetime | None


# ---------------------------------------------------------------------------
# Module-level state (set by ``configure``; see its docstring)
# ---------------------------------------------------------------------------

_configured_dsn: str | None = None


def _utc_now() -> datetime:
    """Return the current UTC datetime. Exposed so tests can override it without
    patching the ``datetime`` module globally."""
    return datetime.now(timezone.utc)


def configure(postgres_dsn: str) -> None:
    """Set the Postgres DSN used by all functions in this module.

    Must be called before any other function in this module is used.
    The DSN is stored at module level so the helper functions are
    stateless (no caller-side object to thread through).

    Args:
        postgres_dsn: e.g. ``postgresql://user:pass@localhost:5432/db``
    """
    global _configured_dsn
    _configured_dsn = postgres_dsn


def _connection() -> psycopg.Connection:
    dsn = _configured_dsn
    if dsn is None:
        raise RuntimeError(
            "oauth_state is not configured: call oauth_state.configure(dsn) "
            "before using any other function in this module."
        )
    conn = psycopg.connect(dsn, autocommit=True)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS oauth_states (
                state          TEXT        NOT NULL PRIMARY KEY,
                user_id        TEXT        NOT NULL,
                broker_id      TEXT        NOT NULL,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                consumed_at    TIMESTAMPTZ NULL
            )
            """
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def issue_state(user_id: str, broker_id: str) -> str:
    """Generate and persist a single-use OAuth state token.

    Args:
        user_id:  The user on whose behalf the OAuth flow is starting.
        broker_id: The broker being connected (broker-agnostic value).

    Returns:
        A urlsafe-base64 string of at least 32 random bytes
        (``secrets.token_urlsafe(32)``), suitable to use as the ``state``
        parameter in an OAuth 2.0 authorisation redirect.

    The token is stored with the current timestamp as ``created_at`` and
    ``consumed_at = NULL``. It is valid for {_STATE_TTL_MINUTES} minutes.
    Two calls will never return the same token.
    """
    state = secrets.token_urlsafe(32)
    now = _utc_now()
    with _connection().cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO oauth_states (state, user_id, broker_id, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (state, user_id, broker_id, now),
        )
    return state


def consume_state(state: str) -> tuple[str, str]:
    """Atomically consume a state token, returning its (user_id, broker_id).

    The token is marked as consumed (``consumed_at = now()``) in the same
    atomic database operation that reads it, so a second call with the
    same token is guaranteed to raise :exc:`OAuthStateReplayError`.

    Args:
        state: The token previously returned by :func:`issue_state`.

    Returns:
        ``(user_id, broker_id)`` as stored when the token was issued.

    Raises:
        InvalidOAuthStateError: the token is not in the store.
        OAuthStateReplayError:  the token was already consumed.
        OAuthStateExpiredError: the token's TTL window has passed.
    """
    now = _utc_now()
    cutoff = now - _STATE_TTL_DELTA

    with _connection().cursor() as cursor:
        # Atomic consume: UPDATE … WHERE consumed_at IS NULL, return the row.
        cursor.execute(
            """
            UPDATE oauth_states
            SET consumed_at = %s
            WHERE state = %s
            RETURNING user_id, broker_id, consumed_at, created_at
            """,
            (now, state),
        )
        row = cursor.fetchone()

    if row is None:
        # No row matched at all → invalid token (never issued or already
        # purged after expiry).
        raise InvalidOAuthStateError(
            f"State token not found: {state!r}"
        )

    user_id, broker_id, consumed_at, created_at = row

    if consumed_at is not None:
        # Row was matched but consumed_at was already set → replay.
        raise OAuthStateReplayError(
            f"State token has already been consumed: {state!r}"
        )

    if created_at < cutoff:
        raise OAuthStateExpiredError(
            f"State token has expired: {state!r} "
            f"(created {created_at}, cutoff {cutoff})"
        )

    return user_id, broker_id


def purge_expired_states() -> int:
    """Delete every oauth_states row whose TTL window has passed.

    "Expired" means ``created_at + {_STATE_TTL_MINUTES} minutes < now()``.
    A row is eligible for deletion whether or not it was ever consumed,
    and whether ``consumed_at`` is NULL or a recent timestamp — the
    consumed/unconsumed status is irrelevant to the purge decision.
    Only rows whose ``created_at`` is still within the TTL window are
    retained, regardless of their ``consumed_at`` value.

    Returns:
        The number of rows deleted.

    This is a table-hygiene operation. Replay protection for *live*
    (un-expired) states is handled entirely by :func:`consume_state`
    via the ``consumed_at IS NOT NULL`` guard in its atomic UPDATE.
    """
    cutoff = _utc_now() - _STATE_TTL_DELTA
    with _connection().cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM oauth_states
            WHERE created_at < %s
            """,
            (cutoff,),
        )
        return cursor.rowcount
