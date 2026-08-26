import pytest

from components.c11_tools_environment import DefaultToolsEnvironment, Tool, ToolCall, ToolResult
from cross_cutting.reliability import DefaultCircuitBreaker


def _tool(name: str, tags: list[str] | None = None) -> Tool:
    return Tool(name=name, schema={"tags": tags or []})


# --- register_tool / discover_tool --------------------------------------


def test_register_tool_makes_it_discoverable_by_exact_name():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("web_search", tags=["search"]))

    found = env.discover_tool("web_search")

    assert [tool.name for tool in found] == ["web_search"]


def test_discover_tool_matches_by_capability_tag():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("web_search", tags=["search", "public_data"]))
    env.register_tool(_tool("cached_index_search", tags=["search"]))
    env.register_tool(_tool("code_exec", tags=["execution"]))

    found = env.discover_tool("search")

    assert {tool.name for tool in found} == {"web_search", "cached_index_search"}


def test_discover_tool_returns_empty_list_when_registry_is_empty():
    env = DefaultToolsEnvironment()

    assert env.discover_tool("anything") == []


def test_registering_same_name_again_replaces_metadata_and_invocable():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("web_search", tags=["search"]), invoke=lambda args: {"v": 1})
    env.register_tool(_tool("web_search", tags=["search", "v2"]), invoke=lambda args: {"v": 2})

    result = env.execute_tool(ToolCall(tool_name="web_search", arguments={}))

    assert result.output == {"v": 2}
    assert "v2" in env.discover_tool("v2")[0].schema["tags"]


# --- select_tool ----------------------------------------------------------


def test_select_tool_prefers_exact_name_match_over_tag_match():
    env = DefaultToolsEnvironment()
    exact = _tool("search", tags=[])
    tagged = _tool("other_search_tool", tags=["search"])

    selected = env.select_tool("search", [tagged, exact])

    assert selected is exact


def test_select_tool_falls_back_to_tag_match():
    env = DefaultToolsEnvironment()
    candidate = _tool("web_search", tags=["search"])

    selected = env.select_tool("search", [candidate])

    assert selected is candidate


def test_select_tool_raises_on_empty_candidates():
    env = DefaultToolsEnvironment()

    with pytest.raises(ValueError):
        env.select_tool("search", [])


def test_select_tool_raises_when_nothing_matches():
    env = DefaultToolsEnvironment()
    candidate = _tool("code_exec", tags=["execution"])

    with pytest.raises(ValueError):
        env.select_tool("search", [candidate])


# --- execute_tool -----------------------------------------------------------


def test_execute_tool_calls_the_registered_invocable_with_call_arguments():
    env = DefaultToolsEnvironment()
    received = {}

    def invoke(arguments: dict) -> dict:
        received.update(arguments)
        return {"price": 123}

    env.register_tool(_tool("market_data"), invoke=invoke)

    result = env.execute_tool(ToolCall(tool_name="market_data", arguments={"ticker": "AAPL"}))

    assert received == {"ticker": "AAPL"}
    assert result.ok is True
    assert result.output == {"price": 123}


def test_execute_tool_on_unknown_tool_returns_failed_result_not_exception():
    env = DefaultToolsEnvironment()

    result = env.execute_tool(ToolCall(tool_name="nonexistent", arguments={}))

    assert result.ok is False
    assert "unknown tool" in result.output["error"]


def test_execute_tool_with_no_invocable_registered_returns_failed_result():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("discoverable_only"))

    result = env.execute_tool(ToolCall(tool_name="discoverable_only", arguments={}))

    assert result.ok is False
    assert "no invocable" in result.output["error"]


def test_execute_tool_catches_invocable_exception_and_returns_failed_result():
    def always_fails(arguments: dict) -> dict:
        raise RuntimeError("upstream timeout")

    env = DefaultToolsEnvironment()
    env.register_tool(_tool("flaky"), invoke=always_fails)

    result = env.execute_tool(ToolCall(tool_name="flaky", arguments={}))

    assert result.ok is False
    assert result.output["error"] == "upstream timeout"
    assert result.output["failure_type"] == "TRANSIENT"


