"""Conformance + behavioural tests for FakeInfrastructure (STORY-4).

Three layers of coverage:

1. **Protocol conformance.** If ``Infrastructure`` is
   ``runtime_checkable`` (i.e. ``isinstance(x, Infrastructure)`` is
   supported), assert ``isinstance(FakeInfrastructure(), Infrastructure)``.
   Otherwise fall back to per-method ``inspect.signature`` comparison —
   every public method on the Protocol must exist on the fake with a
   compatible parameter list. This guards against accidental drift
   between the Protocol and the fake.

2. **Deep-copy semantics.** Verify that ``store`` deep-copies on write
   *and* ``retrieve`` deep-copies on read — a test mutates the
   incoming dict on store and the returned dict on retrieve, then
   re-retrieves to confirm stored state is unchanged both times.

3. **delete contract.** ``delete`` returns ``True`` for an existing
   row, ``False`` for a missing row, and ``retrieve`` after ``delete``
   returns ``None``.

4. **The other six Protocol methods (publish, subscribe, schedule,
   cache_get, cache_set, get_secret) are present** as no-op stubs.

5. **Explicit non-goal.** The fake is *not* a Postgres emulator: it
   does not enforce FKs, unique constraints beyond store-upsert, or
   type coercion. We don't have an assertion that the fake refuses
   these (it must NOT refuse them — that's the whole point) but the
   module docstring carries the warning.
"""

from __future__ import annotations

import inspect

import pytest

from tests.support.fake_infrastructure import FakeInfrastructure
from infrastructure import Infrastructure


# --- 1. Protocol conformance --------------------------------------------

