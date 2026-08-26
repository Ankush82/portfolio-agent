"""Evidence & Verification (component 09) — the trust layer.

Design: Retrieval & Evidence Design, fig. 2 (evidence path)
Decisions: ADR-0013 (mandatory evidence per claim, ALCE), ADR-0014
(contradictory evidence resolved automatically)
"""

from dataclasses import dataclass
from typing import Protocol

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


class EvidenceLinker(Protocol):
    def link(self, claim: Claim) -> list[Evidence]:
        """Searches Context Pack (component 05) and Memory (06)."""
        ...


class StubEvidenceLinker:
    """Structural implementation of EvidenceLinker. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def link(self, claim: Claim) -> list[Evidence]:
        with traced("StubEvidenceLinker.link"):
            return []


class MandatoryEvidenceGate(Protocol):
    def has_evidence(self, evidence: list[Evidence]) -> bool:
        """Fig. 2's 'evidence found?' gate (ADR-0013)."""
        ...

    def block(self, claim: Claim) -> None:
        """Logged, not forwarded to Decision & Policy (component 12)."""
        ...


class StubMandatoryEvidenceGate:
    """Structural implementation of MandatoryEvidenceGate. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def has_evidence(self, evidence: list[Evidence]) -> bool:
        with traced("StubMandatoryEvidenceGate.has_evidence"):
            return True

    def block(self, claim: Claim) -> None:
        with traced("StubMandatoryEvidenceGate.block"):
            return None


class ContradictionResolver(Protocol):
    def sources_agree(self, evidence: list[Evidence]) -> bool:
        ...

    def resolve(self, evidence: list[Evidence]) -> Evidence:
        """Weight by source reliability and freshness, pick the
        higher-confidence side (ADR-0014)."""
        ...


class StubContradictionResolver:
    """Structural implementation of ContradictionResolver. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def sources_agree(self, evidence: list[Evidence]) -> bool:
        with traced("StubContradictionResolver.sources_agree"):
            return True

    def resolve(self, evidence: list[Evidence]) -> Evidence:
        with traced("StubContradictionResolver.resolve"):
            return Evidence(content={}, source="stub", reliability=0.0, freshness=0.0)


class ClaimVerifier(Protocol):
    def verify(self, claim: Claim, evidence: list[Evidence]) -> VerifiedClaim:
        """Citation quality and completeness (ALCE)."""
        ...

    def score_confidence(self, verified: VerifiedClaim) -> float:
        ...


class StubClaimVerifier:
    """Structural implementation of ClaimVerifier. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def verify(self, claim: Claim, evidence: list[Evidence]) -> VerifiedClaim:
        with traced("StubClaimVerifier.verify"):
            return VerifiedClaim(
                claim=Claim(text="stub", source_component="stub"),
                evidence=[],
                confidence=0.0,
            )

    def score_confidence(self, verified: VerifiedClaim) -> float:
        with traced("StubClaimVerifier.score_confidence"):
            return 0.0
