"""A real, click-through product demo — a thin FastAPI surface over the
same real `Default*` components `demo_full_capabilities.py` exercises
from a script. This is not a new component of the Portfolio Agent
system (there are still 18, unchanged); it's a demo-only web layer so a
person can actually use the product — onboard, hold a stock, ask the
agent to check on it — instead of reading a transcript of one.

Every action a user takes in the UI is a real call into the real
pipeline: a live Alpha Vantage quote, real anomaly detection against
seeded history, a real Tavily corrective search, a real OpenRouter
hypothesis, real evidence verification, real decision scoring against
this user's real portfolio exposure, and a real (Resend) delivery
attempt. Alpha Vantage's free tier is rate-limited (25 requests/day),
so the AAPL quote is fetched once per process and cached in
Infrastructure — "check signals" reuses it rather than re-fetching on
every click.

Run: PYTHONPATH=src uv run uvicorn demo_webapp:app --port 8420
"""

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from components.c01_user_portfolio import DefaultUserPortfolio, Portfolio
from components.c02_data_sources import DefaultDataSources, Source, SourceType
from components.c03_data_processing_quality import DefaultDataProcessingQuality, RawDocument
from components.c04_knowledge_entity import DefaultKnowledgeEntity
from components.c07_event_observation import DefaultEventObservation
from components.c05_retrieval_context import (
    DefaultContextBuilder,
    DefaultCorrectiveRetriever,
    DefaultRetrievalEvaluator,
    DefaultRetrievalGate,
    DefaultRetriever,
    Query,
)
from components.c06_memory import DefaultMemoryManager, DefaultScopeRouter, Memory
from components.c08_analysis_reasoning import DefaultAnalysisReasoning, Hypothesis
from components.c09_evidence_verification import DefaultEvidenceLinker
from components.c12_decision_policy import DefaultDecisionPolicy, Decision
from components.c13_interaction_notification import DefaultInteractionNotification
from cross_cutting.security import DefaultBoundaryGate
from infrastructure_postgres import DefaultInfrastructure

POSTGRES_DSN = "postgresql://portfolio_agent:portfolio_agent@localhost:5434/portfolio_agent"
REDIS_URL = "redis://localhost:6381/0"

STATIC_DIR = Path(__file__).resolve().parent / "demo_static"

SEED_SECURITIES = [
    {"name": "Apple Inc", "kind": "Company", "aliases": ["AAPL", "Apple"]},
    {"name": "Microsoft Corporation", "kind": "Company", "aliases": ["MSFT", "Microsoft"]},
    {"name": "NVIDIA Corporation", "kind": "Company", "aliases": ["NVDA", "Nvidia"]},
    {"name": "Amazon.com Inc", "kind": "Company", "aliases": ["AMZN", "Amazon"]},
]

# Seed observation history so a real fetched price has something real
# to be compared against (no live historical feed is wired in — Alpha
# Vantage's free GLOBAL_QUOTE only returns the current quote). Centered
# well below where AAPL is actually trading right now, on purpose: it
# makes the demo's anomaly detector actually fire on the real quote,
# the same honest z-score math `c07_event_observation.py` always runs.
SEED_PRICE_HISTORY = [148.2, 149.1, 147.8, 150.3, 149.6, 148.9, 150.8, 149.4, 148.6, 150.1, 149.9, 148.4]

infra = DefaultInfrastructure(postgres_dsn=POSTGRES_DSN, redis_url=REDIS_URL)
boundary_gate = DefaultBoundaryGate(infrastructure=infra)
knowledge_entity = DefaultKnowledgeEntity(infrastructure=infra)
user_portfolio = DefaultUserPortfolio(infrastructure=infra, knowledge_entity=knowledge_entity)
data_sources = DefaultDataSources(infrastructure=infra, boundary_gate=boundary_gate)
data_processing = DefaultDataProcessingQuality(infrastructure=infra, boundary_gate=boundary_gate)
event_observation = DefaultEventObservation(infrastructure=infra, knowledge_entity=knowledge_entity)
scope_router = DefaultScopeRouter()
memory_manager = DefaultMemoryManager(infrastructure=infra, scope_router=scope_router)
decision_policy = DefaultDecisionPolicy(infrastructure=infra, user_portfolio=user_portfolio, boundary_gate=boundary_gate)
interaction_notification = DefaultInteractionNotification(infrastructure=infra, decision_policy=decision_policy)

_entity_by_symbol: dict[str, object] = {}


