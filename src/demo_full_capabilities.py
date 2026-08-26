"""Full-capability demo — every one of the 18 components' real
`Default*` implementation, wired together against a real, shared
Postgres/Redis-backed Infrastructure and this project's four resolved
external providers: OpenRouter (LLM), Alpha Vantage (market data/news),
Tavily (corrective search), Resend (email delivery).

Not a test suite: a narrated walkthrough, one section per component.
Every section prints what it called and what came back. A section that
hits a real external failure (e.g. Resend's unverified-domain 403, or a
transient network hiccup) is caught and reported so the rest of the
demo keeps running — this script is meant to be watched start to
finish, not to assert pass/fail.

Run with: PYTHONPATH=src uv run python src/demo_full_capabilities.py
Requires docker-compose Postgres/Redis reachable at the ports below,
and OPENROUTER_API_KEY / ALPHA_VANTAGE_API_KEY / TAVILY_API_KEY /
RESEND_API_KEY set (.env at the repo root already has all four).
"""

import random
import time
import traceback
from dataclasses import asdict

from components.c01_user_portfolio import DefaultUserPortfolio
from components.c02_data_sources import DefaultDataSources, Source, SourceType
from components.c03_data_processing_quality import DefaultDataProcessingQuality, RawDocument
from components.c04_knowledge_entity import DefaultKnowledgeEntity
from components.c05_retrieval_context import (
    DefaultContextBuilder,
    DefaultCorrectiveRetriever,
    DefaultRetrievalEvaluator,
    DefaultRetrievalGate,
    DefaultRetriever,
    Query,
)
from components.c06_memory import (
    DefaultEntityLinker,
    DefaultMemoryConsolidator,
    DefaultMemoryEvaluator,
    DefaultMemoryManager,
    DefaultQuarantineGate,
    DefaultScopeRouter,
    Mem0EntityLinker,
    Memory,
    MemoryCandidate,
    _quarantine_id_for,
)
from components.c07_event_observation import DefaultEventObservation
from components.c08_analysis_reasoning import DefaultAnalysisReasoning, Hypothesis
from components.c09_evidence_verification import (
    Claim,
    DefaultClaimVerifier,
    DefaultContradictionResolver,
    DefaultEvidenceLinker,
    DefaultMandatoryEvidenceGate,
    Evidence,
)
from components.c10_agent_runtime import (
    Checkpoint,
    DefaultAgentCoordinator,
    DefaultDelegationManager,
    DefaultRecoveryManager,
    DefaultStateManager,
    DefaultTaskManager,
    DefaultWorkflowManager,
    build_agent_runtime_graph,
    initial_loop_state,
)
from components.c11_tools_environment import DefaultToolsEnvironment, Tool, ToolCall
from components.c12_decision_policy import DefaultDecisionPolicy, Decision
from components.c13_interaction_notification import DefaultInteractionNotification
from components.c14_learning_evaluation import DefaultLearningEvaluation, Prediction
from cross_cutting.observability import DefaultAuditManager
from cross_cutting.reliability import (
    DefaultCircuitBreaker,
    DefaultFailureClassifier,
    FailureEvent,
)
from cross_cutting.security import DefaultBoundaryGate
from infrastructure_postgres import DefaultInfrastructure

POSTGRES_DSN = "postgresql://portfolio_agent:portfolio_agent@localhost:5433/portfolio_agent"
REDIS_URL = "redis://localhost:6380/0"


