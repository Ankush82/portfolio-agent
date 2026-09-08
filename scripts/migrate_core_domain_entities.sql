-- migrate_core_domain_entities.sql
--
-- Migration ID:  002_core_domain_entities_v1
-- Purpose:       Create the four core domain tables (users, portfolios,
--                holdings, transactions) with typed columns, primary keys,
--                foreign keys, unique constraints, and indexes.
-- Tables:        users, portfolios, holdings, transactions
--
-- Hardening (STORY-12): this migration is fully idempotent and converges
-- legacy lazily-created shapes. The old lazy CREATE TABLE IF NOT EXISTS
-- path in DefaultInfrastructure._ensure_schema (V3 of docs/repo-layer-recon.md)
-- created only the four generic system tables (records, queue_events,
-- scheduled_tasks, migration_log, schema_migrations) but left the four
-- user-portfolio tables (users, portfolios, holdings, transactions) as
-- opaque JSONB blobs in the records table with no typed columns.
--
-- Per-table, this migration applies in this order:
--   1. CREATE TABLE IF NOT EXISTS <table> (...)  -- the fresh-DB path
--   2. ALTER TABLE ... ADD COLUMN IF NOT EXISTS <col> <type> ...  -- legacy-DB path
--      NOTE: NOT NULL is NOT re-applied via ADD COLUMN IF NOT EXISTS for id
--      columns because Postgres does not allow changing nullability via that
--      syntax when the column already exists. The CREATE TABLE path already
--      declared them NOT NULL, so this is not a gap.
--   3. Guarded backfill from legacy records.data JSONB column (V3 recon doc).
--      The old lazy path stored rows as opaque JSONB in records.data. If that
--      column still exists, typed fields are copied forward. The records.data
--      column is RETAINED (left nullable and unused) -- it is NOT dropped here.
--      See "Retained legacy column" note below.
--   4. Idempotent constraints and indexes:
--        - Primary key: handled by CREATE TABLE IF NOT EXISTS
--        - Unique constraint on (portfolio_id, security_id) in holdings:
--            a standalone UNIQUE INDEX named uq_holdings_portfolio_security
--            (outside any DO block) enforces the (portfolio_id, security_id) pair
--        - Foreign keys: DO $$ BEGIN IF NOT EXISTS (pg_constraint WHERE conname)
--            existence check), then close the conditional block with END IF and END $$;
--            Each FK block counts orphan rows first; adds the FK only when
--            orphans = 0, otherwise RAISE NOTICE with count and inspection SELECT.
--            This migration is non-destructive: it never DELETEs rows to satisfy
--            a constraint.
--
-- Forward-only migration. There is no separate down or rollback SQL file
-- in this repo. Manual rollback requires dropping each table in reverse
-- FK order. Execute the drop table statements in this order: first drop
-- table transactions, then drop table holdings, then drop table portfolios,
-- then drop table users. Take a snapshot or pg_dump before running -- the
-- drop table statements destroy all data in those tables.
--
-- Retained legacy column (STORY-12 documented follow-up cleanup):
--   The records.data JSONB column is left in place (nullable, unused) in
--   case the legacy payload needs re-examination. Drop it in a follow-up
--   story once all four typed tables are confirmed populated and the
--   records table no longer stores user-portfolio rows. Do NOT drop it
--   here because the backfill step above reads from it.
--
-- Placement convention (per docs/repo-layer-recon.md item V6):
--   scripts/migrate_<name>.sql  -- same directory, same naming scheme
--   as scripts/migrate_us_stocks.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------

