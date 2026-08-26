"""Evidence & Verification (component 09) — the trust layer.

Design: Retrieval & Evidence Design, fig. 2 (evidence path)
Decisions: ADR-0013 (mandatory evidence per claim, ALCE), ADR-0014
(contradictory evidence resolved automatically), ADR-0029 (evidence
linking: relatedness rule and Memory/ContextPack search mechanism),
ADR-0030 (contradiction detection rule and resolution weighting),
ADR-0031 (claim verification: citation completeness and confidence
scoring)

No LLM is used anywhere in this file: every `Default*` class below is
structural comparison — token overlap, dict-field comparison, and
numeric weighting over fields that already exist on `Evidence` and
`Memory` — matching the honest-heuristic precedent
`DefaultEntityLinker` sets in `src/components/c06_memory.py` for the
same reason (no embedding/LLM provider has been chosen for this
project, ADR-0028/ADR-0021).
"""

import time
from dataclasses import dataclass
from typing import Protocol

from components.c05_retrieval_context import ContextPack
from components.c06_memory import Memory, MemoryManager
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced


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


def _text_tokens(text: str) -> set[str]:
    """Lowercase whitespace-split token set for a plain string (a
    claim's own `text`). Same "naive by design" token-overlap
    signal `_content_tokens` in c06_memory.py uses for dict content —
    kept as a separate function here (rather than importing that
    module's private helper) because c09 needs it over both a claim's
    plain `text` and dict-shaped content, and the underlying rule is
    simple enough not to be worth a cross-module private dependency."""
    return {token for token in text.lower().split() if token}


def _dict_content_tokens(content: dict) -> set[str]:
    """Flattens a dict's values into a lowercase token set — the same
    rule `_text_tokens` applies to a plain string, applied to
    `Memory.content` / `Evidence.content` / a ContextPack document's
    fields (ADR-0029)."""
    text = " ".join(str(value) for value in content.values())
    return _text_tokens(text)


