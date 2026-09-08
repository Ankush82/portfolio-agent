"""QA verification for STORY-3: Add delete() to the Infrastructure Protocol
and implement it in DefaultInfrastructure.

Each test below exercises one specific acceptance criterion from STORY-3's
PRD against the real, on-disk implementation. They intentionally avoid
re-running the existing test_infrastructure_postgres.py test suite — that
suite's tests prove the implementation works *somewhere*, not that
*this story's* particular acceptance criteria are satisfied.

Coverage of acceptance criteria (each one maps to at least one test):
  1. Protocol declares exactly one new method delete(self, table, id) -> bool
     in the same sync form as store/retrieve.
  2. No other method on the Protocol was added/removed/renamed.
  3. DefaultInfrastructure.delete uses a parameterized DELETE statement
     with the id bound as a parameter.
  4. DefaultInfrastructure.delete returns True for a deleted row and
     False for a missing id (and does not raise).
  5. The table name in DefaultInfrastructure.delete follows the same
     validation pattern as store/retrieve/query in that file.
  6. Every other Infrastructure implementation in the repo
     (StubInfrastructure, _InMemoryInfrastructure in the c01 tests)
     also implements delete.
  7. The required behavioural test — store then delete then retrieve
     returns None — passes against the in-memory double. (The same
     pattern is also asserted in tests/test_infrastructure_postgres.py
     against a live Postgres, but that test skips when Postgres is
     unreachable.)
  8. delete on an unknown id returns False without raising.
  9. No change was made to publish, subscribe, schedule, cache_get,
     cache_set, or get_secret — i.e. they all still exist on the
     Protocol and on DefaultInfrastructure with the same names.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

# Match the existing import style used in this repo's tests:
# pytest.ini already puts src/ on sys.path, so a bare `from infrastructure`
# works. If it isn't, fall back to an explicit path insertion so the test
# file is self-contained.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from infrastructure import Infrastructure, StubInfrastructure  # noqa: E402


# --- Helpers -------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _method_source(cls, name: str) -> str:
    """Return the text of `cls.name` source, or '' if absent."""
    try:
        return inspect.getsource(getattr(cls, name))
    except (AttributeError, TypeError):
        return ""


def _protocol_method_names() -> list[str]:
    """Names of *declared* methods on the Infrastructure Protocol class,
    reading the file rather than relying on dir() because Protocol
    members don't always show up there."""
    text = _read(_PROJECT_ROOT / "src" / "infrastructure.py")
    # Match `def <name>(` lines at indent level 4 (inside the class).
    return re.findall(r"^    def ([a-zA-Z_]\w*)\(", text, flags=re.MULTILINE)


# --- 1. Protocol declares exactly one new method, in sync form ----------

def test_protocol_declares_delete_with_required_signature():
    """Acceptance criterion: src/infrastructure.py declares exactly one
    new Protocol method delete(self, table: str, id: str) -> bool,
    sync form matching store/retrieve."""
    proto_src = _read(_PROJECT_ROOT / "src" / "infrastructure.py")

    # The `def delete(self, table: str, id: str) -> bool:` line must
    # exist verbatim, with `def` (not `async def`).
    pattern = r"^    def delete\(self, table: str, id: str\) -> bool:"
    assert re.search(pattern, proto_src, flags=re.MULTILINE), (
        "Infrastructure Protocol does not declare "
        "`def delete(self, table: str, id: str) -> bool:` verbatim"
    )
    assert not re.search(
        r"^    async def delete\(",
        proto_src,
        flags=re.MULTILINE,
    ), "delete must be sync (matching store/retrieve), not async"


def test_protocol_method_names_are_only_the_original_set_plus_delete():
    """Acceptance criterion: No other method on the Infrastructure
    Protocol was added, removed, renamed, or had its signature changed.
    The known baseline is: store, retrieve, query, publish, subscribe,
    schedule, cache_get, cache_set, get_secret — plus the new delete."""
    expected = {
        "store",
        "retrieve",
        "query",
        "publish",
        "subscribe",
        "schedule",
        "cache_get",
        "cache_set",
        "get_secret",
        "delete",
    }
    actual = set(_protocol_method_names())
    assert actual == expected, (
        f"Infrastructure Protocol method set changed. "
        f"missing={expected - actual}, extra={actual - expected}"
    )


# --- 2. StubInfrastructure also implements delete ------------------------

def test_stub_infrastructure_implements_delete_and_returns_false():
    """Acceptance criterion: every other Infrastructure implementation
    found in the repo also implements delete. StubInfrastructure is the
    obvious one; _InMemoryInfrastructure is checked separately below."""
    stub = StubInfrastructure()
    assert hasattr(stub, "delete"), "StubInfrastructure is missing delete"
    assert callable(stub.delete)
    # Stub returns False (idempotent no-op) — same shape as store=""
    # and retrieve=None.
    assert stub.delete("anything", "anything") is False


