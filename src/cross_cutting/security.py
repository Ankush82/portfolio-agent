"""Security & Privacy (component 17) — the boundary gate every crossing
goes through: tool calls, memory writes, and delegated sub-agent output.

Design: Phase 0 Cross-Cutting Design, fig. 17.1
Decisions:
  ADR-0003 — document content tagged untrusted at the point it enters
             Agent Runtime's loop
  ADR-0018 — extends that same tag to delegated sub-agent output

Still open (checkpoint.md, loop.md): authority-check granularity, per
task vs. per tool call. Do not resolve this by whatever authorize()
happens to do below — that's a loop.md step 2 gap, not a default.
"""

from enum import Enum, auto
from typing import Protocol

from cross_cutting.observability import DefaultAuditManager, traced


class Provenance(Enum):
    TRUSTED = auto()
    UNTRUSTED = auto()  # documents (ADR-0003) or peer-agent output (ADR-0018)


class BoundaryGate(Protocol):
    def authenticate(self, identity: str) -> bool:
        ...

    def authorize(self, identity: str, action: str, resource: str) -> bool:
        """Granularity not yet decided — see module docstring."""
        ...

    def tag_provenance(self, content: dict, source: str) -> dict:
        """Documents and delegated sub-agent output both get tagged
        UNTRUSTED here, before they can be reasoned over as an
        instruction. Same tag, two sources (ADR-0003, ADR-0018)."""
        ...


class StubBoundaryGate:
    """Structural implementation of BoundaryGate. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def authenticate(self, identity: str) -> bool:
        with traced("StubBoundaryGate.authenticate"):
            return True

    def authorize(self, identity: str, action: str, resource: str) -> bool:
        with traced("StubBoundaryGate.authorize"):
            return True

    def tag_provenance(self, content: dict, source: str) -> dict:
        with traced("StubBoundaryGate.tag_provenance"):
            return {}


class DefaultBoundaryGate:
    """Real implementation of BoundaryGate.

    tag_provenance() and authenticate() are fully specified by
    existing ADRs / by "no real identity provider exists yet"; see
    each method's docstring. authorize() is not: the authority-check
    granularity question (per task vs. per tool call) has been open
    since Phase 0 (loop.md "Still-open items", checkpoint.md's Phase 0
    cross-cutting summary) and no ADR settled it before this class
    needed to exist. Per loop.md step 2, that gap is documented in a
    draft ADR (ADR-0020, status Proposed) rather than resolved here by
    whatever the code happens to do — see authorize()'s docstring.
    """

    def authenticate(self, identity: str) -> bool:
        """Placeholder for a real identity provider — no such provider
        exists in this project yet. This is NOT a security boundary:
        it only checks that `identity` is a non-empty, non-whitespace
        string. It does not verify the identity belongs to whoever is
        claiming it."""
        with traced("DefaultBoundaryGate.authenticate"):
            return bool(identity) and bool(identity.strip())

    def authorize(self, identity: str, action: str, resource: str) -> bool:
        """Provisional, interim default — see ADR-0020
        (adr/0020-security-authorize-interim-default.md, status
        Proposed). This ALWAYS ALLOWS: it returns True for every call,
        regardless of identity, action, or resource. The authority-check
        granularity question (per task vs. per tool call) is unresolved,
        so this does not attempt to enforce anything at either
        granularity — it accepts whatever the caller passes and logs it.
        Every call is recorded via DefaultAuditManager so a decision
        trail exists, but nothing is actually blocked yet. Do not treat
        this as a working authorization boundary; see ADR-0020's
        Consequences section for exactly what is and isn't safe to rely
        on here."""
        with traced("DefaultBoundaryGate.authorize"):
            decision = True
            DefaultAuditManager().record(
                "authorization_decision",
                {
                    "identity": identity,
                    "action": action,
                    "resource": resource,
                    "decision": decision,
                    "enforced": False,
                },
            )
            return decision

    def tag_provenance(self, content: dict, source: str) -> dict:
        """Documents (ADR-0003) and delegated sub-agent output
        (ADR-0018) are both untrusted by default; nothing in either
        ADR, or anywhere else in this project's design, specifies a
        `source` value that should produce TRUSTED. Always tags
        UNTRUSTED rather than inventing a trusted case that no
        decision has authorized. Returns a new dict — the original
        `content` merged with a "provenance" key — without mutating
        the input."""
        with traced("DefaultBoundaryGate.tag_provenance"):
            return {**content, "provenance": Provenance.UNTRUSTED.name}
