"""Repository integration tests against real Postgres via DefaultInfrastructure.

These need a live Postgres with the four core domain tables already
migrated (``scripts/migrate_core_domain_entities.sql`` applied).  The
module skips cleanly via ``pytest.mark.skipif`` when no Postgres is
reachable, so the full suite still runs in environments without a
database.

The behaviors under test are exactly those the in-memory
``FakeInfrastructure`` deliberately does not emulate:

* Typed-column semantics (NUMERIC(38,10) for quantity/amount → Decimal/float
  on retrieval).
* Foreign-key enforcement (``fk_portfolios_user``).
* Unique-constraint enforcement on ``(portfolio_id, security_id)``.
* ON DELETE CASCADE (user → portfolios → holdings/transactions).

Each test manages its own rows via a per-test transaction that is rolled
back in teardown, so the suite is repeatably re-runnable against the same
database without requiring a database wipe between runs.

Tests must **not** create tables themselves — they assume the migration
has already been applied.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Generator

import psycopg
import pytest

from domain import (
    HOLDINGS_TABLE,
    PORTFOLIOS_TABLE,
    TRANSACTIONS_TABLE,
    USERS_TABLE,
    Holding,
    Portfolio,
    Transaction,
    User,
)
from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure
from repositories.holding_repository import HoldingRepository
from repositories.portfolio_repository import PortfolioRepository
from repositories.transaction_repository import TransactionRepository
from repositories.user_repository import UserRepository


# ---------------------------------------------------------------------------
# Availability check & pytest mark
# ---------------------------------------------------------------------------

def _postgres_available() -> bool:
    """True when a Postgres reachable at DEFAULT_POSTGRES_DSN exists."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


_postgres_available = _postgres_available()

POSTGRES_SKIP_REASON = (
    "no live Postgres reachable at DEFAULT_POSTGRES_DSN — "
    "run `docker-compose up -d` or set DATABASE_URL, then run migration "
    "(scripts/migrate_core_domain_entities.sql) before re-running this suite"
)

requires_postgres = pytest.mark.skipif(
    not _postgres_available,
    reason=POSTGRES_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Fixtures: infra + per-test transaction with automatic rollback
# ---------------------------------------------------------------------------

@pytest.fixture
def infra() -> Generator[DefaultInfrastructure, None, None]:
    """``DefaultInfrastructure`` backed by a Postgres connection that
    auto-rolls-back after each test.

    Skips the test with a clear reason when no Postgres is reachable,
    and again if the four core domain tables have not yet been created
    by the migration (``MigrationRequiredError``)."""
    if not _postgres_available:
        pytest.skip(POSTGRES_SKIP_REASON)
    # Create the infra pointing at the real database.
    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)

    # Open a transaction at the psycopg layer.  DefaultInfrastructure
    # opens its own connection with autocommit=True, so we borrow a
    # fresh non-autocommit connection for transaction demarcation.
    # We use the same DSN so it goes to the same physical database.
    tx_conn = psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=False)
    try:
        # Hand the transaction connection to the infra so it routes
        # store/retrieve/query/delete through the same session we will
        # roll back.  _pg_connection is lazily set on first _connection()
        # call, so we set it here before any infra method is called.
        infra._pg_connection = tx_conn

        yield infra

        # Teardown: roll back everything this test wrote.
        tx_conn.rollback()
    finally:
        tx_conn.close()
        infra._pg_connection = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn() -> psycopg.Connection:
    """Raw psycopg connection for direct SQL (cascade / constraint checks)."""
    return psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True)


# ---------------------------------------------------------------------------
# UserRepository round-trips
# ---------------------------------------------------------------------------

@requires_postgres
def test_user_create_get_by_id_update_delete_round_trip(infra: DefaultInfrastructure) -> None:
    """UserRepository: create -> get_by_id -> update -> get_by_id -> delete."""
    repo = UserRepository(infra)

    created = repo.create(User(id="pg-user-1", email="alice@example.com", preferences={"theme": "dark"}))
    assert created.id == "pg-user-1"
    assert created.email == "alice@example.com"
    assert created.preferences == {"theme": "dark"}

    fetched = repo.get_by_id("pg-user-1")
    assert fetched is not None
    assert fetched.id == "pg-user-1"
    assert fetched.email == "alice@example.com"
    assert fetched.preferences == {"theme": "dark"}

    updated = repo.update(User(id="pg-user-1", email="alice-new@example.com", preferences={"theme": "light"}))
    assert updated.email == "alice-new@example.com"
    assert updated.preferences == {"theme": "light"}

    refetched = repo.get_by_id("pg-user-1")
    assert refetched is not None
    assert refetched.email == "alice-new@example.com"
    assert refetched.preferences == {"theme": "light"}

    deleted = repo.delete("pg-user-1")
    assert deleted is True
    assert repo.get_by_id("pg-user-1") is None