class DemoPlanner:
    """Local, demo-only Planner: one real checkpoint for one real task,
    so DefaultAgentCoordinator has something to actually coordinate.
    StubPlanner (the project's own stub) always returns an empty list,
    which would make the coordinator demo a no-op."""

    def __init__(self, checkpoint: Checkpoint) -> None:
        self._checkpoint = checkpoint

    def plan_checkpoints(self, task):
        return [self._checkpoint]


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def s_infrastructure(state):
    infra = state["infra"]
    record_id = infra.store("demo_notes", {"id": "note-1", "text": "hello from the full-capability demo"})
    print("store ->", record_id)
    print("retrieve ->", infra.retrieve("demo_notes", "note-1"))
    print("query ->", infra.query("demo_notes", {"text": "hello from the full-capability demo"}))
    infra.publish("demo_topic", {"msg": "queued event"})
    received = []
    infra.subscribe("demo_topic", lambda event: received.append(event))
    print("publish + subscribe ->", received)
    schedule_id = infra.schedule(60, {"job": "demo"})
    print("schedule ->", schedule_id)
    infra.cache_set("demo_key", {"cached": True}, ttl_seconds=60)
    print("cache_get ->", infra.cache_get("demo_key"))
    import os

    os.environ.setdefault("DEMO_SECRET", "demo-value")
    print("get_secret ->", infra.get_secret("DEMO_SECRET"))


def s_security(state):
    gate = DefaultBoundaryGate(infrastructure=state["infra"])
    print("authenticate('') ->", gate.authenticate(""))
    print("authenticate('demo-user') ->", gate.authenticate("demo-user"))
    print("authorize (no grant yet) ->", gate.authorize("demo-user", "notify", "portfolio"))
    grant_id = gate.grant("demo-user", "notify", "portfolio")
    print("grant ->", grant_id)
    print("authorize (after grant) ->", gate.authorize("demo-user", "notify", "portfolio"))
    print("tag_provenance ->", gate.tag_provenance({"headline": "Apple beats earnings"}, source="news_api"))
    state["boundary_gate"] = gate


def s_reliability(state):
    classifier = DefaultFailureClassifier()
    breaker = DefaultCircuitBreaker()
    fresh_event = FailureEvent(component="tools_environment", tool="market_data_tool", error="timeout", history=[])
    print("classify (fresh failure) ->", classifier.classify(fresh_event))
    looping_event = FailureEvent(
        component="tools_environment", tool="market_data_tool", error="timeout", history=[fresh_event] * 3
    )
    print("classify (repeated same-component failures) ->", classifier.classify(looping_event))
    breaker.trip("market_data_tool")
    print("is_available (after trip) ->", breaker.is_available("market_data_tool"))
    print("find_alternative (nothing registered) ->", breaker.find_alternative("market_data_tool"))


def s_knowledge_entity(state):
    ke = DefaultKnowledgeEntity(infrastructure=state["infra"])
    apple = ke.create_entity({"name": "Apple Inc", "kind": "Company", "aliases": ["AAPL", "Apple"]})
    microsoft = ke.create_entity({"name": "Microsoft Corporation", "kind": "Company", "aliases": ["MSFT"]})
    print("create_entity ->", apple, microsoft)
    print("resolve_entity (exact 'AAPL') ->", ke.resolve_entity("AAPL"))
    print("resolve_entity (fuzzy 'Aple Inc') ->", ke.resolve_entity("Aple Inc"))
    relationship = ke.link_entities(apple, microsoft, "competitor_of")
    print("link_entities ->", relationship)
    print("represent_relationships(apple) ->", ke.represent_relationships(apple))
    duplicate = ke.create_entity({"name": "Apple", "kind": "Company"})
    merged = ke.merge_entities(apple, duplicate)
    print("merge_entities ->", merged)
    updated = ke.update_knowledge(apple, {"sector": "Technology"})
    print("update_knowledge ->", updated)
    print("search_entities(kind='Company') ->", ke.search_entities(kind="Company"))
    print("get_entity(apple.id) ->", ke.get_entity(apple.id))
    state["knowledge_entity"] = ke
    state["apple"] = apple
    state["microsoft"] = microsoft


