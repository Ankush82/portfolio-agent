# US-Stock Portfolio Defaults Migration — Runbook

Operational runbook for the **US-stock portfolio defaults** migration (`migration_name = 'us_stock_portfolio_defaults_v1'`). This is the story of normalizing rows in the `stocks` table so every US ticker ends up with `currency = 'USD'`, `exchange IS NULL`, and `symbol_suffix IS NULL`.

It is written for backend, DevOps, and QA. Anyone on the team should be able to run, verify, and (if needed) reason about rolling back the migration from this document alone — no need to consult the original implementer.

The authoritative files this runbook references, all in this repo, are:

| File | Role |
|---|---|
| `scripts/migrate_us_stocks.sql` | The actual `UPDATE` statement applied to the `stocks` table (idempotent; restricted by a `WHERE` clause that touches only rows that differ from the target values). |
| `scripts/migrate_us_stocks.py` | Python entry point that reads `migrate_us_stocks.sql`, runs the `UPDATE` (or a `COUNT(*)` under dry-run), and **writes exactly one row to `migration_log` on every invocation** — success, dry-run, or failure. The module docstring describes this contract in detail. |
| `scripts/run_migration.sh` | Shell wrapper that invokes `psql` against `migrate_us_stocks.sql`, gated on `MIGRATION_DRY_RUN`. Used by the CI workflow. |
| `scripts/verify_migration.py` | Read-only ops/QA verifier; runs `verify_migration.sql` and exits with code 0 on success, 1 on failure. |
| `scripts/verify_migration.sql` | Two `SELECT`-only result sets: a `stocks` summary and a `migration_log` summary. |
| `.github/workflows/migration.yml` | CI workflow (job `migrate`) that runs `scripts/run_migration.sh` against a real Postgres service container, on every push to `main` and on `workflow_dispatch`. |

---

## 1. Prerequisites

Before running anything:

1. **PostgreSQL client available locally** — `run_migration.sh` shells out to `psql`, so it must be on `PATH`. The Python entry points (`migrate_us_stocks.py`, `verify_migration.py`) need `psycopg` instead.
2. **`DATABASE_URL` exported** — a `postgres://...` DSN pointing at the target database. The wrapper aborts with a clear error if this is unset.
3. **Project dependencies installed** for the Python paths:
   ```bash
   uv sync --extra dev
   ```
   `src/` is on the test path via `pyproject.toml`, so `from infrastructure_postgres import ...` and `from scripts.migrate_us_stocks import ...` style imports resolve without any extra `PYTHONPATH` juggling.

---

## 2. Running the migration

There are two real entry points and two modes. They are **not interchangeable** — the CI workflow uses the shell wrapper; the Python entry point is the one that writes the `migration_log` row.

### 2a. CI workflow (recommended for production / deployment runs)

The migration runs in CI on every push to `main` that touches one of the migration files (see the `paths:` filter in `.github/workflows/migration.yml`), and can be manually re-triggered.

**To manually re-trigger (recommended when you want to verify a dry-run or retry a failed run):**

1. Go to **Actions → "US Stock Migration" → Run workflow**.
2. For a dry-run, tick the **`dry_run`** input. Leave it unticked for a real run.
3. Watch the `migrate` job. A non-zero exit from `bash scripts/run_migration.sh` fails the job, which fails the workflow run and surfaces GitHub's built-in failure notifications — that is the real alerting channel.
4. After the run, download the **`migration-log`** artifact (the `/tmp/migrate_us_stocks.log` file, retained for 90 days by GitHub Actions itself).

The `concurrency: us-stock-migration` group means a second push while a run is still in flight queues behind it — so two migration attempts never race against the same database.

### 2b. Local shell wrapper (`scripts/run_migration.sh`)

Used for local end-to-end runs against a real database (typically `docker-compose up -d` for local Postgres).

**Dry-run (no rows committed):**

```bash
export DATABASE_URL='postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent'
export MIGRATION_DRY_RUN=true
bash scripts/run_migration.sh
```

When `MIGRATION_DRY_RUN=true`, the wrapper prints `INFO: MIGRATION_DRY_RUN=true; running migration in a transaction that will ROLLBACK.` and passes `--single-transaction` to `psql`, so the entire script runs inside one `BEGIN` that is then rolled back. Nothing is committed, but every statement is still executed and any SQL error still aborts the run.

**Real run (commits the `UPDATE`):**

```bash
export DATABASE_URL='postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent'
unset MIGRATION_DRY_RUN   # defaults to "false" inside the wrapper
bash scripts/run_migration.sh
```

