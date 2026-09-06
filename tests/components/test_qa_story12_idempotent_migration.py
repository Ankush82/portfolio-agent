"""Independent QA verification test for STORY-12: make the core domain
migration idempotent and converge legacy lazily-created tables.

This test exercises the nine acceptance criteria of STORY-12 against
the real migrate_core_domain_entities.sql file.

Acceptance criteria exercised here:
  AC1: Migration includes ALTER TABLE ... ADD COLUMN IF NOT EXISTS for
       every target column of all four tables, in addition to the
       CREATE TABLE IF NOT EXISTS statements.
  AC2: Every foreign key is added inside a DO block guarded by a
       pg_constraint conname existence check, so re-running does not error.
  AC3: The unique constraint on (portfolio_id, security_id) is created
       via CREATE UNIQUE INDEX IF NOT EXISTS.
  AC4: Each FK guard counts orphan rows first and adds the constraint
       only when count = 0, otherwise issuing RAISE NOTICE with the
       orphan count and an inspection SELECT.
  AC5: Migration contains no DELETE or DROP COLUMN statement.
  AC6: A guarded backfill from a legacy opaque payload column is present
       if and only if docs/repo-layer-recon.md item V3 recorded such a
       column (it did -- records.data JSONB), and it does not drop that column.
  AC7: Header notes the retained legacy column as documented follow-up cleanup.
  AC8: Running the migration script twice in a row against the same
       database succeeds both times.
  AC9: Applying the migration to a database with the four tables in
       the old lazily-created shape produces the same final column set
       as applying it to a fresh database.
"""

from __future__ import annotations

import re
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scripts.migrate_core_domain_entities import MIGRATION_NAME  # noqa: E402

_MIGRATION_SQL = _PROJECT_ROOT / "scripts" / "migrate_core_domain_entities.sql"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_sql() -> str:
    return _MIGRATION_SQL.read_text(encoding="utf-8")