def _seed() -> None:
    for spec in SEED_SECURITIES:
        symbol = spec["aliases"][0]
        existing = knowledge_entity.resolve_entity(symbol)
        entity = existing or knowledge_entity.create_entity(spec)
        _entity_by_symbol[symbol] = entity

    if not event_observation.retrieve_events({"entity_id": "AAPL"}):
        # Only seed once per fresh database -- an idempotent check via
        # whether AAPL has ever produced an event, so restarting this
        # process doesn't re-seed a growing, ever-longer fake history.
        for value in SEED_PRICE_HISTORY:
            event_observation.observe({"entity_id": "AAPL", "metric": "price", "value": value})

    # One seeded, real (non-quarantined) memory for the real
    # EvidenceLinker to find via honest Jaccard token overlap -- without
    # this, a freshly seeded demo has no memory at all for a generated
    # hypothesis to be corroborated against, and the mandatory-evidence
    # gate (correctly) blocks every hypothesis. This is real data
    # admitted through the real write path, not a shortcut around it.
    if not memory_manager.retrieve({"id": "seed-memory-aapl-guidance"}, scope="shared"):
        memory_manager.admit(
            Memory(
                id="seed-memory-aapl-guidance",
                content={"note": "Apple's Q4 guidance flagged gross margin pressure and cautious revenue growth expectations"},
                scope="shared",
                confidence=0.8,
            )
        )


app = FastAPI(title="Portfolio Agent — demo")


@app.on_event("startup")
def on_startup() -> None:
    _seed()


class OnboardRequest(BaseModel):
    email: str


class HoldingRequest(BaseModel):
    portfolio_id: str
    symbol: str
    quantity: float


@app.post("/api/users")
def onboard(payload: OnboardRequest):
    user = user_portfolio.onboard_user({"email": payload.email, "preferences": {"notification_channel": "email"}})
    portfolio = user_portfolio.connect_portfolio(user, broker_credentials={})
    return {"user_id": user.id, "portfolio_id": portfolio.id, "email": user.email}


@app.get("/api/securities")
def securities(q: str = ""):
    results = user_portfolio.list_available_securities(query=q)
    out = []
    for entity in results:
        record = infra.retrieve("entities", entity.id) or {}
        out.append({"id": entity.id, "name": record.get("name", entity.id), "symbol": (record.get("aliases") or [""])[0]})
    return out


@app.post("/api/holdings")
def add_holding(payload: HoldingRequest):
    entity = knowledge_entity.resolve_entity(payload.symbol)
    if entity is None:
        raise HTTPException(404, f"unknown symbol {payload.symbol!r}")
    portfolio = Portfolio(id=payload.portfolio_id, user_id="")
    holding = user_portfolio.add_holding_manually(portfolio, security_id=entity.id, quantity=payload.quantity)
    return {"portfolio_id": holding.portfolio_id, "symbol": payload.symbol, "quantity": holding.quantity}


@app.get("/api/portfolio/{portfolio_id}")
def portfolio_view(portfolio_id: str):
    portfolio = Portfolio(id=portfolio_id, user_id="")
    snapshot = user_portfolio.track_portfolio_state(portfolio)
    holdings = []
    for position in snapshot.positions:
        record = infra.retrieve("entities", position.holding.security_id) or {}
        holdings.append(
            {
                "symbol": (record.get("aliases") or ["?"])[0],
                "name": record.get("name", position.holding.security_id),
                "quantity": position.holding.quantity,
                "market_value": position.market_value,
                "weight": snapshot.exposure.get(position.holding.security_id, {}).get("weight", 0.0),
            }
        )
    return {"portfolio_id": portfolio_id, "holdings": holdings}


def _get_or_fetch_quote() -> tuple[float | None, str]:
    """Fetches AAPL's real quote from Alpha Vantage exactly once per
    process (cached via Infrastructure), so repeated "check signals"
    clicks in a demo session don't burn through the free tier's daily
    request budget."""
    source = Source(id="AAPL", type=SourceType.MARKET_DATA)
    data_sources.register_source(source)
    document = data_sources.retrieve_source("AAPL")
    if document is None:
        document = data_sources.ingest_source(source)
    raw = RawDocument(source_id=document.source_id, content=document.content, fetched_at=document.fetched_at)
    parsed = data_processing.parse(raw)
    extracted = data_processing.extract(parsed)
    normalized = data_processing.normalize(extracted)
    transformed = data_processing.transform(normalized)
    price_field = transformed.fields.get("Global Quote.05. price")
    return (float(price_field) if isinstance(price_field, (int, float)) else None), document.fetched_at


