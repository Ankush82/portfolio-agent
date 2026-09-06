import os
from cryptography.fernet import Fernet
os.environ["BROKER_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from src.infrastructure_postgres import DefaultInfrastructure

def debug_broker_connections_schema():
    infra = DefaultInfrastructure()
    with infra._connection().cursor() as cursor:
        # Check table exists
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'broker_connections'
            )
            """
        )
        exists = cursor.fetchone()[0]
        print(f"Table exists: {exists}")
        if not exists:
            return

        # Check columns
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'broker_connections'
            ORDER BY ordinal_position
            """
        )
        columns = cursor.fetchall()
        print("Columns:")
        for col in columns:
            print(f"  {col}")

        # Check constraints
        cursor.execute(
            """
            SELECT conname, contype, pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            WHERE c.conrelid = 'broker_connections'::regclass
            """
        )
        constraints = cursor.fetchall()
        print("Constraints:")
        for con in constraints:
            print(f"  {con}")

if __name__ == "__main__":
    debug_broker_connections_schema()