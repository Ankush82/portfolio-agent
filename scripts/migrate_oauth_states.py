"""STORY-12: Add oauth_states table.

Migration: 0022_oauth_states
"""

import os
import sys

# Add project root to path so we can import from src
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    from src.infrastructure_postgres import DefaultInfrastructure

    infra = DefaultInfrastructure()
    conn = infra._connection()

    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                state          TEXT        NOT NULL PRIMARY KEY,
                user_id        TEXT        NOT NULL,
                broker_id      TEXT        NOT NULL,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                consumed_at    TIMESTAMPTZ NULL
            )
            """
        )
        rows = cursor.rowcount
    print(f"oauth_states table ready (rows affected: {rows})")


if __name__ == "__main__":
    main()
