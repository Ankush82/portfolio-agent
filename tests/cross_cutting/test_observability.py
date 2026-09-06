import copy
import json

import pytest
from src.cross_cutting import observability
from src.cross_cutting.observability import DefaultAuditManager, redact_secrets


# ---------------------------------------------------------------------------
# QA Story-1: redact_secrets acceptance criteria
# ---------------------------------------------------------------------------

def test_story1_acceptance_criteria():
    """
    STORY-1 acceptance criteria — exactly 8 assertions, one per criterion.

    AC-1: redact_secrets({'password': 'abc'}) returns {'password': '[REDACTED]'}
    AC-2: redact_secrets({'api_key': 'key123'}) returns {'api_key': '[REDACTED]'}
    AC-3: redact_secrets({'user': 'alice', 'apiKey': 'secret'}) returns
          {'user': 'alice', 'apiKey': '[REDACTED]'}
    AC-4: redact_secrets({'nested': {'token': 'tok'}}) returns
          {'nested': {'token': '[REDACTED]'}}
    AC-5: redact_secrets({'list': [{'password': 'x'}, {'id': 'y'}]}) returns
          {'list': [{'password': '[REDACTED]'}, {'id': 'y'}]}
    AC-6: redact_secrets({'safe': 'ok', 'unsafe_bearer': 'tok'}) returns
          {'safe': 'ok', 'unsafe_bearer': '[REDACTED]'}
    AC-7: redact_secrets({'no_match': 'pass'}) returns {'no_match': 'pass'}
          (literal 'pass' VALUE is not redacted)
    AC-8: original input dict is not mutated
    """
    # AC-1
    assert redact_secrets({"password": "abc"}) == {"password": "[REDACTED]"}

    # AC-2
    assert redact_secrets({"api_key": "key123"}) == {"api_key": "[REDACTED]"}

    # AC-3
    assert redact_secrets({"user": "alice", "apiKey": "secret"}) == {
        "user": "alice",
        "apiKey": "[REDACTED]",
    }

    # AC-4
    assert redact_secrets({"nested": {"token": "tok"}}) == {
        "nested": {"token": "[REDACTED]"}
    }

    # AC-5
    assert redact_secrets({"list": [{"password": "x"}, {"id": "y"}]}) == {
        "list": [{"password": "[REDACTED]"}, {"id": "y"}]
    }

    # AC-6
    assert redact_secrets({"safe": "ok", "unsafe_bearer": "tok"}) == {
        "safe": "ok",
        "unsafe_bearer": "[REDACTED]",
    }

    # AC-7: literal 'pass' value (not a secret key) must survive
    assert redact_secrets({"no_match": "pass"}) == {"no_match": "pass"}

    # AC-8: no mutation
    original = {"password": "abc", "nested": {"token": "tok"}}
    original_copy = copy.deepcopy(original)
    redact_secrets(original)
    assert original == original_copy, "Input dict was mutated by redact_secrets()"


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


# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------

class TestRedactSecrets:
    def test_password(self):
        assert redact_secrets({"password": "abc"}) == {"password": "[REDACTED]"}

    def test_api_key_underscore(self):
        assert redact_secrets({"api_key": "key123"}) == {"api_key": "[REDACTED]"}

    def test_api_key_camel_case(self):
        assert redact_secrets({"user": "alice", "apiKey": "secret"}) == {
            "user": "alice",
            "apiKey": "[REDACTED]",
        }

    def test_nested_token(self):
        assert redact_secrets({"nested": {"token": "tok"}}) == {
            "nested": {"token": "[REDACTED]"}
        }

    def test_list_of_dicts(self):
        assert redact_secrets({"list": [{"password": "x"}, {"id": "y"}]}) == {
            "list": [{"password": "[REDACTED]"}, {"id": "y"}]
        }

    def test_bearer_in_key(self):
        assert redact_secrets({"safe": "ok", "unsafe_bearer": "tok"}) == {
            "safe": "ok",
            "unsafe_bearer": "[REDACTED]",
        }

    def test_literal_pass_value_not_redacted(self):
        # 'pass' as a VALUE is not a secret key, so it must survive
        assert redact_secrets({"no_match": "pass"}) == {"no_match": "pass"}

    def test_does_not_mutate_input(self):
        original = {"password": "secret123", "nested": {"token": "tok"}}
        original_copy = copy.deepcopy(original)
        redact_secrets(original)
        assert original == original_copy

    def test_empty_dict(self):
        assert redact_secrets({}) == {}

    def test_empty_list(self):
        assert redact_secrets([]) == []

    def test_non_dict_list_passthrough(self):
        assert redact_secrets("plain string") == "plain string"
        assert redact_secrets(42) == 42
        assert redact_secrets(None) is None
        assert redact_secrets(True) is True

    def test_deeply_nested(self):
        data = {
            "level1": {
                "level2": [
                    {"level3": {"credential": "cred123"}},
                    {"safe_key": "safe_val"},
                ]
            }
        }
        assert redact_secrets(data) == {
            "level1": {
                "level2": [
                    {"level3": {"credential": "[REDACTED]"}},
                    {"safe_key": "safe_val"},
                ]
            }
        }

    def test_case_insensitive(self):
        assert redact_secrets({"PASSWORD": "x"}) == {"PASSWORD": "[REDACTED]"}
        assert redact_secrets({"SecretToken": "x"}) == {"SecretToken": "[REDACTED]"}
        assert redact_secrets({"AUTH_BEARER": "x"}) == {"AUTH_BEARER": "[REDACTED]"}

    def test_all_secret_key_substrings(self):
        for key in (
            "password", "secret", "token", "credential",
            "api_key", "apikey", "auth", "bearer",
            "private_key", "access_key", "secret_key",
        ):
            assert redact_secrets({key: "val"}) == {key: "[REDACTED]"}
            # adjacent chars don't bypass the check
            assert redact_secrets({f"x{key}y": "val"}) == {f"x{key}y": "[REDACTED]"}
