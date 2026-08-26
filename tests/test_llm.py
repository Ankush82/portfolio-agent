"""Tests for src/llm.py — the shared LLM provider both Agent Runtime's
(c10, ADR-0021) and Analysis & Reasoning's (c08, ADR-0037) reason_fn
seams now resolve to, per ADR-0043.

Almost everything here runs with no network access: prompt construction
(`_build_messages`), response parsing (`_parse_json_object`,
`_normalize_response`), the `get_reason_fn` selection logic, and the
missing-key error path are all pure logic once the real HTTP call
(`requests.post`) is mocked out. Real network access is real money, so
exactly one test — `test_live_openrouter_call_assess_stakes_phase` —
actually calls OpenRouter, and it skips cleanly with a clear reason when
`OPENROUTER_API_KEY` isn't set, the same pattern
`tests/test_infrastructure_postgres.py` already uses for a live Postgres/
Redis.
"""

import os
from dataclasses import dataclass

import pytest
import requests

import llm
from llm import (
    MissingOpenRouterAPIKeyError,
    OPENROUTER_MODEL,
    _build_messages,
    _fallback_response,
    _normalize_response,
    _parse_json_object,
    get_reason_fn,
    openrouter_reason_fn,
)


@dataclass
class _FakeCheckpoint:
    id: str
    subgoal: dict


class _SpyAuditManager:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


class _FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200) -> None:
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"fake status {self.status_code}")

    def json(self) -> dict:
        return self._json_body


def _fake_chat_completion(content: str) -> _FakeResponse:
    return _FakeResponse({"choices": [{"message": {"content": content}}]})