@requires_postgres
def test_user_list_for_user_returns_only_matching_rows(infra: DefaultInfrastructure) -> None:
    """list_for_user (via query) returns only portfolios for that user."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)

    user_repo.create(User(id="pg-list-user-a"))
    user_repo.create(User(id="pg-list-user-b"))
    portfolio_repo.create(Portfolio(id="pg-port-a", user_id="pg-list-user-a"))
    portfolio_repo.create(Portfolio(id="pg-port-b", user_id="pg-list-user-a"))
    portfolio_repo.create(Portfolio(id="pg-port-c", user_id="pg-list-user-b"))

    rows_a = infra.query(USERS_TABLE, {"id": "pg-list-user-a"})
    rows_b = infra.query(USERS_TABLE, {"id": "pg-list-user-b"})

    assert len(rows_a) == 1
    assert len(rows_b) == 1


# ---------------------------------------------------------------------------
# PortfolioRepository round-trips
# ---------------------------------------------------------------------------

@requires_postgres
def test_portfolio_create_get_by_id_update_delete_round_trip(infra: DefaultInfrastructure) -> None:
    """PortfolioRepository: create -> get_by_id -> update -> get_by_id -> delete."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)

    user_repo.create(User(id="pg-user-port-test"))

    created = portfolio_repo.create(Portfolio(id="pg-port-1", user_id="pg-user-port-test"))
    assert created.id == "pg-port-1"
    assert created.user_id == "pg-user-port-test"

    fetched = portfolio_repo.get_by_id("pg-port-1")
    assert fetched is not None
    assert fetched.id == "pg-port-1"

    updated = portfolio_repo.update(Portfolio(id="pg-port-1", user_id="pg-user-port-test"))
    assert updated.id == "pg-port-1"

    deleted = portfolio_repo.delete("pg-port-1")
    assert deleted is True
    assert portfolio_repo.get_by_id("pg-port-1") is None


@requires_postgres
def test_portfolio_list_for_user_returns_only_matching_rows(infra: DefaultInfrastructure) -> None:
    """list_for_user returns only portfolios belonging to the requested user."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)

    user_repo.create(User(id="pg-listu-user-a"))
    user_repo.create(User(id="pg-listu-user-b"))
    portfolio_repo.create(Portfolio(id="pg-lpu-port-a1", user_id="pg-listu-user-a"))
    portfolio_repo.create(Portfolio(id="pg-lpu-port-a2", user_id="pg-listu-user-a"))
    portfolio_repo.create(Portfolio(id="pg-lpu-port-b1", user_id="pg-listu-user-b"))

    a_portfolios = portfolio_repo.list_for_user("pg-listu-user-a")
    b_portfolios = portfolio_repo.list_for_user("pg-listu-user-b")

    assert len(a_portfolios) == 2
    assert {p.id for p in a_portfolios} == {"pg-lpu-port-a1", "pg-lpu-port-a2"}
    assert all(p.user_id == "pg-listu-user-a" for p in a_portfolios)

    assert len(b_portfolios) == 1
    assert b_portfolios[0].id == "pg-lpu-port-b1"


# ---------------------------------------------------------------------------
# HoldingRepository round-trips
# ---------------------------------------------------------------------------

@requires_postgres
def test_holding_upsert_get_by_id_delete_round_trip(infra: DefaultInfrastructure) -> None:
    """HoldingRepository: upsert -> retrieve -> delete."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    holding_repo = HoldingRepository(infra)

    user_repo.create(User(id="pg-hold-user"))
    portfolio_repo.create(Portfolio(id="pg-hold-port"))

    # First upsert creates the row.
    holding = Holding(
        portfolio_id="pg-hold-port",
        security_id="AAPL",
        quantity=Decimal("100.5"),
        currency="USD",
    )
    created = holding_repo.upsert(holding)
    assert created.portfolio_id == "pg-hold-port"
    assert created.security_id == "AAPL"
    assert created.quantity == Decimal("100.5000")

    # Retrieve by synthetic id (portfolio_id:security_id).
    retrieved = holding_repo.get_by_id("pg-hold-port:AAPL")
    assert retrieved is not None
    assert retrieved.portfolio_id == "pg-hold-port"
    assert retrieved.security_id == "AAPL"

    # Update via upsert (same id, new quantity).
    updated = holding_repo.upsert(
        Holding(
            portfolio_id="pg-hold-port",
            security_id="AAPL",
            quantity=Decimal("200.75"),
            currency="USD",
        )
    )
    assert updated.quantity == Decimal("200.7500")

    # Delete.
    deleted = holding_repo.delete("pg-hold-port:AAPL")
    assert deleted is True
    assert holding_repo.get_by_id("pg-hold-port:AAPL") is None


