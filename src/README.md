# Blueprint

The skeleton, not the system: every file below has real class and method signatures with docstrings, and no implementation logic. This *is* the interface contract referenced in `../orchestration.md` — treat a change to any signature here as an architectural decision (`loop.md` step 2), not a routine edit.

## Layout

```
src/
  infrastructure.py        component 18 — the one interface every component
                            talks through (ADR-0019); never import a DB
                            driver, cache client, or storage SDK anywhere else
  cross_cutting/
    reliability.py          component 15 — failure classification, circuit breaker
    observability.py        component 16 — tracing, cost tracking, audit
    security.py              component 17 — boundary gate, provenance tagging
  components/
    c01_user_portfolio.py
    c02_data_sources.py
    c03_data_processing_quality.py
    c04_knowledge_entity.py
    c05_retrieval_context.py
    c06_memory.py
    c07_event_observation.py
    c08_analysis_reasoning.py
    c09_evidence_verification.py
    c10_agent_runtime.py
    c11_tools_environment.py
    c12_decision_policy.py
    c13_interaction_notification.py
    c14_learning_evaluation.py
```

## Two depths of stub

- **05, 06, 09, 10, 15, 16, 17, 18** have completed low-level designs. Their stubs reflect the actual mechanism — every named box and branch in their fig. 1 / fig. 2 diagrams is a method, with a docstring pointing at which branch it implements.
- **The other 10** only have whiteboard-level detail (sub-components and capabilities, no mechanism design yet). Their stubs are one class per component with one method per capability — real signatures, but no branch structure, because that hasn't been designed.

Every file's module docstring names the ADRs and design artifact it implements. Read those before writing anything inside a method body.

## Ports & Adapters, everywhere

Every capability class in this blueprint is split in two, the same way `infrastructure.py`'s `Infrastructure` always was:

- **`Foo(Protocol)`** — the port. Signatures and docstrings only; every method body is `...`. This is what the rest of the codebase is allowed to depend on.
- **`StubFoo`** — the adapter. A traced no-op today; a real implementation (LangGraph for Agent Runtime, Mem0 or Supermemory for Memory, Postgres for `infrastructure.py`, ...) later, swapped in behind the same port without touching any caller.

Real logic, when it eventually gets written, replaces a `Stub*` class — or adds a new adapter alongside it — never the Protocol. Changing a Protocol's signature is an architectural decision (`loop.md` step 2), not a routine edit, exactly like changing `infrastructure.py` already was.

The one exception is `cross_cutting/observability.py`'s `Span` and `traced()` — real, working code, not a stub, since tracing every call is what makes the rest of this blueprint watchable at all.