Both modes tee stdout+stderr to `${LOG_FILE:-/tmp/migrate_us_stocks.log}` and propagate `psql`'s real exit status via `set -euo pipefail`.

### 2c. Python entry point (`scripts/migrate_us_stocks.py`)

This is the path that **writes to `migration_log`**. `MIGRATION_DRY_RUN` is interpreted case-insensitively (`true`/`1`/`yes`); anything else is treated as "real run".

**Dry-run (records one row with `status='DRY_RUN'`, no `UPDATE` issued):**

```bash
export DATABASE_URL='postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent'
export MIGRATION_DRY_RUN=true
python -m scripts.migrate_us_stocks
# prints: DRY-RUN: rows_updated=<N>
```

The dry-run count uses the exact same `WHERE` clause as the real `UPDATE`, so the two numbers always agree on the same input data.

**Real run (records one row with `status='SUCCESS'` and `rows_affected=<N>`):**

```bash
export DATABASE_URL='postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent'
unset MIGRATION_DRY_RUN
python -m scripts.migrate_us_stocks
# prints: APPLIED: rows_updated=<N>
```

**Failure mode:** if the real `UPDATE` raises, the `except` branch writes one `migration_log` row with `status='FAILED'`, the `error_message`, and `dry_run=false` **before** re-raising. The connection is `autocommit=True`, so a failed single-statement migration rolls back on its own — see §4.

---

## 3. Checking `migration_log`

Every invocation of the Python entry point — dry-run, successful real run, or a failed real run — writes exactly one row to the `migration_log` table. The table is created by `src/infrastructure_postgres.py` with this real schema:

```
migration_log(
    id              BIGSERIAL PRIMARY KEY,
    migration_name  VARCHAR NOT NULL,
    run_at          TIMESTAMPTZ NOT NULL,
    status          VARCHAR NOT NULL,    -- 'DRY_RUN' | 'SUCCESS' | 'FAILED'
    rows_affected   BIGINT,
    error_message   TEXT,
    dry_run         BOOLEAN NOT NULL
)
```

with index `idx_migration_log_name_run (migration_name, run_at)`.

### Example: last 10 runs of this migration

```sql
SELECT run_at,
       status,
       rows_affected,
       dry_run,
       LEFT(error_message, 200) AS error_message_excerpt
FROM   migration_log
WHERE  migration_name = 'us_stock_portfolio_defaults_v1'
ORDER BY run_at DESC
LIMIT 10;
```

### Example: most recent status only

```sql
SELECT status, run_at, rows_affected, dry_run
FROM   migration_log
WHERE  migration_name = 'us_stock_portfolio_defaults_v1'
ORDER BY run_at DESC
LIMIT 1;
```

### Example: aggregate counts (matches `verify_migration.sql`'s summary)

```sql
SELECT
    COUNT(*)                                                        AS log_total,
    COUNT(*) FILTER (WHERE status = 'SUCCESS')                      AS log_success,
    COUNT(*) FILTER (WHERE status = 'FAILED')                       AS log_failed,
    COUNT(*) FILTER (WHERE status = 'DRY_RUN')                      AS log_dry_run
FROM   migration_log
WHERE  migration_name = 'us_stock_portfolio_defaults_v1';
```

---

## 4. Running the verification script

`scripts/verify_migration.py` is **strictly read-only** — it never modifies `stocks` or `migration_log`. It runs `scripts/verify_migration.sql` (two `SELECT`s), prints summary counts regardless of outcome, and exits with the documented code.

```bash
export DATABASE_URL='postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent'
python -m scripts.verify_migration
```

### Interpreting the output

The script always prints these lines:

```
stocks.total_rows     = <N>
stocks.bad_currency   = <N>   # must be 0 for a pass
stocks.bad_exchange   = <N>   # must be 0 for a pass
stocks.bad_suffix     = <N>   # must be 0 for a pass
migration_log.success = 1|0   # 1 means at least one SUCCESS row exists
```

**Exit code `0` — pass:** every `bad_*` count is `0` **and** there is at least one `SUCCESS` row for `migration_name = 'us_stock_portfolio_defaults_v1'`.

**Exit code `1` — fail:** one or more checks failed. The script writes the human-readable reasons to stderr:

```
FAIL: <N> stock row(s) have currency <> 'USD'
FAIL: migration_log has no SUCCESS row for migration_name='us_stock_portfolio_defaults_v1'
```

