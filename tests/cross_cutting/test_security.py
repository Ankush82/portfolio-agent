import json

from cross_cutting import observability
from cross_cutting.observability import DefaultAuditManager
from cross_cutting.security import DefaultBoundaryGate, Provenance, StubBoundaryGate


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


def test_authorize_currently_allows_everything_provisional_fail_open():
    """authorize() is the genuine open gap (see ADR-0020): granularity
    is unresolved, so DefaultBoundaryGate fails open for every call.
    This test documents that provisional behavior — it is not an
    endorsement of fail-open as a permanent answer."""
    gate = DefaultBoundaryGate()

    assert gate.authorize("user-1", "read", "document-1") is True
    assert gate.authorize("", "delete", "everything") is True
    assert gate.authorize("user-1", "any-action", "any-resource") is True


def test_authorize_produces_an_audit_record(tmp_path, monkeypatch):
    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    gate = DefaultBoundaryGate()
    gate.authorize("user-1", "read", "document-1")

    lines = audit_log_path.read_text().splitlines()
    assert len(lines) == 1

    logged = json.loads(lines[0])
    assert logged["event_type"] == "authorization_decision"
    assert logged["detail"] == {
        "identity": "user-1",
        "action": "read",
        "resource": "document-1",
        "decision": True,
        "enforced": False,
    }
    assert "timestamp" in logged


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
