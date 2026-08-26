"""LLM provider — the one real reasoning backend this project uses.

Design: resolves the LLM-provider gap named twice in this codebase —
Agent Runtime's `reason`/`assess_stakes` nodes (ADR-0021,
`src/components/c10_agent_runtime.py`) and Analysis & Reasoning's six
cognitive methods (ADR-0037, `src/components/c08_analysis_reasoning.py`)
— with one real implementation, since both ADRs name the exact same
underlying gap: no ADR anywhere in this project had ever chosen an LLM
provider.
Decision: ADR-0043 (`adr/0043-llm-provider-resolved-openrouter.md`) —
OpenRouter, an OpenAI-compatible gateway, called over plain HTTP via
`requests` (already a resolved dependency of this project's own
`langgraph` -> `langsmith` chain — see `uv.lock` — so declaring it
directly in `pyproject.toml` adds nothing new to what's actually
installed; the `openai` SDK was considered and not used, since a
gateway that already speaks the OpenAI wire format needs no SDK beyond
a POST and a Bearer header).

This module is deliberately the *only* place in the codebase that
reads `OPENROUTER_API_KEY` or talks to OpenRouter. Both
`c10_agent_runtime.py` and `c08_analysis_reasoning.py` import
`get_reason_fn` from here rather than reimplementing any part of this.

Two things this module is careful about, both load-bearing for the
test suite:
  - `OPENROUTER_API_KEY` is read at call time, inside `get_reason_fn()`
    and `openrouter_reason_fn()` — never at import time. Importing this
    module never touches the environment or the filesystem, so it
    stays importable (and its prompt/parsing logic stays unit-testable)
    with no key present at all.
  - A missing key raises `MissingOpenRouterAPIKeyError` from
    `openrouter_reason_fn` — it never silently falls back to
    placeholder-shaped output. `get_reason_fn()` is the one place that
    *chooses* between the real function and a placeholder, and it does
    that choice once, explicitly, before either is ever called — so a
    caller of the resulting function never gets a blend of the two.
"""

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Callable

import requests

from cross_cutting.observability import AuditManager, DefaultAuditManager

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"

# anthropic/claude-haiku-4.5 — a real, currently-listed OpenRouter model
# (confirmed against https://openrouter.ai/api/v1/models), fast and cheap
# ($1/$5 per million prompt/completion tokens at the time this was
# chosen) but capable enough for the judgment calls this module actually
# makes (deciding a next action, assessing stakes, hypothesizing). An
# even cheaper `anthropic/claude-3-haiku` also exists on OpenRouter today
# if cost needs to be cut further than quality tolerates.
OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"

_REQUEST_TIMEOUT_SECONDS = 30
_MAX_RESPONSE_TOKENS = 1024

_ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"

_KNOWN_PHASES = frozenset(
    {
        "reason",
        "assess_stakes",
        "infer",
        "generate_hypotheses",
        "generate_explanations",
        "generate_counterarguments",
        "synthesize_findings",
        "estimate_impact",
    }
)


class MissingOpenRouterAPIKeyError(RuntimeError):
    """Raised by `openrouter_reason_fn` when `OPENROUTER_API_KEY` is not
    set in the environment at call time. A specific, named exception —
    not a generic `RuntimeError`, and never a silent fallback to
    placeholder output — so a caller that actually wanted the real
    reasoning backend gets an unambiguous signal that it isn't
    configured, rather than quietly getting non-cognitive placeholder
    behavior it never asked for (see ADR-0043's Decision)."""


