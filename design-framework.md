# Component Design Framework

The repeatable process for taking each of the 18 components (`Thoughts.md`) from name to confirmed low-level design. This is what "Agent Runtime" already went through; every other component goes through the same five steps, in order, before it counts as designed.

Not code. This framework governs the design phase only — technology selection stays deferred until a component has been through all five steps (see `portfolio_ai_three_literature_reviews.md`'s own closing note).

```
FOR EACH component IN [Component 1 … Component 18]:

  1. GROUND IT
     Literature review or engineering discussion. Cite the source.
     → e.g. portfolio_ai_three_literature_reviews.md for the 3 components it covers.
     → for components with no literature review, an engineering discussion stands in.

  2. ABSTRACTIONS
     Sub-components, capabilities, core objects, interfaces, candidate tooling.
     → the level already done for all 18 in the "Component Whiteboards" artifact.

  3. LOW-LEVEL DESIGN
     Trace the actual mechanism / trajectory the component runs.
     Wherever step 1's material surfaces a real tradeoff: surface it as a
     question, get an answer, do not decide unilaterally.

  4. DEFINITION OF DONE
     a. Failure modes, sorted with the knowns/unknowns framework:
          known-known     — we can name it and how it fails
          known-unknown   — we know the risk, not yet its shape or rate
          unknown-known   — an assumption the design leans on, unstated until now
          unknown-unknown — can't be named yet; note a detection strategy instead
     b. Design a response for each named failure mode:
          known-known / known-unknown → a designed mitigation
          unknown-known                → state the assumption explicitly
          unknown-unknown               → a detection strategy (e.g. Observability
                                           & Governance watching for drift), not a fix
     c. IF an assumption or tradeoff can't be resolved by reasoning alone:
          → experiments needed, run them
        ELSE:
          → finalize the approach

  5. PUBLISH
     → a design page (tldraw-style whiteboard artifact) covering steps 1–4
     → a checkpoint.md entry: each decision, and the reason behind it

END FOR EACH
```

## Status against this framework

| Component | 1. Ground | 2. Abstractions | 3. Low-level design | 4. Definition of done | 5. Published |
|---|---|---|---|---|---|
| Agent Runtime (10) | done | done | done | done | done |
| Memory (06) | available | done | — | — | — |
| Retrieval & Context (05) + Evidence & Verification (09) | available | done | — | — | — |
| all other 15 components | not yet reviewed | done | — | — | — |

"Abstractions" is done for all 18 already (the component-whiteboards pass predates this framework but satisfies step 2 retroactively). Everything past step 2 is per-component, one at a time, per the earlier agreement not to run more than 3 components through steps 3–5 in this round.