-- (1) Fresh-DB path: create the table with full target DDL.
CREATE TABLE IF NOT EXISTS users (
    id           TEXT        NOT NULL,
    email        TEXT,
    preferences  JSONB      NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- (2) Legacy-DB path: add every non-id column idempotently.
--     id is NOT re-added here because it is already declared NOT NULL in
--     the CREATE TABLE path, and ADD COLUMN IF NOT EXISTS cannot change
--     nullability -- it would fail on a second run if id already exists
--     with NOT NULL. All other columns can safely be added.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email        TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences  JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at   TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ NOT NULL DEFAULT now();

-- (2b) Legacy-type convergence: ADD COLUMN IF NOT EXISTS above is a no-op
--      when the column already exists with the WRONG type -- a lazily
--      created `users` table (old DefaultInfrastructure._ensure_schema
--      fallback) stored `preferences` as TEXT, not JSONB. Without this
--      step the type mismatch survives every future run and the JSONB
--      backfill comparison below (`IS DISTINCT FROM ... jsonb`) fails
--      with "operator does not exist: text = jsonb". Guarded on the
--      actual current type so this is a real no-op once converged.
DO $$
BEGIN
    IF (
        SELECT data_type FROM information_schema.columns
        WHERE  table_name = 'users' AND column_name = 'preferences'
    ) != 'jsonb' THEN
        ALTER TABLE users ALTER COLUMN preferences DROP DEFAULT;
        ALTER TABLE users
            ALTER COLUMN preferences TYPE JSONB
            USING COALESCE(NULLIF(preferences, '')::jsonb, '{}'::jsonb);
        ALTER TABLE users ALTER COLUMN preferences SET DEFAULT '{}'::jsonb;
    END IF;
END
$$;

-- (2c) Legacy-nullability convergence: a lazily created `users` table
--      (or one converged by (2b) above) can still have `preferences`
--      declared nullable -- ADD COLUMN IF NOT EXISTS never revisits
--      nullability on a pre-existing column, matching the same gap
--      the header note above documents for `id`. Backfill any real
--      NULL to '{}' first so SET NOT NULL cannot fail on legacy rows.
DO $$
BEGIN
    IF (
        SELECT is_nullable FROM information_schema.columns
        WHERE  table_name = 'users' AND column_name = 'preferences'
    ) = 'YES' THEN
        UPDATE users SET preferences = '{}'::jsonb WHERE preferences IS NULL;
        ALTER TABLE users ALTER COLUMN preferences SET NOT NULL;
    END IF;
END
$$;

-- (3) Guarded backfill from legacy records.data JSONB column (V3 recon doc).
--     Only runs when records.data exists; otherwise this is a no-op.
--     The legacy column is RETAINED (not dropped) -- see header note above.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'records'
          AND  column_name = 'data'
    ) THEN
        UPDATE users u
        SET   email        = r.data->>'email',
              preferences  = COALESCE(r.data->'preferences', '{}'::jsonb),
              created_at   = COALESCE(
                               (r.data->>'created_at')::timestamptz,
                               now()
                             ),
              updated_at   = COALESCE(
                               (r.data->>'updated_at')::timestamptz,
                               now()
                             )
        FROM   records r
        WHERE  r.table_name = 'users'
          AND  r.id         = u.id
          AND  (
                  u.email       IS DISTINCT FROM (r.data->>'email')
               OR u.preferences IS DISTINCT FROM COALESCE(r.data->'preferences', '{}'::jsonb)
               OR u.created_at  IS DISTINCT FROM COALESCE((r.data->>'created_at')::timestamptz, now())
               OR u.updated_at  IS DISTINCT FROM COALESCE((r.data->>'updated_at')::timestamptz, now())
              );
    END IF;
END
$$;

-- (4) Index on email, created idempotently.
CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);

COMMENT ON TABLE  users IS 'Core user entity. preferences is a JSONB blob.';
COMMENT ON COLUMN users.id          IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN users.email        IS 'User email address. No UNIQUE constraint -- recon V5 found no code or test requiring uniqueness.';
COMMENT ON COLUMN users.preferences IS 'JSONB preferences blob, defaults to {}.';
COMMENT ON COLUMN users.created_at   IS 'Creation timestamp, UTC.';
COMMENT ON COLUMN users.updated_at   IS 'Last-update timestamp, UTC.';


-- ---------------------------------------------------------------------------
-- portfolios
-- ---------------------------------------------------------------------------