def _jaccard(a: set[str], b: set[str]) -> float:
    """Token-overlap relatedness: |intersection| / |union|. Zero when
    either side is empty (division-by-zero note: an empty union
    already implies both sides are empty, so this returns 0.0 rather
    than raising)."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


# Half-life used to turn Memory.last_touched_at (a unix timestamp) into
# an Evidence.freshness score in (0, 1]: freshness halves every
# _FRESHNESS_HALF_LIFE_SECONDS of age. 30 days is a deliberately
# moderate choice for a financial system's evidence — recent enough
# that month-old evidence is meaningfully discounted, not so aggressive
# that a week-old filing is treated as stale (ADR-0029).
_FRESHNESS_HALF_LIFE_SECONDS = 30 * 24 * 60 * 60

# Evidence pulled from a ContextPack document that carries no explicit
# "reliability"/"freshness"/"timestamp" fields of its own gets these
# neutral defaults (ADR-0029): reliability sits at the midpoint (no
# claim of being especially trustworthy or untrustworthy), freshness
# defaults to "just retrieved" (1.0) since ContextPack documents are,
# by construction, the output of a retrieval that just ran.
_CONTEXT_PACK_DEFAULT_RELIABILITY = 0.5
_CONTEXT_PACK_DEFAULT_FRESHNESS = 1.0


def _freshness_from_timestamp(timestamp: float, now: float) -> float:
    age_seconds = max(now - timestamp, 0.0)
    return 0.5 ** (age_seconds / _FRESHNESS_HALF_LIFE_SECONDS)


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


class DefaultEvidenceLinker:
    """Real implementation of EvidenceLinker (ADR-0029). Searches
    Memory (06) via a real `MemoryManager` and, when one is available,
    a `ContextPack` (05) — real token-overlap relatedness (Jaccard,
    same rule `DefaultEntityLinker` uses in c06_memory.py), not an
    embedding search, since no embedding/LLM provider has been chosen
    for this project (ADR-0028, ADR-0021).

    A quarantined `Memory` (ADR-0007) is never treated as evidence:
    quarantine means "not yet trusted," and evidence is exactly the
    thing a claim's trustworthiness rests on.

    `link()`'s signature is fixed by the `EvidenceLinker` Protocol —
    it takes only a `claim`, with no parameter for a per-query
    `ContextPack` — so a `ContextPack` to search is either bound once
    at construction (`context_pack=`, for a caller with one fixed
    retrieval round) or passed per call via `link_with_context()`, a
    real extra method beyond the Protocol (the same "extra
    real-behavior accessor" pattern `DefaultQuarantineGate.release()`
    uses in c06_memory.py) for a caller that gets a fresh `ContextPack`
    per query.
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        memory_scopes: tuple[str, ...] = ("user", "shared"),
        context_pack: ContextPack | None = None,
        similarity_threshold: float = 0.2,
    ) -> None:
        self._memory_manager = memory_manager
        self._memory_scopes = memory_scopes
        self._context_pack = context_pack
        self._similarity_threshold = similarity_threshold

    def link(self, claim: Claim) -> list[Evidence]:
        with traced("DefaultEvidenceLinker.link"):
            evidence = self._search_memory(claim)
            if self._context_pack is not None:
                evidence.extend(self._search_context_pack(claim, self._context_pack))
            return evidence

    def link_with_context(self, claim: Claim, context_pack: ContextPack) -> list[Evidence]:
        """Not part of the EvidenceLinker Protocol — searches Memory
        plus the given `ContextPack`, for a caller with a fresh
        `ContextPack` per query rather than one fixed at construction."""
        with traced("DefaultEvidenceLinker.link_with_context"):
            evidence = self._search_memory(claim)
            evidence.extend(self._search_context_pack(claim, context_pack))
            return evidence

    def _search_memory(self, claim: Claim) -> list[Evidence]:
        claim_tokens = _text_tokens(claim.text)
        if not claim_tokens:
            return []
        now = time.time()
        found: list[Evidence] = []
        for scope in self._memory_scopes:
            for memory in self._memory_manager.retrieve({}, scope=scope):
                if memory.quarantined:
                    continue
                similarity = _jaccard(claim_tokens, _dict_content_tokens(memory.content))
                if similarity >= self._similarity_threshold:
                    found.append(self._evidence_from_memory(memory, now))
        return found

    def _search_context_pack(self, claim: Claim, context_pack: ContextPack) -> list[Evidence]:
        claim_tokens = _text_tokens(claim.text)
        if not claim_tokens:
            return []
        found: list[Evidence] = []
        for document in context_pack.documents:
            similarity = _jaccard(claim_tokens, _dict_content_tokens(document))
            if similarity >= self._similarity_threshold:
                found.append(self._evidence_from_document(document))
        return found

    @staticmethod
    def _evidence_from_memory(memory: Memory, now: float) -> Evidence:
        return Evidence(
            content=memory.content,
            source=f"memory:{memory.scope}:{memory.id}",
            reliability=memory.confidence,
            freshness=_freshness_from_timestamp(memory.last_touched_at, now),
        )

    @staticmethod
    def _evidence_from_document(document: dict) -> Evidence:
        source = document.get("source_id") or document.get("source") or "context_pack"
        reliability = document.get("reliability", _CONTEXT_PACK_DEFAULT_RELIABILITY)
        timestamp = document.get("timestamp")
        freshness = (
            _freshness_from_timestamp(timestamp, time.time())
            if timestamp is not None
            else document.get("freshness", _CONTEXT_PACK_DEFAULT_FRESHNESS)
        )
        return Evidence(content=document, source=str(source), reliability=reliability, freshness=freshness)


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