def _protocol_method_names() -> list[str]:
    """Names of declared methods on the Infrastructure Protocol class.

    Reading from the source is more reliable than ``dir()`` because
    Protocol members don't always show up there.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "infrastructure.py").read_text(
        encoding="utf-8"
    )
    import re
    return re.findall(r"^    def ([a-zA-Z_]\w*)\(", src, flags=re.MULTILINE)


def test_fake_satisfies_infrastructure_protocol():
    """FakeInfrastructure structurally satisfies the Infrastructure
    Protocol. If the Protocol is runtime_checkable we use isinstance;
    otherwise we fall back to per-method signature comparison."""
    fake = FakeInfrastructure()

    if hasattr(Infrastructure, "_is_runtime_protocol") or isinstance(
        Infrastructure, type
    ) and getattr(Infrastructure, "_is_runtime", False):
        # Defensive: only some Protocol subclasses are runtime_checkable.
        # The real check is to try isinstance() and fall back on TypeError.
        pass

    try:
        assert isinstance(fake, Infrastructure), (
            "FakeInfrastructure() did not satisfy isinstance(..., Infrastructure)"
        )
        return  # isinstance check passed; signature check is redundant.
    except TypeError:
        # Infrastructure is NOT runtime_checkable — fall through to
        # per-method signature comparison.
        pass

    # Per-method signature fallback: every declared Protocol method
    # must exist on FakeInfrastructure with a compatible signature.
    proto_methods = _protocol_method_names()
    assert proto_methods, (
        "Could not enumerate Protocol method names from src/infrastructure.py"
    )

    for name in proto_methods:
        assert hasattr(fake, name), (
            f"FakeInfrastructure is missing Protocol method {name!r}"
        )
        # Compare on the *unbound* form on both sides so the leading
        # `self` is present consistently. ``Infrastructure.<name>``
        # is a Protocol member function; ``FakeInfrastructure.<name>``
        # is the plain function on the class (also has `self`).
        proto_member = getattr(Infrastructure, name, None)
        fake_member = getattr(FakeInfrastructure, name)
        proto_sig = inspect.signature(proto_member)
        fake_sig = inspect.signature(fake_member)
        # Compatible means: same parameter names in the same order,
        # modulo the leading `self` which Protocol members sometimes
        # surface without (Protocol quirk). We strip `self` from both
        # sides for comparison.
        def _strip_self(params):
            return [p for p in params if p != "self"]
        proto_params = _strip_self(list(proto_sig.parameters.keys()))
        fake_params = _strip_self(list(fake_sig.parameters.keys()))
        assert proto_params == fake_params, (
            f"FakeInfrastructure.{name} parameter list {fake_params} "
            f"does not match Infrastructure.{name} parameter list "
            f"{proto_params}"
        )


def test_fake_has_all_protocol_methods_present():
    """Independent of conformance mechanism, every public Protocol
    method must be a callable attribute on FakeInfrastructure. This
    also covers the six non-data methods the PRD names explicitly."""
    fake = FakeInfrastructure()
    expected = {
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
    }
    actual = {name for name in expected if hasattr(fake, name)}
    missing = expected - actual
    assert not missing, f"FakeInfrastructure is missing methods: {sorted(missing)}"


# --- 2. Deep-copy semantics --------------------------------------------

def test_store_deep_copies_on_write():
    """Mutating the dict passed to store() must not change stored state."""
    fake = FakeInfrastructure()
    original = {"id": "r1", "nested": {"k": "v"}, "items": [1, 2, 3]}
    fake.store("t", original)

    # Mutate the original dict (top-level + nested).
    original["new_top"] = "leaked"
    original["nested"]["k"] = "leaked"
    original["items"].append(999)

    stored = fake.retrieve("t", "r1")
    assert stored is not None
    assert "new_top" not in stored, (
        "top-level mutation of the dict passed to store() leaked into stored state"
    )
    assert stored["nested"] == {"k": "v"}, (
        "nested mutation of the dict passed to store() leaked into stored state"
    )
    assert stored["items"] == [1, 2, 3], (
        "list mutation of the dict passed to store() leaked into stored state"
    )


def test_retrieve_deep_copies_on_read():
    """Mutating the dict returned by retrieve() must not change stored
    state, even when mutating nested values."""
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "nested": {"k": "v"}, "items": [1, 2, 3]})

    first = fake.retrieve("t", "r1")
    assert first is not None
    first["new_top"] = "leaked"
    first["nested"]["k"] = "leaked"
    first["items"].append(999)

    second = fake.retrieve("t", "r1")
    assert second is not None
    assert "new_top" not in second, (
        "top-level mutation of retrieve() result leaked into stored state"
    )
    assert second["nested"] == {"k": "v"}, (
        "nested mutation of retrieve() result leaked into stored state"
    )
    assert second["items"] == [1, 2, 3], (
        "list mutation of retrieve() result leaked into stored state"
    )


def test_query_returns_deep_copies():
    """Mutating a dict returned by query() must not change stored state."""
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "nested": {"k": "v"}})

    results = fake.query("t", {"id": "r1"})
    assert len(results) == 1
    results[0]["nested"]["k"] = "leaked"

    again = fake.query("t", {"id": "r1"})
    assert again[0]["nested"] == {"k": "v"}, (
        "mutation of a query() result leaked into stored state"
    )


# --- 3. delete contract ------------------------------------------------

def test_delete_returns_true_for_existing_row():
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "x": 1})
    assert fake.delete("t", "r1") is True


def test_delete_returns_false_for_missing_row():
    fake = FakeInfrastructure()
    assert fake.delete("t", "no-such-id") is False
    # Also: a row that DID exist, then got deleted, is now missing.
    fake.store("t", {"id": "r1", "x": 1})
    assert fake.delete("t", "r1") is True
    assert fake.delete("t", "r1") is False, (
        "second delete of the same id must return False, not raise"
    )


def test_delete_then_retrieve_returns_none():
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "x": 1})
    assert fake.retrieve("t", "r1") == {"id": "r1", "x": 1}
    assert fake.delete("t", "r1") is True
    assert fake.retrieve("t", "r1") is None


def test_delete_unknown_table_returns_false_and_does_not_raise():
    fake = FakeInfrastructure()
    assert fake.delete("never-existed-table", "any-id") is False


# --- 4. Other Protocol methods are present as stubs ---------------------

@pytest.mark.parametrize(
    "name,return_value",
    [
        ("publish", None),
        ("subscribe", None),
        ("schedule", ""),
        ("cache_get", None),
        ("cache_set", None),
        ("get_secret", ""),
    ],
)
def test_stub_methods_present_and_return_documented_shape(name, return_value):
    fake = FakeInfrastructure()
    method = getattr(fake, name)
    assert callable(method), f"{name} is not callable on FakeInfrastructure"
    # Each stub returns its documented no-op / empty value. We invoke
    # with benign args; signature mismatch would raise here.
    if name == "schedule":
        result = method(0.0, {})
    elif name == "cache_set":
        result = method("k", "v", 0)
    elif name == "get_secret":
        result = method("name")
    elif name == "publish":
        result = method("topic", {})
    elif name == "subscribe":
        result = method("topic", lambda *a, **k: None)
    elif name == "cache_get":
        result = method("k")
    else:  # pragma: no cover - guarded by parametrize
        result = None
    assert result == return_value


# --- 5. Module docstring carries the non-goal warning -------------------

def test_module_docstring_documents_non_emulation_of_postgres_semantics():
    """The PRD requires the fake's module docstring to explicitly
    state that it does NOT emulate Postgres type coercion, FK
    enforcement, or unique-constraint enforcement. Guard against
    accidental edits that drop this warning."""
    import tests.support.fake_infrastructure as fake_infrastructure

    doc = fake_infrastructure.__doc__ or ""
    assert doc, "FakeInfrastructure module has no docstring"
    lowered = doc.lower()
    # Type coercion must be named explicitly.
    assert "type coercion" in lowered, (
        f"FakeInfrastructure module docstring must mention non-emulation "
        f"of type coercion; got: {doc!r}"
    )
    # FK enforcement: any of "foreign-key", "foreign key", or "fk".
    assert any(
        needle in lowered for needle in ("foreign-key", "foreign key", "fk")
    ), (
        f"FakeInfrastructure module docstring must mention non-emulation "
        f"of foreign-key enforcement; got: {doc!r}"
    )
    # Unique constraints: any of "unique constraint", "unique-constraint",
    # or just "unique" near "constraint".
    assert any(
        needle in lowered
        for needle in ("unique constraint", "unique-constraint", "unique")
    ), (
        f"FakeInfrastructure module docstring must mention non-emulation "
        f"of unique constraints; got: {doc!r}"
    )


# --- 6. Sync/async form matches the Protocol ---------------------------

def test_data_methods_are_sync_matching_protocol():
    """The four data methods on the Protocol are synchronous (see
    docs/repo-layer-recon.md V1) and the fake must match."""
    fake = FakeInfrastructure()
    for name in ("store", "retrieve", "query", "delete"):
        method = getattr(fake, name)
        # If it were `async def`, inspect.iscoroutinefunction would be True.
        assert not inspect.iscoroutinefunction(method), (
            f"FakeInfrastructure.{name} is `async def` but the Protocol "
            f"declares it as a sync `def`"
        )


# --- 7. STORY-4 acceptance-criteria verification -------------------------
# Each test below maps to a specific bullet from the STORY-4 PRD so a
# regression in any one of them is precisely attributable.

def test_qa4_acceptance_no_production_code_imports_the_fake():
    """AC: 'No file under src/ imports the fake.' A fresh grep is the
    only authoritative way to prove this. We walk every .py file under
    src/ and look for any `import` / `from` that mentions the fake."""
    import pathlib
    import re

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    assert src_root.is_dir(), f"expected src/ at {src_root}"

    bad_patterns = re.compile(
        r"(\bimport\b|\bfrom\b)\s+[^#\n]*\b(fake_infrastructure|FakeInfrastructure)\b"
    )
    offenders = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Strip trailing comments so a `# see FakeInfrastructure` style
            # comment doesn't trip the check; we only care about real imports.
            code = line.split("#", 1)[0]
            if bad_patterns.search(code):
                offenders.append(f"{py.relative_to(src_root.parent)}:{lineno}: {line}")
    assert not offenders, (
        "STORY-4 AC violated — production code imports the fake:\n  "
        + "\n  ".join(offenders)
    )


def test_qa4_acceptance_fake_lives_only_under_tests():
    """AC: 'The fake must live under tests/, never under src/'."""
    import pathlib
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    assert src_root.is_dir()
    bad = list(src_root.rglob("fake_infrastructure*.py"))
    assert not bad, (
        f"STORY-4 AC violated — fake lives under src/: {[str(p) for p in bad]}"
    )
    tests_support_fake = (
        pathlib.Path(__file__).resolve().parent / "fake_infrastructure.py"
    )
    assert tests_support_fake.is_file(), (
        "STORY-4 AC violated — tests/support/fake_infrastructure.py missing"
    )


def test_qa4_acceptance_state_shape_is_table_to_id_to_row():
    """AC: 'Internal state shaped as dict[table_name] -> dict[row_id] -> row dict.'
    Verified by direct inspection — store two distinct rows under the
    same table with distinct ids and confirm nested-dict state shape."""
    fake = FakeInfrastructure()
    fake.store("alpha", {"id": "r1", "x": 1})
    fake.store("alpha", {"id": "r2", "x": 2})
    fake.store("beta", {"id": "r1", "x": 100})

    # Three-level dict shape is a hard requirement.
    assert isinstance(fake._tables, dict)
    assert isinstance(fake._tables["alpha"], dict)
    assert isinstance(fake._tables["alpha"]["r1"], dict)
    assert isinstance(fake._tables["beta"]["r1"], dict)
    assert fake._tables["alpha"]["r1"]["x"] == 1
    assert fake._tables["alpha"]["r2"]["x"] == 2
    assert fake._tables["beta"]["r1"]["x"] == 100


def test_qa4_acceptance_store_upsert_by_id():
    """AC: 'store: ... behaves as upsert-by-id.' Storing twice with
    the same id must overwrite (not duplicate, not raise)."""
    fake = FakeInfrastructure()
    fake.store("t", {"id": "k1", "v": "before"})
    fake.store("t", {"id": "k1", "v": "after"})
    rows = list(fake._tables["t"].values())
    assert len(rows) == 1, f"upsert produced duplicates: {rows}"
    assert rows[0]["v"] == "after"


def test_qa4_acceptance_retrieve_returns_none_for_missing():
    """AC: 'retrieve: ... returns ... None when absent' (covers both
    unknown table and unknown id)."""
    fake = FakeInfrastructure()
    assert fake.retrieve("never-seen", "any") is None
    fake.store("t", {"id": "r1", "v": 1})
    assert fake.retrieve("t", "missing-id") is None


def test_qa4_acceptance_query_filters_match_real_protocol_shape():
    """AC: 'query: filters stored rows by the same predicate shape the
    real query accepts, per the signature recorded in recon' — i.e.
    JSONB @> containment match. We assert: equality filter on each
    key/value, no match when any key differs, multi-key AND."""
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "user_id": "u1", "kind": "BUY", "amount": 10})
    fake.store("t", {"id": "r2", "user_id": "u1", "kind": "SELL", "amount": 5})
    fake.store("t", {"id": "r3", "user_id": "u2", "kind": "BUY", "amount": 7})

    # Single-key filter.
    only_u1 = fake.query("t", {"user_id": "u1"})
    assert {r["id"] for r in only_u1} == {"r1", "r2"}, (
        f"single-key filter wrong: {only_u1}"
    )

    # Multi-key AND filter.
    u1_buy = fake.query("t", {"user_id": "u1", "kind": "BUY"})
    assert {r["id"] for r in u1_buy} == {"r1"}, (
        f"multi-key AND filter wrong: {u1_buy}"
    )

    # Filter that matches nothing.
    assert fake.query("t", {"user_id": "ghost"}) == []


def test_qa4_acceptance_delete_returns_true_for_existing():
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "v": 1})
    assert fake.delete("t", "r1") is True
    # And the row is actually gone.
    assert fake.retrieve("t", "r1") is None


def test_qa4_acceptance_delete_returns_false_for_missing():
    fake = FakeInfrastructure()
    fake.store("t", {"id": "r1", "v": 1})
    # Delete a never-existed id under an existing table.
    assert fake.delete("t", "no-such-id") is False
    # And the real row is still there.
    assert fake.retrieve("t", "r1") == {"id": "r1", "v": 1}
    # Also: delete under a table that was never stored to.
    assert fake.delete("never-seen-table", "x") is False


def test_qa4_acceptance_six_non_data_methods_all_present():
    """AC: 'publish, subscribe, schedule, cache_get, cache_set, and
    get_secret are all present on the fake as at least no-op stubs'."""
    fake = FakeInfrastructure()
    for name in (
        "publish", "subscribe", "schedule",
        "cache_get", "cache_set", "get_secret",
    ):
        assert hasattr(fake, name), f"missing: {name}"
        assert callable(getattr(fake, name)), f"not callable: {name}"


