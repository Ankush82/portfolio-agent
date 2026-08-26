"""Knowledge & Entity Model (component 04) — the system's canonical
world model.

Whiteboard-level only (Component Whiteboards artifact, card 04) — no
low-level design or ADRs yet. Interfaces: <- Data Processing & Quality
(03), -> Observation & Event (07), -> Retrieval & Context (05).
"""

from dataclasses import dataclass, field


@dataclass
class Entity:
    id: str
    kind: str  # Company | Security | Person | Sector | Industry | Index | Geography


@dataclass
class Relationship:
    source_entity_id: str
    target_entity_id: str
    kind: str


class KnowledgeEntity:
    def resolve_entity(self, mention: str) -> Entity | None:
        raise NotImplementedError

    def create_entity(self, details: dict) -> Entity:
        raise NotImplementedError

    def merge_entities(self, a: Entity, b: Entity) -> Entity:
        raise NotImplementedError

    def link_entities(self, a: Entity, b: Entity, relationship: str) -> Relationship:
        raise NotImplementedError

    def represent_relationships(self, entity: Entity) -> list[Relationship]:
        raise NotImplementedError

    def update_knowledge(self, entity: Entity, updates: dict) -> Entity:
        raise NotImplementedError

    def query_relationships(self, entity: Entity, kind: str | None = None) -> list[Relationship]:
        raise NotImplementedError
