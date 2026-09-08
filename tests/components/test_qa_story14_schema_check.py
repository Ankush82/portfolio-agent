"""Schema verification test for STORY-14: broker_holdings table has broker_id and instrument_id columns."""

import pytest


def test_broker_holdings_schema_has_required_columns(postgres_dsn):
    """Verify that the broker_holdings table has broker_id and instrument_id columns as required by STORY-14."""
    import psycopg
    
    conn = psycopg.connect(postgres_dsn)
    try:
        cur = conn.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'broker_holdings'
            ORDER BY ordinal_position
        """)
        cols = {r[0] for r in cur.fetchall()}
        
        # Check required columns exist
        assert 'broker_id' in cols, f"broker_id column missing from broker_holdings table. Found columns: {sorted(cols)}"
        assert 'instrument_id' in cols, f"instrument_id column missing from broker_holdings table. Found columns: {sorted(cols)}"
        
        # Also verify the primary key includes isin (should be part of PK)
        cur = conn.execute("""
            SELECT column_name 
            FROM information_schema.key_column_usage 
            WHERE table_name = 'broker_holdings' 
            AND constraint_name = 'broker_holdings_pkey'
        """)
        pk_cols = {r[0] for r in cur.fetchall()}
        assert 'isin' in pk_cols, f"isin should be part of primary key. PK columns: {pk_cols}"
        
    finally:
        conn.close()