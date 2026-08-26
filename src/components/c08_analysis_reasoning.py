"""Analysis & Reasoning (component 08) — the interpretation engine.

Design: Component Whiteboards artifact, card 08's fan-in figure —
Event, Observations, Context, Memory, and Evidence converge here.
Decision: ADR-0037 (real mechanism: fan-in orchestration across
components 07/06/05/01, Evidence & Verification (09)-backed hypothesis
testing, and the injected `reason_fn` seam for the genuinely cognitive
methods).

Unlike every other component implemented so far this session, this
component's entire job — `infer`, `generate_hypotheses`,
`generate_explanations`, `generate_counterarguments`,
`synthesize_findings`, and the judgment half of `estimate_impact` — is
inference and synthesis, not orchestration or a lookup. There is no
honest rule-based/statistical substitute for "what does this evidence
suggest" the way ADR-0033/ADR-0036 found real heuristic substitutes for
retrieval sufficiency or anomaly detection, so those methods are built
the same way Agent Runtime's `reason`/`assess_stakes` were: behind an
injected `reason_fn: Callable[[dict], dict]`, with `placeholder_reason_fn`
as the explicitly non-cognitive stand-in. ADR-0021's original
LLM-provider gap (restated here rather than duplicated, per ADR-0037)
is now resolved by ADR-0043
(`adr/0043-llm-provider-resolved-openrouter.md`): `get_reason_fn()`
(`src/llm.py`) is `DefaultAnalysisReasoning`'s real default whenever
`OPENROUTER_API_KEY` is set, falling back to this file's own
`placeholder_reason_fn` otherwise. See ADR-0037 for exactly which
methods are real orchestration and which sit behind the `reason_fn`
seam, and why.
"""

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from components.c01_user_portfolio import DefaultUserPortfolio, UserPortfolio
from components.c05_retrieval_context import ContextBuilder, DefaultContextBuilder, Query
from components.c06_memory import DefaultMemoryManager, DefaultScopeRouter, MemoryManager
from components.c07_event_observation import DefaultEventObservation, EventObservation
from components.c09_evidence_verification import (
    Claim,
    ClaimVerifier,
    DefaultClaimVerifier,
    DefaultContradictionResolver,
    DefaultEvidenceLinker,
    DefaultMandatoryEvidenceGate,
    EvidenceLinker,
    MandatoryEvidenceGate,
)
from cross_cutting.observability import traced
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure
from llm import get_reason_fn


@dataclass
class Hypothesis:
    claim: str
    basis: dict


@dataclass
class Analysis:
    hypotheses: list[Hypothesis]
    impact_estimate: dict | None = None


