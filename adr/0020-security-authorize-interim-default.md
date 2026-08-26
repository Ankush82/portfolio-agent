# 0020 — Authorize interim default: fail-open, logged, per-call

**Status:** Proposed — 2026-08-26
**Component:** Security & Privacy (17)

## Context

The authority-check granularity question — should `BoundaryGate.authorize()` decide once per task or separately for every tool call — has been open since Phase 0 research. `loop.md`'s "Still-open items" section names it directly: "Security & Privacy: authority-check granularity, per task vs. per tool call — surfaced during Phase 0 research, never actually asked." `checkpoint.md` repeats the same line under its Phase 0 cross-cutting summary: "Still open: authority-check granularity for Security & Privacy (per task vs. per tool call) — surfaced during research but not asked yet, not blocking anything designed so far." Neither ADR-0003 nor ADR-0018 settles it: both establish *what gets tagged* untrusted, not *how authorization decisions get scoped* once a call needs to cross the boundary gate.

A concrete `DefaultBoundaryGate` is now being implemented alongside the existing `StubBoundaryGate` test double, and `authorize()` needs *some* real behavior to exist — a `Protocol` method can't stay unimplemented in a non-stub class. Per `loop.md` step 2, this is a genuine gap: no ADR or design artifact settled the granularity question, and this decision determines what a caller can even pass to `authorize()` (a task identifier, a tool-call identifier, or both). Implementing something without deciding this by fiat would resolve the real question silently, which `loop.md` explicitly forbids ("Never treat a 'still open' flag in an existing ADR as resolved by whatever the code happens to do").

## Decision

Ship an interim default that is explicitly provisional, not a resolution of the granularity question: `DefaultBoundaryGate.authorize()` accepts the same three arguments regardless of what identifies `action`/`resource` (a task ID, a tool-call ID, or anything else a caller constructs) and evaluates **every call it receives, independently, in fail-open mode** — it returns `True` unconditionally, but records the identity, action, and resource of the attempted call via `DefaultAuditManager` first, so a decision trail exists even though nothing is actually enforced yet.

This is a placeholder that makes the call *observable* without pretending to have decided *at what granularity a real authorization boundary should sit*. It does not choose per-task or per-tool-call scoping — it simply logs whatever the caller passes, at whatever granularity the caller already chose, and lets everything through. The real decision — which granularity a production authorization policy should use, and what it should do when a check fails — is deferred to whoever answers this ADR.

## Alternatives considered

- **Per-task granularity.** Authorize once when a task begins, cache the result, and let every tool call inside that task proceed under the same authorization. Simpler to reason about and cheaper (one check per task, not one per call), but coarser: a single authorized task can still make an arbitrary number of tool calls with no further check, which is a wide blast radius if the task itself was authorized for something narrower than what it ends up doing. Not chosen here because choosing it now would decide the real question by default, not because it's wrong — it may well be the eventual answer.
- **Per-tool-call granularity.** Authorize every individual tool call against the specific action and resource it targets. Finer-grained and closer to least-privilege, but more expensive (one check per call instead of per task) and requires Tools & Environment (11, not yet designed) to expose enough structure to state what "this specific call" is being authorized against — the same kind of dependency ADR-0016 already flagged for circuit-breaker scoping. Also not chosen here for the same reason: it would be deciding, not deferring.
- **Interim default: fail-open, logged, per-call, granularity left to the caller (this ADR).** Neither commits to per-task nor per-tool-call scoping — it accepts whatever the caller already constructed for `action`/`resource` and just logs it, always allowing. Chosen because it lets a concrete `DefaultBoundaryGate` exist and be tested today without silently picking a side in the real fork, and because "log everything, block nothing" is the honest description of what a placeholder authorization boundary should do: visible, not enforced.

## Consequences

- `authorize()` is not a real security boundary. It allows every call unconditionally; anything that depends on it actually blocking unauthorized access is not yet safe to build on top of this implementation.
- The audit log now has a record of every authorization attempt (identity, action, resource, timestamp), which gives Observability & Governance (16) something concrete to look at, but the log records what was *asked for*, not what was actually *permitted* in any enforced sense — the "decision" is always "allow."
- The granularity question remains genuinely open. This ADR intentionally does not answer it, in the same way this project has surfaced every other real fork (Memory technology in ADR-0010, Learning & Evaluation's in-scope status in `loop.md`) as a question for the user rather than deciding it inside an implementation pass. This needs an explicit decision from the user before `authorize()` is trustworthy for anything beyond local development — the same bar `loop.md` step 2 sets for every draft ADR.
- Once the granularity decision is made, `DefaultBoundaryGate.authorize()` will need to change from "log and allow" to "check and enforce," and callers may need to change what they pass as `action`/`resource` depending on which granularity is chosen. This ADR does not attempt to make that transition free.

## Related

- Extends: [ADR-0003](0003-agent-runtime-in-runtime-adversarial-input-defense.md), [ADR-0018](0018-security-peer-agent-untrusted-by-default.md) — both establish provenance tagging; neither settles authorization scoping.
- Analogous precedent: [ADR-0016](0016-reliability-circuit-breaker-per-tool.md) — per-tool scoping decided for circuit breakers, flagged as depending on Tools & Environment (11) structure not yet designed; the per-tool-call alternative above has the same dependency.
- Open question originates in: `../loop.md` ("Still-open items this loop inherits") and `../checkpoint.md` (Phase 0 cross-cutting summary, "Still open: authority-check granularity...").
- Implemented by: `../src/cross_cutting/security.py`, `DefaultBoundaryGate.authorize()`.