def s_user_portfolio(state):
    up = DefaultUserPortfolio(infrastructure=state["infra"], knowledge_entity=state["knowledge_entity"])
    user = up.onboard_user(
        {"email": "demo-investor@example.com", "preferences": {"notification_channel": "email"}}
    )
    print("onboard_user ->", user)
    portfolio = up.connect_portfolio(user, broker_credentials={})
    print("connect_portfolio (PlaceholderBrokerConnector, ADR-0023) ->", portfolio)
    securities = up.list_available_securities(query="Apple")
    print("list_available_securities('Apple') ->", securities)
    holding = up.add_holding_manually(portfolio, security_id=state["apple"].id, quantity=50)
    print("add_holding_manually ->", holding)
    transaction = up.add_transaction_manually(portfolio, kind="buy", amount=9000.0)
    print("add_transaction_manually ->", transaction)
    snapshot = up.synchronize_portfolio(portfolio)
    print("synchronize_portfolio ->", snapshot)
    exposure = up.calculate_exposure(snapshot)
    print("calculate_exposure ->", exposure)
    updated_user = up.manage_preferences(user, {"quiet_mode": False})
    print("manage_preferences ->", updated_user)
    relevant = up.determine_user_relevance(user, {"security_id": state["apple"].id})
    print("determine_user_relevance ->", relevant)
    state["user_portfolio"] = up
    state["user"] = user
    state["portfolio"] = portfolio
    state["snapshot"] = snapshot


def s_data_sources(state):
    ds = DefaultDataSources(infrastructure=state["infra"])
    quote_source = Source(id="AAPL", type=SourceType.MARKET_DATA)
    news_source = Source(id="AAPL", type=SourceType.NEWS)
    ds.register_source(quote_source)
    ds.register_source(news_source)
    print("discover_source(MARKET_DATA) ->", ds.discover_source({"type": "MARKET_DATA"}))
    print("Fetching a real Alpha Vantage GLOBAL_QUOTE for AAPL...")
    quote_doc = ds.ingest_source(quote_source)
    print("ingest_source(quote) content[:300] ->", quote_doc.content[:300])
    retrieved = ds.retrieve_source("AAPL")
    print("retrieve_source ->", retrieved.fetched_at if retrieved else None)
    snapshot = ds.update_source(quote_source)
    print("update_source ->", snapshot)
    provenance = ds.track_source_provenance(quote_doc)
    print("track_source_provenance ->", provenance)
    timestamp = ds.track_source_timestamp(quote_doc)
    print("track_source_timestamp ->", timestamp)
    metadata = ds.track_source_reliability_metadata(quote_source)
    print("track_source_reliability_metadata ->", metadata)
    state["data_sources"] = ds
    state["quote_source"] = quote_source
    state["quote_doc"] = quote_doc


def s_data_processing(state):
    dpq = DefaultDataProcessingQuality(infrastructure=state["infra"])
    quote_doc = state["quote_doc"]
    raw = RawDocument(source_id=quote_doc.source_id, content=quote_doc.content, fetched_at=quote_doc.fetched_at)
    parsed = dpq.parse(raw)
    print("parse -> format:", parsed.structure.get("format"))
    extracted = dpq.extract(parsed)
    print("extract ->", extracted.fields)
    normalized = dpq.normalize(extracted)
    transformed = dpq.transform(normalized)
    print("transform ->", transformed.fields)
    deduped = dpq.deduplicate(transformed)
    print("deduplicate -> duplicate_of:", deduped.fields.get("_duplicate_of"))
    valid = dpq.validate(deduped)
    print("validate ->", valid)
    score = dpq.score_data_quality(deduped)
    print("score_data_quality ->", score)
    stale = dpq.detect_stale_data(deduped)
    print("detect_stale_data ->", stale)
    lineage = dpq.track_data_lineage(deduped)
    print("track_data_lineage ->", lineage)


