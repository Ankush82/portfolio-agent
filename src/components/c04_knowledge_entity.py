"""Knowledge & Entity Model (component 04) — the system's canonical
world model.

Whiteboard-level only (Component Whiteboards artifact, card 04) — no
low-level design or ADRs yet. Interfaces: <- Data Processing & Quality
(03), -> Observation & Event (07), -> Retrieval & Context (05).
"""

from dataclasses import dataclass, field
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class Entity:
    id: str
    kind: str  # Company | Security | Person | Sector | Industry | Index | Geography


@dataclass
class Relationship:
    source_entity_id: str
    target_entity_id: str
    kind: str


class KnowledgeEntity(Protocol):
    def resolve_entity(self, mention: str) -> Entity | None:
        ...

    def create_entity(self, details: dict) -> Entity:
        ...

    def merge_entities(self, a: Entity, b: Entity) -> Entity:
        ...

    def link_entities(self, a: Entity, b: Entity, relationship: str) -> Relationship:
        ...

    def represent_relationships(self, entity: Entity) -> list[Relationship]:
        ...

    def update_knowledge(self, entity: Entity, updates: dict) -> Entity:
        ...

    def query_relationships(self, entity: Entity, kind: str | None = None) -> list[Relationship]:
        ...


class StubKnowledgeEntity:
    """Structural implementation of KnowledgeEntity. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def resolve_entity(self, mention: str) -> Entity | None:
        with traced("StubKnowledgeEntity.resolve_entity"):
            return None

    def create_entity(self, details: dict) -> Entity:
        with traced("StubKnowledgeEntity.create_entity"):
            return Entity(id="stub-id", kind="stub")

    def merge_entities(self, a: Entity, b: Entity) -> Entity:
        with traced("StubKnowledgeEntity.merge_entities"):
            return Entity(id="stub-id", kind="stub")

    def link_entities(self, a: Entity, b: Entity, relationship: str) -> Relationship:
        with traced("StubKnowledgeEntity.link_entities"):
            return Relationship(source_entity_id="stub-id", target_entity_id="stub-id", kind="stub")

    def represent_relationships(self, entity: Entity) -> list[Relationship]:
        with traced("StubKnowledgeEntity.represent_relationships"):
            return []

    def update_knowledge(self, entity: Entity, updates: dict) -> Entity:
        with traced("StubKnowledgeEntity.update_knowledge"):
            return Entity(id="stub-id", kind="stub")

    def query_relationships(self, entity: Entity, kind: str | None = None) -> list[Relationship]:
        with traced("StubKnowledgeEntity.query_relationships"):
            return []