def _extract_create_tables(sql: str) -> dict[str, str]:
    blocks = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\("
        r"(.+?)"
        r"\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(sql):
        blocks[m.group(1).lower()] = m.group(2)
    return blocks


@contextmanager
def _temp_db():
    """Context manager: creates a fresh temp Postgres database and drops it
    on exit. Yields the DSN of the temp database."""
    import os
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent",
    )
    temp_db = f"qa_story12_test_{uuid.uuid4().hex[:12]}"
    import re as _re
    dsn_base = _re.sub(r"/[^/]+\Z", "", dsn)
    admin_dsn = f"{dsn_base}/postgres"
    created = False
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{temp_db}"')
                created = True
        yield f"{dsn_base}/{temp_db}"
    finally:
        if created:
            try:
                with psycopg.connect(admin_dsn, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT pg_terminate_backend(pid)
                            FROM pg_stat_activity
                            WHERE datname = %s AND pid <> pg_backend_pid()
                            """,
                            (temp_db,),
                        )
                        cur.execute(f'DROP DATABASE "{temp_db}"')
            except Exception as exc:
                print(f"WARNING: could not drop temp DB {temp_db}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# AC1 — ALTER TABLE ... ADD COLUMN IF NOT EXISTS for every target column
# ---------------------------------------------------------------------------

def test_ac1_add_column_if_not_exists_for_all_tables():
    """AC1: Every target column of all four tables has a corresponding
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS statement, in addition to
    the CREATE TABLE IF NOT EXISTS path. The 'id' column is excluded per
    design decision (cannot re-apply NOT NULL)."""
    sql = _read_sql()

    # For each table, enumerate the expected columns (excluding 'id').
    # We read the CREATE TABLE to get the authoritative column list.
    blocks = _extract_create_tables(sql)

    for table in ("users", "portfolios", "holdings", "transactions"):
        assert table in blocks, f"AC1 FAIL: {table} not in CREATE TABLE blocks"

        # Extract column names from the CREATE TABLE body.
        col_pattern = re.compile(
            r"(\w+)\s+(?:TEXT|JSONB|NUMERIC|TIMESTAMPTZ|SERIAL|BIGSERIAL)\s*(?:NOT\s+NULL)?(?:\s+DEFAULT\s+[^\s,]+)?",
            re.IGNORECASE,
        )
        declared_cols = set()
        for m in col_pattern.finditer(blocks[table]):
            col = m.group(1).lower()
            if col not in ("primary", "key"):  # skip PRIMARY KEY keywords
                declared_cols.add(col)

        # Also catch columns with types like NUMERIC(38,10).
        col_pattern2 = re.compile(
            r"(\w+)\s+NUMERIC\s*\(\s*\d+\s*,\s*\d+\s*\)",
            re.IGNORECASE,
        )
        for m in col_pattern2.finditer(blocks[table]):
            declared_cols.add(m.group(1).lower())

        # Exclude 'id' from the expected ALTER list (by design).
        non_id_cols = declared_cols - {"id"}
        assert non_id_cols, f"AC1 FAIL: no non-id columns found for {table}"

        # Find all ALTER TABLE ... ADD COLUMN IF NOT EXISTS for this table.
        alter_pattern = re.compile(
            rf"ALTER\s+TABLE\s+{table}\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)",
            re.IGNORECASE,
        )
        added_cols = {m.group(1).lower() for m in alter_pattern.finditer(sql)}

        missing = non_id_cols - added_cols
        assert not missing, (
            f"AC1 FAIL: {table} is missing ALTER TABLE ... ADD COLUMN IF NOT EXISTS "
            f"for: {missing}. Found ALTERs for: {added_cols}"
        )


# ---------------------------------------------------------------------------
# AC2 — Every FK inside a DO block guarded by pg_constraint conname check
# ---------------------------------------------------------------------------

def test_ac2_fk_inside_do_block_with_pg_constraint_guard():
    """AC2: Every ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY is wrapped
    in a DO $$ ... END $$ block whose body begins with:
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '<name>')
    """
    sql = _read_sql()

    # Find all FK ADD CONSTRAINT statements.
    fk_alter_pattern = re.compile(
        r"ALTER\s+TABLE\s+\w+\s+ADD\s+CONSTRAINT\s+(\w+)\s+FOREIGN\s+KEY",
        re.IGNORECASE,
    )
    fk_names = [m.group(1) for m in fk_alter_pattern.finditer(sql)]
    assert len(fk_names) == 3, f"AC2 FAIL: expected 3 FKs, found {len(fk_names)}: {fk_names}"

    for fk_name in fk_names:
        # The ADD CONSTRAINT must be inside a DO $$ block.
        # Find the DO block containing this FK.
        # Pattern: DO $$ ... ADD CONSTRAINT fk_name ... END $$;
        do_block_pattern = re.compile(
            rf"DO\s+\$\$\s+(.*?)\s+END\s+\$\$\s*;",
            re.IGNORECASE | re.DOTALL,
        )
        found_in_block = False
        for block_match in do_block_pattern.finditer(sql):
            block_body = block_match.group(1)
            if re.search(
                rf"ADD\s+CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY",
                block_body,
                re.IGNORECASE,
            ):
                found_in_block = True
                # Inside this block, the pg_constraint existence guard must exist.
                guard_pattern = re.compile(
                    rf"IF\s+NOT\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+pg_constraint\s+"
                    rf"WHERE\s+conname\s*=\s*['\"]?{re.escape(fk_name)}",
                    re.IGNORECASE,
                )
                assert guard_pattern.search(block_body), (
                    f"AC2 FAIL: FK {fk_name} ADD CONSTRAINT is in a DO block "
                    f"but the block lacks the pg_constraint conname existence guard"
                )
                break

        assert found_in_block, (
            f"AC2 FAIL: FK {fk_name} ADD CONSTRAINT is not inside a DO $$ block"
        )


# ---------------------------------------------------------------------------
# AC3 — Unique constraint via CREATE UNIQUE INDEX IF NOT EXISTS
# ---------------------------------------------------------------------------

def test_ac3_uq_holdings_portfolio_security_as_unique_index():
    """AC3: The unique constraint on (portfolio_id, security_id) in holdings
    is created via CREATE UNIQUE INDEX IF NOT EXISTS uq_holdings_portfolio_security."""
    sql = _read_sql()

    # Must exist as CREATE UNIQUE INDEX.
    uq_match = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+uq_holdings_portfolio_security"
        r"\s+ON\s+holdings\s*\(\s*portfolio_id\s*,\s*security_id\s*\)",
        sql,
        re.IGNORECASE,
    )
    assert uq_match, (
        "AC3 FAIL: uq_holdings_portfolio_security must be created as "
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_holdings_portfolio_security "
        "ON holdings (portfolio_id, security_id)"
    )


# ---------------------------------------------------------------------------
# AC4 — Each FK guard counts orphan rows before adding constraint
# ---------------------------------------------------------------------------

def test_ac4_fk_guards_count_orphans_before_adding():
    """AC4: Each FK's DO block counts orphan rows first, and only adds the
    constraint when orphans = 0. Otherwise it issues RAISE NOTICE with the
    orphan count and the exact SELECT statement to inspect them."""
    sql = _read_sql()

    # Find all DO blocks that contain an ADD CONSTRAINT FOREIGN KEY.
    do_block_pattern = re.compile(
        r"DO\s+\$\$\s+(.*?)\s+END\s+\$\$\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    fk_blocks = []
    for block_match in do_block_pattern.finditer(sql):
        body = block_match.group(1)
        if re.search(r"ADD\s+CONSTRAINT\s+\w+\s+FOREIGN\s+KEY", body, re.IGNORECASE):
            fk_blocks.append(body)

    assert len(fk_blocks) == 3, f"AC4 FAIL: expected 3 FK DO blocks, found {len(fk_blocks)}"

    for body in fk_blocks:
        # Must count orphans first (SELECT COUNT(*) INTO _orphan_count or similar).
        orphan_count_pattern = re.compile(
            r"SELECT\s+COUNT\s*\(\s*\*\s*\)\s+INTO\s+_orphan_count",
            re.IGNORECASE,
        )
        assert orphan_count_pattern.search(body), (
            f"AC4 FAIL: FK DO block must count orphans with "
            f"SELECT COUNT(*) INTO _orphan_count. Body:\n{body[:500]}"
        )

        # Must check IF _orphan_count > 0 before deciding to ADD CONSTRAINT.
        orphan_check_pattern = re.compile(
            r"IF\s+_orphan_count\s*>\s*0",
            re.IGNORECASE,
        )
        assert orphan_check_pattern.search(body), (
            f"AC4 FAIL: FK DO block must check IF _orphan_count > 0. Body:\n{body[:500]}"
        )

        # Must RAISE NOTICE with the orphan count and an inspection SELECT.
        raise_notice_pattern = re.compile(
            r"RAISE\s+NOTICE\s+['\"].*?%.*?Inspect\s+orphans\s+with.*?SELECT",
            re.IGNORECASE,
        )
        assert raise_notice_pattern.search(body), (
            f"AC4 FAIL: FK DO block must RAISE NOTICE with orphan count "
            f"and inspection SELECT. Body:\n{body[:500]}"
        )


# ---------------------------------------------------------------------------
# AC5 — No DELETE or DROP COLUMN
# ---------------------------------------------------------------------------

def test_ac5_no_delete_or_drop_column():
    """AC5: The migration contains no DELETE statement and no DROP COLUMN
    statement. The migration is non-destructive."""
    sql = _read_sql()

    # \bDELETE\b in SQL matches DELETE keyword (not part of another word).
    delete_matches = re.findall(r"\bDELETE\b", sql, re.IGNORECASE)
    assert not delete_matches, (
        f"AC5 FAIL: migration must not contain DELETE. Found {len(delete_matches)} "
        f"occurrence(s): {delete_matches}"
    )

    drop_col_matches = re.findall(
        r"DROP\s+COLUMN\b",
        sql,
        re.IGNORECASE,
    )
    assert not drop_col_matches, (
        f"AC5 FAIL: migration must not contain DROP COLUMN. "
        f"Found: {drop_col_matches}"
    )


# ---------------------------------------------------------------------------
# AC6 — Guarded backfill from records.data present; column not dropped
# ---------------------------------------------------------------------------

def test_ac6_guarded_backfill_present_and_column_not_dropped():
    """AC6: A guarded backfill from the legacy records.data JSONB column
    is present (V3 of docs/repo-layer-recon.md confirms it exists).
    The migration does NOT drop that column."""
    sql = _read_sql()

    # Recon V3 confirms: records.data JSONB is the legacy opaque payload.
    # The backfill must check for records.data existence before touching it.
    backfill_guard_pattern = re.compile(
        r"IF\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+information_schema\.columns\s+"
        r"WHERE\s+table_name\s*=\s*['\"]?records['\"]?\s+AND\s+column_name\s*=\s*['\"]?data['\"]?\s*\)",
        re.IGNORECASE,
    )
    assert backfill_guard_pattern.search(sql), (
        "AC6 FAIL: migration must guard backfill with "
        "IF EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name='records' AND column_name='data')"
    )

    # The backfill UPDATE must reference the records table.
    backfill_update_pattern = re.compile(
        r"FROM\s+records\s+r\s+WHERE\s+r\.table_name\s*=\s*['\"]?\w+['\"]?\s+AND\s+r\.id\s*=\s*\w+\.id",
        re.IGNORECASE,
    )
    # At least one backfill block must touch each of the four typed tables.
    for table in ("users", "portfolios", "holdings", "transactions"):
        assert backfill_update_pattern.search(sql) or re.search(
            rf"r\.table_name\s*=\s*['\"]?{table}['\"]?",
            sql,
            re.IGNORECASE,
        ), f"AC6 FAIL: no backfill found for table '{table}'"

    # records.data column must NOT be dropped.
    drop_data_pattern = re.compile(
        r"DROP\s+COLUMN\s+\w*\.?\s*data\b",
        re.IGNORECASE,
    )
    assert not drop_data_pattern.search(sql), (
        "AC6 FAIL: records.data column must NOT be dropped"
    )


# ---------------------------------------------------------------------------
# AC7 — Header notes retained legacy column as documented follow-up
# ---------------------------------------------------------------------------

def test_ac7_header_notes_retained_legacy_column():
    """AC7: The .sql header notes the retained legacy column (records.data)
    as a documented follow-up cleanup item."""
    sql = _read_sql()
    lines = sql.splitlines()

    # Extract leading comment block.
    comment_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--"):
            content = stripped[2:].strip()
            if content and content != "---" and set(content.replace(" ", "")) <= {"-"}:
                continue
            # Skip SQL-like statements in the header.
            sql_keywords = (
                "DROP", "CREATE", "BEGIN", "COMMIT", "ROLLBACK",
                "ALTER", "INSERT", "UPDATE", "DELETE", "GRANT",
            )
            if content.upper().startswith(sql_keywords):
                continue
            comment_lines.append(content)
        elif stripped == "":
            continue
        else:
            break

    header = " ".join(comment_lines).lower()

    # Must mention the retained legacy column.
    assert re.search(r"records\.data|retained\s+legacy|legacy\s+column", header), (
        f"AC7 FAIL: header must mention the retained legacy column "
        f"(records.data). Header:\n{' '.join(comment_lines[:20])}"
    )

    # Must note it as follow-up / future / cleanup.
    assert re.search(
        r"follow[\s_-]?up|future|clean[\s_-]?up|drop\s+it|do\s+not\s+drop",
        header,
    ), (
        f"AC7 FAIL: header must note the retained column as a follow-up "
        f"cleanup item. Header:\n{' '.join(comment_lines[:20])}"
    )


# ---------------------------------------------------------------------------
# AC8 — Running the migration twice in a row succeeds
# ---------------------------------------------------------------------------

def test_ac8_migration_idempotent_second_run_succeeds():
    """AC8: Running the migration twice in a row against the same fresh
    database succeeds on both runs with no error."""
    migration_sql = _read_sql()

    with _temp_db() as temp_dsn:
        # First run.
        with psycopg.connect(temp_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(migration_sql)

        # Second run -- must also succeed.
        with psycopg.connect(temp_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # This will raise psycopg.errors.DuplicateObject or similar on failure.
                cur.execute(migration_sql)

        # Verify all four tables still exist and have correct FKs after second run.
        with psycopg.connect(temp_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conname IN (
                        'fk_portfolios_user',
                        'fk_holdings_portfolio',
                        'fk_transactions_portfolio'
                    )
                    ORDER BY conname
                    """
                )
                fk_names = [r[0] for r in cur.fetchall()]
                assert fk_names == [
                    "fk_holdings_portfolio",
                    "fk_portfolios_user",
                    "fk_transactions_portfolio",
                ], f"AC8 FAIL: FKs missing or wrong after second run: {fk_names}"

                cur.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE indexname = 'uq_holdings_portfolio_security'
                    """
                )
                uq_exists = cur.fetchone()
                assert uq_exists, "AC8 FAIL: uq_holdings_portfolio_security missing after second run"


# ---------------------------------------------------------------------------
# AC9 — Legacy DB converges to same final schema as fresh DB
# ---------------------------------------------------------------------------

def test_ac9_legacy_shape_converges_to_same_final_schema():
    """AC9: Applying the migration to a database that already has the four
    tables in the old lazily-created shape produces the same final column
    set as applying it to a fresh database. Verified by comparing
    information_schema output for both cases."""

    migration_sql = _read_sql()

    # Target columns per table (excluding 'id' -- we know CREATE handles it).
    TARGET_COLS = {
        "users":        {"email", "preferences", "created_at", "updated_at"},
        "portfolios":   {"user_id", "created_at", "updated_at"},
        "holdings":     {"portfolio_id", "security_id", "quantity", "currency",
                         "exchange", "symbol_suffix", "created_at", "updated_at"},
        "transactions": {"portfolio_id", "kind", "amount", "created_at", "updated_at"},
    }

    def get_columns(conn, table: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY column_name
                """,
                (table,),
            )
            return {r[0] for r in cur.fetchall()}

    with _temp_db() as fresh_dsn:
        # Fresh DB: run migration once.
        with psycopg.connect(fresh_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(migration_sql)

        with psycopg.connect(fresh_dsn) as conn:
            fresh_cols = {t: get_columns(conn, t) for t in TARGET_COLS}

    with _temp_db() as legacy_dsn:
        # Legacy DB: create the four tables in the old lazy shape
        # (only 'id' column, everything else in records.data JSONB).
        with psycopg.connect(legacy_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for table in ("users", "portfolios", "holdings", "transactions"):
                    cur.execute(
                        f"""
                        CREATE TABLE {table} (
                            id TEXT NOT NULL,
                            PRIMARY KEY (id)
                        )
                        """
                    )
                # Also create records table with data JSONB (simulates legacy state).
                cur.execute(
                    """
                    CREATE TABLE records (
                        table_name TEXT NOT NULL,
                        id         TEXT NOT NULL,
                        data       JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT now(),
                        PRIMARY KEY (table_name, id)
                    )
                    """
                )
                # Insert a legacy row for each table.
                cur.execute(
                    """
                    INSERT INTO records (table_name, id, data) VALUES
                    ('users',        'u1', '{"email": "a@b.com", "preferences": {}, "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"}'::jsonb),
                    ('portfolios',   'p1', '{"user_id": "u1", "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"}'::jsonb),
                    ('holdings',     'h1', '{"portfolio_id": "p1", "security_id": "AAPL", "quantity": "100", "currency": "USD", "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"}'::jsonb),
                    ('transactions',  't1', '{"portfolio_id": "p1", "kind": "BUY", "amount": "500", "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"}'::jsonb)
                    """
                )

        # Legacy DB: run migration once.
        with psycopg.connect(legacy_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(migration_sql)

        with psycopg.connect(legacy_dsn) as conn:
            legacy_cols = {t: get_columns(conn, t) for t in TARGET_COLS}

    # Compare: each table must have the same final columns in both DBs.
    for table in TARGET_COLS:
        fresh = fresh_cols[table]
        legacy = legacy_cols[table]
        assert fresh == legacy, (
            f"AC9 FAIL: table '{table}' final columns differ between fresh and legacy DB.\n"
            f"  Fresh only:  {fresh - legacy}\n"
            f"  Legacy only: {legacy - fresh}\n"
            f"  Fresh:      {sorted(fresh)}\n"
            f"  Legacy:     {sorted(legacy)}"
        )

        # Every target column must be present.
        missing = TARGET_COLS[table] - legacy
        assert not missing, f"AC9 FAIL: {table} missing columns: {missing}"

        # No 'id' column missing (primary key must survive).
        assert "id" in legacy, f"AC9 FAIL: {table} missing 'id' column after migration"
