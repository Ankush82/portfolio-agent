"""Evidence & Verification (component 09) — the trust layer.

Design: Retrieval & Evidence Design, fig. 2 (evidence path)
Decisions: ADR-0013 (mandatory evidence per claim, ALCE), ADR-0014
(contradictory evidence resolved automatically)
"""

from dataclasses import dataclass

from cross_cutting.observability import traced


@dataclass
class Claim:
    text: str
    source_component: str  # e.g. "Analysis & Reasoning"


@dataclass
class Evidence:
    content: dict
    source: str
    reliability: float
    freshness: float


@dataclass
class VerifiedClaim:
    claim: Claim
    evidence: list[Evidence]
    confidence: float
    was_contradictory: bool = False


class EvidenceLinker:
    def link(self, claim: Claim) -> list[Evidence]:
        """Searches Context Pack (component 05) and Memory (06)."""
        with traced("EvidenceLinker.link"):
            return []


class MandatoryEvidenceGate:
    def has_evidence(self, evidence: list[Evidence]) -> bool:
        """Fig. 2's 'evidence found?' gate (ADR-0013)."""
        with traced("MandatoryEvidenceGate.has_evidence"):
            return True

    def block(self, claim: Claim) -> None:
        """Logged, not forwarded to Decision & Policy (component 12)."""
        with traced("MandatoryEvidenceGate.block"):
            return None


class ContradictionResolver:
    def sources_agree(self, evidence: list[Evidence]) -> bool:
        with traced("ContradictionResolver.sources_agree"):
            return True

    def resolve(self, evidence: list[Evidence]) -> Evidence:
        """Weight by source reliability and freshness, pick the
        higher-confidence side (ADR-0014)."""
        with traced("ContradictionResolver.resolve"):
            return Evidence(content={}, source="stub", reliability=0.0, freshness=0.0)


class ClaimVerifier:
    def verify(self, claim: Claim, evidence: list[Evidence]) -> VerifiedClaim:
        """Citation quality and completeness (ALCE)."""
        with traced("ClaimVerifier.verify"):
            return VerifiedClaim(
                claim=Claim(text="stub", source_component="stub"),
                evidence=[],
                confidence=0.0,
            )

    def score_confidence(self, verified: VerifiedClaim) -> float:
        with traced("ClaimVerifier.score_confidence"):
            return 0.0
