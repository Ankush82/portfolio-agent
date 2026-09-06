-- migrate_core_domain_entities.sql
--
-- Migration ID:  002_core_domain_entities_v1
-- Purpose:       Create the four core domain tables (users, portfolios,
--                holdings, transactions) with typed columns, primary keys,
--                foreign keys, unique constraints, and indexes.
-- Tables:        users, portfolios, holdings, transactions
--
-- Forward-only migration. There is no separate down/rollback SQL file
-- in this repo (per docs/migrations/us_stock_migration.md "There is no
-- separate down-migration script in this repo — do not invent one").
-- Manual rollback requires:
--
--   DROP TABLE IF EXISTS transactions;
--   DROP TABLE IF EXISTS holdings;
--   DROP TABLE IF EXISTS portfolios;
--   DROP TABLE IF EXISTS users;
--
--   -- WARNING: the above DROP TABLE statements destroy all data in those
--   -- tables. Take a snapshot or pg_dump before running.
--
-- Placement convention (per docs/repo-layer-recon.md item V6):
--   scripts/migrate_<name>.sql  — same directory, same naming scheme
--   as scripts/migrate_us_stocks.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id           TEXT        NOT NULL,
    email        TEXT,
    preferences  JSONB      NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);

COMMENT ON TABLE  users IS 'Core user entity. preferences is a JSONB blob.';
COMMENT ON COLUMN users.id          IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN users.email        IS 'User email address. No UNIQUE constraint — recon V5 found no code or test requiring uniqueness.';
COMMENT ON COLUMN users.preferences IS 'JSONB preferences blob, defaults to {}.';
COMMENT ON COLUMN users.created_at   IS 'Creation timestamp, UTC.';
COMMENT ON COLUMN users.updated_at   IS 'Last-update timestamp, UTC.';


-- ---------------------------------------------------------------------------
-- portfolios
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS portfolios (
    id         TEXT        NOT NULL,
    user_id    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT fk_portfolios_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_portfolios_user_id ON portfolios (user_id);

COMMENT ON TABLE  portfolios IS 'Portfolio entity, owned by a user.';
COMMENT ON COLUMN portfolios.id      IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN portfolios.user_id IS 'Owner user. FK to users(id) ON DELETE CASCADE.';


-- ---------------------------------------------------------------------------
-- holdings
-- ---------------------------------------------------------------------------

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
    PRIMARY KEY (id),
    CONSTRAINT fk_holdings_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios(id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_holdings_portfolio_security
    ON holdings (portfolio_id, security_id);

CREATE INDEX IF NOT EXISTS idx_holdings_portfolio_id ON holdings (portfolio_id);

COMMENT ON TABLE  holdings IS 'Security holding within a portfolio.';
COMMENT ON COLUMN holdings.id           IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN holdings.portfolio_id IS 'Parent portfolio. FK to portfolios(id) ON DELETE CASCADE.';
COMMENT ON COLUMN holdings.security_id  IS 'Security identifier (e.g. ticker symbol).';
COMMENT ON COLUMN holdings.quantity     IS 'Held quantity; NUMERIC(38,10) avoids floating-point rounding errors.';
COMMENT ON COLUMN holdings.created_at   IS 'Creation timestamp, UTC.';
COMMENT ON COLUMN holdings.updated_at  IS 'Last-update timestamp, UTC.';


-- ---------------------------------------------------------------------------
-- transactions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT            NOT NULL,
    portfolio_id TEXT            NOT NULL,
    kind         TEXT            NOT NULL,
    amount       NUMERIC(38, 10) NOT NULL,
    created_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT fk_transactions_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_id ON transactions (portfolio_id);

COMMENT ON TABLE  transactions IS 'Financial transaction within a portfolio.';
COMMENT ON COLUMN transactions.id           IS 'Primary key; TEXT (not UUID) per domain model.';
COMMENT ON COLUMN transactions.portfolio_id IS 'Parent portfolio. FK to portfolios(id) ON DELETE CASCADE.';
COMMENT ON COLUMN transactions.kind        IS 'Transaction kind (e.g. BUY/SELL). No CHECK constraint — recon V4 shows plain str, no closed enum set.';
COMMENT ON COLUMN transactions.amount      IS 'Transaction amount; NUMERIC(38,10) avoids floating-point rounding errors.';
COMMENT ON COLUMN transactions.created_at  IS 'Creation timestamp, UTC.';
COMMENT ON COLUMN transactions.updated_at IS 'Last-update timestamp, UTC.';

COMMIT;
