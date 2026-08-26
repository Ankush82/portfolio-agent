"""Interaction & Notification (component 13) — the surface the user
actually sees.

Whiteboard-level only (Component Whiteboards artifact, card 13) — no
low-level design or ADRs yet. Interfaces: <- Decision & Policy (12),
-> User, -> Learning & Evaluation (14).
"""

from dataclasses import dataclass


@dataclass
class Notification:
    user_id: str
    content: str
    priority: str


@dataclass
class UserFeedback:
    notification_id: str
    response: dict


class InteractionNotification:
    def generate_notification(self, decision: dict) -> Notification:
        raise NotImplementedError

    def prioritize_notification(self, notification: Notification) -> str:
        raise NotImplementedError

    def personalize_notification(self, notification: Notification, user: dict) -> Notification:
        raise NotImplementedError

    def deliver_notification(self, notification: Notification) -> bool:
        raise NotImplementedError

    def explain_decision(self, decision: dict) -> str:
        raise NotImplementedError

    def collect_feedback(self, notification: Notification) -> UserFeedback:
        raise NotImplementedError

    def collect_user_response(self, notification: Notification) -> dict:
        raise NotImplementedError
