-- verify_migration.sql
--
-- STORY-4: ops/QA script for confirming the US-stock normalization
-- migration applied correctly. Runs entirely as SELECTs, never
-- modifies data.
--
-- Returns two result sets (in order):
--
--   1. stocks_summary: one row of counts describing the current
--      state of the `stocks` table for the columns the migration
--      normalized (currency, exchange, symbol_suffix).
--        total_rows      — total rows in stocks
--        bad_currency    — rows where currency <> 'USD'
--        bad_exchange    — rows where exchange IS NOT NULL
--        bad_suffix      — rows where symbol_suffix IS NOT NULL
--
--   2. migration_log_summary: one row describing the most recent
--      migration_log entry for migration_name =
--      'us_stock_portfolio_defaults_v1'.
--        log_total       — total rows for that migration_name
--        log_success     — number of those rows with status='SUCCESS'
--        log_failed      — number with status='FAILED'
--        log_dry_run     — number with status='DRY_RUN'
--        last_status     — status of the most recent run (NULL if no
--                          row exists at all)
--        last_run_at     — run_at of the most recent row (NULL if none)
--
-- This script is read-only by design -- it intentionally never
-- touches migration_log or stocks. The wrapper
-- (scripts/verify_migration.py) decides pass/fail from these two
-- result sets and exits with the right code for ops/QA.

-- ---------------------------------------------------------------------------
-- (1) Stocks-table summary
SELECT
    COUNT(*)::bigint                                              AS total_rows,
    COUNT(*) FILTER (WHERE currency <> 'USD')::bigint             AS bad_currency,
    COUNT(*) FILTER (WHERE exchange IS NOT NULL)::bigint          AS bad_exchange,
    COUNT(*) FILTER (WHERE symbol_suffix IS NOT NULL)::bigint     AS bad_suffix
FROM stocks;

-- ---------------------------------------------------------------------------
-- (2) migration_log summary for the us-stock migration
SELECT
    COUNT(*)::bigint                                              AS log_total,
    COUNT(*) FILTER (WHERE status = 'SUCCESS')::bigint            AS log_success,
    COUNT(*) FILTER (WHERE status = 'FAILED')::bigint             AS log_failed,
    COUNT(*) FILTER (WHERE status = 'DRY_RUN')::bigint            AS log_dry_run,
    (
        SELECT status
        FROM migration_log
        WHERE migration_name = 'us_stock_portfolio_defaults_v1'
        ORDER BY run_at DESC
        LIMIT 1
    )                                                             AS last_status,
    (
        SELECT run_at
        FROM migration_log
        WHERE migration_name = 'us_stock_portfolio_defaults_v1'
        ORDER BY run_at DESC
        LIMIT 1
    )                                                             AS last_run_at
FROM migration_log
WHERE migration_name = 'us_stock_portfolio_defaults_v1';
