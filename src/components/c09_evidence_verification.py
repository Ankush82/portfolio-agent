"""Evidence & Verification (component 09) — the trust layer.

Design: Retrieval & Evidence Design, fig. 2 (evidence path)
Decisions: ADR-0013 (mandatory evidence per claim, ALCE), ADR-0014
(contradictory evidence resolved automatically)
"""

from dataclasses import dataclass


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
        raise NotImplementedError


class MandatoryEvidenceGate:
    def has_evidence(self, evidence: list[Evidence]) -> bool:
        """Fig. 2's 'evidence found?' gate (ADR-0013)."""
        raise NotImplementedError

    def block(self, claim: Claim) -> None:
        """Logged, not forwarded to Decision & Policy (component 12)."""
        raise NotImplementedError


class ContradictionResolver:
    def sources_agree(self, evidence: list[Evidence]) -> bool:
        raise NotImplementedError

    def resolve(self, evidence: list[Evidence]) -> Evidence:
        """Weight by source reliability and freshness, pick the
        higher-confidence side (ADR-0014)."""
        raise NotImplementedError


class ClaimVerifier:
    def verify(self, claim: Claim, evidence: list[Evidence]) -> VerifiedClaim:
        """Citation quality and completeness (ALCE)."""
        raise NotImplementedError

    def score_confidence(self, verified: VerifiedClaim) -> float:
        raise NotImplementedError
