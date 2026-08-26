"""Tests for DefaultKnowledgeEntity
(src/components/c04_knowledge_entity.py, ADR-0035).

Most tests run against `_FakeInfrastructure`, a minimal in-memory test
double of the `Infrastructure` Protocol (same shape as
tests/components/test_data_processing_quality.py's own double), so
this component's own logic (normalized/fuzzy resolution, CRUD,
merge's alias/relationship transfer, relationship storage/lookup) is
exercised fast and without a live Postgres.

A small number of `@requires_postgres` tests at the bottom exercise
DefaultKnowledgeEntity against the real DefaultInfrastructure,
mirroring the established skip-cleanly-when-no-live-DB pattern.
"""

import uuid

import pytest

from components.c04_knowledge_entity import DefaultKnowledgeEntity, Entity


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol: only
    store/retrieve/query, since that's all DefaultKnowledgeEntity
    calls. Same semantics as DefaultInfrastructure: store() keys off
    record["id"] when present, query() does containment matching
    (an empty filters dict matches every row in the table)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}

    def store(self, table: str, record: dict) -> str:
        record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
        self._records[(table, record_id)] = dict(record)
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        record = self._records.get((table, id_))
        return dict(record) if record is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        matches = []
        for (record_table, _), record in self._records.items():
            if record_table != table:
                continue
            if all(record.get(key) == value for key, value in filters.items()):
                matches.append(dict(record))
        return matches


def _service(infrastructure=None, fuzzy_match_threshold=None) -> DefaultKnowledgeEntity:
    kwargs = {"infrastructure": infrastructure or _FakeInfrastructure()}
    if fuzzy_match_threshold is not None:
        kwargs["fuzzy_match_threshold"] = fuzzy_match_threshold
    return DefaultKnowledgeEntity(**kwargs)


# --- create_entity -----------------------------------------------------


def test_create_entity_stores_a_real_record_and_returns_a_real_entity():
    service = _service()

    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    assert entity.kind == "Company"
    assert entity.id
    assert service.resolve_entity("Apple Inc") == entity


def test_create_entity_requires_kind():
    service = _service()

    with pytest.raises(ValueError):
        service.create_entity({"name": "Apple Inc"})


def test_create_entity_requires_name():
    service = _service()

    with pytest.raises(ValueError):
        service.create_entity({"kind": "Company"})


def test_create_entity_stores_extra_details_as_attributes():
    infra = _FakeInfrastructure()
    service = _service(infra)

    entity = service.create_entity({"kind": "Company", "name": "Apple Inc", "ticker": "AAPL"})

    record = infra.retrieve("entities", entity.id)
    assert record["attributes"]["ticker"] == "AAPL"


def test_create_entity_propagates_supplied_provenance():
    infra = _FakeInfrastructure()
    service = _service(infra)

    entity = service.create_entity({"kind": "Company", "name": "Apple Inc", "provenance": "UNTRUSTED"})

    record = infra.retrieve("entities", entity.id)
    assert record["provenance"] == "UNTRUSTED"


def test_create_entity_does_not_fabricate_provenance_when_absent():
    infra = _FakeInfrastructure()
    service = _service(infra)

    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    record = infra.retrieve("entities", entity.id)
    assert "provenance" not in record


def test_create_entity_stores_aliases_deduplicated():
    infra = _FakeInfrastructure()
    service = _service(infra)

    entity = service.create_entity(
        {"kind": "Company", "name": "Apple Inc", "aliases": ["Apple", "apple", "AAPL"]}
    )

    record = infra.retrieve("entities", entity.id)
    assert record["aliases"] == ["Apple", "AAPL"]


# --- resolve_entity: exact match ----------------------------------------


def test_resolve_entity_exact_match_on_canonical_name():
    service = _service()
    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    assert service.resolve_entity("Apple Inc") == entity


def test_resolve_entity_exact_match_is_case_and_whitespace_insensitive():
    service = _service()
    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    assert service.resolve_entity("  apple   inc  ") == entity


def test_resolve_entity_exact_match_on_alias():
    service = _service()
    entity = service.create_entity({"kind": "Company", "name": "Apple Inc", "aliases": ["AAPL"]})

    assert service.resolve_entity("aapl") == entity


def test_resolve_entity_returns_none_for_empty_mention():
    service = _service()
    service.create_entity({"kind": "Company", "name": "Apple Inc"})

    assert service.resolve_entity("   ") is None


# --- resolve_entity: fuzzy fallback --------------------------------------


def test_resolve_entity_fuzzy_matches_a_close_typo():
    service = _service()
    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    # "Aple Inc" vs "Apple Inc": edit distance 1, longer length 9, similarity ~0.89
    assert service.resolve_entity("Aple Inc") == entity


def test_resolve_entity_does_not_fuzzy_match_a_short_different_string():
    service = _service()
    service.create_entity({"kind": "Company", "name": "AB"})

    # "AB" vs "AC": edit distance 1, longer length 2, similarity 0.50 — below threshold
    assert service.resolve_entity("AC") is None