A `stocks.total_rows` of `0` is treated as **vacuously normalized** and still passes — an empty `stocks` table trivially has zero bad rows.

The CI workflow does not currently call `verify_migration.py` automatically after a real run. To confirm a deployment succeeded end-to-end, either run the verifier locally against the target database, or query `migration_log` directly (§3).

---

## 5. Rollback

There is **no separate down-migration script in this repo** — do not invent one. The migration is designed to be reversible through two distinct mechanisms depending on whether the bad run has been committed yet.

### 5a. Pre-commit / dry-run safety net (automatic)

`MIGRATION_DRY_RUN=true` does not need any manual cleanup: the shell wrapper passes `--single-transaction` to `psql`, so the entire `migrate_us_stocks.sql` script runs inside one `BEGIN` that is then rolled back when `psql` exits. Nothing is committed, no `migration_log` row is written by the shell path (the `migration_log` row only exists for the **Python** entry point), and the database is left exactly as it was.

Under the Python entry point, the connection is `autocommit=True`. The migration is a single `UPDATE` statement, so a failure raises before the implicit transaction commits — the rollback is automatic, and the `migration_log` row records `status='FAILED'` with the exception text in `error_message`.

In both cases: **if you suspect something is wrong, run with `MIGRATION_DRY_RUN=true` first.** The wrapper's own banner line (`INFO: MIGRATION_DRY_RUN=true; ...`) is the visible confirmation that you are in the safe path.

### 5b. Post-commit manual remediation

If a real (non-dry-run) migration has already been committed and you discover an issue afterwards, the recovery model is **inspect `migration_log` to find the exact run, then write a corrective SQL by hand**. There is no automated down-migration in this repo.

1. **Identify the offending run** by querying `migration_log`:
   ```sql
   SELECT id, run_at, status, rows_affected, dry_run, error_message
   FROM   migration_log
   WHERE  migration_name = 'us_stock_portfolio_defaults_v1'
     AND  dry_run = false
   ORDER BY run_at DESC;
   ```
   The `rows_affected` column tells you how many rows the bad run actually changed. If `status='FAILED'`, the real `UPDATE` was rolled back automatically (§5a) and you likely have no data to undo — proceed to fixing the cause and re-running.

2. **Snapshot before any manual fix**, the same way you would for any other production change:
   ```bash
   pg_dump --data-only --table=stocks "${DATABASE_URL}" \
       > "$(date -u +%Y%m%dT%H%M%SZ)_stocks_snapshot.sql"
   ```

3. **Inspect the affected rows** by cross-referencing `tmp_us_tickers` (the temp table the migration joined against) and the `stocks` columns it normalized:
   ```sql
   SELECT s.id, s.currency, s.exchange, s.symbol_suffix
   FROM   stocks s
   JOIN   tmp_us_tickers t ON t.id = s.id;
   ```

4. **Author a targeted corrective `UPDATE`** that restores the original values for the rows identified in step 1. Do **not** blanket-restore — the idempotency contract of `migrate_us_stocks.sql` (only update rows that actually differ from the target) means a fresh dry-run should report `rows_affected = 0` once the data is consistent.

5. **Record the corrective run** in `migration_log` using a distinct `migration_name` (e.g. `us_stock_portfolio_defaults_v1_revert_<runid>`) and the same `(status, rows_affected, dry_run, error_message)` shape, so the audit trail stays queryable in the same way as the original migration.

6. **Re-run the verifier** (`scripts/verify_migration.py`) and confirm exit code `0` and all `bad_*` counts at zero.

The point: `migration_log` is the audit trail, not a separate rollback table. Any post-commit remediation you do is its own migration in its own right and gets its own log row.

---

## 6. Quick reference — checklist

- [ ] `DATABASE_URL` exported.
- [ ] For a first look at a new environment, run with `MIGRATION_DRY_RUN=true`.
- [ ] If the dry-run looks right, unset `MIGRATION_DRY_RUN` and run the real migration.
- [ ] Confirm via `SELECT … FROM migration_log WHERE migration_name = 'us_stock_portfolio_defaults_v1' ORDER BY run_at DESC LIMIT 1;` that the latest non-dry-run row has `status='SUCCESS'`.
- [ ] Run `python -m scripts.verify_migration`; require exit code `0` and `stocks.bad_* = 0` for each line.
- [ ] If anything went wrong on a real run, query `migration_log` for the `rows_affected` and `error_message` of the offending row before deciding on a corrective `UPDATE`.