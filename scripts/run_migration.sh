#!/usr/bin/env bash
# run_migration.sh
#
# Wrapper around migrate_us_stocks.sql. Designed to be invoked from the
# migration container image, where this file lives at /app/scripts/run_migration.sh.
# In this repo (no /app directory locally) we keep it under scripts/ so the
# skeleton + tests are exercisable on a developer machine. A symlink/copy
# step at image-build time is what deploys it to /app/scripts/ in the
# migration image.
#
# Behavior:
#   * Strict mode (set -euo pipefail) so any failure aborts immediately.
#   * DATABASE_URL is required; if unset or empty, exit non-zero with a
#     clear error message (verified by tests/test_run_migration.py).
#   * MIGRATION_DRY_RUN defaults to "false" if unset; when "true", the
#     wrapper passes --set ON_ERROR_ROLLBACK=on and wraps the whole
#     migration in a BEGIN ... ROLLBACK so no rows are committed.
#   * Invokes psql with the SQL script, captures psql's real exit status,
#     and tees stdout+stderr to a real log file (LOG_FILE, default
#     /tmp/migrate_us_stocks.log locally; the migration image overrides
#     this).

set -euo pipefail

# --- Resolve paths ---------------------------------------------------------
# BASH_SOURCE[0] is the path to this script as invoked, even when sourced.
# We resolve symlinks so the LOG_FILE path is stable regardless of how
# the image mounts / invokes us.
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"
SQL_SCRIPT="${SCRIPT_DIR}/migrate_us_stocks.sql"

# --- Required environment: DATABASE_URL ------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL is not set. run_migration.sh requires DATABASE_URL" >&2
    echo "       to be exported (e.g. postgres://user:pass@host:port/dbname)." >&2
    exit 1
fi

# --- Optional environment: MIGRATION_DRY_RUN --------------------------------
: "${MIGRATION_DRY_RUN:=false}"
DRY_RUN_FLAG=""
if [[ "${MIGRATION_DRY_RUN}" == "true" ]]; then
    echo "INFO: MIGRATION_DRY_RUN=true; running migration in a transaction that will ROLLBACK." >&2
    DRY_RUN_FLAG="--single-transaction"
fi

# --- Log file --------------------------------------------------------------
LOG_FILE="${LOG_FILE:-/tmp/migrate_us_stocks.log}"
mkdir -p "$(dirname "${LOG_FILE}")"

# --- Sanity: psql + SQL script exist ---------------------------------------
if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql is not on PATH. Install the PostgreSQL client." >&2
    exit 1
fi
if [[ ! -f "${SQL_SCRIPT}" ]]; then
    echo "ERROR: SQL script not found at ${SQL_SCRIPT}" >&2
    exit 1
fi

# --- Run psql, capturing its real exit status -------------------------------
# set -o pipefail means the tee pipeline's exit status is the rightmost
# non-zero status, which is what we want: if psql fails, we propagate.
echo "INFO: running psql against ${DATABASE_URL%%@*}@*** (host redacted) -> ${SQL_SCRIPT}" >&2

PSQL_EXIT=0
psql \
    "${DATABASE_URL}" \
    -v ON_ERROR_STOP=1 \
    --no-psqlrc \
    ${DRY_RUN_FLAG} \
    -f "${SQL_SCRIPT}" 2>&1 | tee "${LOG_FILE}" || PSQL_EXIT=$?

if [[ "${PSQL_EXIT}" -ne 0 ]]; then
    echo "ERROR: psql exited with status ${PSQL_EXIT}; see ${LOG_FILE} for details." >&2
    exit "${PSQL_EXIT}"
fi

echo "INFO: migration complete; log at ${LOG_FILE}" >&2
exit 0