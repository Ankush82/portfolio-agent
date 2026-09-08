"""Tests for :class:`UserRepository` against the
:class:`FakeInfrastructure` from STORY-4.

Every test runs against the fake — no real Postgres, no live
Redis — so the suite executes unconditionally. The repository is
the unit under test here; ``FakeInfrastructure`` is the fixture.

These tests exercise:

* ``create`` → ``get_by_id`` round-trip with field-by-field
  equality including the nested ``preferences`` dict.
* ``update`` → ``get_by_id`` reflects the change.
* ``delete`` → ``get_by_id`` returns ``None``.
* ``update`` raises ``KeyError`` when the target row does not exist.
* ``_from_row`` ignores unknown keys (``created_at`` /
  ``updated_at``) without breaking the round-trip.
* ``create`` generates a uuid4 id when the entity has none, and
  the returned entity carries it.
"""

from __future__ import annotations

import inspect
import re

from domain import USERS_TABLE, User
from infrastructure import Infrastructure
from tests.support.fake_infrastructure import FakeInfrastructure
from repositories.user_repository import UserRepository


# --- Round-trip: create -> get_by_id ------------------------------------


def test_create_then_get_by_id_round_trips_every_field_including_preferences() -> None:
    """create -> get_by_id must round-trip every field, including the
    nested ``preferences`` dict, against the fake."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    created = repo.create(
        User(
            id="user-1",
            email="ada@example.com",
            preferences={"theme": "dark", "timezone": "UTC"},
        )
    )

    # `create` returns the persisted entity carrying the id it
    # actually stored.
    assert created.id == "user-1"
    assert created.email == "ada@example.com"
    assert created.preferences == {"theme": "dark", "timezone": "UTC"}

    fetched = repo.get_by_id("user-1")
    assert fetched is not None
    # Field-by-field equality, including the nested dict. Using
    # attribute equality (not `==` against a literal) keeps the
    # assertion honest about what the repository really persisted.
    assert fetched.id == created.id
    assert fetched.email == created.email
    assert fetched.preferences == created.preferences


def test_create_generates_uuid4_id_when_entity_has_none() -> None:
    """create must generate a uuid4 string id when the entity's id
    is empty / unset, and the returned entity must carry it."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    created = repo.create(
        User(id="", email="no-id@example.com", preferences={"k": "v"})
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
    assert fetched.email == "no-id@example.com"


def test_get_by_id_returns_none_for_unknown_id_without_raising() -> None:
    """get_by_id must return ``None`` for an unknown id and never
    raise."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    assert repo.get_by_id("does-not-exist") is None


# --- Update ------------------------------------------------------------


def test_update_replaces_existing_row_and_get_reflects_the_change() -> None:
    """update -> get_by_id must reflect the new mutable fields."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    repo.create(
        User(
            id="user-1",
            email="before@example.com",
            preferences={"theme": "light"},
        )
    )

    updated = repo.update(
        User(
            id="user-1",
            email="after@example.com",
            preferences={"theme": "dark", "lang": "en"},
        )
    )
    assert updated.id == "user-1"
    assert updated.email == "after@example.com"
    assert updated.preferences == {"theme": "dark", "lang": "en"}

    fetched = repo.get_by_id("user-1")
    assert fetched is not None
    assert fetched.email == "after@example.com"
    assert fetched.preferences == {"theme": "dark", "lang": "en"}


def test_update_raises_keyerror_when_target_row_does_not_exist() -> None:
    """update must raise ``KeyError`` when the target row does not
    exist — it must never silently insert."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    try:
        repo.update(
            User(id="ghost", email="ghost@example.com", preferences={})
        )
    except KeyError as exc:
        # The KeyError must carry the missing id so callers can
        # surface a useful error.
        assert exc.args[0] == "ghost"
    else:
        raise AssertionError(
            "UserRepository.update must raise KeyError for an absent id"
        )

    # And the ghost row really was not silently inserted.
    assert repo.get_by_id("ghost") is None


# --- Delete -------------------------------------------------------------


def test_delete_returns_true_for_existing_row_and_get_returns_none() -> None:
    """delete must return ``True`` for an existing row, and a
    subsequent ``get_by_id`` must return ``None``."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    repo.create(User(id="user-1", email="x@example.com", preferences={}))
    assert repo.delete("user-1") is True
    assert repo.get_by_id("user-1") is None