def test_execute_tool_trips_circuit_breaker_after_three_same_tool_failures_in_a_row():
    def always_fails(arguments: dict) -> dict:
        raise RuntimeError("boom")

    env = DefaultToolsEnvironment()
    env.register_tool(_tool("flaky"), invoke=always_fails)

    for _ in range(3):
        result = env.execute_tool(ToolCall(tool_name="flaky", arguments={}))
        assert result.ok is False

    # The 4th call's failure history (3 prior same-tool failures) is what
    # DefaultFailureClassifier.classify() treats as LOOP_OR_CASCADE.
    fourth = env.execute_tool(ToolCall(tool_name="flaky", arguments={}))
    assert fourth.output["failure_type"] == "LOOP_OR_CASCADE"

    fifth = env.execute_tool(ToolCall(tool_name="flaky", arguments={}))
    assert fifth.ok is False
    assert "circuit breaker open" in fifth.output["error"]


def test_execute_tool_records_audit_events_for_attempt_and_result(tmp_path, monkeypatch):
    from cross_cutting import observability

    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    env = DefaultToolsEnvironment(audit_manager=observability.DefaultAuditManager())
    env.register_tool(_tool("market_data"), invoke=lambda args: {"ok": True})

    env.execute_tool(ToolCall(tool_name="market_data", arguments={}))

    lines = audit_log_path.read_text().splitlines()
    assert any('"event_type": "tool_execution_attempt"' in line for line in lines)
    assert any('"event_type": "tool_execution_result"' in line for line in lines)


# --- validate_result --------------------------------------------------------


def test_validate_result_true_for_ok_result_with_dict_output():
    env = DefaultToolsEnvironment()
    call = ToolCall(tool_name="market_data", arguments={})
    result = ToolResult(call=call, output={"price": 1}, ok=True)

    assert env.validate_result(result) is True


def test_validate_result_false_when_not_ok():
    env = DefaultToolsEnvironment()
    call = ToolCall(tool_name="market_data", arguments={})
    result = ToolResult(call=call, output={"error": "boom"}, ok=False)

    assert env.validate_result(result) is False


def test_validate_result_false_when_output_is_not_a_dict():
    env = DefaultToolsEnvironment()
    call = ToolCall(tool_name="market_data", arguments={})
    result = ToolResult(call=call, output="not a dict", ok=True)  # type: ignore[arg-type]

    assert env.validate_result(result) is False


# --- retry_tool --------------------------------------------------------------


def test_retry_tool_succeeds_via_same_dispatch_path():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("market_data"), invoke=lambda args: {"price": 5})

    result = env.retry_tool(ToolCall(tool_name="market_data", arguments={}))

    assert result.ok is True
    assert result.output == {"price": 5}


def test_retry_tool_refuses_a_tripped_tool():
    def always_fails(arguments: dict) -> dict:
        raise RuntimeError("boom")

    env = DefaultToolsEnvironment()
    env.register_tool(_tool("flaky"), invoke=always_fails)

    for _ in range(4):
        env.execute_tool(ToolCall(tool_name="flaky", arguments={}))

    # By now the circuit breaker has tripped (see the dedicated trip test
    # above); retry_tool must not bypass that state.
    result = env.retry_tool(ToolCall(tool_name="flaky", arguments={}))

    assert result.ok is False
    assert "circuit breaker open" in result.output["error"]


# --- alternatives_for / switch_tool ------------------------------------------


def test_alternatives_for_returns_tools_sharing_a_capability_tag():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("web_search", tags=["search", "public_data"]))
    env.register_tool(_tool("cached_index_search", tags=["search"]))
    env.register_tool(_tool("code_exec", tags=["execution"]))

    assert env.alternatives_for("web_search") == ["cached_index_search"]