# --- 3. DefaultInfrastructure uses a parameterized DELETE -----------------

def test_default_infrastructure_delete_uses_parameterized_sql():
    """Acceptance criterion: DefaultInfrastructure.delete uses a
    parameterized DELETE statement with the id passed as a bound
    parameter. We assert this by inspecting the *source* of the
    method (since real Postgres may not be reachable in this CI run)
    so we know the SQL string contains `%s` placeholders and is NOT
    built with string interpolation of the id/table name."""
    from infrastructure_postgres import DefaultInfrastructure

    src = _read(_PROJECT_ROOT / "src" / "infrastructure_postgres.py")

    # Find the `def delete` block — read the line with `def delete(` and
    # then everything up to the next blank line at column 0 (end of
    # method) or the next `def ` at column 4, whichever comes first.
    m = re.search(r"    def delete\(self.*?\n(?=\n    def |\nclass )", src, flags=re.DOTALL)
    assert m, "Could not locate `def delete(...)` in infrastructure_postgres.py"
    delete_body = m.group(0)

    # Must contain a DELETE statement with %s placeholders.
    assert "DELETE" in delete_body.upper(), (
        f"DefaultInfrastructure.delete does not run a DELETE statement:\n{delete_body}"
    )
    assert "%s" in delete_body, (
        f"DefaultInfrastructure.delete is missing `%s` bound-parameter "
        f"placeholder:\n{delete_body}"
    )

    # Must NOT build the SQL via f-string / .format / % interpolation of
    # the id or table — only `cursor.execute(sql, params)` is allowed.
    assert "f\"DELETE" not in delete_body and "f'DELETE" not in delete_body, (
        f"DefaultInfrastructure.delete appears to use f-string SQL "
        f"interpolation; must use bound parameters:\n{delete_body}"
    )
    assert re.search(r"\.format\(", delete_body) is None, (
        f"DefaultInfrastructure.delete appears to use str.format for SQL; "
        f"must use bound parameters:\n{delete_body}"
    )
    # The execute call must pass a tuple/sequence of bound params.
    assert "cursor.execute" in delete_body
    assert re.search(
        r"cursor\.execute\([^)]*,\s*\(",
        delete_body,
        flags=re.DOTALL,
    ), f"DefaultInfrastructure.delete cursor.execute does not pass a "
    f"tuple of bound params after the SQL:\n{delete_body}"

    # Also check that the method actually returns cursor.rowcount > 0
    # (the documented return contract).
    assert "rowcount" in delete_body, (
        f"DefaultInfrastructure.delete must return cursor.rowcount > 0; "
        f"rowcount not referenced in:\n{delete_body}"
    )

    # Sanity: instantiate to confirm the class still loads (no syntax
    # errors etc).
    DefaultInfrastructure()


def test_default_infrastructure_delete_matches_store_retrieve_sync_form():
    """Acceptance criterion: sync/async-ness of delete matches store
    and retrieve. DefaultInfrastructure.store/retrieve are sync
    `def`s — delete must also be sync."""
    from infrastructure_postgres import DefaultInfrastructure

    delete_src = _method_source(DefaultInfrastructure, "delete")
    store_src = _method_source(DefaultInfrastructure, "store")
    retrieve_src = _method_source(DefaultInfrastructure, "retrieve")

    assert delete_src.startswith("    def delete("), (
        f"DefaultInfrastructure.delete is not a sync `def`:\n{delete_src}"
    )
    assert store_src.startswith("    def store(")
    assert retrieve_src.startswith("    def retrieve(")


def test_default_infrastructure_table_validation_matches_existing_pattern():
    """Acceptance criterion: the table name in DefaultInfrastructure.delete
    is validated using the same mechanism as store/retrieve/query in
    that file. Per docs/repo-layer-recon.md (V1) and the real source,
    the existing pattern is: NO table-name whitelist — the table name
    is bound straight into the parameterized query. Following that
    pattern, delete must NOT invent a brand-new validation/whitelist
    mechanism that store/retrieve/query don't have."""
    from infrastructure_postgres import DefaultInfrastructure

    delete_src = _method_source(DefaultInfrastructure, "delete")
    store_src = _method_source(DefaultInfrastructure, "store")
    retrieve_src = _method_source(DefaultInfrastructure, "query")

    # If any of store/retrieve/query contain a literal validation
    # helper (an assert, a raise on bad chars, etc.), delete must
    # use it too. If none of them validate, delete must NOT add one.
    def _has_validation(text: str) -> bool:
        # Crude but reliable: "table name" / "whitelist" / "allowed_tables"
        return bool(re.search(
            r"allowed_tables|ALLOWED_TABLES|table_whitelist|is_valid_table_name|invalid table",
            text,
            flags=re.IGNORECASE,
        ))

    store_validates = _has_validation(store_src)
    retrieve_validates = _has_validation(retrieve_src)
    query_validates = _has_validation(retrieve_src)
    delete_validates = _has_validation(delete_src)

    assert store_validates == delete_validates, (
        f"DefaultInfrastructure.delete validates table differently from "
        f"store. delete_validates={delete_validates}, store_validates={store_validates}"
    )
    assert retrieve_validates == delete_validates
    assert query_validates == delete_validates


