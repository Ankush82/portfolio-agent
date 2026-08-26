# 0018 — Peer-agent output: untrusted by default

**Status:** Accepted — 2026-08-26
**Component:** Security & Privacy (17)

## Context

ADR-0003 tagged document content (news, filings, reports) as untrusted at the point it enters Agent Runtime's loop, addressing AgentDojo's threat model of data being mistaken for instructions. Separate, more recent research on multi-agent trust exploitation found that LLM agents tend to treat peer agents as inherently trustworthy — executing commands from another agent that they would refuse if the identical instruction came directly — bypassing the same safety mechanisms ADR-0003 relies on. Agent Runtime's Delegation Manager sub-component can hand work to another agent and receive its output back, which is exactly this exposure. Security & Privacy needed a decision on whether that output gets the same treatment as document content.

## Decision

Untrusted by default, same as documents: Delegation Manager tags whatever a sub-agent returns using the same provenance-tag mechanism ADR-0003 established for document content, before that output can be reasoned over as an instruction.

## Alternatives considered

- **Trusted by default.** A delegated sub-agent is assumed to operate under the same rules as the parent, so its output is treated as trusted unless something else flags it. Rejected directly on the strength of the trust-exploitation research: this is precisely the assumption that research found agents make and that gets exploited.

## Consequences

- Extends ADR-0003's tagging mechanism rather than introducing a new one — the same "untrusted" tag now has two sources (documents, peer-agent output), and anything downstream that already handles the tag (Memory's quarantine gate, Evidence & Verification's mandatory-evidence gate) handles both without being redesigned.
- Delegation Manager (Agent Runtime, not previously scoped for security logic) now carries the same tagging responsibility Task Manager and Executor already carry under ADR-0003.
- This does not resolve how much a delegated sub-agent should be trusted with in the first place (what it's allowed to do, not just how its output is treated) — that's a separate, still-open question about delegation scope, not settled here.

## Related

- Full design: [Phase 0 Cross-Cutting Design](https://claude.ai/code/artifact/f9146a5d-2770-4f33-9b20-1c029a0cf22f), fig. 17.1.
- Extends: [ADR-0003](0003-agent-runtime-in-runtime-adversarial-input-defense.md).
- Logged narratively in `../checkpoint.md`.