def test_alternatives_for_unknown_tool_returns_empty_list():
    env = DefaultToolsEnvironment()

    assert env.alternatives_for("nonexistent") == []


def test_alternatives_for_tool_with_no_tags_returns_empty_list():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("lonely_tool", tags=[]))

    assert env.alternatives_for("lonely_tool") == []


def test_switch_tool_returns_an_available_alternative_matching_the_need():
    env = DefaultToolsEnvironment()
    failed = _tool("web_search", tags=["search"])
    alternative = _tool("cached_index_search", tags=["search"])
    env.register_tool(failed, invoke=lambda args: {})
    env.register_tool(alternative, invoke=lambda args: {})

    replacement = env.switch_tool(failed, need="search")

    assert replacement.name == "cached_index_search"


def test_switch_tool_raises_when_no_alternative_is_registered():
    env = DefaultToolsEnvironment()
    failed = _tool("web_search", tags=["search"])
    env.register_tool(failed, invoke=lambda args: {})

    with pytest.raises(ValueError):
        env.switch_tool(failed, need="search")


def test_switch_tool_excludes_alternatives_the_circuit_breaker_has_tripped():
    def always_fails(arguments: dict) -> dict:
        raise RuntimeError("boom")

    env = DefaultToolsEnvironment()
    failed = _tool("web_search", tags=["search"])
    tripped_alternative = _tool("flaky_alternative", tags=["search"])
    healthy_alternative = _tool("healthy_alternative", tags=["search"])
    env.register_tool(failed, invoke=lambda args: {})
    env.register_tool(tripped_alternative, invoke=always_fails)
    env.register_tool(healthy_alternative, invoke=lambda args: {})

    for _ in range(4):
        env.execute_tool(ToolCall(tool_name="flaky_alternative", arguments={}))

    replacement = env.switch_tool(failed, need="search")

    assert replacement.name == "healthy_alternative"


# --- DefaultCircuitBreaker.find_alternative wired to alternatives_for -------


def test_default_circuit_breaker_constructed_by_tools_environment_uses_alternatives_for():
    env = DefaultToolsEnvironment()
    env.register_tool(_tool("web_search", tags=["search"]))
    env.register_tool(_tool("cached_index_search", tags=["search"]))

    breaker = env._circuit_breaker  # the instance this class wired in __init__

    assert breaker.find_alternative("web_search") == "cached_index_search"


def test_find_alternative_skips_a_tripped_candidate_and_falls_through_to_none():
    breaker = DefaultCircuitBreaker(alternative_source=lambda name: ["only_candidate"])
    breaker.trip("only_candidate")

    assert breaker.find_alternative("web_search") is None


def test_find_alternative_static_dict_takes_precedence_over_alternative_source():
    breaker = DefaultCircuitBreaker(
        alternatives={"web_search": "pinned_alternative"},
        alternative_source=lambda name: ["ignored_dynamic_alternative"],
    )

    assert breaker.find_alternative("web_search") == "pinned_alternative"


# --- register_environment_adapter / interact_with_environment ---------------


def test_interact_with_environment_dispatches_to_the_named_adapter():
    env = DefaultToolsEnvironment()
    env.register_environment_adapter("paper_broker", lambda action: {"filled": True})

    result = env.interact_with_environment({"adapter": "paper_broker", "order": "buy"})

    assert result == {"filled": True}


def test_interact_with_environment_raises_key_error_with_zero_adapters_registered():
    env = DefaultToolsEnvironment()

    with pytest.raises(KeyError):
        env.interact_with_environment({"adapter": "paper_broker"})


def test_interact_with_environment_raises_value_error_without_an_adapter_key():
    env = DefaultToolsEnvironment()

    with pytest.raises(ValueError):
        env.interact_with_environment({})