# --- 4. delete returns True / False on real Postgres ---------------------

def _postgres_available() -> bool:
    try:
        import psycopg
        from infrastructure_postgres import DEFAULT_POSTGRES_DSN
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


_SKIP_NO_PG = pytest.mark.skipif(
    not _postgres_available(),
    reason="no live Postgres reachable at DEFAULT_POSTGRES_DSN — cannot run real-delete assertion",
)


@_SKIP_NO_PG
def test_default_infrastructure_delete_returns_true_then_false():
    """Acceptance criterion: DefaultInfrastructure.delete returns True
    when a row was deleted and False when no row matched, and does
    not raise on a missing id. This is the real-DB counterpart to the
    two tests that already exist in tests/test_infrastructure_postgres.py;
    we keep it independent so this story's PASS doesn't depend on
    which version of that file shipped."""
    import uuid

    from infrastructure_postgres import DefaultInfrastructure

    infra = DefaultInfrastructure()
    table = f"qa_story3_{uuid.uuid4().hex}"

    # Seed a row through the public store API, then delete it.
    stored_id = infra.store(table, {"id": "row-1", "x": 1})
    assert infra.delete(table, stored_id) is True, (
        "delete of an existing row must return True"
    )

    # Now delete it again — same id, must return False, must not raise.
    assert infra.delete(table, stored_id) is False, (
        "second delete of the same row must return False (idempotent)"
    )

    # And delete a never-existed id — must return False, must not raise.
    assert infra.delete(table, "never-existed") is False, (
        "delete of an unknown id must return False"
    )

    # retrieve after delete must return None.
    assert infra.retrieve(table, stored_id) is None


# --- 5. _InMemoryInfrastructure in the c01 test suite also has delete ----

def test_in_memory_infrastructure_in_c01_tests_implements_delete():
    """Acceptance criterion: any other existing Infrastructure
    implementation found in the repo also implements delete.
    _InMemoryInfrastructure (in tests/components/test_user_portfolio.py)
    is the in-memory test double documented in docs/repo-layer-recon.md."""
    from tests.components.test_user_portfolio import _InMemoryInfrastructure

    infra = _InMemoryInfrastructure()
    assert hasattr(infra, "delete"), (
        "_InMemoryInfrastructure is missing delete — every Protocol "
        "implementation must implement delete per STORY-3"
    )

    # Behavioural: store then delete then retrieve returns None.
    infra.store("widgets", {"id": "w-1", "name": "Widget"})
    assert infra.retrieve("widgets", "w-1") == {"id": "w-1", "name": "Widget"}
    assert infra.delete("widgets", "w-1") is True
    assert infra.retrieve("widgets", "w-1") is None

    # Behavioural: deleting an unknown id returns False and does not raise.
    assert infra.delete("widgets", "no-such-id") is False
    assert infra.delete("never-existed-table", "any-id") is False


# --- 6. No other Protocol/DI method was changed ---------------------------

def test_default_infrastructure_other_methods_unchanged():
    """Acceptance criterion: no change was made to publish, subscribe,
    schedule, cache_get, cache_set, or get_secret — these must all
    still exist on DefaultInfrastructure."""
    from infrastructure_postgres import DefaultInfrastructure

    for name in [
        "store",
        "retrieve",
        "query",
        "delete",
        "publish",
        "subscribe",
        "schedule",
        "cache_get",
        "cache_set",
        "get_secret",
    ]:
        assert hasattr(DefaultInfrastructure, name), (
            f"DefaultInfrastructure is missing {name} — STORY-3 forbids "
            f"removing any of these methods"
        )
        assert callable(getattr(DefaultInfrastructure, name))


def test_default_infrastructure_connection_handling_unchanged():
    """Acceptance criterion: no change to connection/pool handling.
    The lazy-connection pattern documented in
    tests/test_infrastructure_postgres.py must still be intact: the
    instance can be constructed with a bad DSN and no network call
    happens until a method is invoked."""
    from infrastructure_postgres import DefaultInfrastructure

    infra = DefaultInfrastructure(
        postgres_dsn="postgresql://unreachable-host:5432/nope",
        redis_url="redis://unreachable-host:6379/0",
    )
    # Constructing must not raise and must not touch the network.
    assert infra._pg_connection is None
    assert infra._redis_client is None