class AnalysisReasoning(Protocol):
    def analyze(self, event: dict, context: dict, memory: dict) -> Analysis:
        """Converges Event, Observations, Context, Memory, and Evidence
        into one step — see the Component Whiteboards fan-in figure."""
        ...

    def compare(self, a: dict, b: dict) -> dict:
        ...

    def infer(self, premises: list[dict]) -> Hypothesis:
        ...

    def generate_hypotheses(self, analysis_input: dict) -> list[Hypothesis]:
        ...

    def test_hypotheses(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        ...

    def estimate_impact(self, hypothesis: Hypothesis) -> dict:
        ...

    def generate_explanations(self, hypothesis: Hypothesis) -> str:
        ...

    def generate_counterarguments(self, hypothesis: Hypothesis) -> list[str]:
        ...

    def synthesize_findings(self, hypotheses: list[Hypothesis]) -> Analysis:
        ...


class StubAnalysisReasoning:
    """Structural implementation of AnalysisReasoning. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def analyze(self, event: dict, context: dict, memory: dict) -> Analysis:
        with traced("StubAnalysisReasoning.analyze"):
            return Analysis(hypotheses=[], impact_estimate=None)

    def compare(self, a: dict, b: dict) -> dict:
        with traced("StubAnalysisReasoning.compare"):
            return {}

    def infer(self, premises: list[dict]) -> Hypothesis:
        with traced("StubAnalysisReasoning.infer"):
            return Hypothesis(claim="stub", basis={})

    def generate_hypotheses(self, analysis_input: dict) -> list[Hypothesis]:
        with traced("StubAnalysisReasoning.generate_hypotheses"):
            return []

    def test_hypotheses(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        with traced("StubAnalysisReasoning.test_hypotheses"):
            return []

    def estimate_impact(self, hypothesis: Hypothesis) -> dict:
        with traced("StubAnalysisReasoning.estimate_impact"):
            return {}

    def generate_explanations(self, hypothesis: Hypothesis) -> str:
        with traced("StubAnalysisReasoning.generate_explanations"):
            return ""

    def generate_counterarguments(self, hypothesis: Hypothesis) -> list[str]:
        with traced("StubAnalysisReasoning.generate_counterarguments"):
            return []

    def synthesize_findings(self, hypotheses: list[Hypothesis]) -> Analysis:
        with traced("StubAnalysisReasoning.synthesize_findings"):
            return Analysis(hypotheses=[], impact_estimate=None)


# --- DefaultAnalysisReasoning: real fan-in orchestration + injected reasoning seam (ADR-0037) ---

_ANALYSES_TABLE = "analyses"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches every other component's own timestamp format

# The one component this class's hypothesis-testing pipeline treats a
# Hypothesis as: Evidence & Verification's Claim shape (`text`,
# `source_component`) is exactly what a Hypothesis's `claim` string
# needs to become to run through DefaultEvidenceLinker/
# DefaultClaimVerifier/DefaultMandatoryEvidenceGate for real (ADR-0037).
_HYPOTHESIS_CLAIM_SOURCE_COMPONENT = "Analysis & Reasoning"


def placeholder_reason_fn(request: dict) -> dict:
    """Explicitly NOT real cognition — see ADR-0037
    (`adr/0037-analysis-reasoning-real-mechanism-and-reasoning-seam.md`)
    and the still-open gap it references, ADR-0021
    (`adr/0021-agent-runtime-llm-provider-interim.md`). No LLM provider
    has been chosen anywhere in this project, for this or any other
    component. This exists purely so `DefaultAnalysisReasoning`'s
    genuinely cognitive methods are constructible, callable, and
    testable end to end without one.

    It does not look at any premise, analysis input, or hypothesis
    content it's handed — every phase deterministically returns the
    same low-confidence "insufficient basis" answer, or an empty
    collection for a method that returns one, per phase:

      "infer"                    -> a single low-confidence Hypothesis
                                     ("insufficient basis to hypothesize")
      "generate_hypotheses"      -> no hypotheses (empty list)
      "generate_explanations"    -> an empty explanation string
      "generate_counterarguments" -> no counterarguments (empty list)
      "synthesize_findings"      -> no hypotheses, no impact estimate
      "estimate_impact"          -> "unknown" significance, with a
                                     rationale naming the same gap

    Real inference, hypothesis generation, explanation, counter-
    argument generation, synthesis, and impact judgment all require a
    real `reason_fn` implementation behind this same interface."""
    phase = request.get("phase")
    if phase == "infer":
        return {
            "claim": "insufficient basis to hypothesize",
            "basis": {"confidence": "low", "placeholder": True},
        }
    if phase == "generate_hypotheses":
        return {"hypotheses": []}
    if phase == "generate_explanations":
        return {"explanation": ""}
    if phase == "generate_counterarguments":
        return {"counterarguments": []}
    if phase == "synthesize_findings":
        return {"hypotheses": [], "impact_estimate": None}
    if phase == "estimate_impact":
        return {
            "significance": "unknown",
            "rationale": "insufficient basis to assess significance — no reasoning backend yet, see ADR-0021/ADR-0037",
        }
    raise ValueError(f"placeholder_reason_fn: unknown phase {phase!r}")


class DefaultAnalysisReasoning:
    """Real implementation of AnalysisReasoning (ADR-0037).

    Two kinds of methods, deliberately not treated the same way:

    Real orchestration, no `reason_fn` involved — `compare` (a genuine
    structural diff over two given dicts) and `test_hypotheses` (a
    real, complete hand-off of each hypothesis to Evidence &
    Verification's real `EvidenceLinker`/`ClaimVerifier`/
    `MandatoryEvidenceGate`, converting `Hypothesis` into `Claim` — the
    shapes match once `claim.text = hypothesis.claim`). `analyze`'s own
    input-gathering (`_gather_analysis_input`) is also real: it calls
    Event & Observation's `retrieve_events`, Memory's `retrieve`,
    Retrieval & Context's `ContextBuilder.construct`, and — when the
    caller supplies a `PortfolioSnapshot` — User & Portfolio's
    `calculate_exposure`, for real, over real inputs.

    Genuinely cognitive methods — `infer`, `generate_hypotheses`,
    `generate_explanations`, `generate_counterarguments`,
    `synthesize_findings`, and the judgment half of `estimate_impact`
    — call through the injected `reason_fn`: the real OpenRouter-backed
    function (`get_reason_fn`, `src/llm.py`) whenever
    `OPENROUTER_API_KEY` is set, `placeholder_reason_fn` otherwise (see
    ADR-0037 and ADR-0043). `estimate_impact`'s other half (actual
    portfolio exposure to the entity a hypothesis's `basis` references)
    is real, computed via
    `UserPortfolio.calculate_exposure` before `reason_fn` is ever
    called, so a caller gets the real number even while the
    significance judgment next to it is honestly a placeholder.

    `analyze()` composes both kinds into the one fan-in step the
    `AnalysisReasoning` Protocol's own docstring describes: gather real
    inputs -> generate_hypotheses (cognitive) -> test_hypotheses (real)
    -> synthesize_findings (cognitive), which also persists the
    resulting `Analysis` via `Infrastructure` (`DefaultInfrastructure`
    by default) — the durable output of this pipeline, per ADR-0019's
    interface boundary.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        event_observation: EventObservation | None = None,
        memory_manager: MemoryManager | None = None,
        context_builder: ContextBuilder | None = None,
        user_portfolio: UserPortfolio | None = None,
        evidence_linker: EvidenceLinker | None = None,
        claim_verifier: ClaimVerifier | None = None,
        mandatory_evidence_gate: MandatoryEvidenceGate | None = None,
        reason_fn: Callable[[dict], dict] | None = None,
        memory_scopes: tuple[str, ...] = ("user", "shared"),
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._event_observation = event_observation or DefaultEventObservation(infrastructure=self._infrastructure)
        self._memory_manager = memory_manager or DefaultMemoryManager(
            infrastructure=self._infrastructure, scope_router=DefaultScopeRouter()
        )
        self._context_builder = context_builder or DefaultContextBuilder()
        self._user_portfolio = user_portfolio or DefaultUserPortfolio(infrastructure=self._infrastructure)
        self._evidence_linker = evidence_linker or DefaultEvidenceLinker(memory_manager=self._memory_manager)
        self._claim_verifier = claim_verifier or DefaultClaimVerifier(
            contradiction_resolver=DefaultContradictionResolver()
        )
        self._mandatory_evidence_gate = mandatory_evidence_gate or DefaultMandatoryEvidenceGate()
        # Unspecified (None) resolves via get_reason_fn (src/llm.py) at
        # construction time: the real OpenRouter-backed reason_fn when
        # OPENROUTER_API_KEY is set, this file's own placeholder_reason_fn
        # otherwise (ADR-0043). Passing reason_fn explicitly — as every
        # "default placeholder" test in
        # tests/components/test_analysis_reasoning.py now does — always
        # wins, regardless of environment.
        self._reason_fn = reason_fn or get_reason_fn(placeholder_reason_fn)
        self._memory_scopes = memory_scopes

    def _gather_analysis_input(self, event: dict, context: dict, memory: dict) -> dict:
        """Real, mechanical assembly of what an analysis needs — no
        cognition, just calling the four converging components for
        real and structuring what comes back (ADR-0037)."""
        entity_ids = list(event.get("entity_ids") or ([event["entity_id"]] if event.get("entity_id") else []))
        correlated_events = []
        seen_event_ids: set[str] = set()
        for entity_id in entity_ids:
            for found in self._event_observation.retrieve_events({"entity_id": entity_id}):
                if found.id in seen_event_ids:
                    continue
                seen_event_ids.add(found.id)
                correlated_events.append(found)

        memories = []
        for scope in self._memory_scopes:
            memories.extend(self._memory_manager.retrieve(memory, scope))

        documents = context.get("documents", [])
        query = Query(text=context.get("query_text", ""), context=context)
        context_pack = self._context_builder.construct(documents, query=query)

        portfolio_snapshot = context.get("portfolio_snapshot")
        exposure = (
            self._user_portfolio.calculate_exposure(portfolio_snapshot) if portfolio_snapshot is not None else {}
        )

        return {
            "event": event,
            "correlated_events": correlated_events,
            "memories": memories,
            "context_pack": context_pack,
            "exposure": exposure,
        }

    def analyze(self, event: dict, context: dict, memory: dict) -> Analysis:
        with traced("DefaultAnalysisReasoning.analyze"):
            analysis_input = self._gather_analysis_input(event, context, memory)
            hypotheses = self.generate_hypotheses(analysis_input)
            tested_hypotheses = self.test_hypotheses(hypotheses)
            return self.synthesize_findings(tested_hypotheses)

    def compare(self, a: dict, b: dict) -> dict:
        """Real structural diff — no cognition needed to compare two
        given dicts (ADR-0037). `only_in_a`/`only_in_b` are keys unique
        to one side; `changed` holds every shared key whose values
        differ, as `{"a": ..., "b": ...}`."""
        with traced("DefaultAnalysisReasoning.compare"):
            keys_a, keys_b = set(a), set(b)
            only_in_a = {key: a[key] for key in keys_a - keys_b}
            only_in_b = {key: b[key] for key in keys_b - keys_a}
            changed = {key: {"a": a[key], "b": b[key]} for key in keys_a & keys_b if a[key] != b[key]}
            return {"only_in_a": only_in_a, "only_in_b": only_in_b, "changed": changed}

    def infer(self, premises: list[dict]) -> Hypothesis:
        with traced("DefaultAnalysisReasoning.infer"):
            output = self._reason_fn({"phase": "infer", "premises": premises})
            return Hypothesis(claim=output.get("claim", ""), basis=dict(output.get("basis", {})))

    def generate_hypotheses(self, analysis_input: dict) -> list[Hypothesis]:
        with traced("DefaultAnalysisReasoning.generate_hypotheses"):
            output = self._reason_fn({"phase": "generate_hypotheses", "analysis_input": analysis_input})
            return [
                Hypothesis(claim=item.get("claim", ""), basis=dict(item.get("basis", {})))
                for item in output.get("hypotheses", [])
            ]

    def test_hypotheses(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """Real hand-off to Evidence & Verification (09) — no
        placeholder. Each hypothesis becomes a `Claim`
        (`text=hypothesis.claim`), linked against real evidence via
        `EvidenceLinker.link`, and gated by
        `MandatoryEvidenceGate.has_evidence` (ADR-0013): a hypothesis
        with no linkable evidence is blocked (`gate.block`, logged, not
        silently passed) and dropped from the returned list — the same
        "logged, not forwarded" rule ADR-0013 applies to any other
        claim. A hypothesis that clears the gate is verified for real
        via `ClaimVerifier.verify`, and its `basis` is returned
        extended with the verification result (`verified`,
        `confidence`, `evidence_count`, `was_contradictory`) rather
        than replaced, so whatever the hypothesis already carried
        (e.g. from `generate_hypotheses`) survives alongside it."""
        with traced("DefaultAnalysisReasoning.test_hypotheses"):
            tested: list[Hypothesis] = []
            for hypothesis in hypotheses:
                claim = Claim(text=hypothesis.claim, source_component=_HYPOTHESIS_CLAIM_SOURCE_COMPONENT)
                evidence = self._evidence_linker.link(claim)
                if not self._mandatory_evidence_gate.has_evidence(evidence):
                    self._mandatory_evidence_gate.block(claim)
                    continue
                verified = self._claim_verifier.verify(claim, evidence)
                updated_basis = dict(hypothesis.basis)
                updated_basis.update(
                    {
                        "verified": True,
                        "confidence": verified.confidence,
                        "evidence_count": len(verified.evidence),
                        "was_contradictory": verified.was_contradictory,
                    }
                )
                tested.append(Hypothesis(claim=hypothesis.claim, basis=updated_basis))
            return tested

    def estimate_impact(self, hypothesis: Hypothesis) -> dict:
        """Half real, half behind `reason_fn` (ADR-0037) — never forced
        entirely behind the placeholder just because part of the
        answer needs judgment. The real half: actual portfolio exposure
        to whatever entity `hypothesis.basis` references
        (`basis["entity_id"]`), computed via
        `UserPortfolio.calculate_exposure` against a
        `basis["portfolio_snapshot"]` the caller attached — real, not
        guessed, whenever a snapshot is available; `{}` when it isn't,
        since there is nothing real to compute against. The judgment
        half — how significant the underlying event actually is — goes
        through `reason_fn`, merged into the same returned dict."""
        with traced("DefaultAnalysisReasoning.estimate_impact"):
            entity_id = hypothesis.basis.get("entity_id")
            portfolio_snapshot = hypothesis.basis.get("portfolio_snapshot")
            if portfolio_snapshot is not None:
                full_exposure = self._user_portfolio.calculate_exposure(portfolio_snapshot)
                exposure = (
                    full_exposure.get(entity_id, {"market_value": 0.0, "weight": 0.0})
                    if entity_id is not None
                    else full_exposure
                )
            else:
                exposure = {}
            judgment = self._reason_fn({"phase": "estimate_impact", "hypothesis": hypothesis, "exposure": exposure})
            return {"exposure": exposure, **judgment}

    def generate_explanations(self, hypothesis: Hypothesis) -> str:
        with traced("DefaultAnalysisReasoning.generate_explanations"):
            output = self._reason_fn({"phase": "generate_explanations", "hypothesis": hypothesis})
            return output.get("explanation", "")

    def generate_counterarguments(self, hypothesis: Hypothesis) -> list[str]:
        with traced("DefaultAnalysisReasoning.generate_counterarguments"):
            output = self._reason_fn({"phase": "generate_counterarguments", "hypothesis": hypothesis})
            return list(output.get("counterarguments", []))

    def synthesize_findings(self, hypotheses: list[Hypothesis]) -> Analysis:
        """Cognitive selection/summarization behind `reason_fn`
        (ADR-0037), but the resulting `Analysis` is persisted for real
        via `Infrastructure` (`DefaultInfrastructure` by default) —
        this pipeline's durable output, per ADR-0019's interface
        boundary. Persisting only the final `Analysis` (not every
        intermediate `Hypothesis` generated/tested along the way)
        mirrors `DefaultRetriever`'s own choice to log at its own
        real-output boundary rather than every intermediate step."""
        with traced("DefaultAnalysisReasoning.synthesize_findings"):
            output = self._reason_fn({"phase": "synthesize_findings", "hypotheses": hypotheses})
            synthesized_hypotheses = list(output.get("hypotheses", []))
            analysis = Analysis(hypotheses=synthesized_hypotheses, impact_estimate=output.get("impact_estimate"))
            self._infrastructure.store(
                _ANALYSES_TABLE,
                {
                    "id": f"analysis-{uuid.uuid4()}",
                    "hypotheses": [asdict(h) for h in synthesized_hypotheses],
                    "impact_estimate": analysis.impact_estimate,
                    "synthesized_at": time.strftime(_TIMESTAMP_FORMAT),
                },
            )
            return analysis
