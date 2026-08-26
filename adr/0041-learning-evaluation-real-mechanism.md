# 0041 — Learning & Evaluation real mechanism: DataSources-backed outcome measurement, tolerance-based comparison, multi-field trajectory replay, and Memory-write-path closure

**Status:** Accepted — 2026-08-26
**Component:** Learning & Evaluation (14)

## Context

Learning & Evaluation stayed whiteboard-only through the design-framework round (its own module docstring said so), and its in-scope status was an open question through `checkpoint.md`'s Phase 0 summary and `loop.md`'s "Still-open items." Both are now resolved: it's in scope, and this pass builds it for real, following `loop.md` under the operating-mode change (resolve genuine gaps with real engineering judgment and an Accepted ADR, not a pause, except for actual external-credential gaps).

Building this component for real surfaces several judgment calls no prior ADR or design artifact settled, because no prior component ever needed to answer them:

1. **`Prediction`/`Outcome`/`Evaluation` are whiteboard-thin.** `Prediction(claim, confidence)` carries nothing identifying which entity/metric it's about, what value was predicted, or what evidence backed it — there is no honest way to measure an outcome, compare it, or diagnose an error without that data. The same "gained a small number of fields" precedent Event & Observation set for `Observation`/`Change`/`Anomaly`/`Event` (ADR-0036) and Data & Sources set for `SourceDocument` (ADR-0026/ADR-0032) applies here.
2. **`measure_outcome` needs a concrete way to reach Data & Sources (02) and extract a number.** The task brief is explicit that this method should query `DefaultDataSources` and inherit ADR-0027's fetch-provider gap rather than invent a second one — but nothing prior defines how a `SourceDocument`'s raw bytes become a comparable numeric outcome value.
3. **`replay(trajectory_id)` has no shared key to replay against.** No component in this project persists a `trajectory_id`/`task_id` field on any of its records — `DefaultTaskManager` and `DefaultStateManager` (Agent Runtime, component 10) are both in-memory only, never `Infrastructure`-backed (`checkpoint.md`'s Agent Runtime entry states this plainly). The only real cross-component correlating fields that exist today are `entity_id` (observations, events, analyses) and `notification_id`/`user_id` (component 13's alerts/sent-records/interactions) — two disjoint families, not one shared trajectory key.
4. **`update_knowledge` is the first real caller of Memory's full write path.** Every existing `Default*` consumer of Memory (components 08, 09) only ever calls `MemoryManager.retrieve()` — a read. Nothing in this project has yet driven `MemoryEvaluator → EntityLinker → QuarantineGate → MemoryManager.admit()` (fig. 1's write path) end to end for a real candidate. What "provenance verified" honestly means for an internally-computed evaluation, rather than an externally-sourced document, is a new question.
5. **`detect_regression`/`evaluate_versions` need a documented tolerance and aggregate contract.** Neither dataclass nor ADR anywhere in this project defines what "close enough to call correct" or "worse than baseline" means numerically for a prediction.

