"""Security & Privacy (component 17) — the boundary gate every crossing
goes through: tool calls, memory writes, and delegated sub-agent output.

Design: Phase 0 Cross-Cutting Design, fig. 17.1
Decisions:
  ADR-0003 — document content tagged untrusted at the point it enters
             Agent Runtime's loop
  ADR-0018 — extends that same tag to delegated sub-agent output
  ADR-0042 — resolves the authority-check granularity question ADR-0020
             left open: per-call, Infrastructure-backed grant table,
             deny-by-default. Supersedes ADR-0020's fail-open interim.
"""

from enum import Enum, auto
from typing import Protocol

from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


class Provenance(Enum):
    TRUSTED = auto()
    UNTRUSTED = auto()  # documents (ADR-0003) or peer-agent output (ADR-0018)


class BoundaryGate(Protocol):
    def authenticate(self, identity: str) -> bool:
        ...

    def authorize(self, identity: str, action: str, resource: str) -> bool:
        """Per-call granularity (ADR-0042): every call is evaluated
        independently against real policy data. No task-level caching."""
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


_AUTHORIZATION_GRANTS_TABLE = "security_authorization_grants"
_WILDCARD = "*"  # matches any action or any resource within a grant; identity is always exact


class DefaultBoundaryGate:
    """Real implementation of BoundaryGate.

    tag_provenance() and authenticate() are fully specified by
    existing ADRs / by "no real identity provider exists yet"; see
    each method's docstring. authorize() now enforces for real
    (ADR-0042, superseding ADR-0020's fail-open interim): per-call
    granularity against an Infrastructure-backed grant table, deny by
    default. See authorize()'s and grant()'s docstrings.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        audit_manager: AuditManager | None = None,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._audit_manager = audit_manager or DefaultAuditManager()

    def authenticate(self, identity: str) -> bool:
        """Placeholder for a real identity provider — no such provider
        exists in this project yet. This is NOT a security boundary:
        it only checks that `identity` is a non-empty, non-whitespace
        string. It does not verify the identity belongs to whoever is
        claiming it."""
        with traced("DefaultBoundaryGate.authenticate"):
            return bool(identity) and bool(identity.strip())

    def grant(self, identity: str, action: str, resource: str) -> str:
        """Adds one real authorization grant to the Infrastructure-backed
        policy table (ADR-0042). `action` and/or `resource` may be the
        wildcard "*" to grant every action, every resource, or both, to
        `identity`; `identity` itself is always matched exactly — a
        grant is always scoped to one principal, never broadcast to
        every caller. Returns the new grant's id. Nothing seeds any
        grants by default (same reasoning as ADR-0024's empty tool
        registry): a caller must grant explicitly before authorize()
        allows anything for that identity."""
        with traced("DefaultBoundaryGate.grant"):
            return self._infrastructure.store(
                _AUTHORIZATION_GRANTS_TABLE,
                {"identity": identity, "action": action, "resource": resource},
            )

    def authorize(self, identity: str, action: str, resource: str) -> bool:
        """Real enforcement, per-call granularity (ADR-0042, superseding
        ADR-0020's fail-open interim). Every call is evaluated
        independently — no task-level caching — against the real grants
        `grant()` has stored for `identity` in Infrastructure: allowed
        only if at least one stored grant for this exact identity has
        an action matching `action` (exact or "*") AND a resource
        matching `resource` (exact or "*"). No matching grant means
        deny; there is no fail-open path left. Every call is still
        recorded via AuditManager, now with `enforced: True` and the
        real decision, so the audit trail reflects what actually
        happened rather than what was merely logged."""
        with traced("DefaultBoundaryGate.authorize"):
            grants = self._infrastructure.query(_AUTHORIZATION_GRANTS_TABLE, {"identity": identity})
            decision = any(
                grant.get("action") in (action, _WILDCARD) and grant.get("resource") in (resource, _WILDCARD)
                for grant in grants
            )
            self._audit_manager.record(
                "authorization_decision",
                {
                    "identity": identity,
                    "action": action,
                    "resource": resource,
                    "decision": decision,
                    "enforced": True,
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