def _no_env_key(monkeypatch) -> None:
    """Ensures neither a real process env var nor this repo's own real
    .env can leak OPENROUTER_API_KEY into a test that wants to exercise
    the "no key configured" path — points _ENV_FILE_PATH at a file that
    doesn't exist rather than assuming the ambient environment is
    clean."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_ENV_FILE_PATH", llm._ENV_FILE_PATH.parent / "does-not-exist.env")


# --- get_reason_fn: selection logic, no network -----------------------------


def test_get_reason_fn_returns_openrouter_reason_fn_when_key_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    audit = _SpyAuditManager()

    selected = get_reason_fn(placeholder=lambda request: {"placeholder": True}, audit_manager=audit)

    assert selected is openrouter_reason_fn
    assert audit.events == [("reason_fn_selected", {"backend": "openrouter", "model": OPENROUTER_MODEL})]


def test_get_reason_fn_returns_placeholder_when_key_unset(monkeypatch):
    _no_env_key(monkeypatch)
    audit = _SpyAuditManager()

    def my_placeholder(request):
        return {"placeholder": True}

    selected = get_reason_fn(placeholder=my_placeholder, audit_manager=audit)

    assert selected is my_placeholder
    assert audit.events == [("reason_fn_selected", {"backend": "placeholder"})]


def test_get_reason_fn_reads_key_from_dotenv_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('OPENROUTER_API_KEY="sk-or-from-dotenv"\n')
    monkeypatch.setattr(llm, "_ENV_FILE_PATH", env_file)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    try:
        selected = get_reason_fn(placeholder=lambda request: {}, audit_manager=_SpyAuditManager())
        assert selected is openrouter_reason_fn
        assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-dotenv"
    finally:
        # _load_dotenv_into_environ sets a real os.environ entry via
        # setdefault, outside monkeypatch's own tracking (monkeypatch
        # only auto-restores variables it set itself) — clean it up
        # explicitly so this test can't leak a fake key into any test
        # that runs after it in the same process.
        os.environ.pop("OPENROUTER_API_KEY", None)


def test_get_reason_fn_dotenv_never_overwrites_real_env_var(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-or-from-dotenv-should-be-ignored\n")
    monkeypatch.setattr(llm, "_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real-process-env")

    get_reason_fn(placeholder=lambda request: {}, audit_manager=_SpyAuditManager())

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-real-process-env"


# --- missing-key error path, no network --------------------------------------


def test_openrouter_reason_fn_raises_specific_error_when_key_missing(monkeypatch):
    _no_env_key(monkeypatch)

    with pytest.raises(MissingOpenRouterAPIKeyError):
        openrouter_reason_fn({"phase": "reason", "checkpoint": _FakeCheckpoint(id="cp-1", subgoal={}), "history": [], "retry_count": 0})


def test_openrouter_reason_fn_unknown_phase_raises_value_error_even_without_key(monkeypatch):
    _no_env_key(monkeypatch)

    with pytest.raises(ValueError):
        openrouter_reason_fn({"phase": "not_a_real_phase"})


# --- prompt construction: _build_messages, no network ------------------------


def test_build_messages_reason_phase_includes_checkpoint_and_history():
    checkpoint = _FakeCheckpoint(id="cp-42", subgoal={"goal": "assess AAPL exposure"})
    request = {"phase": "reason", "checkpoint": checkpoint, "history": [{"phase": "reason", "output": {}}], "retry_count": 1}

    messages = _build_messages("reason", request)

    assert messages[0]["role"] == "system"
    assert "next action" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert '"cp-42"' in messages[1]["content"]
    assert "assess AAPL exposure" in messages[1]["content"]


def test_build_messages_assess_stakes_phase_includes_last_result():
    checkpoint = _FakeCheckpoint(id="cp-7", subgoal={})
    request = {"phase": "assess_stakes", "checkpoint": checkpoint, "last_result": {"error": "timeout"}}

    messages = _build_messages("assess_stakes", request)

    assert "stakes" in messages[0]["content"].lower()
    assert '"timeout"' in messages[1]["content"]


def test_build_messages_unknown_phase_raises():
    with pytest.raises(ValueError):
        _build_messages("not_a_real_phase", {})


@pytest.mark.parametrize(
    "phase,request_payload",
    [
        ("infer", {"phase": "infer", "premises": [{"signal": "revenue_up"}]}),
        ("generate_hypotheses", {"phase": "generate_hypotheses", "analysis_input": {"event": {}}}),
        ("generate_explanations", {"phase": "generate_explanations", "hypothesis": {"claim": "x", "basis": {}}}),
        ("generate_counterarguments", {"phase": "generate_counterarguments", "hypothesis": {"claim": "x", "basis": {}}}),
        ("synthesize_findings", {"phase": "synthesize_findings", "hypotheses": []}),
        ("estimate_impact", {"phase": "estimate_impact", "hypothesis": {"claim": "x", "basis": {}}, "exposure": {}}),
    ],
)
def test_build_messages_covers_every_analysis_reasoning_phase(phase, request_payload):
    messages = _build_messages(phase, request_payload)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


# --- response parsing: _parse_json_object, no network ------------------------


def test_parse_json_object_plain_json():
    assert _parse_json_object('{"stakes_high": true}') == {"stakes_high": True}


def test_parse_json_object_strips_markdown_fence():
    assert _parse_json_object('```json\n{"stakes_high": false}\n```') == {"stakes_high": False}


def test_parse_json_object_extracts_from_surrounding_prose():
    text = 'Sure, here is the answer:\n{"claim": "x", "basis": {}}\nLet me know if you need more.'
    assert _parse_json_object(text) == {"claim": "x", "basis": {}}


def test_parse_json_object_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        _parse_json_object("no json anywhere in this response")


def test_parse_json_object_raises_when_top_level_is_not_an_object():
    with pytest.raises(ValueError):
        _parse_json_object("[1, 2, 3]")


# --- response normalization: _normalize_response, no network -----------------


def test_normalize_response_reason_defaults_missing_action_to_empty_dict():
    result = _normalize_response("reason", {"checkpoint_complete": True}, {})
    assert result == {"action": {}, "checkpoint_complete": True}


def test_normalize_response_assess_stakes_defaults_missing_key_to_false():
    assert _normalize_response("assess_stakes", {}, {}) == {"stakes_high": False}


def test_normalize_response_generate_hypotheses_drops_malformed_entries():
    parsed = {"hypotheses": [{"claim": "ok", "basis": {"confidence": "high"}}, "not a dict", {"claim": "no basis"}]}
    result = _normalize_response("generate_hypotheses", parsed, {})
    assert result == {
        "hypotheses": [
            {"claim": "ok", "basis": {"confidence": "high"}},
            {"claim": "no basis", "basis": {}},
        ]
    }


def test_normalize_response_estimate_impact_falls_back_to_unknown_for_bad_significance():
    result = _normalize_response("estimate_impact", {"significance": "extremely high", "rationale": "r"}, {})
    assert result == {"significance": "unknown", "rationale": "r"}


def test_normalize_response_synthesize_findings_returns_real_hypothesis_instances():
    from components.c08_analysis_reasoning import Hypothesis

    parsed = {"hypotheses": [{"claim": "kept", "basis": {"confidence": 0.8}}], "impact_estimate": {"significance": "high"}}
    result = _normalize_response("synthesize_findings", parsed, {})

    assert result["hypotheses"] == [Hypothesis(claim="kept", basis={"confidence": 0.8})]
    assert isinstance(result["hypotheses"][0], Hypothesis)
    assert result["impact_estimate"] == {"significance": "high"}


def test_normalize_response_unknown_phase_raises():
    with pytest.raises(ValueError):
        _normalize_response("not_a_real_phase", {}, {})


# --- fallback shapes -----------------------------------------------------------


def test_fallback_response_reason_completes_checkpoint_to_avoid_infinite_retry_loop():
    assert _fallback_response("reason") == {"action": {"tool": "noop"}, "checkpoint_complete": True}


def test_fallback_response_assess_stakes_fails_toward_caution():
    assert _fallback_response("assess_stakes") == {"stakes_high": True}


def test_fallback_response_unknown_phase_raises():
    with pytest.raises(ValueError):
        _fallback_response("not_a_real_phase")


# --- openrouter_reason_fn end to end, HTTP mocked -----------------------------


def test_openrouter_reason_fn_happy_path_parses_real_shaped_response(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(
        llm.requests,
        "post",
        lambda *args, **kwargs: _fake_chat_completion('{"stakes_high": true}'),
    )

    result = openrouter_reason_fn(
        {"phase": "assess_stakes", "checkpoint": _FakeCheckpoint(id="cp-1", subgoal={}), "last_result": None}
    )

    assert result == {"stakes_high": True}


def test_openrouter_reason_fn_sends_bearer_auth_header_and_chosen_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _fake_chat_completion('{"stakes_high": false}')

    monkeypatch.setattr(llm.requests, "post", fake_post)

    openrouter_reason_fn({"phase": "assess_stakes", "checkpoint": _FakeCheckpoint(id="cp-1", subgoal={}), "last_result": None})

    assert captured["url"] == llm.OPENROUTER_CHAT_COMPLETIONS_URL
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test-key"
    assert captured["json"]["model"] == OPENROUTER_MODEL


def test_openrouter_reason_fn_degrades_to_fallback_on_http_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: _FakeResponse({}, status_code=429))
    audit_events = []
    monkeypatch.setattr(llm, "DefaultAuditManager", lambda: type("_A", (), {"record": staticmethod(lambda t, d: audit_events.append((t, d)))})())

    result = openrouter_reason_fn(
        {"phase": "assess_stakes", "checkpoint": _FakeCheckpoint(id="cp-1", subgoal={}), "last_result": None}
    )

    assert result == {"stakes_high": True}  # the documented fail-toward-caution fallback
    assert audit_events and audit_events[0][0] == "openrouter_call_degraded"
    assert audit_events[0][1]["phase"] == "assess_stakes"


def test_openrouter_reason_fn_degrades_to_fallback_on_malformed_response_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: _fake_chat_completion("not json at all, sorry"))

    result = openrouter_reason_fn({"phase": "infer", "premises": [{"x": 1}]})

    assert result == _fallback_response("infer")


# --- one real, live call ------------------------------------------------------


def _openrouter_key_available() -> bool:
    llm._load_dotenv_into_environ()
    return bool(os.environ.get("OPENROUTER_API_KEY"))


requires_openrouter_key = pytest.mark.skipif(
    not _openrouter_key_available(),
    reason="no OPENROUTER_API_KEY set in the environment or .env — set a real key for live coverage",
)


@requires_openrouter_key
def test_live_openrouter_call_assess_stakes_phase():
    """The one deliberately real, live network test in this suite — a
    single minimal, cheap assess_stakes call against the real
    OpenRouter API, so this integration is proven to actually work end
    to end at least once, not just against mocks. Skips cleanly (see
    `requires_openrouter_key` above) when no real key is configured,
    the same convention `tests/test_infrastructure_postgres.py` already
    uses for a live Postgres/Redis."""
    checkpoint = _FakeCheckpoint(id="cp-live-1", subgoal={"goal": "decide whether a 2% AAPL price drop needs review"})
    request = {
        "phase": "assess_stakes",
        "checkpoint": checkpoint,
        "last_result": {"action": {"tool": "check_price"}, "output": {"price_change_pct": -2.0}},
    }

    result = openrouter_reason_fn(request)

    assert isinstance(result, dict)
    assert set(result.keys()) == {"stakes_high"}
    assert isinstance(result["stakes_high"], bool)
