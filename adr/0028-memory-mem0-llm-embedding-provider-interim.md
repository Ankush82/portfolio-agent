# 0028 — Memory: Mem0 LLM/embedding provider interim, deferred to DefaultInfrastructure

**Status:** Superseded by [ADR-0045](0045-memory-mem0-embedding-resolved-fastembed.md) — 2026-08-26. The embedding-similarity half of the gap named below is resolved: `Mem0EntityLinker` (`src/components/c06_memory.py`), real cosine similarity via mem0ai's local `fastembed` embedder, no API key or account needed. The LLM-driven extraction/dedup half remains open, named as real future work in ADR-0045's own Consequences.
**Component:** Memory (06)

## Context

ADR-0010's Consequences (amended in this same pass) record the outcome of actually checking Mem0 (`mem0ai` 2.0.19) against the four Memory design decisions: it does not natively fit structural partition (ADR-0008), quarantine-at-write (ADR-0007), or A-MEM-style explicit linking (ADR-0006), and it is architecturally passive, not a fit for active working-set management (ADR-0005) either. All four ended up implemented directly against `DefaultInfrastructure` instead.

The one real capability Mem0 would have added on top of that — `Memory.add(..., infer=True)`, which uses an LLM to extract facts from raw input and decide whether to ADD/UPDATE/DELETE against existing memories, and `search()`'s own embedding-based similarity — needs a model provider. `add()`'s default LLM is OpenAI; it is configurable to other providers (Anthropic included, via `mem0/llms/anthropic.py`), but every configuration needs some provider's API key. Embeddings have the same shape: OpenAI by default, or a local/offline embedder (`fastembed`) that avoids the API key but needs an extra dependency and a one-time model download, not a zero-config path either. This project has never chosen an LLM provider for *any* component — ADR-0021 names the identical gap for Agent Runtime's own reasoning step, and no ADR, `checkpoint.md` entry, or `Implementation Plan` line has settled it since.

This is a genuine external-credential gap, not a design gap Memory's own ADRs left open: nothing in ADR-0005 through ADR-0008 requires an LLM, and the four `Default*` adapters implemented in this pass (`DefaultMemoryEvaluator`, `DefaultEntityLinker`, `DefaultQuarantineGate`, `DefaultScopeRouter`, `DefaultMemoryManager`, `DefaultMemoryConsolidator`) are all real, structural, threshold-based logic that runs today without any provider. Mem0's LLM-backed extraction is additive polish on top of that, not something any of the four decisions depends on.

## Decision

Do not wire Mem0 into Memory's real write/read path in this pass. `mem0ai` was installed and inspected for the ADR-0010 research (`uv pip install mem0ai`, not `uv add`) but is not a `pyproject.toml` dependency, since nothing in this implementation calls it. The `DefaultInfrastructure`-backed adapters (ADR-0010's amended Consequences) are the real default path, and they do not depend on this ADR being resolved — Memory works end to end without Mem0 today.

If and when an LLM/embedding provider is chosen (by the user, the same way ADR-0021 is waiting on that choice for Agent Runtime), Mem0 integration should be added behind its own injectable interface — most naturally as an additional, optional collaborator `DefaultEntityLinker` or a new `DefaultMemoryEvaluator` variant could call for semantic (not just token-overlap) relatedness and confidence scoring, without changing either Protocol's signature.

## Alternatives considered

- **Configure Mem0 with the local/offline `fastembed` embedder and `infer=False` (raw storage, no LLM extraction) right now**, avoiding any API key. Rejected for this pass: `infer=False` reduces Mem0 to "store this string," which is exactly the capability `DefaultInfrastructure.store` already provides directly — pulling in Mem0, `fastembed`, and a model download for a mode that does not use Mem0's actual differentiator is a real dependency added for no functional gain over what was already built.
- **Pick an LLM provider now, inside this implementation pass**, so `infer=True` extraction could be wired in for real. Rejected per `loop.md` step 2 and the same reasoning ADR-0021 already gives: no ADR or design artifact has settled this anywhere in the project, for any component, and deciding it here — inside one component's implementation — would resolve a project-wide fork by fiat instead of by the user weighing cost, latency, and data-handling tradeoffs across every component that will eventually need it.
- **Ship a mocked/fake LLM call so `infer=True` "works" without a real key.** Rejected as actively misleading: it would make Mem0 appear to be doing real fact-extraction and dedup when nothing is actually reasoning, the same failure mode ADR-0021 explicitly avoided by naming `placeholder_reason_fn` as non-cognitive rather than dressing it up as a working reasoner.

## Consequences

- Memory's four `Default*` adapters implemented in this pass are unaffected by this ADR remaining unresolved — they are the real path, not a stand-in waiting for this decision.
- Mem0's actual value-add (LLM-driven extraction/dedup, semantic similarity search) is not available to Memory yet. `DefaultEntityLinker`'s token-overlap linking and `DefaultMemoryEvaluator`'s confidence-threshold gate are the honest current substitutes — structural, not semantic — and are documented as such in `src/components/c06_memory.py` rather than presented as equivalent.
- This ADR and ADR-0021 name the same underlying gap (no LLM provider chosen anywhere in the project) from two different components. Resolving one does not automatically resolve the other unless the user explicitly decides one provider should serve every component — that question is itself still open (see ADR-0021's Alternatives considered).
- Once a provider is chosen, the fix is narrowly scoped: add `mem0ai` (or the chosen provider's SDK) to `pyproject.toml` for real, configure `Memory(...)`'s LLM/embedder, and wire a new collaborator behind `EntityLinker`/`MemoryEvaluator` — no change needed to `ScopeRouter`, `QuarantineGate`, or `MemoryManager`, since none of those three ever depended on Mem0 in the first place.

## Related

- Depends on: [ADR-0010](0010-memory-technology-purpose-built-layer.md) (Mem0 vendor decision and the per-decision fit check this ADR's gap was found during).
- Extends the same "ask, don't decide" pattern as: [ADR-0021](0021-agent-runtime-llm-provider-interim.md) (identical gap, Agent Runtime's reasoning step — this ADR's direct template), [ADR-0020](0020-security-authorize-interim-default.md), [ADR-0027](0027-data-sources-fetch-provider-interim.md).
- Implemented by: `../src/components/c06_memory.py` — `DefaultMemoryEvaluator`, `DefaultEntityLinker`, `DefaultQuarantineGate`, `DefaultScopeRouter`, `DefaultMemoryManager`, `DefaultMemoryConsolidator` all ship without Mem0; none of them import it.
- Logged narratively in `../checkpoint.md`.
