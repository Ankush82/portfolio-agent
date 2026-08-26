from cross_cutting.reliability import (
    DefaultCircuitBreaker,
    DefaultFailureClassifier,
    FailureEvent,
    FailureType,
)


def _event(component: str, history: list[FailureEvent] | None = None) -> FailureEvent:
    return FailureEvent(component=component, tool="some_tool", error="boom", history=history or [])


def test_classify_returns_loop_or_cascade_for_three_same_component_failures_in_a_row():
    history = [_event("retriever"), _event("retriever"), _event("retriever")]
    event = _event("retriever", history=history)

    result = DefaultFailureClassifier().classify(event)

    assert result == FailureType.LOOP_OR_CASCADE


def test_classify_returns_loop_or_cascade_when_more_than_three_trailing_entries_match():
    history = [
        _event("planner"),
        _event("retriever"),
        _event("retriever"),
        _event("retriever"),
        _event("retriever"),
    ]
    event = _event("retriever", history=history)

    result = DefaultFailureClassifier().classify(event)

    assert result == FailureType.LOOP_OR_CASCADE


def test_classify_returns_transient_when_recent_history_has_a_different_component():
    history = [_event("retriever"), _event("planner"), _event("retriever")]
    event = _event("retriever", history=history)

    result = DefaultFailureClassifier().classify(event)

    assert result == FailureType.TRANSIENT


def test_classify_returns_transient_when_history_shorter_than_loop_window():
    history = [_event("retriever"), _event("retriever")]
    event = _event("retriever", history=history)

    result = DefaultFailureClassifier().classify(event)

    assert result == FailureType.TRANSIENT


def test_classify_returns_transient_for_empty_history():
    event = _event("retriever", history=[])

    result = DefaultFailureClassifier().classify(event)

    assert result == FailureType.TRANSIENT


def test_trip_makes_tool_unavailable_and_leaves_other_tools_available():
    breaker = DefaultCircuitBreaker()

    breaker.trip("web_search")

    assert breaker.is_available("web_search") is False
    assert breaker.is_available("code_exec") is True


def test_find_alternative_returns_none_with_no_mapping_configured():
    breaker = DefaultCircuitBreaker()

    assert breaker.find_alternative("web_search") is None


def test_find_alternative_returns_configured_alternative():
    breaker = DefaultCircuitBreaker(alternatives={"web_search": "cached_index_search"})

    assert breaker.find_alternative("web_search") == "cached_index_search"
    assert breaker.find_alternative("unmapped_tool") is None
