"""Tests for :class:`PortfolioRepository` against the
:class:`FakeInfrastructure` from STORY-4.

Every test runs against the fake — no real Postgres, no live
Redis — so the suite executes unconditionally. The repository is
the unit under test here; ``FakeInfrastructure`` is the fixture.

These tests exercise:

* ``create`` → ``get_by_id`` round-trip with field-by-field
  equality.
* ``update`` → ``get_by_id`` reflects the change.
* ``delete`` → ``get_by_id`` returns ``None``.
* ``update`` raises ``KeyError`` when the target row does not exist.
* ``_from_row`` ignores unknown keys (``created_at`` /
  ``updated_at``) without breaking the round-trip.
* ``create`` generates a uuid4 id when the entity has none, and
  the returned entity carries it.
* ``list_for_user`` returns [] (never None) when the user has no
  portfolios.
* ``list_for_user`` returns only the portfolios belonging to the
  requested user.
* ``list_for_user`` results are deterministically ordered by id.
"""

from __future__ import annotations

import inspect
import re

from domain import PORTFOLIOS_TABLE, Portfolio
from infrastructure import Infrastructure
from tests.support.fake_infrastructure import FakeInfrastructure
from repositories.portfolio_repository import PortfolioRepository


# --- Round-trip: create -> get_by_id ------------------------------------


def test_create_then_get_by_id_round_trips_every_field() -> None:
    """create -> get_by_id must round-trip every field against the fake."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    created = repo.create(
        Portfolio(
            id="portfolio-1",
            user_id="user-1",
        )
    )

    # `create` returns the persisted entity carrying the id it
    # actually stored.
    assert created.id == "portfolio-1"
    assert created.user_id == "user-1"

    fetched = repo.get_by_id("portfolio-1")
    assert fetched is not None
    # Field-by-field equality.
    assert fetched.id == created.id
    assert fetched.user_id == created.user_id


def test_create_generates_uuid4_id_when_entity_has_none() -> None:
    """create must generate a uuid4 string id when the entity's id
    is empty / unset, and the returned entity must carry it."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    created = repo.create(
        Portfolio(id="", user_id="user-1")
    )

    # uuid4 hex with dashes: 8-4-4-4-12.
    uuid4_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert created.id != ""
    assert uuid4_pattern.match(created.id), (
        f"expected uuid4-shaped id, got {created.id!r}"
    )

    # And the same id is observable through a subsequent read.
    fetched = repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.user_id == "user-1"


def test_get_by_id_returns_none_for_unknown_id_without_raising() -> None:
    """get_by_id must return ``None`` for an unknown id and never
    raise."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    assert repo.get_by_id("does-not-exist") is None


# --- Update ------------------------------------------------------------


def test_update_replaces_existing_row_and_get_reflects_the_change() -> None:
    """update -> get_by_id must reflect the new mutable fields."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    repo.create(
        Portfolio(
            id="portfolio-1",
            user_id="user-1",
        )
    )

    updated = repo.update(
        Portfolio(
            id="portfolio-1",
            user_id="user-2",  # changing user_id
        )
    )
    assert updated.id == "portfolio-1"
    assert updated.user_id == "user-2"

    fetched = repo.get_by_id("portfolio-1")
    assert fetched is not None
    assert fetched.user_id == "user-2"


def test_update_raises_keyerror_when_target_row_does_not_exist() -> None:
    """update must raise ``KeyError`` when the target row does not
    exist — it must never silently insert."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    try:
        repo.update(
            Portfolio(id="ghost", user_id="user-1")
        )
    except KeyError as exc:
        # The KeyError must carry the missing id so callers can
        # surface a useful error.
        assert exc.args[0] == "ghost"
    else:
        raise AssertionError(
            "PortfolioRepository.update must raise KeyError for an absent id"
        )

    # And the ghost row really was not silently inserted.
    assert repo.get_by_id("ghost") is None


# --- Delete -------------------------------------------------------------


def test_delete_returns_true_for_existing_row_and_get_returns_none() -> None:
    """delete must return ``True`` for an existing row, and a
    subsequent ``get_by_id`` must return ``None``."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    repo.create(Portfolio(id="portfolio-1", user_id="user-1"))
    assert repo.delete("portfolio-1") is True
    assert repo.get_by_id("portfolio-1") is None


