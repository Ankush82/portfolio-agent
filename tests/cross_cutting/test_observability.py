import json

from cross_cutting import observability
from cross_cutting.observability import DefaultAuditManager


def test_default_audit_manager_record_round_trips_json_line(tmp_path, monkeypatch):
    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    event_type = "quarantine_decision"
    detail = {"claim_id": "c-42", "reason": "unverified source"}

    DefaultAuditManager().record(event_type, detail)

    lines = audit_log_path.read_text().splitlines()
    assert len(lines) == 1

    logged = json.loads(lines[0])
    assert logged["event_type"] == event_type
    assert logged["detail"] == detail
    assert "timestamp" in logged
