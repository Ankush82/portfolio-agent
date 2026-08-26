# Self-Evolving Development Harness
## Literature Review

Grounding for a new concern this project hasn't designed yet: the system that takes an intent/specification, builds against the blueprint, checks its own work from multiple angles, ships, watches what happens in production, and feeds breakage back in as new work — improving at the *process* level over time, not just the *code* level.

This is step 1 of `design-framework.md`'s loop only. No architecture is proposed here, no ADRs are written, and nothing is designed. That's deliberate — this document exists to make sure any later design is answering real, cited questions instead of vibes.

**Naming note:** you asked for this in a file called `blueprint.md`. That name is already taken conceptually — `src/README.md` is the index for the actual code blueprint — so this lives under its own name instead, to avoid two different things both being called "the blueprint." Say if you want it moved/renamed.

---

## The core question

> How does a system get better at building software over successive attempts, without the improvement itself becoming untrustworthy — losing quality, losing the reasons behind its own decisions, or optimizing for the wrong signal?

This single question is the thread through every area below. It's also, near word-for-word, the concern you raised: self-evolving, without compromising quality, without losing *why* things are the way they are.

---

## 1. Harness engineering — the umbrella framing

**Anchor: [Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/) (Weng, 2026)**

This is the single most directly relevant source found, and it uses your own word for the concept unprompted. Its central claim: the practical path to recursive self-improvement isn't a model rewriting its own weights — it's optimizing the **harness**, "the system surrounding a base model that orchestrates execution": workflow design, evaluation, permission controls, and persistent state management. The harness decides "how the model observes, acts, memorizes, checks itself, and improves."

Three structural patterns recur across harness designs:
1. **Workflow automation** — goal-oriented plan → execute → observe → improve loops, with the agent examining its own failure trajectories rather than working from a static prompt.
2. **Filesystem as persistent memory** — logs, diffs, and traces stored durably, not just held in a transient context window, so work survives an interruption.
3. **Sub-agents and backend jobs as explicit, managed processes** — a sub-agent's output has to be written somewhere durable, or it's gone the moment the parent's context is.

The optimization target itself has a progression: instruction prompts → structured context → workflow → harness code → optimizer code (a harness that edits its own harness). This maps directly onto your "G1, G2, G3" framing — it isn't a single system, it's successive generations of what gets optimized.

**Design questions for our architecture:**
- Which layer are we actually trying to improve first — the prompt, the workflow, or the harness code itself?
- Where does this project's persistent memory for the *harness's own* history live — is it the same Memory component (06) already designed for the agent, or a separate concern?
- Do sub-agent outputs in our own orchestration (per `orchestration.md`) already satisfy "explicit, managed process," or are they still transient?

---

## 2. Specification-driven development and agent factories