def test_delete_returns_false_for_missing_row_idempotently() -> None:
    """delete must be idempotent: return ``False`` when no matching
    row exists, never raise."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    assert repo.delete("does-not-exist") is False
    # And a second call still returns False.
    assert repo.delete("does-not-exist") is False


# --- _from_row ignores unknown keys -------------------------------------


def test_from_row_ignores_unknown_keys_like_created_at_and_updated_at() -> None:
    """``_from_row`` must silently drop keys the dataclass does not
    own (``created_at``, ``updated_at``, ...) so a DB-managed
    timestamp column can never break a round-trip."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    row = {
        "id": "portfolio-1",
        "user_id": "user-1",
        # DB-managed columns that the repository never wrote.
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-06-01T12:34:56Z",
    }

    portfolio = repo._from_row(row)
    assert isinstance(portfolio, Portfolio)
    assert portfolio.id == "portfolio-1"
    assert portfolio.user_id == "user-1"


# --- STORY-6 acceptance criteria: focused QA assertions ----------------


def test_qa_story6_constructor_accepts_protocol_typed_infrastructure() -> None:
    """STORY-6 acceptance: ``PortfolioRepository.__init__`` must accept a
    single infrastructure argument typed against the Infrastructure
    Protocol. The fake (FakeInfrastructure from STORY-4) structurally
    satisfies that Protocol, and the repo must accept it cleanly,
    expose it as ``self._infrastructure``, and route every CRUD call
    through that exact instance (no parallel module-level state)."""
    import inspect

    sig = inspect.signature(PortfolioRepository.__init__)
    params = list(sig.parameters.values())
    # Exactly one parameter besides self.
    assert [p.name for p in params] == ["self", "infrastructure"], (
        f"PortfolioRepository.__init__ must take exactly one parameter "
        f"named 'infrastructure' (plus self); got {[p.name for p in params]}"
    )
    # And the annotation must reference the Infrastructure Protocol,
    # not a concrete backend class. The repository file uses
    # ``from __future__ import annotations``, so raw attribute
    # access sees the string "Infrastructure" rather than the class
    # object -- that is fine, the contract is just "the annotation
    # names the Protocol". We also resolve through
    # ``typing.get_type_hints`` to confirm the string really maps
    # back to the Infrastructure Protocol class itself.
    from infrastructure import Infrastructure as _Infra
    from typing import get_type_hints

    annotation = sig.parameters["infrastructure"].annotation
    # Accept either form: the live class (no future-imports), or
    # the string "Infrastructure" (with future-imports).
    assert annotation is _Infra or annotation == "Infrastructure", (
        "PortfolioRepository.__init__ must annotate 'infrastructure' with "
        f"the Infrastructure Protocol; got annotation={annotation!r}"
    )
    # Resolve the type hints of the class itself so the
    # future-imports string form is checked against the real class.
    resolved = get_type_hints(PortfolioRepository.__init__)
    assert resolved.get("infrastructure") is _Infra, (
        f"resolved 'infrastructure' annotation must be Infrastructure; "
        f"got {resolved.get('infrastructure')!r}"
    )

    # Constructor injection: the repo holds the exact fake we passed
    # and routes every CRUD method through it.
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)
    assert repo._infrastructure is fake

    # And every CRUD method really delegates to the injected fake,
    # not to some shared module-level state. We verify this by
    # constructing two repos over two independent fakes: an insert
    # through repo A must NOT appear in repo B's view.
    fake_a = FakeInfrastructure()
    fake_b = FakeInfrastructure()
    repo_a = PortfolioRepository(fake_a)
    repo_b = PortfolioRepository(fake_b)

    repo_a.create(Portfolio(id="iso-a", user_id="user-1"))
    assert repo_a.get_by_id("iso-a") is not None
    assert repo_b.get_by_id("iso-a") is None, (
        "Two repos over independent fakes must not share storage"
    )


def test_qa_story6_to_row_emits_exactly_id_user_id() -> None:
    """STORY-6 acceptance: ``_to_row`` must emit exactly the keys the
    Portfolio dataclass owns (``id``, ``user_id``) -- no
    extra keys, no missing keys, no DB-managed timestamps. This is
    the closed, explicit mapping the row layer is supposed to keep."""
    repo = PortfolioRepository(FakeInfrastructure())
    portfolio = Portfolio(
        id="p-1",
        user_id="user-1",
    )

    row = repo._to_row(portfolio)
    assert set(row.keys()) == {"id", "user_id"}, (
        f"_to_row must emit exactly id/user_id; got keys "
        f"{sorted(row.keys())}"
    )
    assert row["id"] == "p-1"
    assert row["user_id"] == "user-1"

    # And the closed-set contract holds even when the Portfolio has
    # default values -- defaults are still exactly those two keys.
    row_default = repo._to_row(Portfolio(id="p-2", user_id="user-default"))
    assert set(row_default.keys()) == {"id", "user_id"}