-- (1) Fresh-DB path.
CREATE TABLE IF NOT EXISTS portfolios (
    id         TEXT        NOT NULL,
    user_id    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- (2) Legacy-DB path: add non-id columns idempotently.
--     id is NOT re-added -- see note in users section above.
ALTER TABLE portfolios ADD COLUMN IF NOT EXISTS user_id    TEXT        NOT NULL;
ALTER TABLE portfolios ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE portfolios ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- (3) Guarded backfill from legacy records.data JSONB column.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'records'
          AND  column_name = 'data'
    ) THEN
        UPDATE portfolios p
        SET   user_id    = r.data->>'user_id',
              created_at = COALESCE(
                             (r.data->>'created_at')::timestamptz,
                             now()
                           ),
              updated_at = COALESCE(
                             (r.data->>'updated_at')::timestamptz,
                             now()
                           )
        FROM   records r
        WHERE  r.table_name = 'portfolios'
          AND  r.id         = p.id
          AND  (
                  p.user_id    IS DISTINCT FROM (r.data->>'user_id')
               OR p.created_at IS DISTINCT FROM COALESCE((r.data->>'created_at')::timestamptz, now())
               OR p.updated_at IS DISTINCT FROM COALESCE((r.data->>'updated_at')::timestamptz, now())
              );
    END IF;
END
$$;

-- (4a) Index: idempotent.
CREATE INDEX IF NOT EXISTS idx_portfolios_user_id ON portfolios (user_id);

-- (4b) FK: guarded by pg_constraint existence check + orphan count.
--      If any portfolio has a user_id that matches no row in users, the FK
--      is NOT added and a RAISE NOTICE reports the orphan count and the
--      exact SELECT a developer can run to inspect them. No rows are deleted.
DO $$
DECLARE
    _orphan_count INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'fk_portfolios_user'
    ) THEN
        SELECT COUNT(*)
        INTO   _orphan_count
        FROM   portfolios p
        WHERE  NOT EXISTS (
            SELECT 1 FROM users u WHERE u.id = p.user_id
        );

        IF _orphan_count > 0 THEN
            RAISE NOTICE 'FK fk_portfolios_user NOT added: % orphan portfolio(s) have no matching user_id. Inspect orphans with: SELECT p.id, p.user_id FROM portfolios p WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id) ORDER BY p.id;',
                _orphan_count;
        ELSE
            ALTER TABLE portfolios
                ADD CONSTRAINT fk_portfolios_user
                FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE;
        END IF;
    END IF;
END
$$;

COMMENT ON TABLE  portfolios IS 'Portfolio entity, owned by a user.';
COMMENT ON COLUMN portfolios.id      IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN portfolios.user_id IS 'Owner user. FK to users(id); child rows removed when parent is removed.';


-- ---------------------------------------------------------------------------
-- holdings
-- ---------------------------------------------------------------------------