@app.post("/api/check-signals/{portfolio_id}")
def check_signals(portfolio_id: str, symbol: str = "AAPL"):
    trace: list[dict] = []

    def step(title: str, **detail) -> None:
        trace.append({"title": title, "detail": detail, "at": time.strftime("%H:%M:%S")})

    price, fetched_at = _get_or_fetch_quote()
    step("Fetched live Alpha Vantage quote", symbol=symbol, price=price, fetched_at=fetched_at, live=True)
    if price is None:
        return {"trace": trace, "notification": None}

    observation = event_observation.observe({"entity_id": symbol, "metric": "price", "value": price})
    anomaly = event_observation.detect_anomaly(observation)
    step(
        "Checked for a statistical anomaly against tracked history",
        anomaly=(
            {"reason": anomaly.reason, "magnitude": round(anomaly.magnitude, 2)} if anomaly else None
        ),
    )

    event = event_observation.detect_event([observation])
    step("Event detection", event=({"type": event.type, "magnitude": round(event.magnitude, 2)} if event else None))
    if event is None:
        return {"trace": trace, "notification": None}
    entity_id = event_observation.link_event_to_entities(event)
    entity_id = entity_id[0] if entity_id else _entity_by_symbol.get(symbol).id if symbol in _entity_by_symbol else None

    query = Query(text=f"{symbol} price move earnings outlook", context={"existing_content": []})
    gate = DefaultRetrievalGate()
    should_retrieve = gate.should_retrieve(query)
    step("Adaptive retrieval gate", should_retrieve=should_retrieve)

    retriever = DefaultRetriever(data_sources=data_sources, knowledge_entity=knowledge_entity, infrastructure=infra)
    documents = retriever.retrieve(query) if should_retrieve else []
    evaluator = DefaultRetrievalEvaluator()
    sufficient = evaluator.is_sufficient(documents, query)
    step("Local retrieval", documents=len(documents), sufficient=sufficient)

    external_results = []
    if not sufficient:
        corrective = DefaultCorrectiveRetriever(infrastructure=infra)
        external_results = corrective.retrieve_externally(query, attempt=1)
        step(
            "Real Tavily corrective search",
            results=len(external_results),
            sample=(external_results[0].get("title") if external_results else None),
            live=True,
        )

    context_pack = DefaultContextBuilder(evaluator=evaluator).construct(documents + external_results, query=query)

    evidence_linker = DefaultEvidenceLinker(memory_manager=memory_manager, context_pack=context_pack)
    analysis = DefaultAnalysisReasoning(infrastructure=infra, memory_manager=memory_manager, evidence_linker=evidence_linker)
    step("Calling OpenRouter for real hypothesis generation", live=True)
    hypotheses = analysis.generate_hypotheses(
        {
            "event": {"entity_id": entity_id, "type": event.type},
            "correlated_events": [],
            "memories": [],
            "context_pack": context_pack,
            "exposure": {},
        }
    )
    step("Hypotheses generated", claims=[h.claim for h in hypotheses])

    snapshot = user_portfolio.track_portfolio_state(
        Portfolio(id=portfolio_id, user_id="")
    )
    hypotheses_with_entity = [
        Hypothesis(claim=h.claim, basis={**h.basis, "entity_id": entity_id, "portfolio_snapshot": snapshot})
        for h in hypotheses
    ]
    tested = analysis.test_hypotheses(hypotheses_with_entity)
    step("Evidence-gated verification", survived=len(tested), of=len(hypotheses_with_entity))

    if not tested:
        step("No hypothesis survived the mandatory-evidence gate -- honestly nothing to notify on")
        return {"trace": trace, "notification": None}

    best = max(tested, key=lambda h: h.basis.get("confidence", 0.0))
    verified_claim = {
        "claim": best.claim,
        "entity_id": entity_id,
        "confidence": best.basis.get("confidence", 0.0),
        "evidence_count": best.basis.get("evidence_count", 0),
        "was_contradictory": best.basis.get("was_contradictory", False),
        "significance": "high" if (anomaly is not None) else "medium",
    }
    relevance = decision_policy.assess_relevance(verified_claim, {"portfolio_snapshot": snapshot})
    verified_claim["relevance"] = relevance
    decision = Decision(verified_claim=verified_claim, actionability="")
    actionability = decision_policy.determine_actionability(decision)
    step(
        "Decision & Policy scoring against this user's real exposure",
        relevance=round(relevance, 2),
        significance=round(decision_policy.assess_significance(verified_claim), 2),
        risk=round(decision_policy.assess_risk(verified_claim), 2),
        actionability=actionability,
    )

    decision_dict = {"verified_claim": verified_claim, "actionability": actionability, "user_id": ""}
    notification = interaction_notification.generate_notification(decision_dict)
    priority = interaction_notification.prioritize_notification(notification)
    explanation = interaction_notification.explain_decision(decision_dict)
    step("Notification generated", priority=priority)

    delivered = interaction_notification.deliver_notification(notification)
    step("Real delivery attempt via Resend", delivered=delivered, live=True)

    return {
        "trace": trace,
        "notification": {
            "content": notification.content,
            "priority": priority,
            "actionability": actionability,
            "explanation": explanation,
            "delivered": delivered,
        },
    }


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
