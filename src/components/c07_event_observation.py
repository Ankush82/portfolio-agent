"""Event & Observation (component 07) — the system's perception layer.

Whiteboard-level only (Component Whiteboards artifact, card 07) — no
low-level design or ADRs yet. Interfaces: <- Knowledge & Entity Model
(04), -> Memory (06).
"""

from dataclasses import dataclass
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class Observation:
    entity_id: str
    metric: str
    value: float


@dataclass
class Change:
    observation: Observation
    delta: float


@dataclass
class Anomaly:
    observation: Observation
    reason: str


@dataclass
class Event:
    id: str
    type: str
    entity_ids: list[str]


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