def test_delete_returns_false_for_missing_row_idempotently() -> None:
    """delete must be idempotent: return ``False`` when no matching
    row exists, never raise."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    assert repo.delete("does-not-exist") is False
    # And a second call still returns False.
    assert repo.delete("does-not-exist") is False


# --- _from_row ignores unknown keys -------------------------------------


def test_from_row_ignores_unknown_keys_like_created_at_and_updated_at() -> None:
    """``_from_row`` must silently drop keys the dataclass does not
    own (``created_at``, ``updated_at``, ...) so a DB-managed
    timestamp column can never break a round-trip."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    row = {
        "id": "user-1",
        "email": "ada@example.com",
        "preferences": {"theme": "dark"},
        # DB-managed columns that the repository never wrote.
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-06-01T12:34:56Z",
    }

    user = repo._from_row(row)
    assert isinstance(user, User)
    assert user.id == "user-1"
    assert user.email == "ada@example.com"
    assert user.preferences == {"theme": "dark"}


# --- STORY-5 acceptance criteria: focused QA assertions ----------------


def test_qa_story5_constructor_accepts_protocol_typed_infrastructure() -> None:
    """STORY-5 acceptance: ``UserRepository.__init__`` must accept a
    single infrastructure argument typed against the Infrastructure
    Protocol. The fake (FakeInfrastructure from STORY-4) structurally
    satisfies that Protocol, and the repo must accept it cleanly,
    expose it as ``self._infrastructure``, and route every CRUD call
    through that exact instance (no parallel module-level state)."""
    import inspect

    sig = inspect.signature(UserRepository.__init__)
    params = list(sig.parameters.values())
    # Exactly one parameter besides self.
    assert [p.name for p in params] == ["self", "infrastructure"], (
        f"UserRepository.__init__ must take exactly one parameter "
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
        "UserRepository.__init__ must annotate 'infrastructure' with "
        f"the Infrastructure Protocol; got annotation={annotation!r}"
    )
    # Resolve the type hints of the class itself so the
    # future-imports string form is checked against the real class.
    resolved = get_type_hints(UserRepository.__init__)
    assert resolved.get("infrastructure") is _Infra, (
        f"resolved 'infrastructure' annotation must be Infrastructure; "
        f"got {resolved.get('infrastructure')!r}"
    )

    # Constructor injection: the repo holds the exact fake we passed
    # and routes every CRUD method through it.
    fake = FakeInfrastructure()
    repo = UserRepository(fake)
    assert repo._infrastructure is fake

    # And every CRUD method really delegates to the injected fake,
    # not to some shared module-level state. We verify this by
    # constructing two repos over two independent fakes: an insert
    # through repo A must NOT appear in repo B's view.
    fake_a = FakeInfrastructure()
    fake_b = FakeInfrastructure()
    repo_a = UserRepository(fake_a)
    repo_b = UserRepository(fake_b)

    repo_a.create(User(id="iso-a", email="a@example.com", preferences={}))
    assert repo_a.get_by_id("iso-a") is not None
    assert repo_b.get_by_id("iso-a") is None, (
        "Two repos over independent fakes must not share storage"
    )


def test_qa_story5_to_row_emits_exactly_id_email_preferences() -> None:
    """STORY-5 acceptance: ``_to_row`` must emit exactly the keys the
    User dataclass owns (``id``, ``email``, ``preferences``) -- no
    extra keys, no missing keys, no DB-managed timestamps. This is
    the closed, explicit mapping the row layer is supposed to keep."""
    repo = UserRepository(FakeInfrastructure())
    user = User(
        id="u-1",
        email="only-these-three@example.com",
        preferences={"theme": "dark", "lang": "en"},
    )

    row = repo._to_row(user)
    assert set(row.keys()) == {"id", "email", "preferences"}, (
        f"_to_row must emit exactly id/email/preferences; got keys "
        f"{sorted(row.keys())}"
    )
    assert row["id"] == "u-1"
    assert row["email"] == "only-these-three@example.com"
    assert row["preferences"] == {"theme": "dark", "lang": "en"}

    # And the closed-set contract holds even when the User has
    # default values -- defaults are still exactly those three keys.
    row_default = repo._to_row(User(id="u-2"))
    assert set(row_default.keys()) == {"id", "email", "preferences"}


