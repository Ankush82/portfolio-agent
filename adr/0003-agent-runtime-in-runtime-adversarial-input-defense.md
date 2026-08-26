# 0003 — Adversarial-input defense lives inside Agent Runtime

**Status:** Accepted — 2026-08-25
**Component:** Agent Runtime (10)

## Context

AgentDojo's threat model is documents that contain instructions the agent might mistake for commands — directly relevant here, since the system continuously ingests news, filings, reports, presentations and management announcements, any of which could carry an embedded instruction. A dangerous trajectory looks like: financial document → agent reads document → document contains malicious instruction → agent interprets data as instruction → tool call.

The question was where the defense against this actually sits: inside Agent Runtime itself, or entirely delegated to Security & Privacy (17) / Policy & Safety (12).

## Decision

Inside Agent Runtime. Task Manager and Executor tag ingested content as untrusted at the point it enters the loop, before it can be reasoned over as an instruction rather than data.

## Alternatives considered

- **Fully delegated.** Agent Runtime has no awareness of trust boundaries at all; it's entirely Security & Privacy's / Policy & Safety's gate, called like any other check. Rejected because the conflation risk — data being reasoned over as an instruction — happens inside the Reason step itself, and a gate that sits entirely outside the runtime can't intervene at the point the conflation actually occurs.
- **Shared via provenance.** Runtime tags content provenance as it flows through its own state, but the enforcement decision still lives in Security & Privacy. Rejected in favor of the runtime owning both the tagging and the enforcement, since splitting them adds a coordination surface for no clear benefit here.

## Consequences

- Task Manager and Executor now carry security-relevant logic, not just orchestration logic — this is new scope for those two sub-components specifically.
- The tag has to survive being passed through Reason without being silently dropped; this is called out explicitly as an unverified assumption (unknown-known) in the design's failure framework.
- Security & Privacy (17), not yet designed, will need to define what "untrusted" means precisely and how the tag propagates once it reaches other components downstream of Agent Runtime.

## Related

- Full trajectory design: [Agent Runtime Design](https://claude.ai/code/artifact/89de3618-d0b8-44f3-af85-73dbfbd73df6), fig. 2 (the "tag provenance (untrusted)" label on the Act → Observe edge) and the knowns/unknowns grid.
- Logged narratively in `../checkpoint.md`.
