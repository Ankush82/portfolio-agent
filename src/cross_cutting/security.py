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

from cross_cutting.observability import traced


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
