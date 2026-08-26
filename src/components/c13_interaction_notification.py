"""Interaction & Notification (component 13) — the surface the user
actually sees.

Whiteboard-level only (Component Whiteboards artifact, card 13) — no
low-level design or ADRs yet. Interfaces: <- Decision & Policy (12),
-> User, -> Learning & Evaluation (14).
"""

from dataclasses import dataclass

from cross_cutting.observability import traced


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
        with traced("InteractionNotification.generate_notification"):
            return Notification(user_id="stub-id", content="", priority="")

    def prioritize_notification(self, notification: Notification) -> str:
        with traced("InteractionNotification.prioritize_notification"):
            return ""

    def personalize_notification(self, notification: Notification, user: dict) -> Notification:
        with traced("InteractionNotification.personalize_notification"):
            return Notification(user_id="stub-id", content="", priority="")

    def deliver_notification(self, notification: Notification) -> bool:
        with traced("InteractionNotification.deliver_notification"):
            return True

    def explain_decision(self, decision: dict) -> str:
        with traced("InteractionNotification.explain_decision"):
            return ""

    def collect_feedback(self, notification: Notification) -> UserFeedback:
        with traced("InteractionNotification.collect_feedback"):
            return UserFeedback(notification_id="stub-id", response={})

    def collect_user_response(self, notification: Notification) -> dict:
        with traced("InteractionNotification.collect_user_response"):
            return {}
