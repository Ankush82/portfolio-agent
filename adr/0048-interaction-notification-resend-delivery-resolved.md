# 0048 — Interaction & Notification: delivery channel resolved via Resend

**Status:** Accepted — 2026-08-27
**Component:** Interaction & Notification (13), User & Portfolio (01)

## Context

ADR-0040 named the notification-delivery gap without picking a vendor — a transactional-email API, an SMS/telephony API, or a mobile push service — and flagged, in its own Consequences, that the missing piece wasn't only the vendor: `User` (component 01) carries no contact field or device token at all, so none of the three options had anywhere to actually send to yet, regardless of which one got chosen.

The user has now created a real Resend account (free tier) and supplied the resulting API key directly. Email is the option ADR-0040 itself named as needing the smallest additional-infrastructure lift (no device token, no receiving app to build, unlike push) — consistent with resolving the vendor question the same account-driven way ADR-0043/ADR-0046/ADR-0047 already did for this project's other three vendor gaps.

## Decision

**`User.email: str = ""` added to `User`** (`src/components/c01_user_portfolio.py`) — the missing contact field ADR-0040 flagged, resolved as a plain, additive dataclass field (defaulted, so every existing `User(...)` construction site is unaffected), the same pattern ADR-0036/ADR-0039 already used for extending a whiteboard-shaped dataclass with what a real implementation needs. `onboard_user(details)` now reads `details.get("email", "")` into it and persists it alongside `preferences`. `manage_preferences` needed a real fix, not just a pass-through: `Infrastructure.store()` replaces a record's row wholesale rather than merging fields (the same semantics every `Default*` adapter in this project already relies on), so `manage_preferences`'s own store call now explicitly carries `email` forward from the stored record — without this, updating a preference would have silently erased a user's email.

**`Notification.email: str = ""` added to `Notification`** (`src/components/c13_interaction_notification.py`), populated by `personalize_notification` — the one method in this component that already receives a full `user: dict` (the same place `user_id`/`channel` are already personalized from). No other method in this component ever sees a `User`, so this is the correct, and only sensible, place for the email to enter the pipeline.

**One new module, `src/resend_client.py`**, mirroring `src/llm.py`'s, `src/mem0_embedder.py`'s, `src/alpha_vantage_client.py`'s, and `src/tavily_client.py`'s existing per-vendor shape. `get_api_key()` — same `.env`-or-environment, read-at-call-time convention as every other vendor module. `send_email(to, subject, text, from_address=DEFAULT_FROM_ADDRESS)` — one real HTTP POST to `https://api.resend.com/emails`, `DEFAULT_FROM_ADDRESS` defaulting to Resend's own conventional sandbox sender (`onboarding@resend.com`).

**`ResendNotificationChannel` added to `src/components/c13_interaction_notification.py`**, alongside the untouched `PlaceholderNotificationChannel`. `send(notification)`: if `notification.email` is empty, honestly cannot deliver — records the attempt and returns `False`, the same posture the placeholder already had, rather than guessing at a recipient. If populated, calls `send_email` for real, catches any failure, and always records the attempt (delivered or not) via `Infrastructure`, matching `PlaceholderNotificationChannel`'s own bookkeeping shape exactly.

**`get_notification_channel(placeholder=None, infrastructure=None)` resolves `DefaultInteractionNotification`'s default `NotificationChannel`**, the fourth `get_<seam>()` key-gated selection function in this codebase now (after `get_reason_fn`, `get_source_fetcher`, `get_external_search_provider`), same pattern each time: `ResendNotificationChannel` (sharing `infrastructure` with the caller, the same "share one backing store" posture ADR-0044 established) when `RESEND_API_KEY` is configured, `placeholder` otherwise. Constructing `ResendNotificationChannel` never touches the network, so auto-resolving it at construction time is safe.

**No automatic live-send test is checked into the test suite** — a deliberate departure from ADR-0043/ADR-0046/ADR-0047's own "one live test that skips cleanly" pattern. Those three each make a read-only (or otherwise side-effect-free) call; sending a real email is not side-effect-free — it lands in a real inbox — so a live test that fires on every `pytest tests/` run would send a real email every single time the suite runs, indefinitely, to whoever's key happens to be configured. Real delivery was instead verified once, manually, outside the checked-in suite (see Consequences below for what that verification actually found).

## The real, live finding this pass surfaced

Manually verifying `send_email` against this project's actual Resend account (not a mock) returned a real `403`: `"The resend.com domain is not verified. Please, add and verify your domain on https://resend.com/domains"`. This is a materially different, and stricter, constraint than either ADR-0040 or this ADR's own first draft assumed (that the conventional `onboarding@resend.com` sandbox sender would work unverified, only restricted in *recipient*). In fact, on this account, it doesn't work at all — sending, not just receiving, is blocked — until a domain is added and verified at resend.com/domains.

