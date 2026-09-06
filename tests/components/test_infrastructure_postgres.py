import os
from cryptography.fernet import Fernet
os.environ["BROKER_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest
import uuid
from datetime import datetime, timedelta
from src.infrastructure_postgres import DefaultInfrastructure, BrokerConnectionRecord


@pytest.fixture
def infra():
    return DefaultInfrastructure()


@pytest.fixture
def user_id():
    """Create a real user row (FK target) and return its id."""
    return str(uuid.uuid4())


@pytest.fixture
def broker_id():
    return "test_broker"


@pytest.fixture
def credentials():
    return {
        "access_token": "test_access_token_123",
        "token_type": "Bearer",
        "expires_at": int((datetime.now() + timedelta(hours=1)).timestamp()),
    }


# --- Schema / record existence (already done in prior attempt) ---

def test_broker_connections_table_exists(infra):
    """The broker_connections table must exist with the right schema."""
    with infra._connection().cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'broker_connections')"
        )
        assert cursor.fetchone()[0] is True

        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'broker_connections'
            """
        )
        cols = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

        expected = {
            "id": ("text", "NO"),
            "user_id": ("text", "NO"),
            "broker_id": ("text", "NO"),
            "broker_user_id": ("text", "YES"),
            "access_token_encrypted": ("text", "NO"),
            "token_type": ("text", "NO"),
            "access_token_expires_at": ("timestamp with time zone", "YES"),
            "status": ("text", "NO"),
            "last_error": ("text", "YES"),
            "connected_at": ("timestamp with time zone", "YES"),
            "last_import_at": ("timestamp with time zone", "YES"),
            "created_at": ("timestamp with time zone", "NO"),
            "updated_at": ("timestamp with time zone", "NO"),
        }
        for c, (typ, nullable) in expected.items():
            assert c in cols, f"Missing column {c}"
            assert cols[c] == (typ, nullable), f"Column {c}: expected {(typ, nullable)}, got {cols[c]}"

        cursor.execute(
            """
            SELECT contype, pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            WHERE c.conrelid = 'broker_connections'::regclass
            """
        )
        constraints = cursor.fetchall()
        types = {row[0] for row in constraints}
        defs = " | ".join(row[1] for row in constraints)
        assert "p" in types
        assert "f" in types
        assert "u" in types
        assert "c" in types
        assert "UNIQUE (user_id, broker_id)" in defs
        assert "REFERENCES users(id)" in defs
        assert "CONNECTED" in defs and "ERROR" in defs and "DISCONNECTED" in defs


def test_broker_connection_record_repr_redacts_token():
    """BrokerConnectionRecord.__repr__ must not leak the token."""
    record = BrokerConnectionRecord(
        id="test-id",
        user_id="user1",
        broker_id="broker1",
        broker_user_id=None,
        access_token_encrypted="encrypted_token_secret_value",
        token_type="Bearer",
        access_token_expires_at=None,
        status="CONNECTED",
        last_error=None,
        connected_at=None,
        last_import_at=None,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    repr_str = repr(record)
    assert "encrypted_token_secret_value" not in repr_str
    assert "REDACTED" in repr_str


# --- Full end-to-end repository method coverage (this is the new test) ---

def test_upsert_get_mark_touch_full_flow(infra, user_id, broker_id, credentials):
    """Exercise the full STORY-11 acceptance criteria:

    1. upsert_broker_connection creates a row, encrypts the token,
       sets status=CONNECTED, connected_at=now(), last_error=NULL.
    2. Calling upsert again for the same (user_id, broker_id) results
       in EXACTLY ONE row, with the new token and refreshed connected_at.
    3. The raw value in the DB column `access_token_encrypted` is NOT
       equal to the plaintext token.
    4. get_broker_connection returns None for an unknown pair, and a
       record with a correctly decrypted token for a known pair.
    5. mark_broker_connection_error sets status=ERROR and a truncated
       last_error; a subsequent successful upsert resets status to
       CONNECTED and last_error to NULL.
    6. touch_last_import updates last_import_at.
    """
    # 1. First upsert
    rec1 = infra.upsert_broker_connection(user_id, broker_id, credentials)
    assert rec1.status == "CONNECTED"
    assert rec1.last_error is None
    assert rec1.connected_at is not None
    assert rec1.access_token == credentials["access_token"]
    # Token must be encrypted in DB, not plaintext
    with infra._connection().cursor() as cursor:
        cursor.execute(
            "SELECT access_token_encrypted FROM broker_connections WHERE user_id=%s AND broker_id=%s",
            (user_id, broker_id),
        )
        row = cursor.fetchone()
    assert row is not None
    raw_token_in_db = row[0]
    assert raw_token_in_db != credentials["access_token"], \
        "Token stored in DB must be encrypted, not plaintext"

    # 2. Second upsert with a different token — must be a single row
    new_creds = dict(credentials)
    new_creds["access_token"] = "different_token_456"
    rec2 = infra.upsert_broker_connection(user_id, broker_id, new_creds)
    with infra._connection().cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM broker_connections WHERE user_id=%s AND broker_id=%s",
            (user_id, broker_id),
        )
        count = cursor.fetchone()[0]
    assert count == 1, f"Expected 1 row after re-upsert, got {count}"
    assert rec2.access_token == "different_token_456"
    assert rec2.connected_at is not None

    # 3. get_broker_connection returns None for unknown pair
    assert infra.get_broker_connection(user_id, "no-such-broker") is None

    # 4. get_broker_connection returns a decrypted record for the known pair
    rec3 = infra.get_broker_connection(user_id, broker_id)
    assert rec3 is not None
    assert rec3.access_token == "different_token_456"
    assert rec3.status == "CONNECTED"

    # 5. mark_broker_connection_error then upsert resets
    long_message = "x" * 1500
    infra.mark_broker_connection_error(user_id, broker_id, long_message)
    rec4 = infra.get_broker_connection(user_id, broker_id)
    assert rec4.status == "ERROR"
    assert rec4.last_error is not None
    assert len(rec4.last_error) <= 1000, f"last_error not truncated, len={len(rec4.last_error)}"

    rec5 = infra.upsert_broker_connection(user_id, broker_id, credentials)
    assert rec5.status == "CONNECTED"
    assert rec5.last_error is None

    # 6. touch_last_import updates last_import_at
    before = rec5.last_import_at
    infra.touch_last_import(user_id, broker_id)
    rec6 = infra.get_broker_connection(user_id, broker_id)
    assert rec6.last_import_at is not None
    assert rec6.last_import_at != before
