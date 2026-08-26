# 0021 — Agent Runtime LLM provider interim: injectable, non-cognitive placeholder

**Status:** Superseded by [ADR-0043](0043-llm-provider-resolved-openrouter.md) — 2026-08-26. The provider question deliberately left open below is resolved: OpenRouter, `anthropic/claude-haiku-4.5`, via `src/llm.py`'s `get_reason_fn`. `placeholder_reason_fn` and the injectable `reason_fn` seam described below are unchanged — only the "which provider" gap is closed.
**Component:** Agent Runtime (10)

## Context

Agent Runtime's control flow is now fully designed and, as of this pass, fully implemented as real orchestration: the hybrid checkpoint loop (ADR-0001), stakes-dependent reflection (ADR-0002), in-runtime provenance tagging (ADR-0003, extended by ADR-0018), and failure-classified recovery (ADR-0004, revised by ADR-0015) all now run as real control flow inside a compiled LangGraph graph (ADR-0009), via `build_agent_runtime_graph()` in `src/components/c10_agent_runtime.py`.

None of that reaches the one thing fig. 2 actually depends on to mean anything: "Reason" (deciding what to do next) and "assess stakes" (ADR-0002's stakes signal) are both, fundamentally, judgment calls that require an LLM to make. This project has never chosen an LLM provider anywhere — not in any ADR, not in `checkpoint.md`, not in `Thoughts.md`, not in the Implementation Plan. `pyproject.toml` has never listed one as a dependency. The gap was not raised and skipped; it was never reached, because every prior pass stopped at orchestration and design, not at the reasoning step itself.

A concrete, compiled, runnable graph now needs *some* callable behind its reason/assess-stakes nodes — a `Protocol` boundary can be left abstract, but a graph that's supposed to actually run end to end for testing cannot. Per `loop.md` step 2, this is a genuine gap: no ADR or design artifact settled which LLM provider Agent Runtime's reasoning should call, and deciding it here, inside an implementation pass, would resolve a real architectural fork by fiat rather than by the user actually weighing it.

## Decision

Ship `placeholder_reason_fn` as an explicitly non-cognitive stand-in, behind an injectable interface (`reason_fn: Callable[[dict], dict]`, a constructor parameter of `build_agent_runtime_graph()`), so the graph's structure can be built, compiled, run, and tested today, and later handed a real reasoning backend without changing the graph's shape at all — only the one callable passed into it.

`placeholder_reason_fn` does no real reasoning. It does not look at the checkpoint's subgoal, the running history, or the observed result. It deterministically reports "checkpoint complete" after exactly one reason/act/observe cycle, and always reports low stakes. This is what makes the graph genuinely runnable and testable end to end without an LLM — it is also exactly what makes it unmistakably not real cognition. Anyone reading its name or docstring should not be able to mistake it for a working reasoning step.

## Alternatives considered

- **Anthropic Claude API.** Consistent with how this project's design and code have themselves been produced so far (every prior component, ADR, and this implementation pass itself was authored by Claude). Real considerations the user would need to weigh: API cost per reasoning call at whatever volume Agent Runtime ends up running at, latency added to every reason/assess-stakes node (each one is a network round trip on the critical path of every checkpoint), and whether the same provider should also back the reasoning the other 17 components will eventually need (Retrieval & Context's retrieval evaluator, Evidence & Verification's contradiction resolution, etc.) or whether providers should be allowed to differ per component.
- **OpenAI API.** A real, comparably capable alternative with its own cost/latency profile and tooling ecosystem. Not evaluated against this project's specific reasoning shape (checkpoint-scoped, stakes-aware, high call frequency) any more than Claude was — that evaluation hasn't happened yet for either.
- **A locally-hosted open model** (e.g., served via vLLM or similar). Removes per-call API cost and external network latency, and avoids sending checkpoint/task content to a third party at all — potentially relevant given ADR-0003/ADR-0018's own concern with untrusted content flowing through Reason. Trades that for needing to host, serve, and keep current a model good enough to reason reliably about financial checkpoints and judge stakes correctly, which is a meaningfully different operational burden than calling an API.

None of these is picked here. The real considerations — cost, latency, data-handling implications given ADR-0003/ADR-0018, and whether one provider serves every component or providers vary by component — are named so the user has them, not resolved on the user's behalf.

## Consequences

- Nothing `build_agent_runtime_graph()` produces right now is real reasoning. The orchestration around it — checkpoint sequencing, the stakes branch's existence, the recovery/escalation routing, the provenance tagging on Act's output — is real and independently correct; what happens inside the `reason`/`assess_stakes` nodes when `placeholder_reason_fn` backs them is not. Anything that depends on Agent Runtime actually deciding something sensible is not yet safe to build on top of this implementation.
- `placeholder_reason_fn`'s "always complete after one cycle, always low stakes" behavior is why the compiled graph is genuinely testable today (see `tests/components/test_agent_runtime.py`) — but those tests exercise the graph's control flow, not any reasoning quality, and should not be read as validating that the loop "works" in the sense of producing good decisions.
- Once an LLM provider is chosen, the fix is narrowly scoped by design: implement a real `reason_fn` matching the same `Callable[[dict], dict]` contract (`{"phase": "reason", ...} -> {"action": ..., "checkpoint_complete": ...}` and `{"phase": "assess_stakes", ...} -> {"stakes_high": ...}`) and pass it into `build_agent_runtime_graph()` instead of `placeholder_reason_fn`. No node, edge, or state field in the graph needs to change for that swap.
- This is scoped to Agent Runtime's reason/assess-stakes calls only. It says nothing about which provider, if any, other components' eventual reasoning needs (Retrieval & Context's Self-RAG gate, Evidence & Verification's contradiction resolution, Decision & Policy) should use — that is a separate decision, or decisions, not settled here.

## Related

- Depends on: [ADR-0009](0009-agent-runtime-technology-langgraph.md) (LangGraph chosen because fig. 2 maps onto its graph model — this ADR is about what backs the *content* of the reasoning inside that graph, not its shape).
- Extends the same "ask, don't decide" pattern as: [ADR-0010](0010-memory-technology-purpose-built-layer.md) (Mem0 vs. Supermemory, left open), [ADR-0020](0020-security-authorize-interim-default.md) (authorize() interim default — same shape, same tone, this ADR's direct template).
- Implemented by: `../src/components/c10_agent_runtime.py`, `placeholder_reason_fn` and `build_agent_runtime_graph()`.
- Open question originates in: `loop.md`'s Task 4 instruction to a coding subagent building Agent Runtime, surfaced during that pass rather than during prior design rounds — this project's reasoning-backend choice was never actually reached until a runnable graph required one to exist.
- Logged narratively in `../checkpoint.md`.