`ResendNotificationChannel.send()` was verified, live, to handle this correctly: the real `403` is caught, the attempt is recorded via `Infrastructure` with `delivered: False`, and `.send()` returns `False` — the same honest, non-crashing behavior as any other genuine delivery failure. Nothing needs to change in the code once a domain is verified; delivery starts working the moment the account's real state changes.

## Alternatives considered

- **SMS via a telephony API, or push via a mobile push service, as ADR-0040 also named.** Moot once the user actually created a Resend account specifically — this decision documents the account that exists. Email also remains the option with the smallest additional-infrastructure lift of the three (no device token field, no receiving mobile app to build first), which was already true when ADR-0040 named it.
- **Adding `email` to `Notification.channel`-style preference dict instead of a first-class dataclass field.** Rejected: `channel`, `priority`, `significance` are all already first-class `Notification` fields for the same reason — `ResendNotificationChannel` needs a typed, guaranteed-present (if empty) attribute to check, not a dict lookup that could KeyError.
- **Having `ResendNotificationChannel` look up the user's email itself** (e.g., via an injected `UserPortfolio` accessor), instead of `personalize_notification` carrying it onto `Notification`. Rejected: it would require `NotificationChannel`'s Protocol to grow a new dependency (`UserPortfolio`) that `PlaceholderNotificationChannel` and any future channel implementation would also need to accept, just to support one real implementation's lookup — `personalize_notification` already has the full `user: dict` in hand at exactly the right point in this component's own pipeline, so passing it through costs nothing extra.
- **Silently overwriting `Notification.email` even when `user.get("email")` is missing.** Rejected: `personalize_notification`'s real rule (`user.get("email", notification.email)`) preserves whatever was already on `notification` when the caller doesn't have a fresher value, the same "don't overwrite with a worse guess" posture `personalize_notification`'s existing `channel`/`user_id` rules already use.
- **Reporting the manually-verified `403` as a code bug and trying alternate `from_address` values to work around it.** Rejected: this is Resend's own real anti-abuse policy on unverified accounts, not a bug in this integration — the correct fix is the user verifying a domain, not code changing its request shape to route around a deliberate vendor restriction.

## Consequences

- `DefaultInteractionNotification()` (default constructor) now resolves `ResendNotificationChannel` when `RESEND_API_KEY` is configured — real code, real HTTP calls, but delivery genuinely fails today on this project's own account until a domain is verified at resend.com/domains. This is a real, external, actionable next step for the user, not a code gap.
- `User.email` is empty (`""`) for every user onboarded before this pass and for any onboarding call that doesn't supply one — `personalize_notification` then leaves `Notification.email` at whatever it already was (empty, by `generate_notification`'s own construction), and `ResendNotificationChannel.send()` correctly reports `False` rather than guessing at a recipient.
- `pyproject.toml` needs no new dependency — `requests` is already a direct dependency (ADR-0043).
- This closes ADR-0040 in full — the vendor is picked (Resend) and the contact-field gap it separately flagged (`User.email`) is resolved in the same pass, since the two were never independently useful (a chosen vendor with nowhere to send is not actually resolved).

## Related

- Supersedes: [ADR-0040](0040-interaction-notification-delivery-channel-interim.md) — both the vendor choice and the contact-field gap it flagged are resolved together.
- Same shape as: [ADR-0043](0043-llm-provider-resolved-openrouter.md), [ADR-0046](0046-data-sources-alpha-vantage-partial-resolution.md), [ADR-0047](0047-retrieval-tavily-corrective-search-resolved.md) — the fourth `get_<seam>()` key-gated selection function in this codebase.
- Extends the same additive-dataclass-field precedent as: [ADR-0036](0036-event-observation-real-mechanism.md), [ADR-0039](0039-interaction-notification-real-mechanism.md).
- Shares infrastructure the same way as: [ADR-0044](0044-user-portfolio-manual-stock-entry.md) (`DefaultUserPortfolio`/`DefaultKnowledgeEntity` sharing one backing store).
- Implemented by: `../src/resend_client.py` (`get_api_key`, `send_email`, `MissingResendAPIKeyError`); `../src/components/c13_interaction_notification.py` (`ResendNotificationChannel`, `get_notification_channel`, `Notification.email`); `../src/components/c01_user_portfolio.py` (`User.email`, `onboard_user`, `manage_preferences`).
- Tested by: `../tests/test_resend_client.py` (no live-send test — see this ADR's own Decision for why), `../tests/components/test_interaction_notification.py` (`ResendNotificationChannel`/`get_notification_channel`/`personalize_notification` email sections), `../tests/components/test_user_portfolio.py` (`User.email` sections).
- Logged narratively in `../checkpoint.md`.
