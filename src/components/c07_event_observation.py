"""Event & Observation (component 07) — the system's perception layer.

Interfaces: <- Knowledge & Entity Model (04), -> Memory (06).

Design: ADR-0036 (real mechanism: `Infrastructure`-backed observation
history, a percent-change floor for `detect_change`, a z-score outlier
rule for `detect_anomaly`, rule-based aggregation for `detect_event`
and classification for `classify_event`, real entity resolution/
linking via `DefaultKnowledgeEntity` for `link_event_to_entities`, a
shared-entity-or-related-entity time-windowed rule for
`correlate_events`). No LLM anywhere in this component — change/
anomaly/event detection is statistical and rule-based over numeric
observations, not generation.

`Observation`, `Change`, `Anomaly`, and `Event` each gained a small
number of fields in this pass beyond their whiteboard shape (an
identity/timestamp on `Observation`, a computed `percent_change` on
`Change`, a computed `magnitude` on `Anomaly`, and an originating
`metric`/`magnitude`/`detected_at` on `Event`) — the same kind of
necessary, additive dataclass extension ADR-0032 made to `RawDocument`
(adding `fetched_at`) for the same reason: the whiteboard shape didn't
carry what a real (not stubbed) implementation needs to do its job.
See ADR-0036's Context for the full reasoning.
"""

import itertools
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from components.c04_knowledge_entity import DefaultKnowledgeEntity, Entity, KnowledgeEntity
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


@dataclass
class Observation:
    entity_id: str
    metric: str
    value: float
    observed_at: str = ""
    id: str = ""


@dataclass
class Change:
    observation: Observation
    delta: float
    percent_change: float = 0.0


@dataclass
class Anomaly:
    observation: Observation
    reason: str
    magnitude: float = 0.0


@dataclass
class Event:
    id: str
    type: str
    entity_ids: list[str]
    metric: str = ""
    magnitude: float = 0.0
    detected_at: str = ""


class EventObservation(Protocol):
    def observe(self, raw: dict) -> Observation:
        ...

    def detect_change(self, current: Observation, prior: Observation) -> Change | None:
        ...

    def detect_anomaly(self, observation: Observation) -> Anomaly | None:
        ...

    def detect_event(self, observations: list[Observation]) -> Event | None:
        ...

    def classify_event(self, event: Event) -> str:
        ...

    def link_event_to_entities(self, event: Event) -> list[str]:
        ...

    def correlate_events(self, events: list[Event]) -> list[tuple[Event, Event]]:
        ...

    def retrieve_events(self, filters: dict) -> list[Event]:
        ...