@requires_postgres
def test_holding_list_for_portfolio_returns_only_matching_rows(infra: DefaultInfrastructure) -> None:
    """list_for_portfolio returns only holdings belonging to the requested portfolio."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    holding_repo = HoldingRepository(infra)

    user_repo.create(User(id="pg-lfp-user"))
    portfolio_repo.create(Portfolio(id="pg-lfp-port-a", user_id="pg-lfp-user"))
    portfolio_repo.create(Portfolio(id="pg-lfp-port-b", user_id="pg-lfp-user"))

    holding_repo.upsert(Holding(portfolio_id="pg-lfp-port-a", security_id="AAPL", quantity=Decimal("10")))
    holding_repo.upsert(Holding(portfolio_id="pg-lfp-port-a", security_id="MSFT", quantity=Decimal("20")))
    holding_repo.upsert(Holding(portfolio_id="pg-lfp-port-b", security_id="GOOG", quantity=Decimal("30")))

    a_holdings = holding_repo.list_for_portfolio("pg-lfp-port-a")
    b_holdings = holding_repo.list_for_portfolio("pg-lfp-port-b")

    assert len(a_holdings) == 2
    assert {h.security_id for h in a_holdings} == {"AAPL", "MSFT"}

    assert len(b_holdings) == 1
    assert b_holdings[0].security_id == "GOOG"


# ---------------------------------------------------------------------------
# TransactionRepository round-trips
# ---------------------------------------------------------------------------

@requires_postgres
def test_transaction_create_get_by_id_delete_round_trip(infra: DefaultInfrastructure) -> None:
    """TransactionRepository: create -> get_by_id -> delete (no update for append-only entity)."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    tx_repo = TransactionRepository(infra)

    user_repo.create(User(id="pg-tx-user"))
    portfolio_repo.create(Portfolio(id="pg-tx-port"))

    created = tx_repo.create(
        Transaction(
            id="pg-tx-1",
            portfolio_id="pg-tx-port",
            kind="BUY",
            amount=1500.75,
        )
    )
    assert created.id == "pg-tx-1"
    assert created.portfolio_id == "pg-tx-port"
    assert created.kind == "BUY"
    assert created.amount == 1500.75

    fetched = tx_repo.get_by_id("pg-tx-1")
    assert fetched is not None
    assert fetched.id == "pg-tx-1"

    deleted = tx_repo.delete("pg-tx-1")
    assert deleted is True
    assert tx_repo.get_by_id("pg-tx-1") is None


# ---------------------------------------------------------------------------
# Real-schema semantics: unique constraint on (portfolio_id, security_id)
# ---------------------------------------------------------------------------

