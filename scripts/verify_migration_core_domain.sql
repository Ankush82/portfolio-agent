-- verify_migration_core_domain.sql
--
-- STORY-13 / STORY-12: ops/QA script for confirming the four core domain
-- tables (users, portfolios, holdings, transactions) have been created
-- with the correct DDL. This script is read-only -- it never modifies any
-- table or migration_log.
--
-- Returns nine result sets (one per check category):
--
--   1. users_table:      EXISTS (1) or ABSENT (0)
--   2. users_columns:    one row per expected column with
--                          expected_type   — TEXT | TEXT | JSONB | TIMESTAMPTZ | TIMESTAMPTZ
--                          expected_null   — YES | NO  (NOT NULL check)
--                        All rows must be returned; empty means table absent.
--   3. users_pk:         pk_present (1/0) — does PRIMARY KEY (id) exist?
--   4. users_indexes:    one row per expected index; absent rows are
--                          NOT reported (the query only returns rows that
--                          exist, so an absent expected index shows as
--                          zero rows returned).  The wrapper counts how
--                          many rows are expected and fails if fewer are
--                          returned.
--   5. portfolios_table: EXISTS (1) or ABSENT (0)
--   6. portfolios_columns / portfolios_pk / portfolios_indexes: same pattern
--   7. holdings_table:   EXISTS (1) or ABSENT (0)
--   8. holdings_columns / holdings_pk / holdings_indexes: same pattern
--   9. transactions_table: EXISTS (1) or ABSENT (0)
--  10. transactions_columns / transactions_pk / transactions_indexes: same pattern
--  11. foreign_keys:     one row per expected FK; each row carries
--                          fk_name, fk_present (1/0), orphan_count
--                        When fk_present=0, orphan_count is the count of
--                        child rows that have no matching parent (so the FK
--                        was deliberately skipped by the migration rather
--                        than forgotten).

-- ---------------------------------------------------------------------------
-- (1) users table existence
SELECT 1 AS exists_flag
WHERE EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE  table_schema = 'public'
      AND  table_name   = 'users'
);

-- ---------------------------------------------------------------------------
-- (2) users columns
--     Expected: id(TEXT,NO), email(TEXT,YES), preferences(JSONB,NO),
--               created_at(TIMESTAMPTZ,NO), updated_at(TIMESTAMPTZ,NO)
SELECT
    column_name                                   AS col,
    UPPER(data_type)
    || COALESCE('(' || character_maximum_length || ')', '')
    AS expected_type,
    is_nullable                                   AS expected_null
FROM information_schema.columns
WHERE  table_schema = 'public'
  AND  table_name   = 'users'
  AND  column_name IN ('id', 'email', 'preferences', 'created_at', 'updated_at')
ORDER BY column_name;

-- ---------------------------------------------------------------------------
-- (3) users primary key
SELECT 1 AS pk_present
WHERE EXISTS (
    SELECT 1 FROM information_schema.table_constraints tc
    JOIN   information_schema.key_column_usage kcu
           ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema   = kcu.table_schema
          AND tc.table_name     = kcu.table_name
    WHERE  tc.constraint_type = 'PRIMARY KEY'
      AND  tc.table_schema    = 'public'
      AND  tc.table_name      = 'users'
      AND  kcu.column_name    = 'id'
);

-- ---------------------------------------------------------------------------
-- (4) users indexes  (expected: idx_users_email)
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE  schemaname = 'public'
  AND  tablename  = 'users'
  AND  indexname = 'idx_users_email';

-- ---------------------------------------------------------------------------
-- (5) portfolios table existence
SELECT 1 AS exists_flag
WHERE EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE  table_schema = 'public'
      AND  table_name   = 'portfolios'
);

-- ---------------------------------------------------------------------------
-- (6) portfolios columns
SELECT
    column_name                                   AS col,
    UPPER(data_type)
    || COALESCE('(' || character_maximum_length || ')', '')
    AS expected_type,
    is_nullable                                   AS expected_null
FROM information_schema.columns
WHERE  table_schema = 'public'
  AND  table_name   = 'portfolios'
  AND  column_name IN ('id', 'user_id', 'created_at', 'updated_at')
ORDER BY column_name;

-- ---------------------------------------------------------------------------
-- (7) portfolios primary key
SELECT 1 AS pk_present
WHERE EXISTS (
    SELECT 1 FROM information_schema.table_constraints tc
    JOIN   information_schema.key_column_usage kcu
           ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema   = kcu.table_schema
          AND tc.table_name     = kcu.table_name
    WHERE  tc.constraint_type = 'PRIMARY KEY'
      AND  tc.table_schema    = 'public'
      AND  tc.table_name      = 'portfolios'
      AND  kcu.column_name    = 'id'
);

-- ---------------------------------------------------------------------------
-- (8) portfolios indexes  (expected: idx_portfolios_user_id)
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE  schemaname = 'public'
  AND  tablename  = 'portfolios'
  AND  indexname  = 'idx_portfolios_user_id';

-- ---------------------------------------------------------------------------
-- (9) holdings table existence
SELECT 1 AS exists_flag
WHERE EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE  table_schema = 'public'
      AND  table_name   = 'holdings'
);