def s_event_observation(state):
    eo = DefaultEventObservation(infrastructure=state["infra"], knowledge_entity=state["knowledge_entity"])
    base = 150.0
    last_observation = None
    for _ in range(6):
        last_observation = eo.observe({"entity_id": "AAPL", "metric": "price", "value": base + random.uniform(-1, 1)})
    spike = eo.observe({"entity_id": "AAPL", "metric": "price", "value": base + 40})
    print("detect_change (spike vs prior) ->", eo.detect_change(spike, last_observation))
    print("detect_anomaly (spike) ->", eo.detect_anomaly(spike))
    event = eo.detect_event([spike])
    print("detect_event ->", event)
    if event is not None:
        print("classify_event ->", eo.classify_event(event))
        print("link_event_to_entities ->", eo.link_event_to_entities(event))
        events = eo.retrieve_events({"entity_id": "AAPL"})
        print(f"retrieve_events -> {len(events)} event(s)")
        print("correlate_events ->", eo.correlate_events(events))
        state["event"] = event
    state["event_observation"] = eo


def s_retrieval_context(state):
    gate = DefaultRetrievalGate()
    query = Query(text="Apple AAPL earnings price", context={"existing_content": []})
    print("should_retrieve ->", gate.should_retrieve(query))

    retriever = DefaultRetriever(
        data_sources=state["data_sources"], knowledge_entity=state["knowledge_entity"], infrastructure=state["infra"]
    )
    print("Retrieving via Data & Sources + Knowledge & Entity (may ingest AAPL NEWS from Alpha Vantage)...")
    documents = retriever.retrieve(query)
    print(f"retrieve -> {len(documents)} document(s)")

    evaluator = DefaultRetrievalEvaluator()
    sufficient = evaluator.is_sufficient(documents, query)
    print("is_sufficient ->", sufficient)

    corrective = DefaultCorrectiveRetriever(infrastructure=state["infra"])
    print("Calling real Tavily search for corrective retrieval...")
    external_results = corrective.retrieve_externally(query, attempt=1)
    print(f"retrieve_externally (Tavily) -> {len(external_results)} result(s)")
    if external_results:
        print("  sample ->", {k: external_results[0].get(k) for k in ("title", "url") if k in external_results[0]})

    builder = DefaultContextBuilder(evaluator=evaluator)
    context_pack = builder.construct(documents + external_results, query=query)
    print("construct -> sufficient:", context_pack.sufficient, "documents:", len(context_pack.documents))

    state["context_pack"] = context_pack
    state["query"] = query


def s_memory(state):
    infra = state["infra"]
    scope_router = DefaultScopeRouter()
    manager = DefaultMemoryManager(infrastructure=infra, scope_router=scope_router)
    evaluator = DefaultMemoryEvaluator()

    experience = {"content": {"note": "AAPL price spiked 40 points on an earnings beat"}, "confidence": 0.8}
    print("should_become_memory ->", evaluator.should_become_memory(experience))

    candidate = MemoryCandidate(content=experience["content"], source="event_observation", provenance_verified=True)
    jaccard_linker = DefaultEntityLinker()
    existing_shared = manager.retrieve({}, scope="shared")
    print("DefaultEntityLinker.link (Jaccard token overlap) ->", jaccard_linker.link(candidate, existing_shared))

    print("Loading local fastembed model for Mem0EntityLinker (first use on this machine downloads it)...")
    semantic_linker = Mem0EntityLinker()
    unrelated_memory = Memory(
        id="mem-weather-demo", content={"note": "The weather in Paris is sunny today"}, scope="shared", confidence=0.9
    )
    related_content = {"note": "Apple shares fell after a disappointing quarterly earnings report"}
    related_candidate = MemoryCandidate(content=related_content, source="news", provenance_verified=True)
    print(
        "Mem0EntityLinker.link (real cosine similarity, related pair vs. unrelated) ->",
        semantic_linker.link(related_candidate, [unrelated_memory]),
    )

    quarantine_gate = DefaultQuarantineGate(infrastructure=infra)
    unverified_candidate = MemoryCandidate(
        content={"note": "unverified rumor: AAPL acquiring a startup"}, source="social_media", provenance_verified=False
    )
    print("check_provenance (unverified) ->", quarantine_gate.check_provenance(unverified_candidate))
    quarantine_gate.quarantine(unverified_candidate)
    quarantine_id = _quarantine_id_for(unverified_candidate)
    print("is_expired (just quarantined) ->", quarantine_gate.is_expired(quarantine_id))
    released = quarantine_gate.release(quarantine_id)
    print("release -> status:", released["status"])

    print("route(scope='shared') ->", scope_router.route(Memory(id="x", content={}, scope="shared")))
    memory = Memory(id="memory-aapl-1", content=candidate.content, scope="shared", confidence=0.8)
    manager.admit(memory)
    print("admit + is_in_working_set ->", manager.is_in_working_set({"id": memory.id}))
    print("retrieve(scope='shared') count ->", len(manager.retrieve({}, scope="shared")))

    consolidator = DefaultMemoryConsolidator(infrastructure=infra, scope_router=scope_router)
    memory.last_touched_at = time.time() - 40 * 24 * 60 * 60
    print("check_staleness (artificially aged 40 days) ->", consolidator.check_staleness(memory))
    consolidator.update_or_invalidate(memory)
    print("update_or_invalidate -> confidence:", memory.confidence, "quarantined:", memory.quarantined)

    state["memory_manager"] = manager
    state["scope_router"] = scope_router


