# 0013 — Evidence requirement: mandatory per claim (ALCE)

**Status:** Accepted — 2026-08-26
**Component:** Evidence & Verification (09)

## Context

ALCE evaluates generated answers on answer correctness, citation quality, and citation completeness, with the implication that generated claims should have explicit relationships to supporting evidence: Analysis → Claims → Evidence → Claim↔Evidence verification → Decision. RAGTruth separately shows that retrieval-augmented generation does not automatically eliminate hallucination or unsupported claims — grounding has to be checked, not assumed. Evidence & Verification needed a decision on how strictly evidence is required before a claim can proceed.

## Decision

Mandatory per claim (ALCE): a claim without supporting evidence is blocked from reaching Decision & Policy at all — logged, not forwarded, not passed through down-weighted.

## Alternatives considered

- **Graded / confidence-weighted.** Every claim gets an evidence-confidence score, including freshness and source reliability; low-confidence claims still pass through, just down-weighted. Rejected as the primary rule because it lets an unsupported claim reach Decision & Policy under a number that can look more authoritative than it is — exactly the RAGTruth finding this component exists to guard against.

## Consequences

- A genuinely correct claim about a fast-moving, newly-emerging situation, one where evidence simply doesn't exist yet, gets blocked along with everything actually wrong. This is named explicitly as known-unknown in the failure framework: the real rate of this cost isn't known.
- Blocked claims need somewhere to go besides nowhere — this design routes them to a log rather than silent discard, but nothing downstream (Interaction & Notification, Learning & Evaluation) is designed yet to confirm anyone actually sees that log. Named as unknown-known.
- Systematic bias is possible: if evidence exists more often for well-covered subjects than obscure ones, strictness itself shapes what the system ever says anything about — named as unknown-unknown, with Observability & Governance watching block-rate drift as the only current mitigation.

## Related

- Full design: [Retrieval & Evidence Design](https://claude.ai/code/artifact/afae265c-32a2-460f-b537-24a4cfc736d4), fig. 2 (the "evidence found?" gate and the "claim blocked" state).
- Logged narratively in `../checkpoint.md`.
