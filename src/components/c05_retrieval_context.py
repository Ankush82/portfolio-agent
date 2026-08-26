"""Retrieval & Context (component 05) — context engineering: what to
hand the reasoner, and how much of it.

Design: Retrieval & Evidence Design, fig. 1 (retrieval path)
Decisions: ADR-0011 (adaptive retrieval, Self-RAG), ADR-0012
(corrective retrieval, CRAG)
"""

from dataclasses import dataclass
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class Query:
    text: str
    context: dict


@dataclass
class ContextPack:
    documents: list[dict]
    sufficient: bool


class RetrievalGate(Protocol):
    def should_retrieve(self, query: Query) -> bool:
        """Fig. 1's adaptive gate (ADR-0011). Existing context/memory
        is used directly when this is False."""
        ...


class StubRetrievalGate:
    """Structural implementation of RetrievalGate. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def should_retrieve(self, query: Query) -> bool:
        with traced("StubRetrievalGate.should_retrieve"):
            return True


class Retriever(Protocol):
    def retrieve(self, query: Query) -> list[dict]:
        """Calls Source System (02) and Knowledge & Entity Model (04)."""
        ...


class StubRetriever:
    """Structural implementation of Retriever. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def retrieve(self, query: Query) -> list[dict]:
        with traced("StubRetriever.retrieve"):
            return []


class RetrievalEvaluator(Protocol):
    def is_sufficient(self, results: list[dict], query: Query) -> bool:
        """Fig. 1's 'sufficient?' gate (ADR-0012)."""
        ...


class StubRetrievalEvaluator:
    """Structural implementation of RetrievalEvaluator. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def is_sufficient(self, results: list[dict], query: Query) -> bool:
        with traced("StubRetrievalEvaluator.is_sufficient"):
            return True


class CorrectiveRetriever(Protocol):
    def retrieve_externally(self, query: Query, attempt: int) -> list[dict]:
        """External search, bounded attempts. Once the budget is
        exhausted, the caller should treat 'no useful evidence found'
        as a legitimate terminal state (CRAG), not an empty context."""
        ...


class StubCorrectiveRetriever:
    """Structural implementation of CorrectiveRetriever. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def retrieve_externally(self, query: Query, attempt: int) -> list[dict]:
        with traced("StubCorrectiveRetriever.retrieve_externally"):
            return []


class ContextBuilder(Protocol):
    def construct(self, documents: list[dict]) -> ContextPack:
        ...


class StubContextBuilder:
    """Structural implementation of ContextBuilder. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def construct(self, documents: list[dict]) -> ContextPack:
        with traced("StubContextBuilder.construct"):
            return ContextPack(documents=[], sufficient=False)