def s_analysis_reasoning(state):
    infra = state["infra"]
    evidence_linker = DefaultEvidenceLinker(memory_manager=state["memory_manager"], context_pack=state["context_pack"])
    analysis = DefaultAnalysisReasoning(
        infrastructure=infra, memory_manager=state["memory_manager"], evidence_linker=evidence_linker
    )

    print("Calling OpenRouter (anthropic/claude-haiku-4.5) for real inference...")
    premises = [{"fact": "AAPL price rose sharply intraday"}, {"fact": "Earnings beat consensus EPS estimates"}]
    inferred = analysis.infer(premises)
    print("infer ->", inferred)

    print("compare ->", analysis.compare({"price": 150, "eps": 2.1}, {"price": 190, "eps": 2.35}))

    analysis_input = {
        "event": {"entity_id": "AAPL", "type": "earnings"},
        "correlated_events": [],
        "memories": [],
        "context_pack": state["context_pack"],
        "exposure": state["snapshot"].exposure,
    }
    hypotheses = analysis.generate_hypotheses(analysis_input)
    print(f"generate_hypotheses -> {len(hypotheses)} hypothesis(es)")
    for hypothesis in hypotheses:
        print("  -", hypothesis.claim)
    if not hypotheses:
        hypotheses = [inferred]

    hypotheses_with_entity = [
        Hypothesis(
            claim=h.claim,
            basis={**h.basis, "entity_id": state["apple"].id, "portfolio_snapshot": state["snapshot"]},
        )
        for h in hypotheses
    ]
    tested = analysis.test_hypotheses(hypotheses_with_entity)
    print(f"test_hypotheses -> {len(tested)}/{len(hypotheses_with_entity)} survived the mandatory-evidence gate")

    if tested:
        print("estimate_impact ->", analysis.estimate_impact(tested[0]))
        print("generate_explanations ->", analysis.generate_explanations(tested[0]))
        print("generate_counterarguments ->", analysis.generate_counterarguments(tested[0]))

    synthesized = analysis.synthesize_findings(tested)
    print("synthesize_findings ->", synthesized)

    full_analysis = analysis.analyze(
        event={"entity_id": "AAPL", "type": "earnings"},
        context={
            "documents": state["context_pack"].documents,
            "query_text": state["query"].text,
            "portfolio_snapshot": state["snapshot"],
        },
        memory={},
    )
    print("analyze (full fan-in: event+context+memory -> hypotheses -> tested -> synthesized) ->", full_analysis)

    state["analysis"] = analysis
    state["tested_hypotheses"] = tested


