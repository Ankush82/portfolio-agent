# Roadmap

`roadmap.csv` is the import-ready version (Jira, Linear, Trello — any tool that takes a CSV of issues with an epic link and a dependency field). This file is what the columns mean and why the grouping is what it is.

## Two different orderings, don't conflate them

- **Coding can start on all 18 components right now, in parallel.** The blueprint (`src/`) is frozen — every component's public interface already exists as a stub, so nothing needs another component's *real* implementation to begin. Only `infrastructure.py` and `cross_cutting/*` needing an actual signature change would block anyone, and that goes through loop.md step 2, not a silent edit.
- **Integration order still follows the dependency graph.** A component can be *coded* against a stub today and *merged* only once what it actually depends on is real. The groups below (A through G) are integration order, not a restriction on when coding starts.

## Design readiness — where to expect fewer stalls

Four components (05, 06, 09, 10) already have a completed low-level design: every tradeoff is resolved in an ADR, every branch is drawn in a fig. 1 / fig. 2 diagram. loop.md step 2 ("check for gaps") should rarely fire on these — there isn't much left undecided.

The other fourteen are whiteboard-level only: sub-components and capabilities exist, but no mechanism was designed and no tradeoff was surfaced. Expect loop.md step 2 to fire more often here — that's not a problem with the subagent, it's the honest cost of starting from less design. If you want fast, low-friction wins first, start Codex CLI / Broc CLI on 05, 06, 09, 10.

## Integration groups

| Group | Components | Design readiness | Integrates after |
|---|---|---|---|
| A | 15, 16, 17, 18, 01, 11, **10**, **06** | 10 &amp; 06 fully designed; rest whiteboard-only | Blueprint freeze only |
| B | 02 → 03 → 04 | whiteboard-only, internally sequential | Blueprint freeze only |
| C | 07, **05** | 05 fully designed | Group B real |
| D | 08, **09** | 09 fully designed | Group C real, Memory (06) real |
| E | 12 | whiteboard-only | Group D real |
| F | 13 | whiteboard-only | Group E real |
| G | 14 | whiteboard-only, **and the "is this in scope" question is still open** | Group F real |

Each group's row in `roadmap.csv` ends with a `REVIEW-<group>` task — the master-review checklist from `orchestration.md`, gating that group's integration before the next one starts.

## What isn't on the roadmap yet

- Memory's Mem0-vs-Supermemory pick (ADR-0010) — needs to happen before Group A's component 06 story is actually mergeable, even though it can be coded against the stub now.
- Security & Privacy's authority-check granularity — surfaced during Phase 0 research, never asked, not blocking any group yet but will need resolving before component 17 is called done.
- Learning & Evaluation's scope question (Group G).

None of these are silently assumed in the CSV — each shows up as a blocking task on its group, not a resolved dependency.
