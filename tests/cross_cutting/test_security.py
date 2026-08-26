import json

from cross_cutting import observability
from cross_cutting.observability import DefaultAuditManager
from cross_cutting.security import DefaultBoundaryGate, Provenance, StubBoundaryGate


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double — same store/query semantics
    as the equivalent fake in tests/components/test_decision_policy.py.
    Keeps authorize()'s real enforcement (ADR-0042) testable without a
    live Postgres, matching the rest of this project's test doubles."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self._next_id = 0

    def store(self, table: str, record: dict) -> str:
        self._next_id += 1
        record_id = str(record["id"]) if "id" in record else f"generated-{self._next_id}"
        self._tables.setdefault(table, {})[record_id] = dict(record, id=record_id)
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        return self._tables.get(table, {}).get(id_)

    def query(self, table: str, filters: dict) -> list[dict]:
        return [
            record
            for record in self._tables.get(table, {}).values()
            if all(record.get(key) == value for key, value in filters.items())
        ]


def test_tag_provenance_always_tags_untrusted():
    gate = DefaultBoundaryGate()

    result = gate.tag_provenance({"body": "quarterly filing text"}, source="document")
    assert result["provenance"] == Provenance.UNTRUSTED.name

    # Arbitrary content and source — no "source" value produces TRUSTED,
    # per ADR-0003 / ADR-0018 (documents and peer-agent output are both
    # untrusted by default; nothing specifies a trusted case).
    result = gate.tag_provenance({"body": "sub-agent report"}, source="peer_agent")
    assert result["provenance"] == Provenance.UNTRUSTED.name

    result = gate.tag_provenance({}, source="anything")
    assert result["provenance"] == Provenance.UNTRUSTED.name


def test_tag_provenance_does_not_mutate_input_and_preserves_content():
    gate = DefaultBoundaryGate()
    original = {"body": "text", "id": 42}

    result = gate.tag_provenance(original, source="document")

    assert original == {"body": "text", "id": 42}  # untouched
    assert result == {"body": "text", "id": 42, "provenance": Provenance.UNTRUSTED.name}


def test_authenticate_rejects_empty_and_whitespace_identity():
    gate = DefaultBoundaryGate()

    assert gate.authenticate("") is False
    assert gate.authenticate("   ") is False


def test_authenticate_accepts_real_looking_identity():
    gate = DefaultBoundaryGate()

    assert gate.authenticate("user-1234") is True


def test_authorize_denies_by_default_when_no_grant_exists():
    """Real enforcement (ADR-0042, superseding ADR-0020's fail-open
    interim): with no grant stored for an identity, authorize() denies
    — there is no fail-open path left."""
    gate = DefaultBoundaryGate(infrastructure=_InMemoryInfrastructure())

    assert gate.authorize("user-1", "read", "document-1") is False
    assert gate.authorize("", "delete", "everything") is False


def test_authorize_allows_when_a_matching_grant_exists():
    """Per-call granularity (ADR-0042): a grant is checked against the
    exact (identity, action, resource) triple on every call — granting
    one resource does not implicitly allow a different one."""
    infra = _InMemoryInfrastructure()
    gate = DefaultBoundaryGate(infrastructure=infra)
    gate.grant("user-1", "read", "document-1")

    assert gate.authorize("user-1", "read", "document-1") is True
    assert gate.authorize("user-1", "read", "document-2") is False
    assert gate.authorize("user-1", "delete", "document-1") is False
    assert gate.authorize("user-2", "read", "document-1") is False


def test_authorize_wildcard_grant_matches_any_action_or_resource():
    """grant()'s "*" wildcard (ADR-0042) covers every action, every
    resource, or both, for the one identity it was granted to — but
    never crosses identities."""
    infra = _InMemoryInfrastructure()
    gate = DefaultBoundaryGate(infrastructure=infra)
    gate.grant("user-1", "*", "portfolio:pf-1")

    assert gate.authorize("user-1", "read", "portfolio:pf-1") is True
    assert gate.authorize("user-1", "delete", "portfolio:pf-1") is True
    assert gate.authorize("user-1", "read", "portfolio:pf-2") is False
    assert gate.authorize("user-2", "read", "portfolio:pf-1") is False


def test_authorize_produces_an_audit_record_for_both_allow_and_deny(tmp_path, monkeypatch):
    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    infra = _InMemoryInfrastructure()
    gate = DefaultBoundaryGate(infrastructure=infra)
    gate.grant("user-1", "read", "document-1")

    gate.authorize("user-1", "read", "document-1")  # allowed
    gate.authorize("user-1", "delete", "document-1")  # denied

    lines = audit_log_path.read_text().splitlines()
    assert len(lines) == 2

    allowed = json.loads(lines[0])
    assert allowed["event_type"] == "authorization_decision"
    assert allowed["detail"] == {
        "identity": "user-1",
        "action": "read",
        "resource": "document-1",
        "decision": True,
        "enforced": True,
    }
    assert "timestamp" in allowed

    denied = json.loads(lines[1])
    assert denied["detail"] == {
        "identity": "user-1",
        "action": "delete",
        "resource": "document-1",
        "decision": False,
        "enforced": True,
    }


def test_default_audit_manager_used_directly_matches_authorize_record_shape(tmp_path, monkeypatch):
    """Sanity check that DefaultAuditManager, constructed the same way
    authorize() uses it internally, writes the same JSON-line format
    the observability tests already rely on."""
    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    DefaultAuditManager().record("authorization_decision", {"identity": "x"})

    lines = audit_log_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "authorization_decision"


def test_stub_boundary_gate_untouched():
    """StubBoundaryGate stays a lightweight test double: authorize()
    and authenticate() always True, tag_provenance() always returns an
    empty dict. Guards against accidental edits to the stub while
    adding DefaultBoundaryGate alongside it."""
    stub = StubBoundaryGate()

    assert stub.authenticate("anyone") is True
    assert stub.authorize("anyone", "any-action", "any-resource") is True
    assert stub.tag_provenance({"body": "text"}, source="document") == {}
