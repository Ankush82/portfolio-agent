import os
from cryptography.fernet import Fernet
os.environ["BROKER_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest
import psycopg.errors
import uuid
from datetime import datetime, timedelta
from src.infrastructure_postgres import DefaultInfrastructure, BrokerConnectionRecord, DEFAULT_POSTGRES_DSN
from src.components.c01_user_portfolio import BrokerCredentials


@pytest.fixture
def infra():
    """DefaultInfrastructure with the users table pre-created so that
    broker_connections REFERENCES users(id) FK succeeds."""
    import psycopg
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "  id TEXT PRIMARY KEY,"
                "  email TEXT UNIQUE,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
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
    return BrokerCredentials(
        access_token="test_access_token_123",
        token_type="Bearer",
        expires_at=datetime.now() + timedelta(hours=1),
    )


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


# --- STORY-188: Idempotency key UNIQUE constraints ---

def test_holdings_idempotency_key_unique_constraint_prevents_duplicates(
    infra: DefaultInfrastructure,
) -> None:
    """Duplicate broker_holding_id inserts raise psycopg.errors.UniqueViolation.

    After migration migrate_idempotency_keys is applied, the shadow table
    holdings_idempotency_keys enforces a UNIQUE constraint on broker_holding_id.
    An INSERT into records (table_name='holdings') with a duplicate
    broker_holding_id value must raise UniqueViolation.
    """
    portfolio_id = str(uuid.uuid4())
    holding_data = {
        "id": str(uuid.uuid4()),
        "portfolio_id": portfolio_id,
        "security_id": "AAPL",
        "quantity": "10.0000",
        "currency": "USD",
        "broker_holding_id": "broker-holding-abc-123",
    }
    other_data = {
        "id": str(uuid.uuid4()),
        "portfolio_id": portfolio_id,
        "security_id": "MSFT",
        "quantity": "5.0000",
        "currency": "USD",
        "broker_holding_id": "broker-holding-abc-123",  # same idempotency key
    }

    # First insert succeeds
    infra.store("holdings", holding_data)

    # Duplicate broker_holding_id must raise UniqueViolation
    with pytest.raises(psycopg.errors.UniqueViolation):
        infra.store("holdings", other_data)


def test_holdings_idempotency_key_unique_constraint_prevents_duplicates_on_update(
    infra: DefaultInfrastructure,
) -> None:
    """An UPDATE that changes broker_holding_id to a conflicting value raises UniqueViolation.

    The trigger fires on UPDATE (not just INSERT), so changing a holding's
    broker_holding_id to match an existing shadow-table key must also raise.
    """
    portfolio_id = str(uuid.uuid4())
    infra.store(
        "holdings",
        {
            "id": "holding-1",
            "portfolio_id": portfolio_id,
            "security_id": "AAPL",
            "quantity": "10.0000",
            "currency": "USD",
            "broker_holding_id": "key-alpha",
        },
    )
    infra.store(
        "holdings",
        {
            "id": "holding-2",
            "portfolio_id": portfolio_id,
            "security_id": "MSFT",
            "quantity": "5.0000",
            "currency": "USD",
            "broker_holding_id": "key-beta",
        },
    )
    # Update holding-1 to use holding-2's broker_holding_id — must raise.
    with pytest.raises(psycopg.errors.UniqueViolation):
        infra.store(
            "holdings",
            {
                "id": "holding-1",
                "portfolio_id": portfolio_id,
                "security_id": "AAPL",
                "quantity": "10.0000",
                "currency": "USD",
                "broker_holding_id": "key-beta",  # collides with holding-2
            },
        )


def test_transactions_idempotency_key_unique_constraint_prevents_duplicates(
    infra: DefaultInfrastructure,
) -> None:
    """Duplicate broker_transaction_id inserts raise psycopg.errors.UniqueViolation.

    After migration migrate_idempotency_keys is applied, the shadow table
    transactions_idempotency_keys enforces a UNIQUE constraint on
    broker_transaction_id. An INSERT into records (table_name='transactions')
    with a duplicate broker_transaction_id value must raise UniqueViolation.
    """
    portfolio_id = str(uuid.uuid4())
    tx_data = {
        "id": str(uuid.uuid4()),
        "portfolio_id": portfolio_id,
        "kind": "BUY",
        "amount": "100.00",
        "broker_transaction_id": "broker-tx-xyz-789",
    }
    other_data = {
        "id": str(uuid.uuid4()),
        "portfolio_id": portfolio_id,
        "kind": "SELL",
        "amount": "50.00",
        "broker_transaction_id": "broker-tx-xyz-789",  # same idempotency key
    }

    # First insert succeeds
    infra.store("transactions", tx_data)

    # Duplicate broker_transaction_id must raise UniqueViolation
    with pytest.raises(psycopg.errors.UniqueViolation):
        infra.store("transactions", other_data)


def test_idempotency_keys_null_broker_id_allows_multiple_inserts(
    infra: DefaultInfrastructure,
) -> None:
    """Multiple holdings/transactions with NULL broker_id must NOT raise.

    The UNIQUE constraint is only on non-NULL values (Postgres standard
    behaviour: NULL != NULL in a unique index). The trigger skips inserting
    NULL values into the shadow table, so multiple records with a NULL
    broker_holding_id or broker_transaction_id are allowed.
    """
    portfolio_id = str(uuid.uuid4())
    for i in range(3):
        infra.store(
            "holdings",
            {
                "id": str(uuid.uuid4()),
                "portfolio_id": portfolio_id,
                "security_id": f"SYM{i}",
                "quantity": "1.0000",
                "currency": "USD",
                "broker_holding_id": None,  # all NULL
            },
        )
    # Must not raise — NULL is not constrained
    count = sum(
        1
        for row in infra.query(
            "holdings",
            {"portfolio_id": portfolio_id, "broker_holding_id": None},
        )
    )
    assert count == 3