def test_qa4_acceptance_sync_async_form_matches_protocol():
    """AC: 'Match sync/async-ness of the real Protocol exactly.' The
    Protocol declares all 10 methods as plain `def`; verify each
    fake method matches."""
    fake = FakeInfrastructure()
    all_methods = (
        "store", "retrieve", "query", "delete",
        "publish", "subscribe", "schedule",
        "cache_get", "cache_set", "get_secret",
    )
    for name in all_methods:
        method = getattr(fake, name)
        assert not inspect.iscoroutinefunction(method), (
            f"FakeInfrastructure.{name} should be sync `def`, not `async def`"
        )


def test_qa4_acceptance_conformance_against_real_protocol():
    """AC: 'A conformance test asserts the fake satisfies the
    Infrastructure Protocol (isinstance check if runtime_checkable,
    else per-method signature comparison) and it passes.' This test
    is the real, authoritative conformance check — it does not rely
    on regexes over Protocol source and does not assume
    runtime_checkable."""
    fake = FakeInfrastructure()

    # First: try isinstance — if it raises TypeError, the Protocol is
    # NOT runtime_checkable, and we must fall back to signature
    # comparison (per the PRD).
    isinstance_works = True
    try:
        isinstance_ok = isinstance(fake, Infrastructure)
    except TypeError:
        isinstance_works = False
        isinstance_ok = False

    if isinstance_works:
        assert isinstance_ok, (
            "isinstance(FakeInfrastructure(), Infrastructure) returned False"
        )
        return  # Done — isinstance check is the strongest possible.

    # Fallback: per-method signature comparison. Compare against the
    # Protocol source's declared signatures so we don't depend on a
    # regex helper existing.
    proto_methods = {
        "store": ("table", "record"),
        "retrieve": ("table", "id_"),
        "query": ("table", "filters"),
        "delete": ("table", "id"),
        "publish": ("topic", "event"),
        "subscribe": ("topic", "handler"),
        "schedule": ("delay_seconds", "task"),
        "cache_get": ("key",),
        "cache_set": ("key", "value", "ttl_seconds"),
        "get_secret": ("name",),
    }
    for name, expected_params in proto_methods.items():
        assert hasattr(fake, name), f"missing method on fake: {name}"
        sig = inspect.signature(getattr(FakeInfrastructure, name))
        params = [
            p for p in sig.parameters.keys() if p != "self"
        ]
        assert params == list(expected_params), (
            f"FakeInfrastructure.{name} params {params} "
            f"!= Protocol params {list(expected_params)}"
        )