class DefaultMandatoryEvidenceGate:
    """Real implementation of MandatoryEvidenceGate (ADR-0013): a claim
    without evidence is blocked from reaching Decision & Policy (12) —
    "logged, not forwarded, not passed through down-weighted," per
    ADR-0013's Decision.

    `has_evidence()` is a real non-empty check. `block()` actually
    records the block via `AuditManager` (`DefaultAuditManager` by
    default) rather than silently discarding the claim — the
    "blocked claims" case `AuditManager.record`'s own docstring names
    explicitly (cross_cutting/observability.py) — so a blocked claim
    leaves a real trail even though it never reaches Decision & Policy.
    """

    def __init__(self, audit_manager: AuditManager | None = None) -> None:
        self._audit_manager = audit_manager or DefaultAuditManager()

    def has_evidence(self, evidence: list[Evidence]) -> bool:
        with traced("DefaultMandatoryEvidenceGate.has_evidence"):
            return len(evidence) > 0

    def block(self, claim: Claim) -> None:
        with traced("DefaultMandatoryEvidenceGate.block"):
            self._audit_manager.record(
                "claim_blocked",
                {"claim_text": claim.text, "source_component": claim.source_component},
            )
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


# Two pieces of Evidence are only compared for disagreement if they're
# actually about the same thing — a lower bar than EvidenceLinker's own
# similarity_threshold, since two evidence entries linked to the same
# claim are already claim-relevant; this just filters out linked
# evidence pairs that happen to cover different sub-topics of that
# same claim (ADR-0030).
_CONTRADICTION_TOPIC_OVERLAP_THRESHOLD = 0.15


def _normalize_value(value: object) -> str:
    return str(value).strip().lower()


def _shared_keys_conflict(content_a: dict, content_b: dict) -> bool:
    """Real content comparison (ADR-0030): two Evidence entries
    conflict when a dict key present in both `content` payloads holds a
    different (normalized) value — e.g. {"metric": "EPS", "result":
    "beat"} vs. {"metric": "EPS", "result": "missed"}. A pair with no
    shared keys is not directly comparable this way and is treated as
    not conflicting — a real, stated limitation (ADR-0030's
    Consequences), not a silent assumption of agreement."""
    shared_keys = content_a.keys() & content_b.keys()
    return any(_normalize_value(content_a[key]) != _normalize_value(content_b[key]) for key in shared_keys)


class DefaultContradictionResolver:
    """Real implementation of ContradictionResolver (ADR-0014,
    ADR-0030).

    `sources_agree()`: real text/content comparison, not a length
    check. Fewer than two Evidence entries trivially agree (nothing to
    disagree with). Otherwise, every pair of entries whose `content` is
    topically related (Jaccard token overlap over
    `_CONTRADICTION_TOPIC_OVERLAP_THRESHOLD`, so they're actually about
    the same thing) is checked for a direct field conflict via
    `_shared_keys_conflict`; any conflicting pair makes the whole set
    disagree.

    `resolve()`: weights each Evidence by `reliability * freshness`
    (ADR-0014's "weight by source reliability and freshness") and picks
    the highest-scoring entry. Ties break toward the entry with the
    higher `reliability` alone, then toward whichever appeared first —
    the same "first occurrence wins a tie" behavior Python's `max`
    already gives a stable input list, made explicit here since it's
    load-bearing on this method's actual behavior.
    """

    def sources_agree(self, evidence: list[Evidence]) -> bool:
        with traced("DefaultContradictionResolver.sources_agree"):
            if len(evidence) < 2:
                return True
            for i, first in enumerate(evidence):
                first_tokens = _dict_content_tokens(first.content)
                for second in evidence[i + 1 :]:
                    second_tokens = _dict_content_tokens(second.content)
                    if _jaccard(first_tokens, second_tokens) < _CONTRADICTION_TOPIC_OVERLAP_THRESHOLD:
                        continue
                    if _shared_keys_conflict(first.content, second.content):
                        return False
            return True

    def resolve(self, evidence: list[Evidence]) -> Evidence:
        with traced("DefaultContradictionResolver.resolve"):
            if not evidence:
                raise ValueError("DefaultContradictionResolver: cannot resolve an empty evidence list")
            return max(evidence, key=lambda e: (e.reliability * e.freshness, e.reliability))


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


