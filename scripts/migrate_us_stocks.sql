-- migrate_us_stocks.sql
--
-- Skeleton migration script for backfilling/normalizing US stock tickers
-- (e.g. exchange, currency, country) into the user_portfolio / stocks
-- tables. This file is intentionally a skeleton at this stage:
-- subsequent stories (issue #65 and follow-ups) will fill in the real
-- load / count / update logic. The structure here defines the contract
-- the wrapper (scripts/run_migration.sh) and downstream stories rely on.
--
-- Conventions:
--   * Idempotent where possible (safe to re-run).
--   * Runs inside a single transaction; the wrapper captures psql's
--     real exit status and the whole migration aborts on the first
--     statement failure (psql default with -v ON_ERROR_STOP=1 is set
--     by run_migration.sh).
--   * Temporary working tables live in pg_temp so they don't leak.

\echo '== migrate_us_stocks: start =='

-- ---------------------------------------------------------------------------
-- (1) Load the US ticker CSV into tmp_us_tickers
--
-- STUB: a later story will COPY/load the CSV (e.g. data/us_tickers.csv)
-- into a temporary table with columns matching the source file. Expected
-- shape roughly:
--
--   CREATE TEMP TABLE tmp_us_tickers (
--       ticker      text PRIMARY KEY,
--       exchange    text,
--       currency    text,
--       country     text,
--       ...
--   );
--   -- \copy tmp_us_tickers FROM 'data/us_tickers.csv' WITH (FORMAT csv, HEADER true)
--
-- For now this section is a placeholder so the wrapper has a runnable
-- script end-to-end.
\echo '-- (1) load US ticker CSV: STUB --'

-- ---------------------------------------------------------------------------
-- (2) Count matched rows
--
-- STUB: a later story will join tmp_us_tickers against the target table
-- (e.g. stocks / user_portfolio_holdings) and SELECT count(*) so we can
-- log how many rows are about to be updated. Expected shape:
--
--   SELECT count(*) AS rows_to_update
--     FROM stocks s
--     JOIN tmp_us_tickers t ON s.ticker = t.ticker
--    WHERE s.country IS DISTINCT FROM 'US' OR s.exchange IS DISTINCT FROM t.exchange ...;
\echo '-- (2) count matched rows: STUB --'

-- ---------------------------------------------------------------------------
-- (3) Perform the UPDATE
--
-- STUB: a later story will run the real UPDATE joining tmp_us_tickers
-- onto the target table. Expected shape:
--
--   UPDATE stocks s
--      SET country   = t.country,
--          exchange  = t.exchange,
--          currency  = t.currency,
--          updated_at = now()
--     FROM tmp_us_tickers t
--    WHERE s.ticker = t.ticker
--      AND (...)
--    ;
\echo '-- (3) perform UPDATE: STUB --'

\echo '== migrate_us_stocks: end (skeleton) =='