# Orchestration — Parallel Execution and the Master Review Loop

Answers two things: how loop.md runs across many components at once without the pieces contradicting each other, and what "master reviewer" actually means when the work is split across Codex CLI and Broc CLI.

## The real risk in going parallel

Going component-by-component sequentially never has this problem: whatever was built last is the only thing that could be wrong. Parallel has a different failure mode entirely — two components built independently, each individually correct against its own spec, that don't actually agree with each other at the seam. Concretely, for this project:

- **Interface drift.** Component A codes against what it assumes Component B's interface looks like; Component B's real implementation ends up slightly different. Neither is "wrong" against its own design doc — they just don't fit.
- **Reinvented cross-cutting wiring.** loop.md requires every component to wire in Reliability & Resilience, Observability & Governance, Security & Privacy, and System Infrastructure (15–18). Built in isolation, five parallel subagents produce five slightly different circuit breakers instead of one shared one.
- **Draft-ADR collisions.** Two subagents hit related gaps at the same time and each proposes a different resolution, discovered only later.
- **Tool-idiom drift.** Codex CLI and Broc CLI produce structurally different code even when both are "correct" against the ADRs.
- **Dependency-order violations.** Component 07 built against an assumption about component 04 that turns out not to match what 04 actually does, because 04 wasn't finished yet.

None of these are arguments against parallel. They're arguments for **fixing the interface before parallel work starts**, which is exactly what a blueprint is for.

## The mitigation: blueprint first, then parallel against it

The blueprint (`src/`) is not documentation of the plan — it's the actual contract. Every component's file, class, and method signature in it is what every *other* component is allowed to assume exists. Once it's reviewed and treated as frozen:

1. **Any component can be coded in parallel**, by any tool, against the blueprint's stub signatures — not against another component's real implementation, which may not exist yet.
2. **A signature change to a shared interface (especially `infrastructure.py` or anything in `cross_cutting/`) is itself an architectural decision** and goes through loop.md step 2 — draft ADR, not a silent edit. This is the single rule that keeps parallel work from drifting.
3. **Coding in parallel is not the same as integrating in parallel.** The dependency graph below still governs merge order, even though coding can happen out of order.

## Parallel-safe groups

Can be coded simultaneously, by different subagents or tools, against the frozen blueprint:

| Group | Components | Why they're parallel-safe |
|---|---|---|
| A | 15, 16, 17, 18, 01, 11 | No dependency on any other component's real behavior — only on the frozen interface. |
| B | 02 → 03 → 04 | Internally sequential (03 consumes 02's output, 04 consumes 03's), but the whole chain can be coded in parallel with Group A. |
| C | 07 | Needs 04's real output to integration-test, not to code against the stub. |
| D | 08 | Needs 06 (done) and Group C's real output to integration-test. |
| E | 12 | Needs 08 and 09 (done) real. |
| F | 13 | Needs 12 real. |
| G | 14 | Needs 13 real, and the still-open scoping question resolved first. |

**Coding** can start on any group as soon as the blueprint is frozen. **Integration** (merging into a branch other components will build on) follows A → B → C → D → E → F → G, because that's the actual data-dependency order, not a scheduling preference.

## The master reviewer

This is the role assigned to this conversation. It runs once a component (from either Codex CLI or Broc CLI) is reported done via loop.md step 6, before it's treated as integrated.

**Checklist, in order — stop at the first failure and report it, don't keep checking:**

1. **Signature match.** Does the real implementation's public interface match the blueprint stub exactly? A silent signature change is the #1 way parallel work quietly breaks something else — this is checked first, every time.
2. **ADR compliance.** Does the implementation satisfy every ADR tagged to this component — not partially?
3. **Diagram fidelity.** Does the control flow actually match fig. 1 / fig. 2 for this component, or was a branch quietly simplified away?
4. **Cross-cutting wiring.** Are 15–18 actually called, not stubbed or bypassed?
5. **Failure-mode coverage.** Does every known-known in the component's knowns/unknowns grid have a real, passing test?
6. **Cross-component contradiction check.** Does this component's assumptions about any other component's interface or behavior match what that other component actually does (or, if not yet built, what its blueprint stub promises)? This is the check that specifically catches Codex-CLI-built-X assuming something different from Broc-CLI-built-Y.
7. **Draft ADRs.** Were any filed in step 2 of loop.md? If so, they need a real decision before this component is called done, not after.

**Report shape** — one of three outcomes, always in writing:

- **PASS.** Ready to integrate. Logged as a checkpoint.md entry.
- **FAIL.** Specific checklist item(s) failed, with the exact mismatch. Sent back to whichever tool built it.
- **CONTRADICTION.** Two components (possibly built by different tools) disagree about a shared interface or assumption. Neither is unilaterally "wrong" — this becomes a draft ADR that resolves which one is right, same as any other undecided gap.

## What this means for running Codex CLI and Broc CLI side by side

- Both read the same `loop.md`, the same `adr/`, and the same frozen `src/` blueprint. Neither tool gets its own interpretation of either.
- Neither tool merges its own work. Everything lands here for the checklist above before it's treated as integrated — that's what makes "bring it to your master reviewer" a real gate instead of a formality.
- If the two tools are ever assigned overlapping components, whichever one finishes first goes through the checklist and integrates; the second one's version gets reviewed as a potential CONTRADICTION against what's now real, not as a fresh PASS/FAIL against the stub alone.