def test_resolve_entity_does_not_fuzzy_match_an_unrelated_name():
    service = _service()
    service.create_entity({"kind": "Company", "name": "Apple Inc"})

    assert service.resolve_entity("Union Pacific Corporation") is None


def test_resolve_entity_fuzzy_threshold_is_configurable():
    service = _service(fuzzy_match_threshold=0.95)
    service.create_entity({"kind": "Company", "name": "Apple Inc"})

    # would match at the default 0.80 threshold, not at a stricter 0.95
    assert service.resolve_entity("Aple Inc") is None


def test_resolve_entity_returns_none_when_nothing_clears_the_bar():
    service = _service()

    assert service.resolve_entity("Nonexistent Corp") is None


def test_resolve_entity_excludes_merge_tombstones_from_matching():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer"})

    service.merge_entities(survivor, loser)

    # "Apple Computer" now resolves via the transferred alias, to the survivor,
    # never independently to the tombstoned loser record.
    assert service.resolve_entity("Apple Computer") == survivor


# --- merge_entities ------------------------------------------------------


def test_merge_entities_returns_the_first_argument_as_survivor():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})

    result = service.merge_entities(survivor, loser)

    assert result.id == survivor.id


def test_merge_entities_transfers_losers_name_as_an_alias():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})

    service.merge_entities(survivor, loser)

    assert service.resolve_entity("Apple Computer Inc") == survivor


def test_merge_entities_transfers_losers_aliases():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity(
        {"kind": "Company", "name": "Apple Computer Inc", "aliases": ["Apple Computer"]}
    )

    service.merge_entities(survivor, loser)

    assert service.resolve_entity("Apple Computer") == survivor


def test_merge_entities_transfers_relationships_referencing_the_loser():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})
    supplier = service.create_entity({"kind": "Company", "name": "Foxconn"})
    service.link_entities(supplier, loser, "supplies")

    service.merge_entities(survivor, loser)

    relationships = service.query_relationships(survivor, kind="supplies")
    assert len(relationships) == 1
    assert relationships[0].source_entity_id == supplier.id
    assert relationships[0].target_entity_id == survivor.id


def test_merge_entities_does_not_duplicate_relationships():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})
    service.link_entities(survivor, loser, "predecessor_of")

    service.merge_entities(survivor, loser)

    relationships = service.represent_relationships(survivor)
    assert len(relationships) == 1


def test_merge_entities_survivors_attributes_win_conflicts():
    infra = _FakeInfrastructure()
    service = _service(infra)
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc", "sector": "Tech"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc", "sector": "Legacy Tech"})

    result = service.merge_entities(survivor, loser)

    record = infra.retrieve("entities", result.id)
    assert record["attributes"]["sector"] == "Tech"


def test_merge_entities_fills_in_attributes_survivor_lacks():
    infra = _FakeInfrastructure()
    service = _service(infra)
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc", "founded": "1976"})

    result = service.merge_entities(survivor, loser)

    record = infra.retrieve("entities", result.id)
    assert record["attributes"]["founded"] == "1976"


def test_merge_entities_leaves_loser_as_a_tombstone():
    infra = _FakeInfrastructure()
    service = _service(infra)
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})

    service.merge_entities(survivor, loser)

    tombstone = infra.retrieve("entities", loser.id)
    assert tombstone["merged_into"] == survivor.id


# --- link_entities / represent_relationships / query_relationships -------


def test_link_entities_creates_a_real_relationship():
    service = _service()
    apple = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    tech = service.create_entity({"kind": "Sector", "name": "Technology"})

    relationship = service.link_entities(apple, tech, "belongs_to")

    assert relationship.source_entity_id == apple.id
    assert relationship.target_entity_id == tech.id
    assert relationship.kind == "belongs_to"


def test_represent_relationships_returns_relationships_where_entity_is_source():
    service = _service()
    apple = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    tech = service.create_entity({"kind": "Sector", "name": "Technology"})
    service.link_entities(apple, tech, "belongs_to")

    relationships = service.represent_relationships(apple)

    assert len(relationships) == 1
    assert relationships[0].kind == "belongs_to"


def test_represent_relationships_returns_relationships_where_entity_is_target():
    service = _service()
    apple = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    tech = service.create_entity({"kind": "Sector", "name": "Technology"})
    service.link_entities(apple, tech, "belongs_to")

    relationships = service.represent_relationships(tech)

    assert len(relationships) == 1
    assert relationships[0].source_entity_id == apple.id


def test_query_relationships_filters_by_kind():
    service = _service()
    apple = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    tech = service.create_entity({"kind": "Sector", "name": "Technology"})
    foxconn = service.create_entity({"kind": "Company", "name": "Foxconn"})
    service.link_entities(apple, tech, "belongs_to")
    service.link_entities(foxconn, apple, "supplies")

    belongs_to = service.query_relationships(apple, kind="belongs_to")
    supplies = service.query_relationships(apple, kind="supplies")

    assert len(belongs_to) == 1
    assert belongs_to[0].kind == "belongs_to"
    assert len(supplies) == 1
    assert supplies[0].source_entity_id == foxconn.id


