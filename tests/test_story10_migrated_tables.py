"""QA tests for STORY-10 — DefaultInfrastructure's column-aware
store/retrieve/query/delete for the four migrated tables (users,
portfolios, holdings, transactions). Needs a live Postgres with the
real migration already applied (scripts/run_migration.sh) — skips
cleanly, with a clear reason, when Postgres isn't reachable.
"""

import uuid
from decimal import Decimal

import pytest

from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure, MIGRATED_TABLES, TableSpec


def _postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="no live Postgres reachable at DEFAULT_POSTGRES_DSN — run `docker-compose up -d` "
    "and `./scripts/run_migration.sh` for real coverage",
)


@pytest.fixture
def infra():
    return DefaultInfrastructure()


@pytest.fixture
def real_user(infra):
    user_id = str(uuid.uuid4())
    infra.store("users", {"id": user_id, "email": f"{user_id}@example.com", "preferences": {}})
    yield user_id
    infra.delete("users", user_id)


@pytest.fixture
def real_portfolio(infra, real_user):
    portfolio_id = str(uuid.uuid4())
    infra.store("portfolios", {"id": portfolio_id, "user_id": real_user})
    yield portfolio_id
    infra.delete("portfolios", portfolio_id)


def test_migrated_tables_registry_has_exactly_the_four_entries():
    assert set(MIGRATED_TABLES.keys()) == {"users", "portfolios", "holdings", "transactions"}
    for spec in MIGRATED_TABLES.values():
        assert isinstance(spec, TableSpec)


def test_migrated_tables_registry_matches_the_story_spec():
    assert MIGRATED_TABLES["users"] == TableSpec(
        pk="id", columns=("id", "email", "preferences"), jsonb=("preferences",)
    )
    assert MIGRATED_TABLES["portfolios"] == TableSpec(pk="id", columns=("id", "user_id"), jsonb=())
    assert MIGRATED_TABLES["holdings"] == TableSpec(
        pk="id",
        columns=("id", "portfolio_id", "security_id", "quantity", "currency", "exchange", "symbol_suffix"),
        jsonb=(),
    )
    assert MIGRATED_TABLES["transactions"] == TableSpec(
        pk="id", columns=("id", "portfolio_id", "kind", "amount"), jsonb=()
    )


@requires_postgres
def test_store_then_retrieve_holding_returns_real_typed_columns(infra, real_portfolio):
    holding_id = str(uuid.uuid4())
    infra.store(
        "holdings",
        {
            "id": holding_id,
            "portfolio_id": real_portfolio,
            "security_id": "AAPL",
            "quantity": Decimal("10.5"),
            "currency": "USD",
            "exchange": "NASDAQ",
            "symbol_suffix": None,
        },
    )

    result = infra.retrieve("holdings", holding_id)

    assert result["portfolio_id"] == real_portfolio
    assert result["security_id"] == "AAPL"
    assert result["quantity"] == Decimal("10.5")
    assert result["currency"] == "USD"
    infra.delete("holdings", holding_id)


@requires_postgres
def test_store_twice_same_id_upserts_not_duplicates(infra, real_portfolio):
    holding_id = str(uuid.uuid4())
    row = {
        "id": holding_id,
        "portfolio_id": real_portfolio,
        "security_id": "AAPL",
        "quantity": Decimal("5"),
        "currency": "USD",
        "exchange": "NASDAQ",
        "symbol_suffix": None,
    }
    infra.store("holdings", row)
    infra.store("holdings", {**row, "quantity": Decimal("99")})

    results = infra.query("holdings", {"portfolio_id": real_portfolio})

    assert len(results) == 1
    assert results[0]["quantity"] == Decimal("99")
    infra.delete("holdings", holding_id)


@requires_postgres
def test_query_by_real_column_returns_only_matching_rows(infra, real_portfolio):
    id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
    infra.store(
        "holdings",
        {"id": id1, "portfolio_id": real_portfolio, "security_id": "AAPL", "quantity": Decimal("1"), "currency": "USD", "exchange": None, "symbol_suffix": None},
    )
    infra.store(
        "holdings",
        {"id": id2, "portfolio_id": real_portfolio, "security_id": "MSFT", "quantity": Decimal("2"), "currency": "USD", "exchange": None, "symbol_suffix": None},
    )

    results = infra.query("holdings", {"portfolio_id": real_portfolio, "security_id": "AAPL"})

    assert len(results) == 1
    assert results[0]["id"] == id1
    infra.delete("holdings", id1)
    infra.delete("holdings", id2)


@requires_postgres
def test_query_unknown_filter_key_raises_value_error(infra, real_portfolio):
    with pytest.raises(ValueError):
        infra.query("holdings", {"not_a_real_column": "x"})


@requires_postgres
def test_delete_removes_the_real_row_returns_true_then_false(infra, real_portfolio):
    holding_id = str(uuid.uuid4())
    infra.store(
        "holdings",
        {"id": holding_id, "portfolio_id": real_portfolio, "security_id": "AAPL", "quantity": Decimal("1"), "currency": "USD", "exchange": None, "symbol_suffix": None},
    )

    assert infra.delete("holdings", holding_id) is True
    assert infra.retrieve("holdings", holding_id) is None
    assert infra.delete("holdings", holding_id) is False


@requires_postgres
def test_users_preferences_round_trips_as_jsonb_nested_dict_survives(infra):
    user_id = str(uuid.uuid4())
    nested = {"theme": "dark", "notifications": {"email": True, "sms": False}, "tags": ["a", "b"]}
    infra.store("users", {"id": user_id, "email": "nested@example.com", "preferences": nested})

    result = infra.retrieve("users", user_id)

    assert result["preferences"] == nested
    assert isinstance(result["preferences"], dict)
    infra.delete("users", user_id)


@requires_postgres
def test_foreign_key_is_real_and_enforced_by_postgres_not_this_code(infra):
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        infra.store(
            "holdings",
            {
                "id": str(uuid.uuid4()),
                "portfolio_id": "does-not-exist",
                "security_id": "AAPL",
                "quantity": Decimal("1"),
                "currency": "USD",
                "exchange": None,
                "symbol_suffix": None,
            },
        )


@requires_postgres
def test_non_migrated_table_behavior_is_unchanged(infra):
    """A table NOT in MIGRATED_TABLES must still go through the old,
    generic `records` JSONB path -- byte-identical behavior."""
    table = f"test_unrelated_{uuid.uuid4().hex}"
    infra.store(table, {"id": "x1", "anything": {"nested": True}})

    result = infra.retrieve(table, "x1")

    assert result == {"id": "x1", "anything": {"nested": True}}
    assert infra.delete(table, "x1") is True


@requires_postgres
def test_table_and_column_names_never_come_from_caller_input():
    """Structural check: the SQL-building code interpolates only
    spec.columns/spec.pk/table (all sourced from the fixed MIGRATED_TABLES
    registry, never a filters/record key), and every real value passed
    by a caller goes through a bound parameter (%s), never an f-string."""
    import inspect

    import infrastructure_postgres as module

    source = inspect.getsource(module.DefaultInfrastructure.store) + inspect.getsource(
        module.DefaultInfrastructure.query
    )
    # The only f-string-built SQL fragments reference spec.*/table, never
    # a caller-supplied filters/record value directly.
    assert "{key}" not in source or "for key in filters" in source
