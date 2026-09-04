-- migrate_us_stocks.sql
--
-- Story STORY-3: normalize identified US stock rows so they all carry
-- currency='USD', exchange=NULL, symbol_suffix=NULL.
--
-- Preconditions (created by the caller / test harness):
--   * stocks(id TEXT PK, currency TEXT, exchange TEXT, symbol_suffix TEXT)
--   * tmp_us_tickers(id TEXT PK)  -- the rows this migration targets
--
-- This script is idempotent: the WHERE clause restricts the UPDATE to
-- rows that actually differ from the target values, so a second run
-- affects zero rows. The caller is responsible for dry-run gating
-- (see migrate_us_stocks.py); the SQL itself always issues the real
-- UPDATE and returns the rowcount.

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