"""Retrieval & Context (component 05) — context engineering: what to
hand the reasoner, and how much of it.

Design: Retrieval & Evidence Design, fig. 1 (retrieval path)
Decisions: ADR-0011 (adaptive retrieval, Self-RAG), ADR-0012
(corrective retrieval, CRAG)
"""

from dataclasses import dataclass

from cross_cutting.observability import traced


@dataclass
class Query:
    text: str
    context: dict


@dataclass
class ContextPack:
    documents: list[dict]
    sufficient: bool


class RetrievalGate:
    def should_retrieve(self, query: Query) -> bool:
        """Fig. 1's adaptive gate (ADR-0011). Existing context/memory
        is used directly when this is False."""
        with traced("RetrievalGate.should_retrieve"):
            return True


class Retriever:
    def retrieve(self, query: Query) -> list[dict]:
        """Calls Source System (02) and Knowledge & Entity Model (04)."""
        with traced("Retriever.retrieve"):
            return []


class RetrievalEvaluator:
    def is_sufficient(self, results: list[dict], query: Query) -> bool:
        """Fig. 1's 'sufficient?' gate (ADR-0012)."""
        with traced("RetrievalEvaluator.is_sufficient"):
            return True


class CorrectiveRetriever:
    def retrieve_externally(self, query: Query, attempt: int) -> list[dict]:
        """External search, bounded attempts. Once the budget is
        exhausted, the caller should treat 'no useful evidence found'
        as a legitimate terminal state (CRAG), not an empty context."""
        with traced("CorrectiveRetriever.retrieve_externally"):
            return []


class ContextBuilder:
    def construct(self, documents: list[dict]) -> ContextPack:
        with traced("ContextBuilder.construct"):
            return ContextPack(documents=[], sufficient=False)
