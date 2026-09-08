"""tests/components conftest: overrides the infra fixture so that
the users table exists before broker_connections FK check fires.

The DefaultInfrastructure._ensure_schema() creates broker_connections
with REFERENCES users(id), so the users table must exist first.
"""

import pytest


@pytest.fixture
def infra():
    """Same as the parent infra fixture but creates users first so that
    broker_connections' REFERENCES users(id) FK doesn't cause a
    psycopg.errors.UndefinedTable when _ensure_schema runs."""
    import psycopg
    from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure

    # Ensure users table exists before any DefaultInfrastructure method fires.
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