def _load_dotenv_into_environ() -> None:
    """Manual `.env` parsing, not `python-dotenv`: the only format this
    repo's `.env` ever needs is single-line `KEY=VALUE` (see `.env` at
    the repo root), so a dependency for that is not worth adding. Never
    overwrites a variable already set in the real process environment
    (matches `python-dotenv`'s own default behavior). Called only from
    inside `get_reason_fn`/`openrouter_reason_fn`, never at import
    time, and it never logs or returns what it read — it only sets
    `os.environ`."""
    if not _ENV_FILE_PATH.exists():
        return
    for line in _ENV_FILE_PATH.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, _, value = stripped_line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_reason_fn(
    placeholder: Callable[[dict], dict],
    audit_manager: AuditManager | None = None,
) -> Callable[[dict], dict]:
    """Selection function — the same `Default*` vs `Stub*` pattern this
    codebase already uses everywhere else (e.g. `DefaultInfrastructure`
    vs `StubInfrastructure`, `infrastructure.py`), applied to reasoning
    backends instead of the infrastructure backend. Returns
    `openrouter_reason_fn` when `OPENROUTER_API_KEY` is set in the
    environment (or a `.env` file at the repo root) at call time,
    otherwise returns `placeholder` unchanged.

    `placeholder` is always supplied by the caller rather than defaulted
    here, because Agent Runtime's and Analysis & Reasoning's own
    `placeholder_reason_fn` functions answer different phase sets with
    different shapes (see ADR-0021 and ADR-0037) — there is no single
    correct placeholder this module could default to on either
    caller's behalf.

    Logs which backend was selected via `AuditManager` (`DefaultAuditManager`
    by default) so a run's audit trail records which reasoning path was
    actually active, the same way `DefaultRecoveryManager.escalate` and
    `DefaultDecisionPolicy.enforce_policy` already record their own
    real decisions."""
    _load_dotenv_into_environ()
    audit_manager = audit_manager or DefaultAuditManager()
    if os.environ.get("OPENROUTER_API_KEY"):
        audit_manager.record("reason_fn_selected", {"backend": "openrouter", "model": OPENROUTER_MODEL})
        return openrouter_reason_fn
    audit_manager.record("reason_fn_selected", {"backend": "placeholder"})
    return placeholder