def test_qa_story6_update_is_full_replace_not_partial_patch() -> None:
    """STORY-6 acceptance: ``update`` is a FULL replace of the
    mutable columns, not a partial patch. If the caller passes a
    Portfolio with the same id but a brand-new user_id that
    drops the old user_id, the old user_id must NOT linger
    after the update (the repository stores the row it was given,
    not a merged row)."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    repo.create(
        Portfolio(
            id="p-1",
            user_id="user-old",
        )
    )
    # Confirm the pre-state really has the old user_id.
    pre = repo.get_by_id("p-1")
    assert pre is not None
    assert pre.user_id == "user-old"

    # Full replace with a new user_id.
    repo.update(
        Portfolio(
            id="p-1",
            user_id="user-new",
        )
    )

    post = repo.get_by_id("p-1")
    assert post is not None
    # Id preserved.
    assert post.id == "p-1"
    # user_id FULLY replaced -- old user_id must be
    # gone, not silently merged.
    assert post.user_id == "user-new", (
        f"update must FULL-replace user_id; got {post.user_id!r}"
    )


def test_qa_story6_list_for_user_returns_empty_list_when_no_portfolios() -> None:
    """STORY-6 acceptance: ``list_for_user`` must return [] (never
    None) when the user has no portfolios."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    # No portfolios for user-1
    result = repo.list_for_user("user-1")
    assert isinstance(result, list)
    assert result == []


