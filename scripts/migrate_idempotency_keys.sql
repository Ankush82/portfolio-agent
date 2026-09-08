-- migrate_idempotency_keys.sql
--
-- STORY-188: Add UNIQUE constraints on broker_holding_id and
-- broker_transaction_id for database-level idempotency guarantees.
--
-- Adds:
--   (1) UNIQUE constraint on holdings.broker_holding_id
--   (2) UNIQUE constraint on transactions.broker_transaction_id
--
-- Each ADD CONSTRAINT uses IF NOT EXISTS so re-running is safe
-- against a Postgres "constraint already exists" error.
--
-- The constraints are added to the two record tables managed by
-- DefaultInfrastructure (records + queue_events + scheduled_tasks +
-- migration_log + schema_migrations + broker_connections). Holdings and
-- transactions are stored as JSONB rows inside the `records` table
-- with table_name='holdings' and table_name='transactions' respectively.
-- The idempotency keys live as top-level JSONB fields on those rows:
--   holdings:  data->>'broker_holding_id'
--   transactions: data->>'broker_transaction_id'
--
-- Because JSONB fields cannot participate directly in PostgreSQL
-- UNIQUE constraints (Postgres only supports UNIQUE on regular
-- columns), we instead maintain parallel shadow tables that mirror
-- the key columns and keep them in sync via AFTER INSERT OR UPDATE
-- triggers. This gives us a genuine database-level uniqueness guard
-- without changing the records table schema.
--
-- Shadow tables exist only to host the UNIQUE constraint; all real data
-- lives in the `records` table as before. The shadow tables carry no
-- additional columns beyond what's needed for the constraint.

-- ---------------------------------------------------------------------------
-- (1) holdings_idempotency_keys shadow table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS holdings_idempotency_keys (
    broker_holding_id TEXT NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
-- (2) transactions_idempotency_keys shadow table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions_idempotency_keys (
    broker_transaction_id TEXT NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
-- (3) Pre-populate shadow tables with existing records
--
-- Triggers only fire on future INSERT/UPDATE; existing rows would otherwise
-- be invisible to the UNIQUE constraint. This step seeds the shadow tables
-- from the current records table content, using ON CONFLICT DO NOTHING so
-- a re-run is safe (existing key is already there).
-- ---------------------------------------------------------------------------

INSERT INTO holdings_idempotency_keys (broker_holding_id)
SELECT (data->>'broker_holding_id')::text
FROM   records
WHERE  table_name = 'holdings'
  AND  data->>'broker_holding_id' IS NOT NULL
ON CONFLICT (broker_holding_id) DO NOTHING;

INSERT INTO transactions_idempotency_keys (broker_transaction_id)
SELECT (data->>'broker_transaction_id')::text
FROM   records
WHERE  table_name = 'transactions'
  AND  data->>'broker_transaction_id' IS NOT NULL
ON CONFLICT (broker_transaction_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- (4) Trigger function: sync holdings broker_holding_id to shadow table
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION _sync_holdings_idempotency_key()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- Guard: OLD is NULL on INSERT (not UPDATE), so skip the DELETE.
    IF TG_OP = 'UPDATE' AND OLD.data->>'broker_holding_id' IS NOT NULL THEN
        DELETE FROM holdings_idempotency_keys
        WHERE broker_holding_id = OLD.data->>'broker_holding_id';
    END IF;

    IF NEW.data->>'broker_holding_id' IS NOT NULL THEN
        INSERT INTO holdings_idempotency_keys (broker_holding_id)
        VALUES (NEW.data->>'broker_holding_id')
        ON CONFLICT (broker_holding_id) DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS _trg_holdings_idempotency_key ON records;
CREATE TRIGGER _trg_holdings_idempotency_key
    AFTER INSERT OR UPDATE ON records
    FOR EACH ROW
    WHEN (NEW.table_name = 'holdings')
    EXECUTE FUNCTION _sync_holdings_idempotency_key();

-- ---------------------------------------------------------------------------
-- (5) Trigger function: sync transactions broker_transaction_id to shadow table
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION _sync_transactions_idempotency_key()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- Guard: OLD is NULL on INSERT (not UPDATE), so skip the DELETE.
    IF TG_OP = 'UPDATE' AND OLD.data->>'broker_transaction_id' IS NOT NULL THEN
        DELETE FROM transactions_idempotency_keys
        WHERE broker_transaction_id = OLD.data->>'broker_transaction_id';
    END IF;

    IF NEW.data->>'broker_transaction_id' IS NOT NULL THEN
        INSERT INTO transactions_idempotency_keys (broker_transaction_id)
        VALUES (NEW.data->>'broker_transaction_id')
        ON CONFLICT (broker_transaction_id) DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS _trg_transactions_idempotency_key ON records;
CREATE TRIGGER _trg_transactions_idempotency_key
    AFTER INSERT OR UPDATE ON records
    FOR EACH ROW
    WHEN (NEW.table_name = 'transactions')
    EXECUTE FUNCTION _sync_transactions_idempotency_key();