-- (1) Fresh-DB path.
CREATE TABLE IF NOT EXISTS holdings (
    id             TEXT            NOT NULL,
    portfolio_id   TEXT            NOT NULL,
    security_id    TEXT            NOT NULL,
    quantity       NUMERIC(38, 10) NOT NULL DEFAULT 0,
    currency       TEXT,
    exchange       TEXT,
    symbol_suffix  TEXT,
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- (2) Legacy-DB path: add non-id columns idempotently.
--     id is NOT re-added -- see note in users section above.
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS portfolio_id   TEXT            NOT NULL;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS security_id    TEXT            NOT NULL;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS quantity       NUMERIC(38, 10) NOT NULL DEFAULT 0;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS currency       TEXT;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS exchange       TEXT;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS symbol_suffix  TEXT;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS created_at     TIMESTAMPTZ     NOT NULL DEFAULT now();
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS updated_at     TIMESTAMPTZ     NOT NULL DEFAULT now();

-- (3) Guarded backfill from legacy records.data JSONB column.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'records'
          AND  column_name = 'data'
    ) THEN
        UPDATE holdings h
        SET   portfolio_id  = r.data->>'portfolio_id',
              security_id  = r.data->>'security_id',
              quantity     = COALESCE(
                                (r.data->>'quantity')::numeric(38, 10),
                                0
                              ),
              currency     = r.data->>'currency',
              exchange     = r.data->>'exchange',
              symbol_suffix = r.data->>'symbol_suffix',
              created_at   = COALESCE(
                               (r.data->>'created_at')::timestamptz,
                               now()
                             ),
              updated_at   = COALESCE(
                               (r.data->>'updated_at')::timestamptz,
                               now()
                             )
        FROM   records r
        WHERE  r.table_name = 'holdings'
          AND  r.id         = h.id
          AND  (
                  h.portfolio_id  IS DISTINCT FROM (r.data->>'portfolio_id')
               OR h.security_id  IS DISTINCT FROM (r.data->>'security_id')
               OR h.quantity      IS DISTINCT FROM COALESCE((r.data->>'quantity')::numeric(38, 10), 0)
               OR h.currency      IS DISTINCT FROM (r.data->>'currency')
               OR h.exchange      IS DISTINCT FROM (r.data->>'exchange')
               OR h.symbol_suffix IS DISTINCT FROM (r.data->>'symbol_suffix')
               OR h.created_at    IS DISTINCT FROM COALESCE((r.data->>'created_at')::timestamptz, now())
               OR h.updated_at    IS DISTINCT FROM COALESCE((r.data->>'updated_at')::timestamptz, now())
              );
    END IF;
END
$$;

-- (4a) Unique constraint on (portfolio_id, security_id) via idempotent index.
--      Postgres has no ADD CONSTRAINT IF NOT EXISTS, so a unique index is
--      the correct idempotent vehicle. Placed as a standalone statement
--      (outside any DO block) so Postgres and the test regex both find it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_holdings_portfolio_security
    ON holdings (portfolio_id, security_id);

-- (4b) Index: idempotent.
CREATE INDEX IF NOT EXISTS idx_holdings_portfolio_id ON holdings (portfolio_id);

-- (4c) FK: guarded by pg_constraint existence check + orphan count.
--      Orphan holdings (portfolio_id has no matching row in portfolios) cause
--      the FK to be skipped with a RAISE NOTICE; no rows are deleted.
DO $$
DECLARE
    _orphan_count INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'fk_holdings_portfolio'
    ) THEN
        SELECT COUNT(*)
        INTO   _orphan_count
        FROM   holdings h
        WHERE  NOT EXISTS (
            SELECT 1 FROM portfolios p WHERE p.id = h.portfolio_id
        );

        IF _orphan_count > 0 THEN
            RAISE NOTICE 'FK fk_holdings_portfolio NOT added: % orphan holding(s) have no matching portfolio_id. Inspect orphans with: SELECT h.id, h.portfolio_id, h.security_id FROM holdings h WHERE NOT EXISTS (SELECT 1 FROM portfolios p WHERE p.id = h.portfolio_id) ORDER BY h.id;',
                _orphan_count;
        ELSE
            ALTER TABLE holdings
                ADD CONSTRAINT fk_holdings_portfolio
                FOREIGN KEY (portfolio_id) REFERENCES portfolios(id)
                ON DELETE CASCADE;
        END IF;
    END IF;
END
$$;

COMMENT ON TABLE  holdings IS 'Security holding within a portfolio.';
COMMENT ON COLUMN holdings.id           IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN holdings.portfolio_id IS 'Parent portfolio. FK to portfolios(id); child rows removed when parent is removed.';
COMMENT ON COLUMN holdings.security_id  IS 'Security identifier (e.g. ticker symbol).';
COMMENT ON COLUMN holdings.quantity     IS 'Held quantity; NUMERIC(38,10) avoids floating-point rounding errors.';
COMMENT ON COLUMN holdings.created_at   IS 'Creation timestamp, UTC.';
COMMENT ON COLUMN holdings.updated_at   IS 'Last-update timestamp, UTC.';


-- ---------------------------------------------------------------------------
-- transactions
-- ---------------------------------------------------------------------------