def test_query_relationships_without_kind_returns_everything():
    service = _service()
    apple = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    tech = service.create_entity({"kind": "Sector", "name": "Technology"})
    foxconn = service.create_entity({"kind": "Company", "name": "Foxconn"})
    service.link_entities(apple, tech, "belongs_to")
    service.link_entities(foxconn, apple, "supplies")

    relationships = service.query_relationships(apple)

    assert len(relationships) == 2


def test_query_relationships_returns_empty_for_an_unrelated_entity():
    service = _service()
    apple = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    assert service.query_relationships(apple) == []


# --- update_knowledge ------------------------------------------------------


def test_update_knowledge_replaces_the_name_field():
    infra = _FakeInfrastructure()
    service = _service(infra)
    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    updated = service.update_knowledge(entity, {"name": "Apple Incorporated"})

    assert updated.id == entity.id
    assert service.resolve_entity("Apple Incorporated") == entity
    assert service.resolve_entity("Apple Inc") is None


def test_update_knowledge_writes_arbitrary_keys_into_attributes():
    infra = _FakeInfrastructure()
    service = _service(infra)
    entity = service.create_entity({"kind": "Company", "name": "Apple Inc"})

    service.update_knowledge(entity, {"market_cap": "3T"})

    record = infra.retrieve("entities", entity.id)
    assert record["attributes"]["market_cap"] == "3T"


def test_update_knowledge_redirects_a_merged_away_entity_to_the_survivor():
    infra = _FakeInfrastructure()
    service = _service(infra)
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})
    service.merge_entities(survivor, loser)

    service.update_knowledge(loser, {"market_cap": "3T"})

    record = infra.retrieve("entities", survivor.id)
    assert record["attributes"]["market_cap"] == "3T"


def test_update_knowledge_handles_an_entity_with_no_stored_record():
    service = _service()
    phantom = Entity(id="never-created", kind="Company")

    updated = service.update_knowledge(phantom, {"name": "Phantom Corp"})

    assert updated.id == "never-created"
    assert service.resolve_entity("Phantom Corp") == phantom


# --- get_entity / search_entities (ADR-0044) --------------------------------


def test_get_entity_returns_a_real_entity_for_a_known_id():
    service = _service()
    created = service.create_entity({"kind": "Security", "name": "Apple Inc"})

    assert service.get_entity(created.id) == created


def test_get_entity_returns_none_for_an_unknown_id():
    service = _service()

    assert service.get_entity("never-created") is None


def test_get_entity_redirects_a_merged_away_id_to_the_survivor():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})
    service.merge_entities(survivor, loser)

    assert service.get_entity(loser.id) == survivor


def test_search_entities_on_an_empty_registry_returns_an_empty_list():
    service = _service()

    assert service.search_entities(kind="Security") == []


def test_search_entities_filters_by_kind():
    service = _service()
    security = service.create_entity({"kind": "Security", "name": "Apple Inc"})
    service.create_entity({"kind": "Sector", "name": "Technology"})

    assert service.search_entities(kind="Security") == [security]


def test_search_entities_filters_by_query_against_name_and_aliases():
    service = _service()
    apple = service.create_entity({"kind": "Security", "name": "Apple Inc", "aliases": ["AAPL"]})
    service.create_entity({"kind": "Security", "name": "Microsoft Corp"})

    assert service.search_entities(kind="Security", query="aapl") == [apple]
    assert service.search_entities(kind="Security", query="app") == [apple]


def test_search_entities_excludes_merge_tombstones():
    service = _service()
    survivor = service.create_entity({"kind": "Company", "name": "Apple Inc"})
    loser = service.create_entity({"kind": "Company", "name": "Apple Computer Inc"})
    service.merge_entities(survivor, loser)

    results = service.search_entities(kind="Company")

    assert results == [survivor]


# --- Live Postgres integration (skips cleanly without docker-compose) ------


def _postgres_available() -> bool:
    try:
        import psycopg

        from infrastructure_postgres import DEFAULT_POSTGRES_DSN

        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="no live Postgres reachable — run `docker-compose up -d` for real coverage",
)


@requires_postgres
def test_create_resolve_link_and_query_against_real_postgres():
    from infrastructure_postgres import DefaultInfrastructure

    service = DefaultKnowledgeEntity(infrastructure=DefaultInfrastructure())
    suffix = uuid.uuid4().hex
    apple = service.create_entity({"kind": "Company", "name": f"Apple Inc {suffix}"})
    tech = service.create_entity({"kind": "Sector", "name": f"Technology {suffix}"})

    service.link_entities(apple, tech, "belongs_to")
    resolved = service.resolve_entity(f"apple inc {suffix}")
    relationships = service.query_relationships(apple, kind="belongs_to")

    assert resolved == apple
    assert len(relationships) == 1
    assert relationships[0].target_entity_id == tech.id
