# 0042 — Authorize granularity resolved: per-call, real enforcement

**Status:** Accepted — 2026-08-26
**Component:** Security & Privacy (17)

## Context

ADR-0020 shipped an explicitly provisional default for `DefaultBoundaryGate.authorize()`: fail-open, logged, granularity left to whatever the caller happened to pass. It named the real fork — per-task vs. per-tool-call authorization — and gave both alternatives real tradeoffs (blast radius vs. cost) without picking one, because at the time Tools & Environment (component 11) didn't exist yet and the per-tool-call alternative specifically depended on it: "requires Tools & Environment (11, not yet designed) to expose enough structure to state what 'this specific call' is being authorized against."

That dependency is gone. `src/components/c11_tools_environment.py`'s `DefaultToolsEnvironment` is now real: `ToolCall(tool_name, arguments)` and `Tool(name, schema)` give concrete, inspectable structure to "this specific call" — a tool name, its argument dict, and capability tags in `schema["tags"]`. The specific objection ADR-0020 raised against per-tool-call granularity no longer holds.

Separately, `authorize()`'s only real caller — `DefaultDecisionPolicy.authorize_action` in `src/components/c12_decision_policy.py` — was already built around a per-call shape: it destructures one `action` dict (`identity`/`action`/`resource`) per invocation and calls straight through, with no task identifier or task-scoped cache anywhere in that path. The granularity question isn't only a Tools & Environment question; it's also a question of what the one existing caller already does.

This ADR makes the decision ADR-0020 deferred, with the reasoning ADR-0020 itself asked the eventual answer to use: blast radius vs. cost, and whether Tools & Environment now gives per-tool-call structure enough to authorize against.

## Decision

**Per-call granularity, with real enforcement.** `DefaultBoundaryGate.authorize(identity, action, resource)` evaluates every call independently — no task-level caching, no "authorize once and let everything inside a task through." It checks the exact `(identity, action, resource)` triple against a real, minimal, Infrastructure-backed policy table: `security_authorization_grants`, storing `{identity, action, resource}` records written by a new `grant()` method. `action` and/or `resource` may be the wildcard `"*"` (matches anything); `identity` is always matched exactly — a grant is scoped to one principal, never broadcast. No matching grant means deny. Nothing seeds any grants by default, so with a fresh `DefaultBoundaryGate` every call is denied until something explicitly grants it — deny-by-default, not fail-open.

This resolves the blast-radius side of ADR-0020's tradeoff directly: a per-task cache would let one authorized task make an unbounded number of tool calls under a single check, which is exactly the wide blast radius ADR-0020 flagged as per-task's real cost. Per-call closes that: every action/resource pair is checked against real policy data on every call, so an authorized identity can still be denied for a specific action or resource it was never granted.

## Alternatives considered

- **Per-task granularity with caching.** Authorize once when a task begins, cache the result, let every tool call inside proceed unchecked. Cheaper (one check per task), but this project has no task-identifier concept threaded through `authorize()`'s only caller (`authorize_action` takes one destructured `action` dict, not a task handle), and no cache-invalidation mechanism exists anywhere in this codebase to build on. Choosing this would mean inventing a new mechanism nothing currently asks for, just to get a cheaper check — not chosen, because the actual caller shape doesn't need it and the blast-radius cost ADR-0020 already named is real: a single per-task check would let `authorize_action` grant everything a task's remaining tool calls do, with no further look.
- **Per-tool-call granularity, unresolved dependency (ADR-0020's original blocker).** ADR-0020 correctly flagged this as depending on Tools & Environment (11) to expose real per-call structure. That dependency is now resolved — `DefaultToolsEnvironment`'s `ToolCall`/`Tool` shapes give `authorize()`'s callers something concrete to build `action`/`resource` strings from (a tool name, a capability tag, a specific argument). This alternative is, in substance, the decision made here; it's listed separately only because ADR-0020 named it as blocked and this ADR is the one un-blocking and adopting it.
- **Keep fail-open, defer real enforcement further.** Rejected outright — `loop.md` and this project's standing operating instruction require resolving a flagged gap with documented engineering judgment once the blocking dependency (Tools & Environment) is gone, not leaving it open indefinitely. ADR-0020 itself said this needed "an explicit decision from the user before `authorize()` is trustworthy for anything beyond local development" — that decision is this ADR.
- **Full RBAC (roles, role hierarchies, policy expressions).** Rejected as over-building for what this decision actually requires. Nothing in this project has more than one caller of `authorize()` yet, and no design artifact calls for roles, inheritance, or anything beyond "can this identity do this action to this resource." A flat grant table with an identity-scoped wildcard is the smallest real policy representation that answers the actual question; scaling it up is a future ADR's problem if a caller ever needs more.

## Consequences

- `authorize()` is now a real security boundary: it denies by default, and only allows what has been explicitly granted via `grant()`. Anything that previously relied on ADR-0020's "always allow" behavior now gets a real `False` unless a grant exists — this is intentional, not a regression. `DefaultDecisionPolicy.authorize_action` (its only production caller) already just returns whatever the gate decides, so no caller code needed to change.
- Nothing in this project currently calls `grant()` anywhere outside tests — no concrete identity/action/resource shape has been decided by any component yet, so no grants are seeded by default (same reasoning ADR-0024 used for Tools & Environment's empty registry). A real deployment needs to call `grant()` explicitly before `authorize_action` allows anything; that is correct, not a gap, but worth flagging so it isn't mistaken for "authorization broke."
- The audit trail now records real decisions (`enforced: True`, `decision` reflecting the actual allow/deny), replacing ADR-0020's honest-but-inert `enforced: False` records.
- `DefaultBoundaryGate` now takes an `infrastructure` constructor parameter (`Infrastructure | None`, defaulting to `DefaultInfrastructure()`), matching every other `Default*` component's own dependency-injection shape. Since `DefaultInfrastructure` opens connections lazily, constructing `DefaultBoundaryGate()` with no arguments still never touches the network — only calling `authorize()` or `grant()` does.
- The per-task alternative is not permanently foreclosed. If a future caller needs task-scoped batching for cost reasons, it can be layered on top of this per-call mechanism (e.g. a caller-side cache keyed by its own task identifier) without changing `authorize()`'s contract — the granularity `authorize()` itself enforces stays per-call.

## Related

- Supersedes: [ADR-0020](0020-security-authorize-interim-default.md) — this ADR resolves the granularity question ADR-0020 deliberately left open.
- Unblocked by: `../src/components/c11_tools_environment.py`'s `DefaultToolsEnvironment` (ADR-0024, ADR-0025) — the concrete per-call structure ADR-0020 said didn't exist yet.
- Extends: [ADR-0003](0003-agent-runtime-in-runtime-adversarial-input-defense.md), [ADR-0018](0018-security-peer-agent-untrusted-by-default.md) — provenance tagging is unaffected by this ADR; only `authorize()` changes.
- Analogous precedent for a minimal, Infrastructure-backed table over a full mechanism: [ADR-0024](0024-tools-environment-registry-and-dispatch-mechanism.md) (empty registry as the correct default, not a gap).
- Implemented by: `../src/cross_cutting/security.py`, `DefaultBoundaryGate.authorize()` and `DefaultBoundaryGate.grant()`.
- Tested by: `../tests/cross_cutting/test_security.py`.