class StubEventObservation:
    """Structural implementation of EventObservation. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def observe(self, raw: dict) -> Observation:
        with traced("StubEventObservation.observe"):
            return Observation(entity_id="stub-id", metric="stub", value=0.0)

    def detect_change(self, current: Observation, prior: Observation) -> Change | None:
        with traced("StubEventObservation.detect_change"):
            return None

    def detect_anomaly(self, observation: Observation) -> Anomaly | None:
        with traced("StubEventObservation.detect_anomaly"):
            return None

    def detect_event(self, observations: list[Observation]) -> Event | None:
        with traced("StubEventObservation.detect_event"):
            return None

    def classify_event(self, event: Event) -> str:
        with traced("StubEventObservation.classify_event"):
            return ""

    def link_event_to_entities(self, event: Event) -> list[str]:
        with traced("StubEventObservation.link_event_to_entities"):
            return []

    def correlate_events(self, events: list[Event]) -> list[tuple[Event, Event]]:
        with traced("StubEventObservation.correlate_events"):
            return []

    def retrieve_events(self, filters: dict) -> list[Event]:
        with traced("StubEventObservation.retrieve_events"):
            return []


# --- DefaultEventObservation: real, statistical/rule-based mechanism ---
#
# ADR-0036. Tables this component owns:
#   "observations" — one row per recorded observation: {"id",
#                     "entity_id", "metric", "value", "observed_at",
#                     "sequence"}. "sequence" is a real, monotonically
#                     increasing per-(entity_id, metric) counter
#                     (the count of prior rows for that pair at
#                     insertion time) — the ordering signal
#                     detect_change/detect_anomaly's history lookups
#                     rely on, since a caller-supplied "observed_at"
#                     string alone gives no guaranteed tie-break.
#   "events"       — one row per detected event: {"id", "type",
#                     "entity_ids", "metric", "magnitude",
#                     "detected_at"}, written by detect_event and read
#                     back by retrieve_events.
#
# Observation.entity_id is treated as a *mention* (whatever identifier
# string the raw observation feed uses — a ticker, a company name),
# not assumed to already be a canonical Entity id: link_event_to_entities
# resolves it for real against the Knowledge & Entity registry (04)
# via resolve_entity, the same "mention" contract that Protocol method
# already documents.

_OBSERVATIONS_TABLE = "observations"
_EVENTS_TABLE = "events"

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches c02/c03's time.strftime format

# A raw percent-change floor below which a move is treated as noise,
# not a real change — 2% is small enough to catch a genuine intraday
# move in a financial metric (price, volume, a fundamentals figure)
# while filtering out the kind of sub-1%-of-value jitter that's
# indistinguishable from measurement/rounding noise for this domain.
# Overridable per instance, documented default, not a silent constant.
_DEFAULT_CHANGE_PERCENT_FLOOR = 0.02

# detect_anomaly's real statistical check: a z-score against the last
# N observations for the same entity/metric. N=20 balances a large
# enough sample for a stable mean/stdev estimate against staying
# responsive to a metric whose regime shifts over time (a 20-reading
# window ages out stale history rather than anchoring to all-time
# stats). A z-score threshold of 2.5 is roughly the 98.8th percentile
# two-tailed under a normal approximation — deliberately stricter than
# detect_change's own floor, since "anomalous" should be a rarer,
# stronger claim than "changed." Fewer than 5 prior observations is
# treated as not enough history to compute a defensible statistic —
# detect_anomaly honestly reports "unknown" (None) rather than a
# guess built on a near-empty sample.
_DEFAULT_ANOMALY_HISTORY_SIZE = 20
_DEFAULT_ANOMALY_MIN_HISTORY = 5
_DEFAULT_ANOMALY_Z_THRESHOLD = 2.5

# detect_event's own bar, stacked on top of detect_change's: a Change
# already cleared the "is this real, not noise" floor above by the
# time detect_event sees it, but not every real change is worth
# surfacing as an Event for downstream components to act on. 5% is a
# documented, stricter magnitude bar for "event-worthy" — an anomaly
# (which already cleared a much stronger statistical bar) always
# qualifies regardless of this constant.
_DEFAULT_EVENT_CHANGE_MAGNITUDE_THRESHOLD = 0.05

# correlate_events' real time window: two events are only considered
# correlated if they were both detected within one hour of each other.
# Financial events for the same or related entities that land within
# the same trading session plausibly describe the same real-world
# development (e.g., an earnings anomaly and a market-movement event
# for the same company minutes apart); events days apart that happen
# to share an entity are a coincidence of scope, not a real temporal
# correlation. Overridable per instance.
_DEFAULT_CORRELATION_TIME_WINDOW_SECONDS = 60.0 * 60.0

_EVENT_COOCCURRENCE_RELATIONSHIP_KIND = "co_occurred_in_event"

# classify_event's rule: category is a property of *what* is being
# measured (an earnings figure vs. a market/price figure), not of how
# large the move was — a 3% EPS move and a 30% EPS move are both
# "earnings" events. Magnitude is deliberately not a classification
# axis for this reason (see ADR-0036's Alternatives); it's still
# carried on Event for any downstream consumer that wants severity as
# a separate signal. Metric-name keyword matching, case-insensitive,
# substring match (so "quarterly_revenue" still matches "revenue").
_EARNINGS_METRIC_KEYWORDS = ("earnings", "eps", "revenue", "profit", "net_income", "guidance")
_MARKET_MOVEMENT_METRIC_KEYWORDS = ("price", "close", "open", "volume", "market_cap", "shares")
_GENERAL_EVENT_TYPE = "general"


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT)
    except (ValueError, TypeError):
        return None


class DefaultEventObservation:
    """Real implementation of EventObservation (ADR-0036).

    Every method is `Infrastructure`-backed (ADR-0019's interface,
    `DefaultInfrastructure` by default) — never an in-memory registry.
    observe/detect_change/detect_anomaly/detect_event/classify_event/
    correlate_events/retrieve_events are all statistical or rule-based
    over numeric observation history; link_event_to_entities is real
    entity resolution/linking delegated to `KnowledgeEntity`
    (`DefaultKnowledgeEntity` by default) rather than reinventing
    resolution here. No LLM anywhere in this class.

    detect_anomaly audits every real anomaly it finds via
    `AuditManager` (`DefaultAuditManager` by default) — an anomaly is
    exactly the kind of surprising, worth-a-record signal
    `AuditManager.record`'s own docstring already names quarantine
    decisions and blocked claims as (Memory/Evidence & Verification's
    precedent); detect_change does not audit every real change, since
    a change clearing only a 2% floor is common, expected traffic, not
    an event worth a standing audit trail entry on its own.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        knowledge_entity: KnowledgeEntity | None = None,
        audit_manager: AuditManager | None = None,
        change_percent_floor: float = _DEFAULT_CHANGE_PERCENT_FLOOR,
        anomaly_history_size: int = _DEFAULT_ANOMALY_HISTORY_SIZE,
        anomaly_min_history: int = _DEFAULT_ANOMALY_MIN_HISTORY,
        anomaly_z_threshold: float = _DEFAULT_ANOMALY_Z_THRESHOLD,
        event_change_magnitude_threshold: float = _DEFAULT_EVENT_CHANGE_MAGNITUDE_THRESHOLD,
        correlation_time_window_seconds: float = _DEFAULT_CORRELATION_TIME_WINDOW_SECONDS,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._knowledge_entity = knowledge_entity or DefaultKnowledgeEntity(infrastructure=self._infrastructure)
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._change_percent_floor = change_percent_floor
        self._anomaly_history_size = anomaly_history_size
        self._anomaly_min_history = anomaly_min_history
        self._anomaly_z_threshold = anomaly_z_threshold
        self._event_change_magnitude_threshold = event_change_magnitude_threshold
        self._correlation_time_window_seconds = correlation_time_window_seconds

    def _history_records(self, entity_id: str, metric: str) -> list[dict]:
        records = self._infrastructure.query(_OBSERVATIONS_TABLE, {"entity_id": entity_id, "metric": metric})
        return sorted(records, key=lambda record: record["sequence"])

    def observe(self, raw: dict) -> Observation:
        with traced("DefaultEventObservation.observe"):
            entity_id = raw.get("entity_id")
            metric = raw.get("metric")
            if not entity_id:
                raise ValueError("observe requires a non-empty 'entity_id' in raw")
            if not metric:
                raise ValueError("observe requires a non-empty 'metric' in raw")
            if "value" not in raw:
                raise ValueError("observe requires a 'value' in raw")
            value = float(raw["value"])
            observed_at = raw.get("observed_at") or datetime.now().strftime(_TIMESTAMP_FORMAT)
            observation_id = str(raw.get("id") or f"observation-{uuid.uuid4()}")
            sequence = len(self._infrastructure.query(_OBSERVATIONS_TABLE, {"entity_id": entity_id, "metric": metric}))
            self._infrastructure.store(
                _OBSERVATIONS_TABLE,
                {
                    "id": observation_id,
                    "entity_id": entity_id,
                    "metric": metric,
                    "value": value,
                    "observed_at": observed_at,
                    "sequence": sequence,
                },
            )
            return Observation(entity_id=entity_id, metric=metric, value=value, observed_at=observed_at, id=observation_id)

    def _prior_observation(self, observation: Observation) -> Observation | None:
        """The immediately-preceding stored observation for the same
        entity/metric, found by matching `observation.id` against
        recorded history and stepping back one `sequence` slot. If
        `observation` isn't found in its own history (it was never
        passed through `observe()`, so it carries no real `id`), this
        falls back to the most recently stored record for that
        entity/metric — the best available prior, documented here
        rather than silently assumed correct."""
        history = self._history_records(observation.entity_id, observation.metric)
        if not history:
            return None
        for index, record in enumerate(history):
            if record["id"] == observation.id and observation.id:
                if index == 0:
                    return None
                prior_record = history[index - 1]
                return Observation(
                    entity_id=prior_record["entity_id"],
                    metric=prior_record["metric"],
                    value=prior_record["value"],
                    observed_at=prior_record["observed_at"],
                    id=prior_record["id"],
                )
        fallback = history[-1]
        return Observation(
            entity_id=fallback["entity_id"],
            metric=fallback["metric"],
            value=fallback["value"],
            observed_at=fallback["observed_at"],
            id=fallback["id"],
        )

    def _recent_history_values(self, entity_id: str, metric: str, exclude_id: str, limit: int) -> list[float]:
        history = self._history_records(entity_id, metric)
        recent = [record for record in reversed(history) if not exclude_id or record["id"] != exclude_id]
        return [record["value"] for record in recent[:limit]]

    def detect_change(self, current: Observation, prior: Observation) -> Change | None:
        """Real delta/percent-change computation between two given
        observations, floored at `_change_percent_floor` (default 2%,
        see module docstring). When `prior.value == 0`, percent change
        is mathematically undefined by division; any nonzero delta off
        a zero baseline is treated as automatically clearing the floor
        (a real move from nothing is never noise), while a delta of
        exactly zero still yields "no real change"."""
        with traced("DefaultEventObservation.detect_change"):
            delta = current.value - prior.value
            if prior.value == 0:
                percent_change = float("inf") if delta != 0 else 0.0
            else:
                percent_change = delta / abs(prior.value)
            if abs(percent_change) < self._change_percent_floor:
                return None
            return Change(observation=current, delta=delta, percent_change=percent_change)

    def detect_anomaly(self, observation: Observation) -> Anomaly | None:
        """Real z-score outlier check against the last
        `_anomaly_history_size` observations (default 20, excluding
        `observation` itself) for the same entity/metric. Fewer than
        `_anomaly_min_history` (default 5) prior points is reported as
        "not enough history to judge" (None), not a guess. When the
        history has zero variance (a genuinely constant series), a
        z-score is undefined by division; a differing current value is
        still reported as anomalous (deviation from a constant is real
        evidence), using absolute deviation from the mean as the
        documented magnitude proxy in that one case."""
        with traced("DefaultEventObservation.detect_anomaly"):
            history_values = self._recent_history_values(
                observation.entity_id, observation.metric, observation.id, self._anomaly_history_size
            )
            if len(history_values) < self._anomaly_min_history:
                return None
            mean = statistics.mean(history_values)
            stdev = statistics.stdev(history_values)
            if stdev == 0:
                if observation.value == mean:
                    return None
                magnitude = abs(observation.value - mean)
                reason = (
                    f"value {observation.value} deviates from a constant history "
                    f"(mean={mean:.4f}, stdev=0) over the last {len(history_values)} observations"
                )
            else:
                z_score = (observation.value - mean) / stdev
                if abs(z_score) < self._anomaly_z_threshold:
                    return None
                magnitude = abs(z_score)
                reason = (
                    f"z-score {z_score:.2f} exceeds threshold {self._anomaly_z_threshold} "
                    f"against the last {len(history_values)} observations (mean={mean:.4f}, stdev={stdev:.4f})"
                )
            anomaly = Anomaly(observation=observation, reason=reason, magnitude=magnitude)
            self._audit_manager.record(
                "anomaly_detected",
                {
                    "entity_id": observation.entity_id,
                    "metric": observation.metric,
                    "value": observation.value,
                    "reason": reason,
                    "magnitude": magnitude,
                },
            )
            return anomaly

    def _classify(self, metric: str) -> str:
        normalized_metric = metric.lower()
        if any(keyword in normalized_metric for keyword in _EARNINGS_METRIC_KEYWORDS):
            return "earnings"
        if any(keyword in normalized_metric for keyword in _MARKET_MOVEMENT_METRIC_KEYWORDS):
            return "market_movement"
        return _GENERAL_EVENT_TYPE

    def detect_event(self, observations: list[Observation]) -> Event | None:
        """Combines the given observations' own detect_anomaly/
        detect_change results into one Event when at least one clears
        the documented bar: an anomaly always qualifies (it already
        cleared detect_anomaly's stronger statistical bar); a change
        only qualifies if its magnitude clears
        `_event_change_magnitude_threshold` (default 5%, stricter than
        detect_change's own 2% "is this real" floor — see module
        docstring). The strongest-magnitude trigger's metric decides
        the event's `type` (via `_classify`) and populates `metric`/
        `magnitude`; every triggering observation's entity contributes
        to `entity_ids` (deduplicated, order preserved). Returns None
        when nothing in the batch clears the bar. The resulting Event
        is persisted to the "events" table before being returned, so
        `retrieve_events` can find it later."""
        with traced("DefaultEventObservation.detect_event"):
            triggers: list[tuple[str, str, float]] = []
            for observation in observations:
                anomaly = self.detect_anomaly(observation)
                if anomaly is not None:
                    triggers.append((observation.entity_id, observation.metric, anomaly.magnitude))
                    continue
                prior = self._prior_observation(observation)
                if prior is None:
                    continue
                change = self.detect_change(observation, prior)
                if change is not None and abs(change.percent_change) >= self._event_change_magnitude_threshold:
                    triggers.append((observation.entity_id, observation.metric, abs(change.percent_change)))
            if not triggers:
                return None
            entity_ids = list(dict.fromkeys(entity_id for entity_id, _, _ in triggers))
            strongest_entity_id, strongest_metric, strongest_magnitude = max(triggers, key=lambda trigger: trigger[2])
            event = Event(
                id=f"event-{uuid.uuid4()}",
                type=self._classify(strongest_metric),
                entity_ids=entity_ids,
                metric=strongest_metric,
                magnitude=strongest_magnitude,
                detected_at=datetime.now().strftime(_TIMESTAMP_FORMAT),
            )
            self._infrastructure.store(
                _EVENTS_TABLE,
                {
                    "id": event.id,
                    "type": event.type,
                    "entity_ids": event.entity_ids,
                    "metric": event.metric,
                    "magnitude": event.magnitude,
                    "detected_at": event.detected_at,
                },
            )
            return event

    def classify_event(self, event: Event) -> str:
        with traced("DefaultEventObservation.classify_event"):
            return self._classify(event.metric)

    def link_event_to_entities(self, event: Event) -> list[str]:
        """Real entity resolution and linking, delegated entirely to
        `KnowledgeEntity` (04) rather than reinvented here: every
        mention in `event.entity_ids` is resolved via
        `resolve_entity`, unresolvable mentions are dropped (this
        component doesn't own entity-creation policy, so it never
        fabricates a new entity for a miss), and every resolved pair
        is linked via `link_entities` with relationship kind
        `"co_occurred_in_event"` — a real knowledge-graph edge
        recording that these entities were part of the same detected
        event, not just a returned list. Returns the resolved
        entities' canonical ids."""
        with traced("DefaultEventObservation.link_event_to_entities"):
            resolved_entities = [
                entity
                for entity in (self._knowledge_entity.resolve_entity(mention) for mention in event.entity_ids)
                if entity is not None
            ]
            for first, second in itertools.combinations(resolved_entities, 2):
                self._knowledge_entity.link_entities(first, second, _EVENT_COOCCURRENCE_RELATIONSHIP_KIND)
            return [entity.id for entity in resolved_entities]

    def _within_time_window(self, first_detected_at: str, second_detected_at: str) -> bool:
        first_timestamp = _parse_timestamp(first_detected_at)
        second_timestamp = _parse_timestamp(second_detected_at)
        if first_timestamp is None or second_timestamp is None:
            return False
        return abs((first_timestamp - second_timestamp).total_seconds()) <= self._correlation_time_window_seconds

    def _entities_related(self, first_entity_ids: list[str], second_entity_ids: list[str]) -> bool:
        second_set = set(second_entity_ids)
        for entity_id in first_entity_ids:
            relationships = self._knowledge_entity.query_relationships(Entity(id=entity_id, kind=""))
            related_ids = {relationship.source_entity_id for relationship in relationships} | {
                relationship.target_entity_id for relationship in relationships
            }
            if related_ids & second_set:
                return True
        return False

    def _events_correlate(self, first: Event, second: Event) -> bool:
        if not self._within_time_window(first.detected_at, second.detected_at):
            return False
        if set(first.entity_ids) & set(second.entity_ids):
            return True
        return self._entities_related(first.entity_ids, second.entity_ids)

    def correlate_events(self, events: list[Event]) -> list[tuple[Event, Event]]:
        """Real correlation rule over every pair in `events`: two
        events correlate when they were detected within
        `_correlation_time_window_seconds` (default one hour) of each
        other AND either directly share an entity, or their entities
        are linked in the Knowledge & Entity graph (via
        `query_relationships` — e.g. two companies in the same
        sector). A missing or unparseable `detected_at` on either side
        conservatively excludes the pair (can't confirm proximity,
        won't guess), same posture as `detect_stale_data` (ADR-0032)."""
        with traced("DefaultEventObservation.correlate_events"):
            return [
                (first, second)
                for first, second in itertools.combinations(events, 2)
                if self._events_correlate(first, second)
            ]

    def retrieve_events(self, filters: dict) -> list[Event]:
        """Real `Infrastructure`-backed query. Supported filter keys:
        `"type"` (exact match, pushed down to `Infrastructure.query`'s
        own containment filter), `"entity_id"` (membership test
        against each event's `entity_ids`, applied after the query
        since containment matching can't test list membership),
        `"since"`/`"until"` (ISO-format timestamp bounds on
        `detected_at`, inclusive). Unrecognized filter keys are
        ignored rather than raising, matching `Infrastructure.query`'s
        own permissive filter-dict contract."""
        with traced("DefaultEventObservation.retrieve_events"):
            infra_filters = {key: value for key, value in filters.items() if key == "type"}
            records = self._infrastructure.query(_EVENTS_TABLE, infra_filters)
            entity_id_filter = filters.get("entity_id")
            since = _parse_timestamp(filters["since"]) if filters.get("since") else None
            until = _parse_timestamp(filters["until"]) if filters.get("until") else None
            results = []
            for record in records:
                if entity_id_filter is not None and entity_id_filter not in record.get("entity_ids", []):
                    continue
                detected_at = record.get("detected_at", "")
                detected_timestamp = _parse_timestamp(detected_at) if detected_at else None
                if since is not None and (detected_timestamp is None or detected_timestamp < since):
                    continue
                if until is not None and (detected_timestamp is None or detected_timestamp > until):
                    continue
                results.append(
                    Event(
                        id=record["id"],
                        type=record["type"],
                        entity_ids=list(record.get("entity_ids", [])),
                        metric=record.get("metric", ""),
                        magnitude=record.get("magnitude", 0.0),
                        detected_at=detected_at,
                    )
                )
            return results
