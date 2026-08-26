"""Knowledge & Entity Model (component 04) — the system's canonical
world model.

Interfaces: <- Data Processing & Quality (03), -> Observation & Event
(07), -> Retrieval & Context (05, which already calls
`KnowledgeEntity.resolve_entity` for real — see `DefaultRetriever` in
c05_retrieval_context.py), -> User & Portfolio (01, which calls
`DefaultKnowledgeEntity.search_entities`/`get_entity` for real — see
`DefaultUserPortfolio.list_available_securities`/`add_holding_manually`
in c01_user_portfolio.py).

Design: ADR-0035 (real mechanism: normalized exact match plus an
edit-distance fuzzy fallback for `resolve_entity`, `Infrastructure`-
backed CRUD for entities and relationships, a documented alias/
relationship transfer rule for `merge_entities`). ADR-0044 adds
`get_entity`/`search_entities`, real extra accessors beyond the
KnowledgeEntity Protocol for id-based lookup and registry enumeration.
No LLM anywhere in this component — entity resolution/canonicalization
is a real structural/lookup problem against a known registry, not
generation.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from cross_cutting.observability import traced
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


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


# --- DefaultKnowledgeEntity: real, structural/lookup mechanism ---
#
# ADR-0035. Tables this component owns:
#   "entities"       — one row per entity: {"id", "kind", "name",
#                       "aliases": [str, ...], "attributes": dict,
#                       "provenance"? , "merged_into"?}. A row carrying
#                       "merged_into" is a tombstone left behind by
#                       merge_entities — its id still resolves (via
#                       update_knowledge's redirect) to the surviving
#                       entity, but it never independently matches a
#                       mention (its own name/aliases were transferred
#                       to the survivor at merge time).
#   "relationships"  — one row per link: {"id", "source_entity_id",
#                       "target_entity_id", "kind"}. `id` is this
#                       component's own synthesized row key (a
#                       Relationship itself carries no id field), used
#                       so merge_entities can repoint a specific row's
#                       source/target in place rather than duplicating
#                       it.
#
# Entity name/alias matching: resolve_entity normalizes (lowercase,
# collapsed whitespace) and checks every live entity's name + aliases
# for an exact match first; if none clears, it falls back to a real
# fuzzy match — normalized Levenshtein edit-distance similarity above
# a documented threshold (see _DEFAULT_FUZZY_MATCH_THRESHOLD) — rather
# than only ever exact-matching. See ADR-0035 for why edit distance
# was chosen over the project's existing Jaccard-token-overlap
# precedent (c06/c09) for this specific matching shape.

_ENTITIES_TABLE = "entities"
_RELATIONSHIPS_TABLE = "relationships"

# Entity mentions are short strings (company/security/person names),
# where the dominant real-world noise is a handful of character-level
# edits (a typo, a missing "Inc.", a punctuation slip) rather than
# vocabulary substitution — the failure mode token overlap is built
# for. Edit-distance similarity, ratio'd against the longer string's
# length, directly measures that. 0.80 tolerates a couple of edits on
# a medium-length name ("Aple Inc" -> "Apple Inc" scores ~0.89) while
# still rejecting short, genuinely different strings ("AB" -> "AC"
# scores 0.50) — documented default, overridable per instance.
_DEFAULT_FUZZY_MATCH_THRESHOLD = 0.80

_RESERVED_ENTITY_DETAIL_KEYS = frozenset({"id", "kind", "name", "aliases", "provenance"})
_MUTABLE_ENTITY_RECORD_KEYS = frozenset({"kind", "name", "aliases"})

_WHITESPACE_PATTERN = re.compile(r"\s+")


def _normalize_name(value: str) -> str:
    """Case/whitespace normalization shared by exact and fuzzy
    matching: lowercased, leading/trailing whitespace stripped,
    internal whitespace runs collapsed to a single space."""
    return _WHITESPACE_PATTERN.sub(" ", value.strip().lower())


def _dedup_preserving_order(values: list[str]) -> list[str]:
    """Drops blanks and normalized-duplicate names, keeping the first
    original-casing occurrence of each — used to merge alias lists
    without losing casing or introducing repeats."""
    seen_normalized: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = _normalize_name(value)
        if normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)
        result.append(value)
    return result


def _levenshtein_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current_row = [i]
        for j, char_b in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (char_a != char_b)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]


def _name_similarity(a: str, b: str) -> float:
    """1.0 for an exact match, decaying toward 0.0 as edit distance
    grows relative to the longer of the two strings."""
    longer_length = max(len(a), len(b))
    if longer_length == 0:
        return 1.0
    return 1.0 - (_levenshtein_distance(a, b) / longer_length)


class DefaultKnowledgeEntity:
    """Real implementation of KnowledgeEntity (ADR-0035).

    Every method is `Infrastructure`-backed (ADR-0019's interface,
    `DefaultInfrastructure` by default) — never an in-memory registry.
    resolve_entity is structural/lookup only (normalized exact match,
    then edit-distance fuzzy fallback); create_entity/merge_entities/
    update_knowledge are real CRUD; link_entities/represent_
    relationships/query_relationships are real relationship storage
    and lookup. No LLM anywhere in this class.

    Provenance (ADR-0003): this component canonicalizes already-
    processed `StructuredData` (component 03's output), it does not
    ingest raw external content itself, so it never calls
    `BoundaryGate.tag_provenance` on its own — that boundary crossing
    already happened upstream. It does, however, propagate forward
    whatever `details["provenance"]` a caller supplies (e.g. copied
    from `StructuredData.fields["provenance"]`) onto the stored entity
    record, so a caller building an `Entity` from untrusted upstream
    content doesn't lose that signal at this hop. No provenance claim
    is fabricated when the caller doesn't supply one.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        fuzzy_match_threshold: float = _DEFAULT_FUZZY_MATCH_THRESHOLD,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._fuzzy_match_threshold = fuzzy_match_threshold

    def _get_record(self, entity_id: str) -> dict | None:
        return self._infrastructure.retrieve(_ENTITIES_TABLE, entity_id)

    def _live_entity_records(self) -> list[dict]:
        """Every entities-table row that isn't a merge tombstone.
        `query(table, {})` is an unfiltered scan (empty-dict
        containment matches every row, in both `DefaultInfrastructure`
        and the in-memory test double) — a real, if currently
        brute-force, way to search a registry with no dedicated index
        or search infra behind it yet."""
        return [record for record in self._infrastructure.query(_ENTITIES_TABLE, {}) if "merged_into" not in record]

    def resolve_entity(self, mention: str) -> Entity | None:
        with traced("DefaultKnowledgeEntity.resolve_entity"):
            normalized_mention = _normalize_name(mention)
            if not normalized_mention:
                return None
            candidates = self._live_entity_records()
            for record in candidates:
                names = [record.get("name", ""), *record.get("aliases", [])]
                if normalized_mention in {_normalize_name(name) for name in names if name}:
                    return Entity(id=record["id"], kind=record["kind"])
            best_record: dict | None = None
            best_score = 0.0
            for record in candidates:
                names = [record.get("name", ""), *record.get("aliases", [])]
                for name in names:
                    if not name:
                        continue
                    score = _name_similarity(normalized_mention, _normalize_name(name))
                    if score > best_score:
                        best_score = score
                        best_record = record
            if best_record is not None and best_score >= self._fuzzy_match_threshold:
                return Entity(id=best_record["id"], kind=best_record["kind"])
            return None

    def create_entity(self, details: dict) -> Entity:
        with traced("DefaultKnowledgeEntity.create_entity"):
            kind = details.get("kind")
            name = details.get("name")
            if not kind:
                raise ValueError("create_entity requires a non-empty 'kind' in details")
            if not name:
                raise ValueError("create_entity requires a non-empty 'name' in details")
            entity_id = str(details.get("id") or f"entity-{uuid.uuid4()}")
            record = {
                "id": entity_id,
                "kind": kind,
                "name": name,
                "aliases": _dedup_preserving_order(list(details.get("aliases", []))),
                "attributes": {
                    key: value for key, value in details.items() if key not in _RESERVED_ENTITY_DETAIL_KEYS
                },
            }
            if "provenance" in details:
                record["provenance"] = details["provenance"]
            self._infrastructure.store(_ENTITIES_TABLE, record)
            return Entity(id=entity_id, kind=kind)

    def _repoint_relationships(self, old_entity_id: str, new_entity_id: str) -> None:
        for row in self._infrastructure.query(_RELATIONSHIPS_TABLE, {"source_entity_id": old_entity_id}):
            row["source_entity_id"] = new_entity_id
            self._infrastructure.store(_RELATIONSHIPS_TABLE, row)
        for row in self._infrastructure.query(_RELATIONSHIPS_TABLE, {"target_entity_id": old_entity_id}):
            row["target_entity_id"] = new_entity_id
            self._infrastructure.store(_RELATIONSHIPS_TABLE, row)

    def merge_entities(self, a: Entity, b: Entity) -> Entity:
        """`a` is the documented survivor, `b` the loser (ADR-0035).
        The loser's name becomes an alias of the survivor and its own
        aliases are transferred in (deduplicated, survivor's existing
        aliases kept first) — so a mention that used to resolve to `b`
        still resolves, now to `a`. `b`'s attributes fill in any keys
        the survivor doesn't already have (survivor's own values win
        conflicts). Every relationship row referencing `b`'s id is
        rewritten in place to reference `a` instead — relationships
        transfer for real, they don't vanish. `b` is left behind as a
        tombstone (`merged_into: a.id`) rather than deleted —
        `Infrastructure` has no delete primitive, and the tombstone is
        what lets `update_knowledge` redirect a stale reference to
        `b` onto the survivor."""
        with traced("DefaultKnowledgeEntity.merge_entities"):
            survivor = self._get_record(a.id) or {
                "id": a.id,
                "kind": a.kind,
                "name": "",
                "aliases": [],
                "attributes": {},
            }
            loser = self._get_record(b.id) or {
                "id": b.id,
                "kind": b.kind,
                "name": "",
                "aliases": [],
                "attributes": {},
            }
            survivor["aliases"] = _dedup_preserving_order(
                [*survivor.get("aliases", []), loser.get("name", ""), *loser.get("aliases", [])]
            )
            survivor["attributes"] = {**loser.get("attributes", {}), **survivor.get("attributes", {})}
            self._infrastructure.store(_ENTITIES_TABLE, survivor)
            self._repoint_relationships(loser["id"], survivor["id"])
            self._infrastructure.store(
                _ENTITIES_TABLE,
                {
                    "id": loser["id"],
                    "kind": loser.get("kind", b.kind),
                    "name": loser.get("name", ""),
                    "aliases": [],
                    "attributes": {},
                    "merged_into": survivor["id"],
                },
            )
            return Entity(id=survivor["id"], kind=survivor["kind"])

    def link_entities(self, a: Entity, b: Entity, relationship: str) -> Relationship:
        with traced("DefaultKnowledgeEntity.link_entities"):
            row = {
                "id": f"relationship-{uuid.uuid4()}",
                "source_entity_id": a.id,
                "target_entity_id": b.id,
                "kind": relationship,
            }
            self._infrastructure.store(_RELATIONSHIPS_TABLE, row)
            return Relationship(source_entity_id=a.id, target_entity_id=b.id, kind=relationship)

    def _relationships_for(self, entity_id: str, kind: str | None) -> list[Relationship]:
        source_filter: dict = {"source_entity_id": entity_id}
        target_filter: dict = {"target_entity_id": entity_id}
        if kind is not None:
            source_filter["kind"] = kind
            target_filter["kind"] = kind
        rows = self._infrastructure.query(_RELATIONSHIPS_TABLE, source_filter) + self._infrastructure.query(
            _RELATIONSHIPS_TABLE, target_filter
        )
        seen_row_ids: set[str] = set()
        relationships: list[Relationship] = []
        for row in rows:
            if row["id"] in seen_row_ids:
                continue
            seen_row_ids.add(row["id"])
            relationships.append(
                Relationship(
                    source_entity_id=row["source_entity_id"],
                    target_entity_id=row["target_entity_id"],
                    kind=row["kind"],
                )
            )
        return relationships

    def represent_relationships(self, entity: Entity) -> list[Relationship]:
        with traced("DefaultKnowledgeEntity.represent_relationships"):
            return self._relationships_for(entity.id, kind=None)

    def update_knowledge(self, entity: Entity, updates: dict) -> Entity:
        """Applies `updates` to the entity's stored record: "kind",
        "name", and "aliases" replace the record's own field directly
        (callers wanting to extend rather than replace aliases pass
        the full desired list); any other key is written into
        "attributes". If `entity` has been merged away (its record
        carries `merged_into`), the update is redirected onto the
        surviving entity rather than silently applied to a tombstone
        no one can look up again."""
        with traced("DefaultKnowledgeEntity.update_knowledge"):
            record = self._get_record(entity.id) or {
                "id": entity.id,
                "kind": entity.kind,
                "name": "",
                "aliases": [],
                "attributes": {},
            }
            merged_into = record.get("merged_into")
            if merged_into is not None:
                record = self._get_record(merged_into) or {
                    "id": merged_into,
                    "kind": entity.kind,
                    "name": "",
                    "aliases": [],
                    "attributes": {},
                }
            record.setdefault("attributes", {})
            for key, value in updates.items():
                if key == "id":
                    continue
                if key in _MUTABLE_ENTITY_RECORD_KEYS:
                    record[key] = value
                else:
                    record["attributes"][key] = value
            self._infrastructure.store(_ENTITIES_TABLE, record)
            return Entity(id=record["id"], kind=record["kind"])

    def query_relationships(self, entity: Entity, kind: str | None = None) -> list[Relationship]:
        with traced("DefaultKnowledgeEntity.query_relationships"):
            return self._relationships_for(entity.id, kind=kind)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Not part of the KnowledgeEntity Protocol — a real, direct
        id lookup, as opposed to resolve_entity's mention/name-based
        fuzzy match (ADR-0044). Same "extra real-behavior accessor"
        pattern DefaultQuarantineGate.release()/is_expired() establish
        in c06_memory.py and DefaultEvidenceLinker.link_with_context()
        establishes in c09_evidence_verification.py: a caller that
        already holds a specific entity id — e.g. validating a
        security_id a user picked from search_entities()'s own output
        — needs to confirm it is still a real, live entity, not
        re-resolve it by name. A merged-away id (its record carries
        `merged_into`) redirects to the surviving entity, the same
        redirect update_knowledge applies, rather than returning a
        tombstone the caller can't act on."""
        with traced("DefaultKnowledgeEntity.get_entity"):
            record = self._get_record(entity_id)
            if record is None:
                return None
            merged_into = record.get("merged_into")
            if merged_into is not None:
                record = self._get_record(merged_into)
                if record is None:
                    return None
            return Entity(id=record["id"], kind=record["kind"])

    def search_entities(self, kind: str | None = None, query: str = "") -> list[Entity]:
        """Not part of the KnowledgeEntity Protocol — real enumeration
        of the registry, as opposed to resolve_entity's one-mention-
        to-one-entity resolution (ADR-0044). Same "extra real-behavior
        accessor" pattern as get_entity() above. Filters live
        (non-tombstoned) entities to those matching `kind` when given,
        and, when `query` is non-blank, to those whose name or any
        alias contains `query` (same case/whitespace normalization
        resolve_entity uses) — so a caller can list-then-narrow, the
        shape a search-as-you-type dropdown needs. An empty registry,
        or a query nothing matches, returns an empty list — a correct
        answer, not a bug."""
        with traced("DefaultKnowledgeEntity.search_entities"):
            normalized_query = _normalize_name(query)
            results: list[Entity] = []
            for record in self._live_entity_records():
                if kind is not None and record["kind"] != kind:
                    continue
                if normalized_query:
                    names = [record.get("name", ""), *record.get("aliases", [])]
                    if not any(normalized_query in _normalize_name(name) for name in names if name):
                        continue
                results.append(Entity(id=record["id"], kind=record["kind"]))
            return results
