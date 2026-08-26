# 0024 — Tools & Environment registry and dispatch mechanism

**Status:** Accepted — 2026-08-26
**Component:** Tools & Environment (11)

## Context

Tools & Environment was whiteboard-only before this pass: a `ToolsEnvironment` Protocol and a `StubToolsEnvironment` existed in `src/components/c11_tools_environment.py`, with `Tool`, `ToolCall`, and `ToolResult` dataclasses already defined, but no registry, no dispatch, and no ADR. Implementing it for real required deciding three things no prior document settled:

1. How a registered `Tool` gets connected to something that actually runs when `execute_tool` is called, given that `register_tool(self, tool: Tool) -> None` — the only registration entry point the Protocol defines — takes no callable parameter, and `Tool` itself carries only `name` and `schema`.
2. What rule `select_tool`/`discover_tool` use to match a stated `need` against registered tools, given `Tool.schema` is an untyped `dict` with no prior schema convention.
3. Whether the registry should start pre-populated with any tools, given this project has never designed a single concrete tool (no market-data tool, no broker API tool, nothing) in any component or ADR to date.

Unlike the other components built in this wave, Tools & Environment has no external-credential gap of its own — it is a registry and dispatcher, not a specific tool — so none of this needed the "flag and wait" exception; all three were resolvable by engineering judgment against the already-fixed `Tool`/`ToolCall`/`ToolResult` shapes and the existing `ToolsEnvironment` Protocol, which is why this is Accepted rather than Proposed.

## Decision

**Invocation model:** keep `Tool` as pure metadata (unchanged) and add a parallel registry, `self._invocables: dict[str, Callable[[dict], dict]]`, populated through an additional optional parameter on `DefaultToolsEnvironment.register_tool(tool, invoke=None)`. `execute_tool` looks up `call.tool_name` in both `self._tools` and `self._invocables`; a tool registered without `invoke` is real and discoverable but reports `no_invocable` as a real, honest `ToolResult(ok=False)` rather than silently no-op succeeding.

**Matching rule:** one rule, used identically by `discover_tool`, `select_tool`, and (component 11's own use) `alternatives_for`/`switch_tool` — a tool matches a `need` if `need == tool.name` (exact name match, highest precedence) or `need in tool.schema.get("tags", [])` (a capability-tag list convention this ADR establishes for `Tool.schema`). `select_tool` picks the first exact-name match, else the first tag match, else raises `ValueError` — never an arbitrary pick among non-matching candidates.

**Empty registry and adapter set by default:** `DefaultToolsEnvironment` starts with zero tools and zero environment adapters registered. This is the correct default, not a placeholder standing in for a missing one: no concrete tool or environment adapter has been designed anywhere in this project, so pre-registering a fake one would be dishonest scaffolding that looks more complete than it is.

**In-memory-only registry state:** the tool registry, invocable registry, environment-adapter registry, and per-tool failure history are all plain instance-level dicts, not routed through `DefaultInfrastructure` (System Infrastructure, ADR-0019). Judgment call, stated once here rather than repeated per field: which tools a running agent process currently has wired up is a runtime/process-lifetime concern, not durable state that needs to survive a process restart — nothing in this project's design says a tool registration should outlive the process that made it.

## Alternatives considered

- **`Tool` carries the invocable directly** (`Tool.invoke: Callable[[dict], dict]`). Keeps registration to the Protocol's single `register_tool(tool)` call with no signature growth. Rejected: it would force every existing caller of the already-defined `Tool` dataclass — including `StubToolsEnvironment`, which constructs bare `Tool(name="stub", schema={})` — to either supply a callable or accept a default, and it conflates "what a tool is" (metadata, safe to pass around, log, and compare) with "how it runs" (a closure with its own captured state) in one dataclass. The parallel-registry approach keeps `Tool` a clean value type and only required a minimal, backward-compatible addition to `register_tool`'s own signature.
- **Schema-driven structural matching** (validate `need` against a JSON-schema-shaped `Tool.schema` rather than a flat tag list). More expressive, but invents a schema convention with no consumer yet needing anything beyond name/capability matching — over-building ahead of an actual need, for a component that has zero real tools to validate the extra complexity against.
- **Pre-register a small set of illustrative tools** (e.g., a toy "echo" tool) so the registry isn't empty out of the box. Rejected for the same reason a placeholder LLM provider wasn't invented in ADR-0021: it would look like a real default and get built on top of by mistake. An empty registry that raises clear, real errors (`unknown_tool`, `no_invocable`) is more honest than a fake tool nobody asked for.

## Consequences

- `execute_tool` against an empty registry always returns `ToolResult(ok=False)` with a clear reason (`unknown_tool`) rather than raising — callers get a structured result to inspect via `validate_result`, consistent with `ToolResult`'s existing shape, not an unhandled exception for the ordinary "nothing is registered yet" case.
- Registering a tool without an `invoke` callable is a legitimate, real state (useful for e.g. exposing discoverable-but-not-yet-wired tools) — but it is indistinguishable from a typo'd tool name at `execute_tool` time except by the `no_invocable` vs. `unknown_tool` reason string; a future caller debugging "why didn't my tool run" needs to check both.
- The capability-tag convention (`Tool.schema["tags"]`) is now load-bearing for `discover_tool`, `select_tool`, and `alternatives_for` (ADR-0025) alike — any concrete tool this project eventually designs needs to populate `schema["tags"]` with meaningful capability labels for selection and interchangeability to work as intended; a tool registered with an empty or missing `tags` list is discoverable only by exact name and has no interchangeability partners.
- Tool registration does not survive a process restart. If a future need arises for registrations to persist (e.g., a supervisor process that must know what tools were available before a crash), that is a new decision, not something this ADR's in-memory choice quietly already handles.

## Related

- Depends on: `Tool`/`ToolCall`/`ToolResult` dataclasses, already defined in `src/components/c11_tools_environment.py` before this pass.
- Extended by: [ADR-0025](0025-tools-environment-interchangeability-closes-circuit-breaker-gap.md) (the same matching rule powers tool interchangeability).
- Implemented by: `../src/components/c11_tools_environment.py`, `DefaultToolsEnvironment`.
- Tested by: `../tests/components/test_tools_environment.py`.
- Logged narratively in `../checkpoint.md`.