def s_evidence_verification(state):
    linker = DefaultEvidenceLinker(memory_manager=state["memory_manager"], context_pack=state["context_pack"])
    claim = Claim(text="AAPL price spiked after an earnings beat", source_component="demo")
    evidence = linker.link(claim)
    print(f"EvidenceLinker.link -> {len(evidence)} evidence item(s)")

    gate = DefaultMandatoryEvidenceGate()
    has_evidence = gate.has_evidence(evidence)
    print("has_evidence ->", has_evidence)
    if not has_evidence:
        gate.block(claim)
        print("block -> recorded in audit.log (claim_blocked)")

    resolver = DefaultContradictionResolver()
    evidence_a = Evidence(content={"metric": "EPS", "result": "beat"}, source="analyst_a", reliability=0.9, freshness=1.0)
    evidence_b = Evidence(content={"metric": "EPS", "result": "missed"}, source="analyst_b", reliability=0.6, freshness=0.8)
    print("sources_agree (deliberately conflicting pair) ->", resolver.sources_agree([evidence_a, evidence_b]))
    print("resolve ->", resolver.resolve([evidence_a, evidence_b]))

    verifier = DefaultClaimVerifier(contradiction_resolver=resolver)
    verified = verifier.verify(claim, [evidence_a, evidence_b])
    print("verify ->", verified)
    print("score_confidence ->", verifier.score_confidence(verified))

    state["verified_claim_dict"] = {
        "claim": claim.text,
        "entity_id": state["apple"].id,
        "confidence": verified.confidence,
        "evidence_count": len(verified.evidence),
        "was_contradictory": verified.was_contradictory,
        "significance": "high",
    }


def s_decision_policy(state):
    infra = state["infra"]
    policy = DefaultDecisionPolicy(
        infrastructure=infra, user_portfolio=state["user_portfolio"], boundary_gate=state["boundary_gate"]
    )
    verified_claim = state["verified_claim_dict"]

    relevance = policy.assess_relevance(verified_claim, {"portfolio_snapshot": state["snapshot"]})
    print("assess_relevance ->", relevance)
    verified_claim["relevance"] = relevance
    print("assess_significance ->", policy.assess_significance(verified_claim))
    print("assess_risk ->", policy.assess_risk(verified_claim))

    decision = Decision(verified_claim=verified_claim, actionability="")
    print("determine_actionability ->", policy.determine_actionability(decision))

    state["boundary_gate"].grant(state["user"].id, "notify", "portfolio")
    authorized = policy.authorize_action({"identity": state["user"].id, "action": "notify", "resource": "portfolio"})
    print("authorize_action (after grant) ->", authorized)

    for attempt in range(1, 7):
        allowed = policy.enforce_policy({"identity": state["user"].id, "action": "notify"})
        print(f"enforce_policy attempt {attempt} ->", allowed)

    policy.escalate("manual demo escalation", {"reason": "showing escalate()"})
    print("escalate -> recorded in audit.log (decision_policy_escalation)")

    print("request_approval ->", policy.request_approval({"id": "demo-action-1", "action": "rebalance_portfolio"}))

    state["decision_policy"] = policy
    state["decision"] = decision


def s_interaction_notification(state):
    ino = DefaultInteractionNotification(infrastructure=state["infra"], decision_policy=state["decision_policy"])
    decision_dict = {**asdict(state["decision"]), "user_id": state["user"].id}

    notification = ino.generate_notification(decision_dict)
    print("generate_notification ->", notification)
    print("prioritize_notification ->", ino.prioritize_notification(notification))

    user_dict = {
        "id": state["user"].id,
        "email": state["user"].email or "demo-investor@example.com",
        "preferences": {"notification_channel": "email", "notification_verbosity": "detailed"},
    }
    personalized = ino.personalize_notification(notification, user_dict)
    print("personalize_notification ->", personalized)

    print("Attempting real delivery via Resend (unverified sandbox domain -> expect a documented 403)...")
    delivered = ino.deliver_notification(personalized)
    print("deliver_notification ->", delivered)

    print("explain_decision ->", ino.explain_decision(decision_dict))
    print("collect_feedback ->", ino.collect_feedback(personalized))
    print("collect_user_response ->", ino.collect_user_response(personalized))

    state["notification"] = personalized


