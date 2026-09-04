-- migrate_us_stocks.sql
--
-- Skeleton migration script for backfilling/normalizing US stock tickers
-- (e.g. exchange, currency, country) into the user_portfolio / stocks
-- tables. The structure here defines the contract the wrapper
-- (scripts/run_migration.sh) and downstream stories rely on.
--
-- Conventions:
--   * Idempotent where possible (safe to re-run).
--   * Runs inside a single transaction; the wrapper captures psql's
--     real exit status and the whole migration aborts on the first
--     statement failure (psql default with -v ON_ERROR_STOP=1 is set
--     by run_migration.sh).
--   * Temporary working tables live in pg_temp so they don't leak.
--
-- NOTE (STORY-3 vs STORY-2/STORY-1 schema mismatch, left for a follow-up
-- story to reconcile): section (3) below was authored and real-tested
-- (issue #65) against a standalone `stocks(id, currency, exchange,
-- symbol_suffix)` table joined to `tmp_us_tickers` on `id`. The US stock
-- detection story (issue #64/#71, already merged) instead populates
-- `tmp_us_tickers(ticker TEXT PRIMARY KEY)` with no `id` column. Kept
-- exactly as QA verified it rather than hand-edited unverified to match
-- -- the join key needs a real decision (and a re-tested change) in a
-- follow-up story, not a guess made while resolving a merge conflict.
--
-- NOTE (real bug, found live while merging): this file has two real
-- callers with incompatible needs -- run_migration.sh invokes it via the
-- real `psql` CLI (which understands `\echo` and other backslash
-- meta-commands), but scripts/migrate_us_stocks.py's Python entry point
-- reads this same file and executes it directly through psycopg, which
-- only understands real SQL and raises a syntax error on `\echo`. No
-- `\echo` (or any other psql meta-command) belongs in this file again --
-- plain `--` comments carry the same information for a human reader
-- without breaking the psycopg-based caller.

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
-- (1) load US ticker CSV: STUB

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
-- (2) count matched rows: STUB

-- ---------------------------------------------------------------------------
-- (3) Perform the UPDATE (STORY-3, issue #65)
--
-- Idempotent: the WHERE clause restricts the UPDATE to rows that
-- actually differ from the target values, so a second run affects zero
-- rows. The caller is responsible for dry-run gating (see
-- run_migration.sh / MIGRATION_DRY_RUN); this SQL always issues the
-- real UPDATE and returns the rowcount via RETURNING.

UPDATE stocks
SET currency = 'USD',
    exchange = NULL,
    symbol_suffix = NULL
FROM tmp_us_tickers
WHERE stocks.id = tmp_us_tickers.id
  AND (
        stocks.currency <> 'USD'
        OR stocks.exchange IS NOT NULL
        OR stocks.symbol_suffix IS NOT NULL
      )
RETURNING stocks.id;