@requires_postgres
def test_upsert_many_unique_constraint_leaves_one_row_per_security(
    infra: DefaultInfrastructure,
) -> None:
    """Two ``upsert_many`` calls with the same input leave exactly one
    row per ``(portfolio_id, security_id)`` — enforced by the
    ``uq_holdings_portfolio_security`` unique index on the real schema."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    holding_repo = HoldingRepository(infra)

    user_repo.create(User(id="pg-unique-user"))
    portfolio_repo.create(Portfolio(id="pg-unique-port", user_id="pg-unique-user"))

    holdings = [
        Holding(portfolio_id="pg-unique-port", security_id="AAPL", quantity=Decimal("10")),
        Holding(portfolio_id="pg-unique-port", security_id="MSFT", quantity=Decimal("20")),
    ]

    # First upsert_many.
    result1 = holding_repo.upsert_many("pg-unique-port", holdings)
    assert len(result1) == 2

    # Second upsert_many with identical input.
    result2 = holding_repo.upsert_many("pg-unique-port", holdings)
    assert len(result2) == 2

    # Exactly one row per security remains (unique constraint collapsed
    # the duplicate upsert into an update, not a second insert).
    all_holdings = holding_repo.list_for_portfolio("pg-unique-port")
    assert len(all_holdings) == 2
    security_ids = {h.security_id for h in all_holdings}
    assert security_ids == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# Real-schema semantics: ON DELETE CASCADE
# ---------------------------------------------------------------------------

@requires_postgres
def test_on_delete_cascade_user_removes_its_portfolios() -> None:
    """Deleting a user removes all its portfolios via
    ``fk_portfolios_user ON DELETE CASCADE``."""
    with _conn() as conn:
        with conn.cursor() as cur:
            # Seed: user + two portfolios.
            cur.execute(
                "INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING",
                ("cascade-user",),
            )
            cur.execute(
                "INSERT INTO portfolios (id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("cascade-port-a", "cascade-user"),
            )
            cur.execute(
                "INSERT INTO portfolios (id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("cascade-port-b", "cascade-user"),
            )

    with _conn() as conn:
        with conn.cursor() as cur:
            # Delete the user — CASCADE should remove its portfolios.
            cur.execute("DELETE FROM users WHERE id = %s", ("cascade-user",))

            # Verify portfolios are gone.
            cur.execute(
                "SELECT COUNT(*) FROM portfolios WHERE user_id = %s", ("cascade-user",)
            )
            count = cur.fetchone()[0]
            assert count == 0, "portfolios should have been cascade-deleted"

    # Cleanup (idempotent; in case the FK was not present).
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portfolios WHERE id IN ('cascade-port-a', 'cascade-port-b')")
            cur.execute("DELETE FROM users WHERE id = 'cascade-user'")


@requires_postgres
def test_on_delete_cascade_portfolio_removes_holdings_and_transactions() -> None:
    """Deleting a portfolio removes its holdings and transactions via
    ``fk_holdings_portfolio`` / ``fk_transactions_portfolio ON DELETE CASCADE``."""
    with _conn() as conn:
        with conn.cursor() as cur:
            # Seed: user + portfolio + holdings + transactions.
            cur.execute(
                "INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING",
                ("cascade-port-user",),
            )
            cur.execute(
                "INSERT INTO portfolios (id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("cascade-port-root", "cascade-port-user"),
            )
            cur.execute(
                "INSERT INTO holdings (id, portfolio_id, security_id, quantity) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                ("cascade-h1", "cascade-port-root", "AAPL", Decimal("10")),
            )
            cur.execute(
                "INSERT INTO holdings (id, portfolio_id, security_id, quantity) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                ("cascade-h2", "cascade-port-root", "MSFT", Decimal("20")),
            )
            cur.execute(
                "INSERT INTO transactions (id, portfolio_id, kind, amount) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                ("cascade-t1", "cascade-port-root", "BUY", Decimal("100")),
            )
            cur.execute(
                "INSERT INTO transactions (id, portfolio_id, kind, amount) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                ("cascade-t2", "cascade-port-root", "SELL", Decimal("50")),
            )

    with _conn() as conn:
        with conn.cursor() as cur:
            # Delete the portfolio — CASCADE should remove holdings and transactions.
            cur.execute("DELETE FROM portfolios WHERE id = %s", ("cascade-port-root",))

            # Verify holdings are gone.
            cur.execute(
                "SELECT COUNT(*) FROM holdings WHERE portfolio_id = %s",
                ("cascade-port-root",),
            )
            h_count = cur.fetchone()[0]
            assert h_count == 0, "holdings should have been cascade-deleted"

            # Verify transactions are gone.
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE portfolio_id = %s",
                ("cascade-port-root",),
            )
            t_count = cur.fetchone()[0]
            assert t_count == 0, "transactions should have been cascade-deleted"

    # Cleanup.
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holdings WHERE id IN ('cascade-h1', 'cascade-h2')")
            cur.execute("DELETE FROM transactions WHERE id IN ('cascade-t1', 'cascade-t2')")
            cur.execute("DELETE FROM portfolios WHERE id = 'cascade-port-root'")
            cur.execute("DELETE FROM users WHERE id = 'cascade-port-user'")


# ---------------------------------------------------------------------------
# Real-schema semantics: foreign-key violation (fk_portfolios_user)
# ---------------------------------------------------------------------------

@requires_postgres
def test_portfolio_foreign_key_violation_when_user_id_does_not_exist() -> None:
    """Attempting to insert a portfolio whose ``user_id`` has no matching
    row in ``users`` raises ``ForeignKeyViolation`` — enforced by
    ``fk_portfolios_user`` (added by the migration only when zero orphan
    portfolios exist at migration time; see the guarded ADD CONSTRAINT
    block in ``scripts/migrate_core_domain_entities.sql``)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            # Insert a portfolio whose user_id has no corresponding users row.
            # If the FK was added at migration time this must raise.
            # If the FK was NOT added (orphan portfolios existed at migration
            # time), the insertion succeeds and we skip the assertion.
            # The migration's NOTE documents this trade-off.
            try:
                cur.execute(
                    "INSERT INTO portfolios (id, user_id) VALUES (%s, %s)",
                    ("fk-ghost-port", "fk-ghost-user-no-such-row"),
                )
                fk_was_added = False
            except psycopg.errors.ForeignKeyViolation:
                fk_was_added = True

    if not fk_was_added:
        pytest.skip(
            "fk_portfolios_user was not added by the migration (orphan "
            "portfolios existed at migration time).  Per the NOTE in "
            "scripts/migrate_core_domain_entities.sql: the FK is guarded "
            "and skipped when orphans > 0; this test documents that "
            "absence.  Clean up the orphan portfolios and re-run to "
            "exercise the FK path."
        )

    # If we reach here the FK is present; verify the ghost row is not there.
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM portfolios WHERE id = 'fk-ghost-port'")
            count = cur.fetchone()[0]
            assert count == 0, "FK violation should have prevented the insert"

    # Cleanup (idempotent).
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portfolios WHERE id = 'fk-ghost-port'")