def s_learning_evaluation(state):
    infra = state["infra"]
    learning = DefaultLearningEvaluation(
        infrastructure=infra, data_sources=state["data_sources"], memory_manager=state["memory_manager"]
    )
    prediction = Prediction(
        claim="AAPL price will rise further on continued earnings momentum",
        confidence=0.7,
        id="prediction-1",
        entity_id="AAPL",
        metric="price",
        reference_value=150.0,
        predicted_value=165.0,
        source_type="MARKET_DATA",
        source_ids=[state["quote_doc"].source_id],
        made_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    outcome = learning.measure_outcome(prediction)
    print("measure_outcome ->", outcome)
    comparison = learning.compare_prediction_vs_outcome(prediction, outcome)
    print("compare_prediction_vs_outcome ->", comparison)
    evaluation = learning.evaluate(prediction, outcome)
    print("evaluate -> correct:", evaluation.correct)
    print("analyze_errors ->", learning.analyze_errors(evaluation))
    print("collect_feedback ->", learning.collect_feedback(evaluation))

    learning.update_knowledge(evaluation)
    print("update_knowledge -> drove Memory's full write path (see audit.log: knowledge_updated)")

    baseline = learning.evaluate(prediction, outcome)
    print("detect_regression (vs. itself) ->", learning.detect_regression(evaluation, baseline))
    versions = learning.evaluate_versions(
        {"version": "v1", "evaluations": [asdict(evaluation)]},
        {"version": "v2", "evaluations": [asdict(baseline)]},
    )
    print("evaluate_versions ->", versions)

    trajectory = learning.replay("AAPL")
    print(f"replay('AAPL') -> {len(trajectory)} entries across the pipeline")
    for entry in trajectory[:5]:
        print("  -", entry["component"], entry["type"], entry["at"])


def s_tools_environment(state):
    tools_env = DefaultToolsEnvironment()
    market_data_tool = Tool(name="alpha_vantage_quote", schema={"tags": ["market_data"]})
    backup_tool = Tool(name="backup_quote_source", schema={"tags": ["market_data"]})
    tools_env.register_tool(market_data_tool, invoke=lambda args: {"price": 190.5, "symbol": args.get("symbol")})
    tools_env.register_tool(backup_tool, invoke=lambda args: {"price": 190.0, "symbol": args.get("symbol")})

    print("discover_tool('market_data') ->", tools_env.discover_tool("market_data"))
    selected = tools_env.select_tool("market_data", tools_env.discover_tool("market_data"))
    print("select_tool ->", selected)

    result = tools_env.execute_tool(ToolCall(tool_name="alpha_vantage_quote", arguments={"symbol": "AAPL"}))
    print("execute_tool (success) ->", result)
    print("validate_result ->", tools_env.validate_result(result))

    def always_fails(_args):
        raise RuntimeError("simulated tool failure")

    failing_tool = Tool(name="flaky_tool", schema={"tags": ["market_data"]})
    tools_env.register_tool(failing_tool, invoke=always_fails)
    failed_result = None
    for _ in range(3):
        failed_result = tools_env.execute_tool(ToolCall(tool_name="flaky_tool", arguments={}))
    print("execute_tool (after 3 repeated failures, circuit should trip) ->", failed_result)

    replacement = tools_env.switch_tool(failing_tool, need="market_data")
    print("switch_tool ->", replacement)
    print("retry_tool ->", tools_env.retry_tool(ToolCall(tool_name="alpha_vantage_quote", arguments={"symbol": "MSFT"})))

    tools_env.register_environment_adapter("demo_adapter", lambda action: {"echo": action})
    print("interact_with_environment ->", tools_env.interact_with_environment({"adapter": "demo_adapter", "value": 42}))


def s_agent_runtime(state):
    task_manager = DefaultTaskManager()
    task = task_manager.create_task({"trigger": "demo"})
    print("create_task ->", task)
    task_manager.pause(task.id)
    print("status (after pause) ->", task_manager.status(task.id))
    task_manager.resume(task.id)
    print("status (after resume) ->", task_manager.status(task.id))

    state_manager = DefaultStateManager()
    state_manager.update_state(task.id, {"phase": "planning"})
    print("get_state ->", state_manager.get_state(task.id))

    delegation_manager = DefaultDelegationManager()
    print("delegate ->", delegation_manager.delegate({"sub_task": "fetch AAPL filings"}))

    recovery_manager = DefaultRecoveryManager()
    checkpoint = Checkpoint(id="demo-checkpoint", subgoal={"goal": "assess AAPL earnings impact"})
    print(
        "recover (transient failure) ->",
        recovery_manager.recover(checkpoint, {"component": "tools_environment", "tool": "flaky_tool", "error": "timeout"}),
    )

    workflow_manager = DefaultWorkflowManager()

    print("Compiling the real LangGraph checkpoint loop; reason_fn resolves to OpenRouter automatically...")
    graph = build_agent_runtime_graph(recovery_manager=recovery_manager, delegation_manager=delegation_manager)
    final_state = graph.invoke(initial_loop_state(checkpoint))
    print("graph.invoke -> done:", final_state["done"], "| steps:", [step["phase"] for step in final_state["history"]])

    coordinator = DefaultAgentCoordinator(
        planner=DemoPlanner(checkpoint), compiled_graph=graph, workflow_manager=workflow_manager
    )
    outcome = coordinator.coordinate(task)
    print("DefaultAgentCoordinator.coordinate ->", outcome)


def main() -> None:
    infra = DefaultInfrastructure(postgres_dsn=POSTGRES_DSN, redis_url=REDIS_URL)
    state = {"infra": infra}

    sections = [
        ("System Infrastructure (18) -- real Postgres + Redis", s_infrastructure),
        ("Security & Privacy (17) -- BoundaryGate", s_security),
        ("Reliability & Resilience (15) -- FailureClassifier + CircuitBreaker", s_reliability),
        ("Knowledge & Entity Model (04)", s_knowledge_entity),
        ("User & Portfolio (01)", s_user_portfolio),
        ("Data & Sources (02) -- real Alpha Vantage", s_data_sources),
        ("Data Processing & Quality (03)", s_data_processing),
        ("Event & Observation (07)", s_event_observation),
        ("Retrieval & Context (05) -- real Tavily corrective search", s_retrieval_context),
        ("Memory (06) -- incl. real fastembed semantic linking", s_memory),
        ("Analysis & Reasoning (08) -- real OpenRouter LLM calls", s_analysis_reasoning),
        ("Evidence & Verification (09)", s_evidence_verification),
        ("Decision & Policy (12)", s_decision_policy),
        ("Interaction & Notification (13) -- real Resend send attempt", s_interaction_notification),
        ("Learning & Evaluation (14) -- closes the loop back into Memory", s_learning_evaluation),
        ("Tools & Environment (11)", s_tools_environment),
        ("Agent Runtime (10) -- real LangGraph checkpoint loop via OpenRouter", s_agent_runtime),
    ]

    failures = []
    for title, fn in sections:
        section(title)
        try:
            fn(state)
        except Exception:
            failures.append(title)
            print(f"[SECTION FAILED -- continuing] {title}")
            traceback.print_exc()

    section("Demo complete")
    print(f"{len(sections) - len(failures)}/{len(sections)} sections completed without error.")
    if failures:
        print("Failed sections:", failures)
    print("See trace.log for the full component call trace, audit.log for every audited decision.")


if __name__ == "__main__":
    main()