-- (1) Fresh-DB path.
CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT            NOT NULL,
    portfolio_id TEXT            NOT NULL,
    kind         TEXT            NOT NULL,
    amount       NUMERIC(38, 10) NOT NULL,
    created_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- (2) Legacy-DB path: add non-id columns idempotently.
--     id is NOT re-added -- see note in users section above.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS portfolio_id TEXT            NOT NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS kind         TEXT            NOT NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS amount       NUMERIC(38, 10) NOT NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ     NOT NULL DEFAULT now();
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ     NOT NULL DEFAULT now();

-- (3) Guarded backfill from legacy records.data JSONB column.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'records'
          AND  column_name = 'data'
    ) THEN
        UPDATE transactions t
        SET   portfolio_id = r.data->>'portfolio_id',
              kind         = r.data->>'kind',
              amount       = COALESCE(
                               (r.data->>'amount')::numeric(38, 10),
                               0
                             ),
              created_at   = COALESCE(
                               (r.data->>'created_at')::timestamptz,
                               now()
                             ),
              updated_at   = COALESCE(
                               (r.data->>'updated_at')::timestamptz,
                               now()
                             )
        FROM   records r
        WHERE  r.table_name = 'transactions'
          AND  r.id         = t.id
          AND  (
                  t.portfolio_id IS DISTINCT FROM (r.data->>'portfolio_id')
               OR t.kind         IS DISTINCT FROM (r.data->>'kind')
               OR t.amount       IS DISTINCT FROM COALESCE((r.data->>'amount')::numeric(38, 10), 0)
               OR t.created_at   IS DISTINCT FROM COALESCE((r.data->>'created_at')::timestamptz, now())
               OR t.updated_at   IS DISTINCT FROM COALESCE((r.data->>'updated_at')::timestamptz, now())
              );
    END IF;
END
$$;

-- (4a) Index: idempotent.
CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_id ON transactions (portfolio_id);

-- (4b) FK: guarded by pg_constraint existence check + orphan count.
--      Orphan transactions (portfolio_id has no matching row in portfolios)
--      cause the FK to be skipped with a RAISE NOTICE; no rows are deleted.
DO $$
DECLARE
    _orphan_count INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'fk_transactions_portfolio'
    ) THEN
        SELECT COUNT(*)
        INTO   _orphan_count
        FROM   transactions tx
        WHERE  NOT EXISTS (
            SELECT 1 FROM portfolios p WHERE p.id = tx.portfolio_id
        );

        IF _orphan_count > 0 THEN
            RAISE NOTICE 'FK fk_transactions_portfolio NOT added: % orphan transaction(s) have no matching portfolio_id. Inspect orphans with: SELECT tx.id, tx.portfolio_id, tx.kind FROM transactions tx WHERE NOT EXISTS (SELECT 1 FROM portfolios p WHERE p.id = tx.portfolio_id) ORDER BY tx.id;',
                _orphan_count;
        ELSE
            ALTER TABLE transactions
                ADD CONSTRAINT fk_transactions_portfolio
                FOREIGN KEY (portfolio_id) REFERENCES portfolios(id)
                ON DELETE CASCADE;
        END IF;
    END IF;
END
$$;

COMMENT ON TABLE  transactions IS 'Financial transaction within a portfolio.';
COMMENT ON COLUMN transactions.id           IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN transactions.portfolio_id IS 'Parent portfolio. FK to portfolios(id); child rows removed when parent is removed.';
COMMENT ON COLUMN transactions.kind        IS 'Transaction kind (e.g. BUY/SELL). No CHECK constraint -- recon V4 shows plain str, no closed enum set.';
COMMENT ON COLUMN transactions.amount      IS 'Transaction amount; NUMERIC(38,10) avoids floating-point rounding errors.';
COMMENT ON COLUMN transactions.created_at  IS 'Creation timestamp, UTC.';
COMMENT ON COLUMN transactions.updated_at  IS 'Last-update timestamp, UTC.';

COMMIT;