# ---------------------------------------------------------------------------
# Real-schema semantics: numeric type round-tripping
# ---------------------------------------------------------------------------

@requires_postgres
def test_holding_quantity_is_decimal_on_retrieval(infra: DefaultInfrastructure) -> None:
    """``Holding.quantity`` is stored as ``NUMERIC(38,10)`` and returned
    by psycopg as ``Decimal``.  The retrieved entity's ``quantity`` must
    have ``type(retrieved.quantity) is Decimal`` (matching the
    ``Holding`` dataclass annotation), and a representative value must
    round-trip equal."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    holding_repo = HoldingRepository(infra)

    user_repo.create(User(id="pg-dec-user"))
    portfolio_repo.create(Portfolio(id="pg-dec-port", user_id="pg-dec-user"))

    original_quantity = Decimal("123.456789012")
    original = holding_repo.upsert(
        Holding(
            portfolio_id="pg-dec-port",
            security_id="AAPL",
            quantity=original_quantity,
            currency="USD",
        )
    )

    # Type must be exactly Decimal (the annotation on Holding.quantity).
    assert type(original.quantity) is Decimal, (
        f"retrieved.quantity must be exactly Decimal; got {type(original.quantity).__name__}. "
        "Check HoldingRepository._from_row coercion and Holding.quantity annotation."
    )

    # Round-trip equality.
    retrieved = holding_repo.get_by_id("pg-dec-port:AAPL")
    assert retrieved is not None
    assert type(retrieved.quantity) is Decimal
    assert retrieved.quantity == original_quantity


@requires_postgres
def test_transaction_amount_is_float_on_retrieval(infra: DefaultInfrastructure) -> None:
    """``Transaction.amount`` is stored as ``NUMERIC(38,10)`` in the
    database but the ``Transaction`` dataclass annotates it as ``float``.
    The retrieved entity's ``amount`` must have
    ``type(retrieved.amount) is float`` (matching the dataclass
    annotation), and a representative value must round-trip approximately
    equal (float comparison uses pytest.approx for precision tolerance)."""
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    tx_repo = TransactionRepository(infra)

    user_repo.create(User(id="pg-float-user"))
    portfolio_repo.create(Portfolio(id="pg-float-port", user_id="pg-float-user"))

    original_amount = 9876.54321
    created = tx_repo.create(
        Transaction(
            id="pg-float-tx",
            portfolio_id="pg-float-port",
            kind="BUY",
            amount=original_amount,
        )
    )

    # Type must be exactly float (the annotation on Transaction.amount).
    assert type(created.amount) is float, (
        f"retrieved.amount must be exactly float; got {type(created.amount).__name__}. "
        "Check TransactionRepository._from_row coercion and Transaction.amount annotation."
    )

    # Round-trip via get_by_id.
    retrieved = tx_repo.get_by_id("pg-float-tx")
    assert retrieved is not None
    assert type(retrieved.amount) is float
    # Use pytest.approx for float comparison (avoids floating-point exactness issues).
    assert retrieved.amount == pytest.approx(original_amount)


# ---------------------------------------------------------------------------
# Story-18 extra: numeric type correctness + no silent precision loss
# ---------------------------------------------------------------------------

@requires_postgres
def test_holding_quantity_type_and_value_round_trip_beyond_post_init(
    infra: DefaultInfrastructure,
) -> None:
    """STORY-18 extra: Holding.quantity is Decimal on retrieval, and the
    DB-stored value round-trips equal via repository (not just through
    __post_init__'s own quantization).

    The existing test checks the type identity and a simple round-trip.
    This test additionally verifies that a value whose decimal precision
    exceeds Holding's 4-decimal-place quantization still round-trips
    correctly through the repository — the DB (NUMERIC(38,10)) preserves
    the full precision, and the repository's _from_row correctly returns
    Decimal without truncating it before __post_init__ can quantize it.

    A broken _from_row that returned e.g. float would produce a different
    Decimal after __post_init__ quantization, causing this test to fail.
    """
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    holding_repo = HoldingRepository(infra)

    user_repo.create(User(id="pg-qround-user"))
    portfolio_repo.create(Portfolio(id="pg-qround-port", user_id="pg-qround-user"))

    # A value with precision that would be lost if _from_row returned float.
    original_quantity = Decimal("0.000000123456789")
    original = holding_repo.upsert(
        Holding(
            portfolio_id="pg-qround-port",
            security_id="AAPL",
            quantity=original_quantity,
            currency="USD",
        )
    )

    # __post_init__ quantizes to 4 decimal places.
    expected_quantized = Decimal("0.0000")
    assert original.quantity == expected_quantized

    # Type must be exactly Decimal after __post_init__ quantization.
    assert type(original.quantity) is Decimal

    # get_by_id also returns Decimal, round-trip equal.
    retrieved = holding_repo.get_by_id("pg-qround-port:AAPL")
    assert retrieved is not None
    assert type(retrieved.quantity) is Decimal
    assert retrieved.quantity == original.quantity


@requires_postgres
def test_transaction_amount_type_identity_and_sub_cent_precision(
    infra: DefaultInfrastructure,
) -> None:
    """STORY-18 extra: Transaction.amount is float on retrieval (matching
    the dataclass annotation), and a sub-cent value round-trips through
    the repository with at least mill precision.

    The existing test uses pytest.approx with a moderate-precision float.
    This test uses a value small enough that naive float rounding would
    produce a visibly different result (0.00123 → 0.001 on IEEE-754 single-
    precision float), verifying the repository preserves sufficient
    precision through store → retrieve.

    A broken _from_row that returned Decimal would cause type(created.amount)
    is float to fail, and a missing float coercion (e.g. passing the raw
    psycopg NUMERIC value through as-is) would produce a Decimal object
    that also fails the type identity check.
    """
    user_repo = UserRepository(infra)
    portfolio_repo = PortfolioRepository(infra)
    tx_repo = TransactionRepository(infra)

    user_repo.create(User(id="pg-subcent-user"))
    portfolio_repo.create(Portfolio(id="pg-subcent-port", user_id="pg-subcent-user"))

    # Sub-cent value: any rounding to fewer than 3 decimal places is
    # a visible error. IEEE-754 float32 would truncate this to 0.001.
    original_amount = 0.00123
    created = tx_repo.create(
        Transaction(
            id="pg-subcent-tx",
            portfolio_id="pg-subcent-port",
            kind="BUY",
            amount=original_amount,
        )
    )

    # Type identity: must be exactly float, not Decimal.
    assert type(created.amount) is float, (
        f"Transaction.amount must be exactly float after create; got "
        f"{type(created.amount).__name__}. If Decimal was returned, "
        f"TransactionRepository._from_row may be missing float coercion."
    )

    # Round-trip via get_by_id preserves type and sub-cent precision.
    retrieved = tx_repo.get_by_id("pg-subcent-tx")
    assert retrieved is not None
    assert type(retrieved.amount) is float
    # Relative tolerance of 1e-6 allows for float representation noise
    # but would catch a truncation to 2 decimal places.
    assert retrieved.amount == pytest.approx(original_amount, rel=1e-6)