def test_qa_story5_update_is_full_replace_not_partial_patch() -> None:
    """STORY-5 acceptance: ``update`` is a FULL replace of the
    mutable columns, not a partial patch. If the caller passes a
    User with the same id but a brand-new preferences dict that
    drops keys present in the old row, those keys must NOT linger
    after the update (the repository stores the row it was given,
    not a merged row)."""
    fake = FakeInfrastructure()
    repo = UserRepository(fake)

    repo.create(
        User(
            id="u-1",
            email="before@example.com",
            preferences={"theme": "dark", "lang": "en", "tz": "UTC"},
        )
    )
    # Confirm the pre-state really has all three preferences.
    pre = repo.get_by_id("u-1")
    assert pre is not None
    assert pre.preferences == {"theme": "dark", "lang": "en", "tz": "UTC"}

    # Full replace with only one preferences key.
    repo.update(
        User(
            id="u-1",
            email="after@example.com",
            preferences={"theme": "light"},
        )
    )

    post = repo.get_by_id("u-1")
    assert post is not None
    # Email replaced, id preserved.
    assert post.id == "u-1"
    assert post.email == "after@example.com"
    # preferences FULLY replaced -- old keys ('lang', 'tz') must be
    # gone, not silently merged into the new dict.
    assert post.preferences == {"theme": "light"}, (
        f"update must FULL-replace preferences; got {post.preferences!r}"
    )


def test_qa_story5_module_is_stateless_and_no_sql_or_forbidden_imports() -> None:
    """STORY-5 acceptance: the repository module must not import
    psycopg, redis, sqlalchemy, or src.infrastructure_postgres, must
    not contain SQL strings, and must not carry module-level caches
    or connection state. We verify this by reading the real source
    and asserting the module's ``__dict__`` carries no caches."""
    import ast
    import repositories.user_repository as ur_module

    source = inspect.getsource(ur_module)
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
        "user_repository.py must not import psycopg, redis, sqlalchemy, "
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
    # ``"email"`` -- we only flag strings that contain a SQL
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
        "user_repository.py must not contain SQL strings; found: "
        f"{offending_sql_strings}"
    )

    # 3) Module-level caches / state: the module's __dict__ must NOT
    # carry any of the names one would expect if the repo were
    # caching things at import time (no _CACHE / _STORE / _CONN /
    # _TABLE globals of its own -- it owns no storage).
    forbidden_globals = {
        name
        for name in ur_module.__dict__.keys()
        if name in ("_CACHE", "_STORE", "_CONNECTION", "_DB", "_POOL", "_TABLE_CACHE")
    }
    assert forbidden_globals == set(), (
        f"user_repository.py must not carry module-level caches / "
        f"connections / storage; found: {sorted(forbidden_globals)}"
    )


def test_qa_story5_get_by_email_is_omitted_when_recon_v5_says_no_email_lookup() -> None:
    """STORY-5 acceptance: ``get_by_email`` exists if and only if
    docs/repo-layer-recon.md V5 records an existing email lookup
    in src/. The recon's V5 section explicitly enumerates zero
    email-lookup call sites under src/ (every production caller
    keys on ``user.id``); therefore the repository must NOT define
    ``get_by_email`` -- it would be unused API."""
    repo = UserRepository(FakeInfrastructure())
    assert not hasattr(repo, "get_by_email"), (
        "UserRepository must not define get_by_email: docs/repo-layer-recon.md "
        "V5 records no existing email-lookup call sites under src/, so this "
        "method would be unused API"
    )

    # And the import surface confirms it: the public symbols of the
    # module are create/get_by_id/update/delete plus row-mapping
    # helpers, nothing else.
    import repositories.user_repository as ur_module
    public_crud = {
        name
        for name in dir(repo)
        if not name.startswith("_")
        and callable(getattr(repo, name, None))
    }
    assert "get_by_email" not in public_crud
