"""Tests for DefaultEventObservation
(src/components/c07_event_observation.py, ADR-0036).

Most tests run against `_FakeInfrastructure`, a minimal in-memory test
double of the `Infrastructure` Protocol (same shape as
tests/components/test_knowledge_entity.py's own double), paired with a
real `DefaultKnowledgeEntity` (04) backed by the same fake
infrastructure instance — so link_event_to_entities/correlate_events'
real entity resolution/linking is exercised for real, not mocked away.

A small number of `@requires_postgres` tests at the bottom exercise
DefaultEventObservation against the real DefaultInfrastructure,
mirroring the established skip-cleanly-when-no-live-DB pattern.
"""

import uuid

import pytest

from components.c04_knowledge_entity import DefaultKnowledgeEntity
from components.c07_event_observation import (
    _DEFAULT_ANOMALY_Z_THRESHOLD,
    DefaultEventObservation,
    Event,
    Observation,
)


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol: only
    store/retrieve/query, since that's all DefaultEventObservation and
    DefaultKnowledgeEntity call. Same semantics as
    DefaultInfrastructure: store() keys off record["id"] when present,
    query() does containment matching (an empty filters dict matches
    every row in the table)."""

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


class _SpyAuditManager:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, detail: dict) -> None:
        self.events.append((event_type, detail))


def _service(infrastructure=None, audit_manager=None, **kwargs) -> DefaultEventObservation:
    infra = infrastructure or _FakeInfrastructure()
    knowledge_entity = kwargs.pop("knowledge_entity", None) or DefaultKnowledgeEntity(infrastructure=infra)
    return DefaultEventObservation(
        infrastructure=infra,
        knowledge_entity=knowledge_entity,
        audit_manager=audit_manager or _SpyAuditManager(),
        **kwargs,
    )


def _observe_sequence(service: DefaultEventObservation, entity_id: str, metric: str, values: list[float]) -> list[Observation]:
    return [service.observe({"entity_id": entity_id, "metric": metric, "value": value}) for value in values]


# --- observe -----------------------------------------------------------


def test_observe_stores_a_real_record_and_returns_a_real_observation():
    infra = _FakeInfrastructure()
    service = _service(infra)

    observation = service.observe({"entity_id": "AAPL", "metric": "price", "value": 190.5})

    assert observation.entity_id == "AAPL"
    assert observation.metric == "price"
    assert observation.value == 190.5
    assert observation.id
    assert observation.observed_at
    record = infra.retrieve("observations", observation.id)
    assert record["value"] == 190.5
    assert record["sequence"] == 0


def test_observe_assigns_increasing_sequence_per_entity_metric():
    infra = _FakeInfrastructure()
    service = _service(infra)

    first = service.observe({"entity_id": "AAPL", "metric": "price", "value": 100.0})
    second = service.observe({"entity_id": "AAPL", "metric": "price", "value": 101.0})

    assert infra.retrieve("observations", first.id)["sequence"] == 0
    assert infra.retrieve("observations", second.id)["sequence"] == 1


def test_observe_requires_entity_id():
    service = _service()
    with pytest.raises(ValueError):
        service.observe({"metric": "price", "value": 1.0})


def test_observe_requires_metric():
    service = _service()
    with pytest.raises(ValueError):
        service.observe({"entity_id": "AAPL", "value": 1.0})


def test_observe_requires_value():
    service = _service()
    with pytest.raises(ValueError):
        service.observe({"entity_id": "AAPL", "metric": "price"})


# --- detect_change -------------------------------------------------------


def test_detect_change_returns_none_below_the_percent_floor():
    service = _service()
    prior = Observation(entity_id="AAPL", metric="price", value=100.0)
    current = Observation(entity_id="AAPL", metric="price", value=100.5)  # 0.5% move

    assert service.detect_change(current, prior) is None


def test_detect_change_returns_a_real_change_above_the_percent_floor():
    service = _service()
    prior = Observation(entity_id="AAPL", metric="price", value=100.0)
    current = Observation(entity_id="AAPL", metric="price", value=105.0)  # 5% move

    change = service.detect_change(current, prior)

    assert change is not None
    assert change.delta == 5.0
    assert change.percent_change == pytest.approx(0.05)


def test_detect_change_handles_zero_prior_value_as_a_real_change_when_nonzero():
    service = _service()
    prior = Observation(entity_id="AAPL", metric="new_metric", value=0.0)
    current = Observation(entity_id="AAPL", metric="new_metric", value=10.0)

    change = service.detect_change(current, prior)

    assert change is not None
    assert change.percent_change == float("inf")


def test_detect_change_handles_zero_prior_and_zero_current_as_no_change():
    service = _service()
    prior = Observation(entity_id="AAPL", metric="new_metric", value=0.0)
    current = Observation(entity_id="AAPL", metric="new_metric", value=0.0)

    assert service.detect_change(current, prior) is None


def test_detect_change_threshold_is_configurable():
    service = _service(change_percent_floor=0.10)
    prior = Observation(entity_id="AAPL", metric="price", value=100.0)
    current = Observation(entity_id="AAPL", metric="price", value=105.0)  # 5% move, below 10% floor

    assert service.detect_change(current, prior) is None


# --- detect_anomaly --------------------------------------------------------


def test_detect_anomaly_returns_none_with_insufficient_history():
    infra = _FakeInfrastructure()
    service = _service(infra)
    observations = _observe_sequence(service, "AAPL", "price", [100.0, 101.0, 99.0])

    result = service.detect_anomaly(observations[-1])

    assert result is None
    assert infra.query("observations", {"entity_id": "AAPL", "metric": "price"})


def test_detect_anomaly_flags_a_real_statistical_outlier():
    service = _service()
    stable_values = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2]
    _observe_sequence(service, "AAPL", "price", stable_values)
    outlier = service.observe({"entity_id": "AAPL", "metric": "price", "value": 500.0})

    anomaly = service.detect_anomaly(outlier)

    assert anomaly is not None
    assert anomaly.observation == outlier
    assert anomaly.magnitude > _DEFAULT_ANOMALY_Z_THRESHOLD


def test_detect_anomaly_returns_none_for_a_value_within_normal_range():
    service = _service()
    stable_values = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2]
    _observe_sequence(service, "AAPL", "price", stable_values)
    normal = service.observe({"entity_id": "AAPL", "metric": "price", "value": 100.1})

    assert service.detect_anomaly(normal) is None


def test_detect_anomaly_handles_constant_history_with_zero_stdev():
    service = _service()
    _observe_sequence(service, "AAPL", "flat_metric", [50.0] * 6)
    differing = service.observe({"entity_id": "AAPL", "metric": "flat_metric", "value": 75.0})

    anomaly = service.detect_anomaly(differing)

    assert anomaly is not None
    assert anomaly.magnitude == pytest.approx(25.0)


def test_detect_anomaly_records_via_audit_manager():
    audit_manager = _SpyAuditManager()
    service = _service(audit_manager=audit_manager)
    stable_values = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2]
    _observe_sequence(service, "AAPL", "price", stable_values)
    outlier = service.observe({"entity_id": "AAPL", "metric": "price", "value": 500.0})

    service.detect_anomaly(outlier)

    assert any(event_type == "anomaly_detected" for event_type, _ in audit_manager.events)


# --- detect_event ----------------------------------------------------------


def test_detect_event_returns_none_when_nothing_qualifies():
    service = _service()
    observations = _observe_sequence(service, "AAPL", "price", [100.0, 100.5])

    assert service.detect_event(observations) is None


def test_detect_event_qualifies_a_large_enough_change():
    service = _service()
    _observe_sequence(service, "AAPL", "price", [100.0])
    big_move = service.observe({"entity_id": "AAPL", "metric": "price", "value": 110.0})  # 10% move

    event = service.detect_event([big_move])

    assert event is not None
    assert event.entity_ids == ["AAPL"]
    assert event.metric == "price"
    assert event.type == "market_movement"


def test_detect_event_small_real_change_does_not_qualify_alone():
    service = _service()
    _observe_sequence(service, "AAPL", "price", [100.0])
    small_move = service.observe({"entity_id": "AAPL", "metric": "price", "value": 103.0})  # 3% move: real change, not event-worthy

    assert service.detect_event([small_move]) is None


def test_detect_event_anomaly_always_qualifies():
    service = _service()
    stable_values = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2]
    _observe_sequence(service, "AAPL", "price", stable_values)
    outlier = service.observe({"entity_id": "AAPL", "metric": "price", "value": 500.0})

    event = service.detect_event([outlier])

    assert event is not None
    assert event.metric == "price"


def test_detect_event_combines_multiple_entities_and_picks_strongest_metric():
    service = _service()
    _observe_sequence(service, "AAPL", "price", [100.0])
    aapl_move = service.observe({"entity_id": "AAPL", "metric": "price", "value": 106.0})  # 6% move
    _observe_sequence(service, "MSFT", "revenue", [1000.0])
    msft_move = service.observe({"entity_id": "MSFT", "metric": "revenue", "value": 1200.0})  # 20% move

    event = service.detect_event([aapl_move, msft_move])

    assert event is not None
    assert set(event.entity_ids) == {"AAPL", "MSFT"}
    assert event.metric == "revenue"  # the stronger-magnitude trigger
    assert event.type == "earnings"


def test_detect_event_persists_to_infrastructure():
    infra = _FakeInfrastructure()
    service = _service(infra)
    _observe_sequence(service, "AAPL", "price", [100.0])
    big_move = service.observe({"entity_id": "AAPL", "metric": "price", "value": 110.0})

    event = service.detect_event([big_move])

    stored = infra.retrieve("events", event.id)
    assert stored is not None
    assert stored["type"] == "market_movement"


# --- classify_event ----------------------------------------------------


@pytest.mark.parametrize(
    "metric,expected_type",
    [
        ("quarterly_earnings", "earnings"),
        ("eps", "earnings"),
        ("revenue", "earnings"),
        ("close_price", "market_movement"),
        ("trading_volume", "market_movement"),
        ("employee_count", "general"),
    ],
)
def test_classify_event_maps_metric_to_category(metric, expected_type):
    service = _service()
    event = Event(id="event-1", type="", entity_ids=["AAPL"], metric=metric)

    assert service.classify_event(event) == expected_type


# --- link_event_to_entities ----------------------------------------------


def test_link_event_to_entities_resolves_and_links_real_entities():
    infra = _FakeInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Company", "name": "Apple Inc", "aliases": ["AAPL"]})
    microsoft = knowledge_entity.create_entity({"kind": "Company", "name": "Microsoft", "aliases": ["MSFT"]})
    service = _service(infra, knowledge_entity=knowledge_entity)
    event = Event(id="event-1", type="market_movement", entity_ids=["AAPL", "MSFT"])

    resolved_ids = service.link_event_to_entities(event)

    assert set(resolved_ids) == {apple.id, microsoft.id}
    relationships = knowledge_entity.query_relationships(apple, kind="co_occurred_in_event")
    assert len(relationships) == 1
    assert relationships[0].target_entity_id == microsoft.id


def test_link_event_to_entities_drops_unresolvable_mentions():
    infra = _FakeInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Company", "name": "Apple Inc", "aliases": ["AAPL"]})
    service = _service(infra, knowledge_entity=knowledge_entity)
    event = Event(id="event-1", type="market_movement", entity_ids=["AAPL", "UNKNOWN_TICKER"])

    resolved_ids = service.link_event_to_entities(event)

    assert resolved_ids == [apple.id]


def test_link_event_to_entities_single_entity_creates_no_relationship():
    infra = _FakeInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Company", "name": "Apple Inc", "aliases": ["AAPL"]})
    service = _service(infra, knowledge_entity=knowledge_entity)
    event = Event(id="event-1", type="market_movement", entity_ids=["AAPL"])

    resolved_ids = service.link_event_to_entities(event)

    assert resolved_ids == [apple.id]
    assert knowledge_entity.query_relationships(apple) == []


# --- correlate_events ----------------------------------------------------


def test_correlate_events_matches_shared_entity_within_window():
    service = _service()
    first = Event(id="e1", type="market_movement", entity_ids=["AAPL"], detected_at="2026-08-26T10:00:00")
    second = Event(id="e2", type="earnings", entity_ids=["AAPL"], detected_at="2026-08-26T10:30:00")

    pairs = service.correlate_events([first, second])

    assert pairs == [(first, second)]


def test_correlate_events_excludes_shared_entity_outside_window():
    service = _service()
    first = Event(id="e1", type="market_movement", entity_ids=["AAPL"], detected_at="2026-08-26T10:00:00")
    second = Event(id="e2", type="earnings", entity_ids=["AAPL"], detected_at="2026-08-26T14:00:00")

    assert service.correlate_events([first, second]) == []


def test_correlate_events_excludes_unrelated_events_within_window():
    service = _service()
    first = Event(id="e1", type="market_movement", entity_ids=["AAPL"], detected_at="2026-08-26T10:00:00")
    second = Event(id="e2", type="earnings", entity_ids=["MSFT"], detected_at="2026-08-26T10:05:00")

    assert service.correlate_events([first, second]) == []


def test_correlate_events_matches_related_entities_via_knowledge_graph():
    infra = _FakeInfrastructure()
    knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
    apple = knowledge_entity.create_entity({"kind": "Company", "name": "Apple Inc"})
    tech_sector = knowledge_entity.create_entity({"kind": "Sector", "name": "Technology"})
    knowledge_entity.link_entities(apple, tech_sector, "belongs_to")
    service = _service(infra, knowledge_entity=knowledge_entity)
    first = Event(id="e1", type="market_movement", entity_ids=[apple.id], detected_at="2026-08-26T10:00:00")
    second = Event(id="e2", type="market_movement", entity_ids=[tech_sector.id], detected_at="2026-08-26T10:15:00")

    pairs = service.correlate_events([first, second])

    assert pairs == [(first, second)]


def test_correlate_events_excludes_pairs_with_unparseable_timestamps():
    service = _service()
    first = Event(id="e1", type="market_movement", entity_ids=["AAPL"], detected_at="not-a-timestamp")
    second = Event(id="e2", type="earnings", entity_ids=["AAPL"], detected_at="2026-08-26T10:00:00")

    assert service.correlate_events([first, second]) == []


# --- retrieve_events ----------------------------------------------------


def test_retrieve_events_filters_by_type():
    infra = _FakeInfrastructure()
    service = _service(infra)
    _observe_sequence(service, "AAPL", "price", [100.0])
    price_event = service.detect_event([service.observe({"entity_id": "AAPL", "metric": "price", "value": 110.0})])
    _observe_sequence(service, "MSFT", "revenue", [1000.0])
    revenue_event = service.detect_event(
        [service.observe({"entity_id": "MSFT", "metric": "revenue", "value": 1300.0})]
    )

    results = service.retrieve_events({"type": "earnings"})

    assert [event.id for event in results] == [revenue_event.id]
    assert price_event.id not in [event.id for event in results]


def test_retrieve_events_filters_by_entity_id():
    service = _service()
    _observe_sequence(service, "AAPL", "price", [100.0])
    aapl_event = service.detect_event([service.observe({"entity_id": "AAPL", "metric": "price", "value": 110.0})])
    _observe_sequence(service, "MSFT", "price", [100.0])
    service.detect_event([service.observe({"entity_id": "MSFT", "metric": "price", "value": 110.0})])

    results = service.retrieve_events({"entity_id": "AAPL"})

    assert [event.id for event in results] == [aapl_event.id]


def test_retrieve_events_filters_by_since_and_until():
    infra = _FakeInfrastructure()
    service = _service(infra)
    infra.store(
        "events",
        {"id": "old-event", "type": "market_movement", "entity_ids": ["AAPL"], "metric": "price", "magnitude": 0.1, "detected_at": "2026-01-01T00:00:00"},
    )
    infra.store(
        "events",
        {"id": "new-event", "type": "market_movement", "entity_ids": ["AAPL"], "metric": "price", "magnitude": 0.1, "detected_at": "2026-08-26T00:00:00"},
    )

    results = service.retrieve_events({"since": "2026-06-01T00:00:00"})

    assert [event.id for event in results] == ["new-event"]


def test_retrieve_events_returns_empty_list_when_nothing_matches():
    service = _service()

    assert service.retrieve_events({"type": "earnings"}) == []


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
def test_observe_detect_event_and_retrieve_against_real_postgres():
    from infrastructure_postgres import DefaultInfrastructure

    infra = DefaultInfrastructure()
    service = DefaultEventObservation(infrastructure=infra)
    suffix = uuid.uuid4().hex
    entity_id = f"AAPL-{suffix}"
    service.observe({"entity_id": entity_id, "metric": "price", "value": 100.0})
    moved = service.observe({"entity_id": entity_id, "metric": "price", "value": 112.0})

    event = service.detect_event([moved])
    retrieved = service.retrieve_events({"entity_id": entity_id})

    assert event is not None
    assert any(found.id == event.id for found in retrieved)