None of these is a live-external-credential gap (ADR-0021's template) — every one is answerable today with the data and components already in hand. Per the operating-mode change, they're resolved here, as a real Accepted ADR, not deferred.

## Decision

**1. Additive fields on `Prediction`/`Outcome`/`Evaluation`**, all defaulted so `StubLearningEvaluation`'s existing construction calls are unaffected:
- `Prediction` gains `id`, `entity_id`, `metric`, `reference_value` (the metric's value at prediction time — the baseline direction is measured against), `predicted_value`, `source_type` (a `SourceType` name, default `"MARKET_DATA"`), `source_ids` (evidence/analysis provenance behind the prediction — real inputs, used by `analyze_errors`), `made_at`.
- `Outcome` gains `id`, `measured_at`.
- `Evaluation` gains `id`, `evaluated_at`.

Direction is computed, not caller-asserted: `predicted_direction = sign(predicted_value - reference_value)`, `actual_direction = sign(actual_value - reference_value)`, both computed the same way in `compare_prediction_vs_outcome` — the same "compute it from real deltas" posture `Change`/`Anomaly` (component 07) already take, rather than trusting a free-form label a caller could set inconsistently with the numbers.

**2. `measure_outcome` calls `DataSources.retrieve_source`/`ingest_source`/`track_source_reliability_metadata` for real**, building `Outcome.actual = {entity_id, metric, actual_value, reliability, synthetic, fetched_at}`. `actual_value` is extracted by a real, structural parse: the document's content must decode as UTF-8 JSON with a numeric field named after `Prediction.metric` or a generic `"value"` key; anything else (unparseable bytes, wrong shape, non-numeric) honestly returns `None` rather than guessing — the same posture `c03`'s `_parse_structure` already takes for non-JSON content (ADR-0032). `synthetic` is `document.content == PLACEHOLDER_FETCH_MARKER` (component 02's own unmistakable marker). Under `PlaceholderSourceFetcher` (ADR-0027, still Proposed), `synthetic` is always `True` and `actual_value` is always `None` — this is ADR-0027's gap inherited exactly as instructed, not a second Proposed ADR for the same root cause.

**3. `compare_prediction_vs_outcome` is "comparable" only when `predicted_value`, `reference_value`, and `actual_value` are all present.** When comparable: `entity_match` (`actual.entity_id == prediction.entity_id`), `direction_correct` (computed sign match), and `magnitude_within_tolerance` (`relative_error <= 0.10`) are all computed. `_MAGNITUDE_RELATIVE_ERROR_TOLERANCE = 0.10` is a documented default (same order of magnitude as component 07's own 5%/2% documented floors for "is this move real"), overridable per instance. `evaluate` sets `correct = comparable and entity_match and direction_correct and magnitude_within_tolerance` — a prediction measured against the wrong entity is never genuinely correct, even if its numbers happen to line up by coincidence. `error` is the full comparison dict when not correct, `None` when correct — never a placeholder value.

**4. `analyze_errors` categorizes structurally from what's actually recorded**, in priority order: not comparable + no `source_ids` → `insufficient_evidence`; not comparable + `synthetic` actual → `stale_data` (ADR-0027's gap, named using the task brief's own suggested category); not comparable + non-synthetic but unparseable → `unverifiable_source_data`; comparable + entity mismatch (`actual.entity_id != prediction.entity_id`) → `wrong_entity_resolution`; comparable + wrong direction → `direction_miss`; comparable + right direction, outside tolerance → `magnitude_miss`. Every branch reads a real recorded field (`Prediction.source_ids`, `Outcome.actual`'s `synthetic`/`entity_id`, the stored comparison) — nothing here is generated prose.

**5. `replay(trajectory_id)` treats the identifier polymorphically, matching it against whatever identifying field each of the six known cross-component tables actually carries**, since no shared trajectory-scoping key exists anywhere in this project's persisted schema (Context, point 3):

| table | owning component | matched field(s) |
|---|---|---|
| `observations` | 07 | `entity_id` |
| `events` | 07 | `trajectory_id in entity_ids` |
| `analyses` | 08 | any `hypotheses[i].basis.entity_id` |
| `decision_policy_notifications` | 12 | `identity` |
| `decision_policy_pending_approvals` | 12 | `id`, `action.id`, `action.identity`, `action.entity_id` |
| `notification_alerts` | 13 | `notification_id` |
| `notifications_sent` | 13 | `notification_id`, `user_id` |
| `interactions` | 13 | `id`, `notification_id`, `user_id` |

Each table is read directly through `Infrastructure.query(table, {})` under a locally-declared constant naming that table (`_C07_OBSERVATIONS_TABLE = "observations"`, etc.) — the same read-only cross-component coupling pattern `c03_data_processing_quality.py`'s `_C02_SOURCES_TABLE` already established (ADR-0032), not a new import of another component's private implementation. Matches are normalized into `{"component", "type", "at", "record"}` and sorted by a real parsed timestamp (`_epoch()` handles both this project's `"%Y-%m-%dT%H:%M:%S"` string format and the one raw-epoch-float field, `decision_policy_notifications.issued_at`).

**Alternatives considered for replay:**
- *Thread a new `trajectory_id` field through every write in components 07/08/12/13.* Rejected: a real schema change across five already-shipped, reviewed components' persisted tables, out of this pass's scope (`loop.md`'s per-component subagent boundary), for a capability this pass can build honestly without it.
- *Key only on `entity_id`.* Rejected: silently drops component 13's notifications/interactions, which the task brief explicitly names as records `replay` should cover, and which carry no `entity_id` at all today.
- *Chosen: match against every plausible identifying field each table's already-shipped records carry, union everything, sort by real timestamp.* Real, uses only what's already persisted, degrades honestly (a caller who passes an id nothing was ever tagged with just gets an empty list, not a fabricated timeline).

**6. `update_knowledge` drives Memory's full write path for real — the first component to do so.** Builds a `MemoryCandidate` from the evaluation (claim, entity/metric, correctness, error), gates it through `DefaultMemoryEvaluator.should_become_memory` using the original prediction's own `confidence` as the experience signal, retrieves existing memories in `self._memory_scope` (default `"shared"` — an evaluation is about a market entity's behavior, not private user data; documented judgment call, no prior ADR fixed this), links via `DefaultEntityLinker.link` before the quarantine check (matching fig. 1's documented ordering), then branches on `DefaultQuarantineGate.check_provenance`. `provenance_verified` is computed, not hardcoded: `True` only when the outcome's `actual_value` is real (non-`None`) and non-`synthetic` — i.e., only when the evaluation is genuinely grounded in a real fetch, not `PlaceholderSourceFetcher` output. Verified candidates go through `MemoryManager.admit()` (a real, retrievable `Memory`); unverified ones go to `DefaultQuarantineGate.quarantine()` (ADR-0007's lifecycle, not silently dropped). Under today's `PlaceholderSourceFetcher`, every `update_knowledge` call in production would quarantine — the honest, inherited consequence of ADR-0027, not a bug in this component; tests exercise both branches directly (a genuinely comparable `Evaluation` proves the admit path, a placeholder-derived one proves the quarantine path).

**7. `detect_regression(current, baseline)`**: `True` when `baseline.correct and not current.correct` (a real correct→incorrect flip); `False` on any correct/no-change case; when both are incorrect, `True` only if `current`'s `relative_error` exceeds `baseline`'s by more than `_REGRESSION_RELATIVE_ERROR_MARGIN = 0.05` (5 percentage points) — a documented margin against noise, not "any measurable difference." `DefaultAuditManager.record("regression_detected", ...)` fires only when a regression is actually found, mirroring component 07's `detect_anomaly` audit pattern (record the surprising case, not routine traffic).

**8. `evaluate_versions(a, b)`** takes each side as `{"version": str, "evaluations": [asdict(Evaluation), ...]}`, computes real aggregate accuracy and mean relative error per side, and reports `accuracy_delta` and `better_version` ("tie" when equal) — a genuine structural/statistical comparison over whatever evaluations were actually collected for each version, no LLM.

## Alternatives considered

- **Give `Learning & Evaluation` its own numeric "market value" data contract independent of Data & Sources**, e.g., call `Event & Observation`'s already-real `Observation` history instead of `DataSources`. Rejected: the task brief is explicit that `measure_outcome` should query `DefaultDataSources` and inherit its existing ADR-0027 gap rather than route around it through a different, already-real component just to get a number today — that would hide the real state of the fetch-provider gap rather than surface it honestly.
- **Treat `correct` as direction-only, ignoring magnitude.** Rejected: a prediction whose direction is right but whose predicted value is wildly off is not a genuinely "correct" prediction for a financial system; the 10% tolerance keeps `correct` meaningful for `detect_regression`/`evaluate_versions` to aggregate over.
- **Always admit `update_knowledge`'s candidate directly, skip quarantine.** Rejected: this project has quarantined every use of externally-sourced or synthetically-fetched content since ADR-0007; an evaluation built on `PlaceholderSourceFetcher` output is exactly the "not yet trusted" case that gate exists for, and skipping it here would special-case Learning & Evaluation out of a project-wide invariant for no real reason.

## Consequences

- `Prediction`/`Outcome`/`Evaluation` are no longer whiteboard-thin, but every new field is defaulted — no existing call site (there are none outside this file and its tests) breaks.
- `replay`'s coverage is exactly as good as what other components have chosen to persist and tag with an identifying field. If a future component starts persisting records with no `entity_id`/`notification_id`/`user_id`-shaped field at all, `replay` will not find them without a further extension to `_REPLAY_SOURCES` — named here as the real, bounded scope of this mechanism, not a claim of universal coverage.
- `update_knowledge`'s admit-vs-quarantine split is entirely downstream of ADR-0027 (Data & Sources' fetch-provider gap) being unresolved: until a real `SourceFetcher` exists, no evaluation this component produces from `measure_outcome` will ever admit directly to Memory — every one will quarantine. This is the loop closing honestly, not a broken loop; once ADR-0027 resolves, `update_knowledge`'s own logic needs no change at all for admits to start happening for real.
- `detect_regression`'s 5-point margin and `compare_prediction_vs_outcome`'s 10% tolerance are both real, documented, overridable defaults — not tuned against any real historical data, since none exists yet. Revisiting them once real evaluations accumulate is expected, not a defect in this pass.

## Related

- Inherits: [ADR-0027](0027-data-sources-fetch-provider-interim.md) (Data & Sources fetch-provider interim — `measure_outcome` calls through the same seam and the same `PlaceholderSourceFetcher`, no second Proposed ADR raised for the same root cause).
- Builds on: [ADR-0005](0005-memory-active-working-set-management.md), [ADR-0006](0006-memory-linked-network-structure.md), [ADR-0007](0007-memory-quarantine-at-write.md), [ADR-0008](0008-memory-structural-partition.md) (Memory's write path, driven end to end for the first time by `update_knowledge`).
- Same read-only cross-component table coupling pattern as: [ADR-0032](0032-data-processing-quality-real-mechanism.md) (`c03`'s `_C02_SOURCES_TABLE`).
- Same "gained fields the whiteboard shape didn't carry" precedent as: [ADR-0036](0036-event-observation-real-mechanism.md) (`Observation`/`Change`/`Anomaly`/`Event`).
- Same honest-placeholder-consequence posture as: [ADR-0038](0038-decision-policy-real-mechanism.md)'s `request_approval`, [ADR-0039](0039-interaction-notification-real-mechanism.md)'s `collect_feedback`/`collect_user_response` — `collect_feedback` here follows the identical pending-mechanism shape, not ADR-0021's non-cognitive-placeholder shape (this is a "no UI exists" gap, not an LLM/credential gap).
- Implemented by: `../src/components/c14_learning_evaluation.py`, `DefaultLearningEvaluation`.
- Logged narratively in `../checkpoint.md`.