def _json_safe(value):
    """Recursively converts dataclass instances (`Checkpoint`,
    `Hypothesis`, ...) found anywhere inside a `reason_fn` request
    payload into plain JSON-serializable data, without this module
    importing any specific component dataclass — it only needs to
    recognize *that* something is a dataclass, not which one, which is
    what avoids a circular import between this module and the
    component modules that import `get_reason_fn` from it."""
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _build_messages(phase: str, request: dict) -> list[dict]:
    """Builds the real system + user prompt for one `reason_fn` phase.
    Every phase gets a system prompt describing that phase's actual
    job (not one generic prompt reused everywhere) and a user prompt
    that is the phase's real request payload, JSON-serialized. Every
    system prompt below ends by fixing the exact JSON shape
    `_normalize_response` then parses back out — the two are a
    matched pair, change one only with the other."""
    if phase == "reason":
        checkpoint = request.get("checkpoint")
        system = (
            "You are the Reason step of an autonomous financial-agent runtime, deciding "
            "what to do next inside one checkpoint of a larger task. Given the checkpoint's "
            "subgoal and the reason/act/observe history so far in this checkpoint, decide "
            "the single next action to take and whether the checkpoint's subgoal is now "
            "satisfied. Respond with a single strict JSON object and nothing else — no "
            "markdown fences, no commentary: "
            '{"action": {"tool": "<short action name>", "rationale": "<why this action>"}, '
            '"checkpoint_complete": <true or false>}.'
        )
        user = json.dumps(
            {
                "checkpoint_id": getattr(checkpoint, "id", None),
                "subgoal": _json_safe(getattr(checkpoint, "subgoal", {})),
                "history": _json_safe(request.get("history", [])),
                "retry_count": request.get("retry_count", 0),
            }
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "assess_stakes":
        checkpoint = request.get("checkpoint")
        system = (
            "You are the stakes-assessment step of an autonomous financial-agent runtime. "
            "Given the checkpoint's subgoal and the most recent action's result, decide "
            "whether this step is high-stakes enough to need step-level reflection right now "
            "(for example: an irreversible action, a large sum of money, or a surprising or "
            "uncertain result) rather than waiting for reflection at the end of the whole "
            "trajectory. Respond with a single strict JSON object and nothing else: "
            '{"stakes_high": <true or false>}.'
        )
        user = json.dumps(
            {
                "checkpoint_id": getattr(checkpoint, "id", None),
                "subgoal": _json_safe(getattr(checkpoint, "subgoal", {})),
                "last_result": _json_safe(request.get("last_result")),
            }
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "infer":
        system = (
            "You are the inference step of a financial analysis engine. Given a list of "
            "premises (facts or observations), infer the single most defensible hypothesis "
            "they support. Respond with a single strict JSON object and nothing else: "
            '{"claim": "<one-sentence hypothesis>", "basis": {"confidence": "<low|medium|high>", '
            '"reasoning": "<why the premises support this>"}}.'
        )
        user = json.dumps({"premises": _json_safe(request.get("premises", []))})
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "generate_hypotheses":
        system = (
            "You are the hypothesis-generation step of a financial analysis engine. Given "
            "gathered analysis input (an event, correlated historical events, retrieved "
            "memories, a context pack, and portfolio exposure), propose the most plausible "
            "hypotheses that explain what is happening. Respond with a single strict JSON "
            'object and nothing else: {"hypotheses": [{"claim": "<one-sentence hypothesis>", '
            '"basis": {"confidence": "<low|medium|high>", "reasoning": "<why>"}}, ...]}. '
            "Return between one and four hypotheses; return an empty list only if the input "
            "genuinely gives no basis for any hypothesis."
        )
        user = json.dumps({"analysis_input": _json_safe(request.get("analysis_input", {}))})
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "generate_explanations":
        system = (
            "You are the explanation step of a financial analysis engine. Given a hypothesis "
            "and its basis, write one clear paragraph explaining why this hypothesis is "
            "plausible, in plain English suitable for an investor. Respond with a single "
            'strict JSON object and nothing else: {"explanation": "<paragraph>"}.'
        )
        user = json.dumps({"hypothesis": _json_safe(request.get("hypothesis"))})
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "generate_counterarguments":
        system = (
            "You are the counterargument step of a financial analysis engine. Given a "
            "hypothesis and its basis, list the strongest reasons this hypothesis could be "
            "wrong or incomplete. Respond with a single strict JSON object and nothing else: "
            '{"counterarguments": ["<reason 1>", "<reason 2>", ...]}. Return an empty list '
            "only if you genuinely cannot find a plausible counterargument."
        )
        user = json.dumps({"hypothesis": _json_safe(request.get("hypothesis"))})
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "synthesize_findings":
        system = (
            "You are the synthesis step of a financial analysis engine. Given a list of "
            "evidence-tested hypotheses (each already carries a verification confidence, an "
            "evidence count, and whether it was found contradictory), select and, where "
            "useful, merge them into the final findings worth reporting, and give an overall "
            "impact estimate. Respond with a single strict JSON object and nothing else: "
            '{"hypotheses": [{"claim": "<finding>", "basis": {"...": "carried over or refined"}}, ...], '
            '"impact_estimate": {"significance": "<low|medium|high|unknown>", "rationale": "<why>"} '
            "or null if there is nothing worth reporting}."
        )
        user = json.dumps({"hypotheses": _json_safe(request.get("hypotheses", []))})
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if phase == "estimate_impact":
        system = (
            "You are the impact-judgment step of a financial analysis engine. Given a "
            "hypothesis and the user's already-computed real portfolio exposure to the "
            "entity it concerns, judge how significant the underlying event actually is. "
            "Respond with a single strict JSON object and nothing else: "
            '{"significance": "<low|medium|high|unknown>", "rationale": "<why>"}.'
        )
        user = json.dumps(
            {
                "hypothesis": _json_safe(request.get("hypothesis")),
                "exposure": _json_safe(request.get("exposure", {})),
            }
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    raise ValueError(f"openrouter_reason_fn: unknown phase {phase!r}")


def _parse_json_object(text: str) -> dict:
    """Parses the model's raw response content into a dict. Models
    sometimes wrap JSON in markdown code fences despite being told not
    to — this strips those, then falls back to extracting the first
    balanced-looking `{...}` substring, before giving up. Raises
    `ValueError` rather than returning `{}` on failure, so a genuinely
    unparseable response is treated by `openrouter_reason_fn` as the
    same kind of real API error a timeout or a rate limit is (see its
    own docstring), not confused with a model that legitimately
    returned an empty object."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[len("json") :]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"openrouter_reason_fn: no JSON object found in model response: {text!r}")
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"openrouter_reason_fn: model response was not a JSON object: {text!r}")
    return parsed


def _normalize_response(phase: str, parsed: dict, request: dict) -> dict:
    """Converts the model's parsed JSON into the exact output shape
    `c10_agent_runtime.py`'s graph nodes / `c08_analysis_reasoning.py`'s
    methods expect for this phase — mirroring each phase's own
    `placeholder_reason_fn` shape exactly, so a real response and a
    placeholder response are interchangeable to every caller. Every
    key is read defensively (a missing or wrong-typed key degrades to
    a safe default rather than raising), since this is untrusted model
    output, not a payload this module controls the shape of."""
    if phase == "reason":
        action = parsed.get("action")
        return {
            "action": action if isinstance(action, dict) else {},
            "checkpoint_complete": bool(parsed.get("checkpoint_complete", False)),
        }

    if phase == "assess_stakes":
        return {"stakes_high": bool(parsed.get("stakes_high", False))}

    if phase == "infer":
        basis = parsed.get("basis")
        return {"claim": str(parsed.get("claim", "")), "basis": basis if isinstance(basis, dict) else {}}

    if phase == "generate_hypotheses":
        hypotheses = []
        for item in parsed.get("hypotheses") or []:
            if isinstance(item, dict):
                basis = item.get("basis")
                hypotheses.append(
                    {"claim": str(item.get("claim", "")), "basis": basis if isinstance(basis, dict) else {}}
                )
        return {"hypotheses": hypotheses}

    if phase == "generate_explanations":
        return {"explanation": str(parsed.get("explanation", ""))}

    if phase == "generate_counterarguments":
        raw = parsed.get("counterarguments")
        return {"counterarguments": [str(item) for item in raw] if isinstance(raw, list) else []}

    if phase == "synthesize_findings":
        # DefaultAnalysisReasoning.synthesize_findings passes this
        # phase's "hypotheses" output straight into dataclasses.asdict()
        # for persistence (see c08_analysis_reasoning.py), so it must
        # come back as real Hypothesis instances, not dicts. Imported
        # here, deferred, rather than at module top: importing
        # c08_analysis_reasoning eagerly would be a circular import,
        # since that module imports get_reason_fn from this one. By the
        # time this phase actually runs, c08_analysis_reasoning is
        # already fully imported (something had to construct a
        # DefaultAnalysisReasoning to get here), so this is a plain
        # cache hit, not a real re-import.
        from components.c08_analysis_reasoning import Hypothesis

        hypotheses = []
        for item in parsed.get("hypotheses") or []:
            if isinstance(item, dict):
                basis = item.get("basis")
                hypotheses.append(
                    Hypothesis(claim=str(item.get("claim", "")), basis=basis if isinstance(basis, dict) else {})
                )
        impact_estimate = parsed.get("impact_estimate")
        return {
            "hypotheses": hypotheses,
            "impact_estimate": impact_estimate if isinstance(impact_estimate, dict) else None,
        }

    if phase == "estimate_impact":
        significance = parsed.get("significance")
        if significance not in ("low", "medium", "high", "unknown"):
            significance = "unknown"
        return {"significance": significance, "rationale": str(parsed.get("rationale", ""))}

    raise ValueError(f"openrouter_reason_fn: unknown phase {phase!r}")


def _fallback_response(phase: str) -> dict:
    """The degraded-but-real-call-was-attempted fallback — used only
    after `OPENROUTER_API_KEY` was genuinely present and a call to
    OpenRouter was genuinely made, but it failed (timeout, rate limit,
    non-200 status) or came back unparseable. Shaped like each phase's
    own `placeholder_reason_fn` output, with two deliberate exceptions
    that fail toward safety rather than toward silence:
      - "reason" reports `checkpoint_complete: True` (not False) so a
        persistently failing API degrades the graph to a bounded stop,
        never an unbounded reason-loop retrying a broken call forever.
      - "assess_stakes" reports `stakes_high: True` (not False) so a
        degraded call is flagged for reflection rather than silently
        treated as low-stakes.
    Every use of this fallback is recorded via `AuditManager` by
    `openrouter_reason_fn`, so a degraded call is visible in the audit
    trail rather than indistinguishable from a real answer."""
    if phase == "reason":
        return {"action": {"tool": "noop"}, "checkpoint_complete": True}
    if phase == "assess_stakes":
        return {"stakes_high": True}
    if phase == "infer":
        return {"claim": "openrouter call failed — no hypothesis available", "basis": {"confidence": "low", "degraded": True}}
    if phase == "generate_hypotheses":
        return {"hypotheses": []}
    if phase == "generate_explanations":
        return {"explanation": ""}
    if phase == "generate_counterarguments":
        return {"counterarguments": []}
    if phase == "synthesize_findings":
        return {"hypotheses": [], "impact_estimate": None}
    if phase == "estimate_impact":
        return {"significance": "unknown", "rationale": "openrouter call failed — no judgment available"}
    raise ValueError(f"openrouter_reason_fn: unknown phase {phase!r}")


def _call_openrouter_chat_completion(api_key: str, messages: list[dict]) -> str:
    """One real HTTP call to OpenRouter's OpenAI-compatible chat
    completions endpoint. `requests.post` with a Bearer header is all
    an OpenAI-shaped gateway needs — see this module's own docstring
    for why no SDK is used. `temperature: 0.0` because every phase here
    is a decision/judgment call this project wants reproducible, not
    creative. Raises on a non-2xx status (`raise_for_status`) or a
    malformed body (`KeyError`/`IndexError` on the `choices[0]` lookup)
    — both are caught by `openrouter_reason_fn`'s own error handling,
    not handled here."""
    response = requests.post(
        OPENROUTER_CHAT_COMPLETIONS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": _MAX_RESPONSE_TOKENS,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return body["choices"][0]["message"]["content"]


def openrouter_reason_fn(request: dict) -> dict:
    """The real `reason_fn` implementation — matches the
    `Callable[[dict], dict]` contract both `build_agent_runtime_graph`
    (`c10_agent_runtime.py`, ADR-0021) and `DefaultAnalysisReasoning`
    (`c08_analysis_reasoning.py`, ADR-0037) already inject a callable
    through, so it is a drop-in for either's `reason_fn` parameter with
    no change to either caller's shape (see ADR-0043).

    Reads `OPENROUTER_API_KEY` from the environment at call time only
    — never at import time, so this module stays importable, and its
    prompt-building/response-parsing logic stays unit-testable, with no
    key present at all (see `tests/test_llm.py`). Raises
    `MissingOpenRouterAPIKeyError` if the key genuinely isn't set when
    this function is actually invoked, rather than silently falling
    back to placeholder-shaped output — that fallback only happens
    after the key was present and a real call was genuinely attempted
    and failed (see `_fallback_response`)."""
    phase = request.get("phase")
    if phase not in _KNOWN_PHASES:
        raise ValueError(f"openrouter_reason_fn: unknown phase {phase!r}")

    _load_dotenv_into_environ()
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise MissingOpenRouterAPIKeyError(
            "OPENROUTER_API_KEY is not set. openrouter_reason_fn requires a real OpenRouter "
            "API key at call time (from the environment or a .env file at the repo root) — "
            "pass the caller's own placeholder_reason_fn instead if no key is configured."
        )

    messages = _build_messages(phase, request)
    try:
        raw_content = _call_openrouter_chat_completion(api_key, messages)
        parsed = _parse_json_object(raw_content)
        return _normalize_response(phase, parsed, request)
    except Exception as exc:  # network error, timeout, rate limit, malformed response
        DefaultAuditManager().record("openrouter_call_degraded", {"phase": phase, "error": str(exc)})
        return _fallback_response(phase)