# ALCE's citation-completeness idea, made concrete (ADR-0031): a claim
# backed by this many *independent* sources (distinct Evidence.source
# values) counts as fully cited; more sources than this don't add
# further confidence, but fewer scale it down proportionally, so one
# clean source is never scored identically to a claim triangulated
# across several.
_TARGET_INDEPENDENT_SOURCES = 2

# A claim whose evidence disagreed and was resolved automatically
# (ADR-0014) is real evidence, but ADR-0014's own Consequences flags
# that "an automatically-resolved contradiction is exactly the shape of
# a claim that can look well-supported... while actually being wrong."
# This penalty is the concrete number that makes a resolved-contradiction
# claim score lower than an equally-reliable claim that never
# disagreed — the "differing only in the number attached" ADR-0014
# anticipates (ADR-0031).
_CONTRADICTION_RESOLUTION_PENALTY = 0.85


class DefaultClaimVerifier:
    """Real implementation of ClaimVerifier (ADR-0031).

    `verify()` does real structural citation checking, ALCE-style: it
    determines whether the claim's linked evidence actually agrees
    (via the composed `ContradictionResolver`) and carries that finding
    — and the full evidence list itself, so a caller can see exactly
    how much evidence and from where — forward on `VerifiedClaim`.

    `score_confidence()` computes a real number, not a hardcoded one:

      confidence = base_quality * source_diversity_factor * contradiction_penalty

    where `base_quality` is the mean `reliability * freshness` across
    all evidence when sources agree, or the resolved winner's own
    `reliability * freshness` when they didn't (ADR-0014's automatic
    resolution already picked that winner); `source_diversity_factor`
    is `min(independent_sources / _TARGET_INDEPENDENT_SOURCES, 1.0)`
    (ALCE citation completeness); and `contradiction_penalty` is
    `_CONTRADICTION_RESOLUTION_PENALTY` when the evidence disagreed,
    else `1.0`. The result is clamped to `[0.0, 1.0]`.
    """

    def __init__(
        self,
        contradiction_resolver: ContradictionResolver,
        target_independent_sources: int = _TARGET_INDEPENDENT_SOURCES,
        contradiction_penalty: float = _CONTRADICTION_RESOLUTION_PENALTY,
    ) -> None:
        self._contradiction_resolver = contradiction_resolver
        self._target_independent_sources = max(target_independent_sources, 1)
        self._contradiction_penalty = contradiction_penalty

    def verify(self, claim: Claim, evidence: list[Evidence]) -> VerifiedClaim:
        with traced("DefaultClaimVerifier.verify"):
            if not evidence:
                return VerifiedClaim(claim=claim, evidence=[], confidence=0.0, was_contradictory=False)
            was_contradictory = not self._contradiction_resolver.sources_agree(evidence)
            verified = VerifiedClaim(
                claim=claim, evidence=list(evidence), confidence=0.0, was_contradictory=was_contradictory
            )
            verified.confidence = self.score_confidence(verified)
            return verified

    def score_confidence(self, verified: VerifiedClaim) -> float:
        with traced("DefaultClaimVerifier.score_confidence"):
            evidence = verified.evidence
            if not evidence:
                return 0.0
            if verified.was_contradictory:
                winner = self._contradiction_resolver.resolve(evidence)
                base_quality = winner.reliability * winner.freshness
            else:
                base_quality = sum(e.reliability * e.freshness for e in evidence) / len(evidence)
            independent_sources = len({e.source for e in evidence})
            diversity_factor = min(independent_sources / self._target_independent_sources, 1.0)
            penalty = self._contradiction_penalty if verified.was_contradictory else 1.0
            confidence = base_quality * diversity_factor * penalty
            return max(0.0, min(confidence, 1.0))