**Sources:** [MetaGPT](https://arxiv.org/pdf/2308.00352) (Hong et al., 2023) — "Code = SOP(Team)"; [ADAS](https://arxiv.org/pdf/2408.08435)-style meta-agent search (Hu et al., 2025, cited in Weng); [AFlow](https://arxiv.org/pdf/2410.10762) (Zhang et al., 2025); the spec-driven development practice write-up in [ianhxu/agentic-engineering-field-study](https://github.com/ianhxu/agentic-engineering-field-study/blob/main/04-spec-driven-development.md).

MetaGPT is the closest real match to "agent factory": a virtual software team where each agent has a fixed role (requirements analyst, architect, engineer, QA) and a Standard Operating Procedure constrains what each role can hand to the next. The SOP itself — not the model's raw ability — is what MetaGPT credits for its result quality; role-appropriate intermediate artifacts (requirements docs, design docs, interface specs) are checked before code generation even starts.

ADAS and AFlow push one level further: instead of a human authoring the workflow, a meta-agent searches over possible agent workflows (ADAS emits agent code directly; AFlow treats the workflow as a graph and searches it with Monte Carlo Tree Search). This is the literature's version of "the harness should be able to build the entire thing" from an intent — but note both keep an evaluation signal in the loop to prevent the search from wandering.

Spec-driven development, separately, treats the written specification — not the code — as the primary, versioned artifact; code becomes a regenerable output of the spec rather than the thing under direct edit.

**Design questions for our architecture:**
- Is our "contract" (spec → blueprint → scaffold) closer to MetaGPT's fixed SOP, or to ADAS/AFlow's searched workflow? These have very different quality/predictability tradeoffs.
- What plays the role of MetaGPT's intermediate artifacts in our project — is it the ADRs and fig. 1/2 designs we already produce, or does the harness need its own?
- If a meta-agent is allowed to search over workflows, what stops it from designing around our existing Anti-Slop rules rather than through them?

---

## 3. Self-recursive / evolutionary improvement — the "generations" concept

**Sources:** [Darwin Gödel Machine](https://sakana.ai/dgm/) (Zhang et al., 2025); [AlphaEvolve](https://arxiv.org/abs/2506.13131) (Novikov et al., 2025); [STOP](https://arxiv.org/abs/2310.02304) (Self-Taught Optimizer, Zelikman et al., 2023); [SICA](https://arxiv.org/pdf/2504.15228) (Self-Improving Coding Agent, Robeyns et al., 2025); [ReVeal](https://arxiv.org/pdf/2506) (self-evolving code agents via self-verification, NeurIPS 2025); the [Red Queen Gödel Machine](https://arxiv.org/pdf/2606.26294) (co-evolving agents and their evaluators, 2026); [A Survey of Self-Evolving Agents](https://arxiv.org/pdf/2507.21046) (2025) as the field-level overview.

This is the actual research family behind "G1, G2, G3." Darwin Gödel Machine keeps an **archive** of agent versions rather than a single lineage, letting multiple evolutionary branches run in parallel and be revisited — explicitly framed as avoiding getting trapped in one suboptimal design. It edits its own orchestration code (not the underlying model weights) and reports doubling coding benchmark performance this way. AlphaEvolve is the same idea applied to algorithm/code discovery specifically: an evolutionary pool, LLM-proposed diffs, and a fitness function that has to be well-specified for the search to mean anything.

STOP is the earliest and most literal match to "self-recursive": an improver improves itself, recursively, bounded by a meta-utility function it's also optimizing against — the paper is explicit that this needs a genuine, hard-to-game utility signal or the recursion just amplifies whatever the signal actually rewards.

The Red Queen Gödel Machine's contribution matters directly for your concern about not degrading quality: it co-evolves the agent **and its evaluator together**, because a static evaluator gets gamed by an evolving agent over enough generations — this is the formal version of "if only one side gets smarter, the grading stops meaning anything."

**Design questions for our architecture:**
- What is our fitness/utility signal, concretely — and can it be gamed the way a static evaluator can?
- Do we keep an archive of harness versions (Darwin Gödel Machine's approach), or a single evolving lineage? An archive is more expensive but recoverable if a generation regresses.
- If the evaluator has to evolve alongside the builder (Red Queen), what evolves *our* QA process, and who reviews that?

---

## 4. Multi-perspective QA and verification

**Sources:** Agentic Harness Engineering's three observability pillars — component, experience, decision (Lin et al., 2026, cited in Weng); [FullStack-Agent](https://arxiv.org/pdf/2602.03798) (development-oriented testing + repository back-translation, 2026); [WebTestBench](https://arxiv.org/pdf/2603.25226) (end-to-end automated web/browser testing via computer-use agents, 2026); [SWR-Bench](https://arxiv.org/pdf/2509.01494) (real-world code review comment generation); multi-agent code review survey work in the [Multi-Vocal Literature Review](https://arxiv.org/pdf/2604.16321) (2026).

Your "developer, agent decoding, browser QA" split has a real precedent in AHE's three pillars, though they're framed slightly differently: **component-level** (does this piece work in isolation, closest to your "developer" lens), **experience-level** (does the actual interaction/output look right from outside, closest to "browser QA"), and **decision-level** (was the *reasoning* that produced this defensible, not just the output — closest to "agent decoding," i.e. did the agent's own trace make sense). The field's general finding is that these three genuinely catch different failure classes; collapsing them into one LLM-as-judge pass misses defects a specialized pass would have caught, particularly reasoning-level defects that produce a correct-looking output for the wrong reason.

FullStack-Agent specifically develops "development-oriented testing" — tests written to mirror how a human developer would actually validate the change, not generic assertions — and reports this catches more real defects than standard unit-test generation. WebTestBench is the concrete literature for the "browser QA" leg: computer-use agents driving an actual browser session end to end, evaluated against how well that matches real user-facing behavior.

**Design questions for our architecture:**
- Does "decision-level" QA (was the reasoning defensible) map onto anything we already have? This is close to what an ADR's "Alternatives considered" section is *for* — worth checking whether that's already doing this job informally.
- Do the three QA perspectives run as three separate passes with three separate pass/fail gates, or as one combined review the way our current master-reviewer checklist does?
- What corpus of "how a human would actually validate this" exists for our project, the way FullStack-Agent's development-oriented tests do?

---

## 5. Context, state, and graph engineering

**Sources:** Agentic Context Engineering (ACE) and Meta Context Engineering (MCE), both cited in Weng — ACE's Generator-Reflector-Curator loop treats context as an evolving, deduplicated playbook rather than an ever-growing transcript; MCE separates the *mechanism* for managing context from the *content* inside it, optimizing them separately. [CodexGraph](https://arxiv.org/pdf/2408.03910) (code graph databases bridging LLMs and repositories, 2024); [Codebase-Memory](https://arxiv.org/pdf/2603.27277) (Tree-sitter-based knowledge graphs via MCP, 2026) — reports roughly 10x lower token use and 2.1x fewer tool calls across 31 repositories versus file-by-file exploration; [LARGER](https://arxiv.org/pdf/2605.16352) (lexically anchored repository graph retrieval, 2026).

The shared finding across this group: an agent reading a large codebase file-by-file is "contextually blind by default" (a phrase used almost verbatim across several of these papers) and has to rebuild its understanding of the repository's structure on every task. A graph index — call chains, dependency edges, entity relationships — answers structural questions ("what calls this," "what does this depend on") that vector similarity search alone cannot, and the Codebase-Memory result suggests this isn't a marginal optimization; it changes the cost/latency/accuracy profile of every agent action, not just retrieval quality.

This is directly relevant to us specifically: our own architecture already treats Knowledge & Entity Model (04) and Retrieval & Context (05) as separate concerns for the *financial* agent's reasoning. The literature here is describing the same distinction — entity/relationship graph vs. retrieval — applied to the *harness's own* understanding of its codebase.

**Design questions for our architecture:**
- Is the harness's index of our own codebase (18 components, 37 ports/adapters, `checkpoint.md`, `adr/`) a genuinely separate concern from components 04/05, or literally the same mechanism pointed at our own repo instead of the user's portfolio domain?
- ACE's "evolving playbook" implies old, superseded context gets pruned or merged, not just appended — does that conflict with `checkpoint.md`'s current append-only convention, or complement it?

---

## 6. Deployment, monitoring, and the closed loop

**Sources:** Continual Harness (Karten et al., 2026, cited in Weng) — long-horizon policy distillation across repeated gameplay-style tasks; SIA (Hebbar et al., 2026, cited in Weng) — a Feedback-Agent that decides, per failure, whether the fix belongs in the harness or in the model itself.

This is the "deploy to prod, monitor, see if there's breakage, put it back in the queue" part of what you described, and it's the least novel part of the literature relative to what this project has already designed: it's a closed loop over Observability (16) and Reliability & Resilience (15), which already exist as designed components with real ADRs (0015–0018). The one genuinely new idea from this literature is SIA's routing decision — a real failure needs an explicit decision about *where* the fix belongs (workflow? harness code? the underlying model/prompt?), not just "retry" or "escalate," which is one level more specific than what Reliability & Resilience currently decides (ADR-0015 only routes transient-vs-loop, not fix-location).

**Design questions for our architecture:**
- Does a production breakage route back to a specific component's design (a new/amended ADR), the harness's own build process, or both — and who decides which?
- Is "back to the queue" the same queue as new feature work (competing for priority) or a separate, higher-priority lane?

---

## 7. What the literature warns about — directly relevant to "without compromising quality"

Weng's post names seven concrete bottlenecks to real recursive self-improvement; five of them are precisely the risk you flagged before any of this research happened:

1. **Weak evaluators.** Taste, novelty, and "is this actually good architecture" have no precise verifier — the same problem your Anti-Slop rules exist to guard against by hand.
2. **Negative-result bias.** Training/example data skews toward successes, so a system has fewer real examples to learn "why this failed" from than "why this worked."
3. **Diversity collapse.** Evolutionary loops tend to exploit whatever pattern already scores well, homogenizing solutions over generations rather than genuinely improving — this is a formal name for "everything starts looking like AI slop after enough self-editing."
4. **Reward hacking.** Optimizing whatever signal is actually measured, not the real goal behind it — the precise mechanism by which a self-improving system loses track of "why."
5. **Long-horizon blindness.** Sandbox/benchmark training captures short-horizon success and misses maintainability, ownership, and compatibility — the things that matter after the harness has moved on to the next task.

The other two are about human role and long-term success more broadly, and Weng's own conclusion is explicit: systems should give humans oversight at the *right level of abstraction*, not remove them.

**The connection worth naming directly:** this project's existing discipline — every architectural decision gets an ADR stating context, alternatives, and consequences; nothing gets decided unilaterally; Anti-Slop rules constrain what "improvement" is even allowed to look like — is not incidental to this question. It's already a partial, hand-built answer to bottlenecks 1, 3, and 4 above. Any harness design that comes out of this review should be evaluated against whether it **preserves** that discipline under automation, or quietly routes around it because automation is faster without it.

---

## Comparative view

```text
Intent / specification
        │
        ▼
  Agent factory ── SOP or searched workflow (§2)
        │              (MetaGPT / ADAS / AFlow)
        ▼
  Build against blueprint ── context & graph engineering (§5)
        │                        (ACE / MCE / code graphs)
        ▼
  Multi-perspective QA (§4) ── component / experience / decision
        │
        ▼
  Deploy ── monitor ── breakage detected (§6)
        │                  (Observability 16, Reliability 15 — already designed)
        ▼
  Fed back as new work ── routed: harness fix? model fix? new ADR? (§6)
        │
        ▼
  Harness itself updated ── generations / archive (§3)
        (Darwin Gödel Machine / STOP / Red Queen)
        │
        └──────────────► back to Agent factory
```

Every arrow in this loop is also a place bottlenecks 1–5 (§7) can enter unnoticed — that's the honest shape of the problem, not a solved one.

---

## What this review deliberately does not do

- No technology is chosen. No harness architecture is proposed.
- No ADRs were written — nothing here has been decided, only grounded.
- The "G1/G2/G3" framing, "agent factory," and "three-perspective QA" are your own terms; this review maps them onto named literature so the next design pass has real tradeoffs to surface (per `loop.md` step 3), not so this document can quietly decide for you which one is right.

Next step, when you're ready: this becomes the "ground it" input for a new entry in the design-framework loop — most likely its own new component (a 19th concern: the build harness itself, distinct from the 18 that make up the portfolio agent), or an extension of Agent Runtime's own design. That choice is exactly the kind of thing this project's process says gets asked, not assumed.