-- ---------------------------------------------------------------------------
-- (10) holdings columns
SELECT
    column_name                                   AS col,
    UPPER(data_type)
    || COALESCE('(' || character_maximum_length || ')', '')
    AS expected_type,
    is_nullable                                   AS expected_null
FROM information_schema.columns
WHERE  table_schema = 'public'
  AND  table_name   = 'holdings'
  AND  column_name IN (
          'id', 'portfolio_id', 'security_id', 'quantity',
          'currency', 'exchange', 'symbol_suffix',
          'created_at', 'updated_at'
      )
ORDER BY column_name;

-- ---------------------------------------------------------------------------
-- (11) holdings primary key
SELECT 1 AS pk_present
WHERE EXISTS (
    SELECT 1 FROM information_schema.table_constraints tc
    JOIN   information_schema.key_column_usage kcu
           ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema   = kcu.table_schema
          AND tc.table_name     = kcu.table_name
    WHERE  tc.constraint_type = 'PRIMARY KEY'
      AND  tc.table_schema    = 'public'
      AND  tc.table_name      = 'holdings'
      AND  kcu.column_name    = 'id'
);

-- ---------------------------------------------------------------------------
-- (12) holdings indexes  (expected: idx_holdings_portfolio_id, uq_holdings_portfolio_security)
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE  schemaname = 'public'
  AND  tablename  = 'holdings'
  AND  indexname  IN ('idx_holdings_portfolio_id', 'uq_holdings_portfolio_security');

-- ---------------------------------------------------------------------------
-- (13) transactions table existence
SELECT 1 AS exists_flag
WHERE EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE  table_schema = 'public'
      AND  table_name   = 'transactions'
);

-- ---------------------------------------------------------------------------
-- (14) transactions columns
SELECT
    column_name                                   AS col,
    UPPER(data_type)
    || COALESCE('(' || character_maximum_length || ')', '')
    AS expected_type,
    is_nullable                                   AS expected_null
FROM information_schema.columns
WHERE  table_schema = 'public'
  AND  table_name   = 'transactions'
  AND  column_name IN ('id', 'portfolio_id', 'kind', 'amount', 'created_at', 'updated_at')
ORDER BY column_name;

-- ---------------------------------------------------------------------------
-- (15) transactions primary key
SELECT 1 AS pk_present
WHERE EXISTS (
    SELECT 1 FROM information_schema.table_constraints tc
    JOIN   information_schema.key_column_usage kcu
           ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema   = kcu.table_schema
          AND tc.table_name     = kcu.table_name
    WHERE  tc.constraint_type = 'PRIMARY KEY'
      AND  tc.table_schema    = 'public'
      AND  tc.table_name      = 'transactions'
      AND  kcu.column_name    = 'id'
);

-- ---------------------------------------------------------------------------
-- (16) transactions indexes  (expected: idx_transactions_portfolio_id)
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE  schemaname = 'public'
  AND  tablename  = 'transactions'
  AND  indexname  = 'idx_transactions_portfolio_id';

-- ---------------------------------------------------------------------------
-- (17) foreign keys: presence + orphan count
--
--   Each expected FK may or may not be present (STORY-12 deliberately skips
--   adding an FK when legacy orphan rows exist).  The migration still
--   passes if the FK is absent AND orphan_count > 0 -- that state is
--   intentional.  The FK-absent / orphan=0 case is a real failure (FK
--   should have been added and wasn't).  The FK-present case is always OK.
--
--   fk_name           — constraint name as declared in migrate_core_domain_entities.sql
--   fk_present        — 1 if the constraint exists in pg_constraint, 0 otherwise
--   orphan_count      — rows in the child table whose foreign key column
--                        references a parent row that does not exist
SELECT
    fkf.fk_name,
    (c.oid IS NOT NULL)::int AS fk_present,
    CASE
        WHEN fkf.fk_name = 'fk_portfolios_user'
             THEN (
                 SELECT COUNT(*) FROM portfolios p
                 WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)
             )
        WHEN fkf.fk_name = 'fk_holdings_portfolio'
             THEN (
                 SELECT COUNT(*) FROM holdings h
                 WHERE NOT EXISTS (SELECT 1 FROM portfolios p WHERE p.id = h.portfolio_id)
             )
        WHEN fkf.fk_name = 'fk_transactions_portfolio'
             THEN (
                 SELECT COUNT(*) FROM transactions tx
                 WHERE NOT EXISTS (SELECT 1 FROM portfolios p WHERE p.id = tx.portfolio_id)
             )
        ELSE 0
    END AS orphan_count
FROM (
    -- Expected FKs for this migration
    VALUES
        ('fk_portfolios_user',      'portfolios', 'user_id',       'users'),
        ('fk_holdings_portfolio',   'holdings',   'portfolio_id',  'portfolios'),
        ('fk_transactions_portfolio','transactions','portfolio_id', 'portfolios')
) AS fkf(fk_name, child_table, fk_column, parent_table)
LEFT JOIN pg_constraint c
       ON c.conname = fkf.fk_name
      AND c.contype = 'f';

