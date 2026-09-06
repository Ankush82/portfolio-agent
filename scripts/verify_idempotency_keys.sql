-- verify_idempotency_keys.sql
--
-- STORY-188 companion: verify that the UNIQUE constraints for
-- broker_holding_id and broker_transaction_id are in place and working.
--
-- Strictly SELECT-only — never modifies any table.
-- Run via scripts/verify_migration.py or directly.

-- (1) Holdings shadow table exists and has UNIQUE constraint
SELECT
    'holdings_idempotency_keys' AS table_name,
    CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'holdings_idempotency_keys'
        ) THEN 'MISSING'
        WHEN NOT EXISTS (
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            WHERE t.relname = 'holdings_idempotency_keys'
              AND c.contype = 'u'
        ) THEN 'NO_UNIQUE_CONSTRAINT'
        ELSE 'OK'
    END AS status
UNION ALL

-- (2) Transactions shadow table exists and has UNIQUE constraint
SELECT
    'transactions_idempotency_keys' AS table_name,
    CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'transactions_idempotency_keys'
        ) THEN 'MISSING'
        WHEN NOT EXISTS (
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            WHERE t.relname = 'transactions_idempotency_keys'
              AND c.contype = 'u'
        ) THEN 'NO_UNIQUE_CONSTRAINT'
        ELSE 'OK'
    END AS status
UNION ALL

-- (3) Trigger on records table for holdings (underscore-prefixed as created by migrate SQL)
SELECT
    '_trg_holdings_idempotency_key' AS object_name,
    CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname = '_trg_holdings_idempotency_key'
              AND tgrelid = 'records'::regclass
        ) THEN 'MISSING'
        ELSE 'OK'
    END AS status
UNION ALL

-- (4) Trigger on records table for transactions (underscore-prefixed as created by migrate SQL)
SELECT
    '_trg_transactions_idempotency_key' AS object_name,
    CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname = '_trg_transactions_idempotency_key'
              AND tgrelid = 'records'::regclass
        ) THEN 'MISSING'
        ELSE 'OK'
    END AS status
UNION ALL

-- (5) Successful migration_log entry exists
SELECT
    'migration_log_idempotency_keys_v1' AS object_name,
    CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM migration_log
            WHERE migration_name = 'idempotency_keys_v1'
              AND status = 'SUCCESS'
        ) THEN 'MISSING'
        ELSE 'OK'
    END AS status;
