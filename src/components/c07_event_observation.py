"""Event & Observation (component 07) — the system's perception layer.

Whiteboard-level only (Component Whiteboards artifact, card 07) — no
low-level design or ADRs yet. Interfaces: <- Knowledge & Entity Model
(04), -> Memory (06).
"""

from dataclasses import dataclass


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


class EventObservation:
    def observe(self, raw: dict) -> Observation:
        raise NotImplementedError

    def detect_change(self, current: Observation, prior: Observation) -> Change | None:
        raise NotImplementedError

    def detect_anomaly(self, observation: Observation) -> Anomaly | None:
        raise NotImplementedError

    def detect_event(self, observations: list[Observation]) -> Event | None:
        raise NotImplementedError

    def classify_event(self, event: Event) -> str:
        raise NotImplementedError

    def link_event_to_entities(self, event: Event) -> list[str]:
        raise NotImplementedError

    def correlate_events(self, events: list[Event]) -> list[tuple[Event, Event]]:
        raise NotImplementedError

    def retrieve_events(self, filters: dict) -> list[Event]:
        raise NotImplementedError