def test_qa_story6_list_for_user_returns_only_portfolios_for_requested_user() -> None:
    """STORY-6 acceptance: ``list_for_user`` must return only the
    portfolios belonging to the requested user -- a test seeds
    portfolios for two different users and asserts no cross-user
    leakage."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    # Create portfolios for user-1
    repo.create(Portfolio(id="p1", user_id="user-1"))
    repo.create(Portfolio(id="p2", user_id="user-1"))
    # Create a portfolio for user-2
    repo.create(Portfolio(id="p3", user_id="user-2"))

    # Query for user-1 should return only p1 and p2
    user1_portfolios = repo.list_for_user("user-1")
    assert len(user1_portfolios) == 2
    assert {p.id for p in user1_portfolios} == {"p1", "p2"}
    # Ensure each portfolio belongs to user-1
    for p in user1_portfolios:
        assert p.user_id == "user-1"

    # Query for user-2 should return only p3
    user2_portfolios = repo.list_for_user("user-2")
    assert len(user2_portfolios) == 1
    assert user2_portfolios[0].id == "p3"
    assert user2_portfolios[0].user_id == "user-2"


def test_qa_story6_list_for_user_results_are_deterministically_ordered_by_id() -> None:
    """STORY-6 acceptance: ``list_for_user`` results must be
    deterministically ordered by id when the Protocol's query
    offers no ordering, asserted by a test that seeds rows out
    of order."""
    fake = FakeInfrastructure()
    repo = PortfolioRepository(fake)

    # Insert portfolios in non-id order: z, a, m
    repo.create(Portfolio(id="z-portfolio", user_id="user-1"))
    repo.create(Portfolio(id="a-portfolio", user_id="user-1"))
    repo.create(Portfolio(id="m-portfolio", user_id="user-1"))

    result = repo.list_for_user("user-1")
    # Expect ordering by id: a, m, z
    assert [p.id for p in result] == ["a-portfolio", "m-portfolio", "z-portfolio"]


def test_qa_story6_module_is_stateless_and_no_sql_or_forbidden_imports() -> None:
    """STORY-6 acceptance: the repository module must not import
    psycopg, redis, sqlalchemy, or src.infrastructure_postgres, must
    not contain SQL strings, and must not carry module-level caches
    or connection state. We verify this by reading the real source
    and asserting the module's ``__dict__`` carries no caches."""
    import ast
    import repositories.portfolio_repository as pr_module

    source = inspect.getsource(pr_module)
    tree = ast.parse(source)

    # 1) Forbidden library imports: walk the top-level AST and find
    # any import statement whose module / name actually resolves to
    # a forbidden package. ``ast`` walks the real syntax tree, so a
    # docstring mention of "psycopg" inside a triple-quoted string
    # does not trip this -- only a real ``import psycopg`` /
    # ``from psycopg import ...`` does.
    forbidden_roots = {"psycopg", "redis", "sqlalchemy"}
    forbidden_modules = {
        "src.infrastructure_postgres",
        "infrastructure_postgres",
    }
    offending_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in forbidden_roots:
                    offending_imports.append(
                        f"import {alias.name} (line {node.lineno})"
                    )
        elif isinstance(node, ast.ImportFrom):
            # ``from x.y.z import ...`` -- if x.y.z is exactly the
            # forbidden module, that's a violation. And if x is one
            # of the forbidden roots, that's also a violation
            # (e.g. ``from psycopg import connect``).
            if node.module is None:
                continue
            if node.module in forbidden_modules:
                offending_imports.append(
                    f"from {node.module} import ... (line {node.lineno})"
                )
            elif node.module.split(".")[0] in forbidden_roots:
                offending_imports.append(
                    f"from {node.module} import ... (line {node.lineno})"
                )

    assert offending_imports == [], (
        "portfolio_repository.py must not import psycopg, redis, sqlalchemy, "
        f"or src.infrastructure_postgres; offending imports: {offending_imports}"
    )

    # 2) No SQL strings. Walk every real string constant the AST
    # knows about (including docstrings -- a docstring that quotes
    # a SQL keyword is fine, but a *statement-level* SQL string
    # literal is a real fail). To distinguish the two, only check
    # string nodes whose value is a ``str`` and whose line number
    # is inside a non-docstring context: we approximate that by
    # skipping any string node that is the first statement of a
    # module / function / class body (those are docstrings, by
    # convention) and skipping plain expressions like ``"id"``,
    # ``"user_id"`` -- we only flag strings that contain a SQL
    # keyword as a SUBSTRING.
    sql_keywords = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "FROM",
        "WHERE", "JOIN", "TRUNCATE", "DROP ",
    )

    def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
        # A docstring is an Expr whose value is a Constant string
        # AND is the first statement in a Module / FunctionDef /
        # AsyncFunctionDef / ClassDef body. This helper is called on
        # the parent ``ast.Expr`` (not on the inner Constant), because
        # that is the actual statement node -- the Constant is just
        # the value the Expr wraps.
        if not isinstance(node, ast.Expr):
            return False
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value, str
        ):
            return False
        if parent is None or not hasattr(parent, "body"):
            return False
        return (
            isinstance(
                parent,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
            and len(parent.body) > 0
            and parent.body[0] is node
        )

    # Pre-compute docstring line numbers so the SQL check below can
    # skip every line that is part of any docstring (those lines are
    # documentation, not executable code, and a docstring may quote
    # SQL keywords without violating the "no SQL" contract).
    docstring_lines: set[int] = set()
    offending_sql_strings: list[str] = []

    def _collect_docstrings(node: ast.AST) -> None:
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                # Mark every line of this docstring as "docstring"
                # so the SQL check can ignore any SQL keyword that
                # appears on one of those lines.
                start = node.body[0].lineno
                # AST does not give us end_lineno for old Pythons,
                # but Python 3.11+ does. Fall back to start.
                end = getattr(node.body[0], "end_lineno", start)
                for ln in range(start, end + 1):
                    docstring_lines.add(ln)
        for child in ast.iter_child_nodes(node):
            _collect_docstrings(child)

    _collect_docstrings(tree)

    def _walk(node: ast.AST, parent: ast.AST | None = None) -> None:
        # Recurse first so we visit every node.
        for child in ast.iter_child_nodes(node):
            _walk(child, node)
        # Skip docstring statements entirely (their text is
        # documentation, not code that would build a SQL string).
        if _is_docstring(node, parent):
            return
        # Only flag a string constant that is on a NON-docstring
        # line. We check the string value directly.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in docstring_lines:
                return
            value_upper = node.value.upper()
            for kw in sql_keywords:
                if kw in value_upper:
                    offending_sql_strings.append(
                        f"line {node.lineno}: {node.value!r}"
                    )
                    break

    _walk(tree)

    assert offending_sql_strings == [], (
        "portfolio_repository.py must not contain SQL strings; found: "
        f"{offending_sql_strings}"
    )

    # 3) Module-level caches / state: the module's __dict__ must NOT
    # carry any of the names one would expect if the repo were
    # caching things at import time (no _CACHE / _STORE / _CONN /
    # _DB / _POOL / _TABLE globals of its own -- it owns no storage).
    forbidden_globals = {
        name
        for name in pr_module.__dict__.keys()
        if name in ("_CACHE", "_STORE", "_CONNECTION", "_DB", "_POOL", "_TABLE_CACHE")
    }
    assert forbidden_globals == set(), (
        f"portfolio_repository.py must not carry module-level caches / "
        f"connections / storage; found: {sorted(forbidden_globals)}"
    )