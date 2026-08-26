# Portfolio-Aware Financial Intelligence System
## Priority Literature Reviews

This document captures the three literature reviews prioritized for the system architecture.

The selection is based on the failure-mode map and the system trajectory:

**Source → Retrieval → Memory → Reasoning → Evidence → Decision → Outcome → Learning → Memory**

The three highest-priority research areas are:

1. Retrieval + Evidence-Grounded Reasoning
2. Agent Runtime Reliability + Evaluation
3. Memory for Long-Lived Agents

---

# 1. Retrieval + Evidence-Grounded Reasoning

## Core question

> How do we ensure the agent reasons from the right, sufficient, and verifiable information?

This is foundational because the system will continuously ingest financial information and must determine:

- What information should be retrieved?
- Is the retrieved information relevant?
- Is the context sufficient?
- Does the evidence support the generated claim?
- What should happen when evidence is insufficient or contradictory?

## Basic failure trajectory

```text
Information exists
      ↓
Retrieve information
      ↓
Construct context
      ↓
Generate claim
      ↓
Verify evidence
      ↓
Make decision
```

Failure can occur at every transition.

---

## 1.1 Standard RAG

The basic architecture is:

```text
Query
 ↓
Retriever
 ↓
Documents
 ↓
LLM
 ↓
Answer
```

The fundamental weakness is that retrieval errors can propagate directly into generation.

---

## 1.2 Self-RAG

Self-RAG introduces adaptive retrieval and self-reflection:

```text
Query
 ↓
Should I retrieve?
 ↓
Retrieve
 ↓
Generate
 ↓
Critique retrieved evidence
 ↓
Critique generation
 ↓
Final answer
```

The important architectural insight is:

> Retrieval should be a decision-making component rather than an unconditional preprocessing step.

### Paper

- [Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection](https://arxiv.org/abs/2310.11511)

---

## 1.3 Corrective RAG (CRAG)

CRAG addresses the problem of poor retrieval.

```text
Query
 ↓
Retriever
 ↓
Retrieval Evaluator
       │
       ├── Good → use documents
       │
       └── Bad → corrective retrieval
                       ↓
                   External search
                       ↓
                 Reconstruct context
```

The architectural insight is:

> "No useful evidence found" should be a legitimate system state.

### Paper

- [Corrective Retrieval Augmented Generation](https://arxiv.org/abs/2401.15884)

---

## 1.4 ALCE — Attributed Language Model Generation

ALCE evaluates generated answers along dimensions including:

- Answer correctness
- Citation quality
- Citation completeness

The important implication for this system is that generated claims should have explicit relationships to supporting evidence.

```text
Analysis
   ↓
Claims
   ↓
Evidence
   ↓
Claim ↔ Evidence verification
   ↓
Decision
```

### Paper

- [Enabling Large Language Models to Generate Text with Citations](https://aclanthology.org/2023.emnlp-main.398/)

---

## 1.5 RAGTruth

RAGTruth provides a dataset for studying hallucination in retrieval-augmented generation.

The important architectural insight is:

> RAG does not automatically eliminate hallucination or unsupported claims.

### Paper

- [RAGTruth: A Hallucination Corpus for Developing Trustworthy Retrieval-Augmented Language Models](https://aclanthology.org/2024.acl-long.585/)

---

## What the literature suggests

The emerging architecture is closer to:

```text
Retrieve
   ↓
Evaluate retrieval
   ↓
Correct / expand if necessary
   ↓
Construct context
   ↓
Generate
   ↓
Verify claims
   ↓
Evaluate citations / evidence
```

rather than simply:

```text
Retrieve → Generate
```

## Design questions for our architecture

- What constitutes sufficient evidence?
- Should retrieval be adaptive?
- When should the system perform additional retrieval?
- How should source independence be represented?
- How should contradictory evidence be represented?
- Should every material claim require evidence?
- How should evidence freshness affect confidence?
- What happens when the system cannot establish sufficient evidence?

---

# 2. Agent Runtime Reliability + Evaluation

## Core question

> How do we know an agent trajectory is reliable, safe, and improving?

Our system is not a simple input/output system.

A realistic trajectory is:

```text
Detect event
 ↓
Retrieve sources
 ↓
Resolve entities
 ↓
Retrieve memory
 ↓
Analyze
 ↓
Verify
 ↓
Assess portfolio
 ↓
Notify
 ↓
Observe outcome
 ↓
Evaluate
```

A locally correct step can still result in a globally incorrect trajectory.

---

## 2.1 AgentBench

AgentBench evaluates LLMs as agents across multiple interactive environments.

The important conceptual shift is:

```text
Prompt → Answer
```

toward:

```text
Task
 ↓
Trajectory
 ↓
Environment interaction
 ↓
Outcome
```

This is much closer to the evaluation problem for the portfolio intelligence system.

### Paper

- [AgentBench: Evaluating LLMs as Agents](https://arxiv.org/abs/2308.03688)

---

## 2.2 ReAct

ReAct combines reasoning and action:

```text
Reason
 ↓
Act
 ↓
Observe
 ↓
Reason
 ↓
Act
 ↓
Observe
```

Instead of requiring the model to construct the entire answer from its internal knowledge, the agent can acquire information from the environment during reasoning.

### Paper

- [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)

---

## 2.3 Reflexion

Reflexion introduces a feedback loop:

```text
Trajectory
 ↓
Outcome
 ↓
Feedback
 ↓
Reflection
 ↓
Memory
 ↓
Next trajectory
```

This is closely aligned with the system's intended learning loop:

```text
Prediction
 ↓
Market outcome
 ↓
Evaluation
 ↓
Learning
 ↓
Memory
 ↓
Future prediction
```

### Paper

- [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)

---

## 2.4 AgentDojo

AgentDojo evaluates agents operating on untrusted external information.

This is particularly relevant because financial agents will consume:

- News
- Reports
- Web pages
- Filings
- Documents

These sources are data, but they can also contain adversarial instructions.

A dangerous trajectory could be:

```text
Financial document
      ↓
Agent reads document
      ↓
Document contains malicious instruction
      ↓
Agent interprets data as instruction
      ↓
Tool call
```

### Paper

- [AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents](https://arxiv.org/abs/2406.13352)

---

## What the literature suggests

Evaluation should operate at multiple levels:

```text
Model
  ↓
Component
  ↓
Step
  ↓
Trajectory
  ↓
Outcome
  ↓
User impact
```

The evaluation layer should therefore not only ask:

> "Did the agent answer correctly?"

It should also ask:

- Did it select the correct tool?
- Did it retrieve the correct evidence?
- Did it follow the correct trajectory?
- Did it recover from failure?
- Did it remain within policy?
- How much did the trajectory cost?
- How long did it take?
- Was the final outcome correct?

## Design questions for our architecture

- What is the unit of evaluation?
- How do we evaluate long-running trajectories?
- How do we prevent future information leakage during historical replay?
- How do we evaluate intermediate decisions?
- How do we distinguish reasoning errors from tool errors?
- How do we evaluate safety separately from capability?
- How do we evaluate an agent when market outcomes are noisy?
- How do we measure whether a new version actually improved?

---

# 3. Memory for Long-Lived Agents

## Core question

> How should an agent retain, retrieve, update, and learn from experience without contaminating future reasoning?

This is especially important for this project because memory is intended to create a feedback loop:

```text
TCS event
 ↓
Analysis
 ↓
Market outcome
 ↓
Evaluation
 ↓
Memory
 ↓
Future TCS event
 ↓
Better analysis
```

This is more than retrieval.

Memory becomes part of the learning system.

---

## 3.1 MemGPT

MemGPT treats memory as a hierarchical resource.

Instead of putting everything into the context window:

```text
Working context
      ↕
External memory
```

The system actively manages what enters and leaves the working context.

The architectural insight is:

> Memory is an active resource-management mechanism, not simply a vector database.

### Paper

- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)

---

## 3.2 MemoryBank

MemoryBank studies mechanisms for long-term memory and memory updating over sustained interactions.

The important architectural question is not just how to store memories, but how memories are recalled and updated.

### Paper

- [MemoryBank: Enhancing Large Language Models with Long-Term Memory](https://ojs.aaai.org/index.php/AAAI/article/view/29946)

---

## 3.3 A-MEM

A-MEM treats memories as interconnected structures rather than isolated records.

A new memory can be:

```text
New memory
 ↓
Analyze
 ↓
Create structured representation
 ↓
Find related memories
 ↓
Create links
 ↓
Update memory network
```

This is particularly relevant for historical financial events.

Instead of:

```text
Event A
Event B
Event C
```

the system can represent relationships between:

```text
Event
Company
Sector
Metric
Historical reaction
Market regime
```

### Paper

- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110)

---

## 3.4 Reflexion

Reflexion also provides a useful memory pattern:

```text
Experience
 ↓
Feedback
 ↓
Reflection
 ↓
Episodic memory
 ↓
Future trajectory
```

### Paper

- [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)

---

## 3.5 Memory poisoning

Persistent memory introduces a new failure class:

```text
Bad analysis
 ↓
Memory
 ↓
Future analysis
 ↓
More bad analysis
 ↓
More memory
```

Recent work explicitly studies memory poisoning, where untrusted information gets written into persistent agent memory and later influences agent behavior.

### Paper

- [From Untrusted Input to Trusted Memory: A Systematic Study of Memory Poisoning Attacks in LLM Agents](https://arxiv.org/abs/2606.04329)

---

## What the literature suggests

Memory should probably look more like:

```text
Experience
 ↓
Evaluate
 ↓
Should this become memory?
 ↓
Structure
 ↓
Link to existing knowledge
 ↓
Track provenance
 ↓
Track confidence
 ↓
Track freshness
 ↓
Store
 ↓
Retrieve when relevant
 ↓
Update / invalidate when necessary
```

rather than:

```text
Store → Retrieve
```

## Design questions for our architecture

- What deserves to become memory?
- What should never become memory?
- What is user-specific versus globally shared?
- How should memory be structured?
- How should memories be linked?
- How should memory confidence be represented?
- How should stale memories be invalidated?
- How should incorrect memories be corrected?
- How should memory provenance be preserved?
- How do we prevent memory poisoning?
- How do we prevent historical context from causing overgeneralization?

---

# Comparative Architectural View

The three literature areas connect directly:

```text
                 ┌─────────────────────┐
                 │   RETRIEVAL         │
                 │                     │
                 │ Find relevant       │
                 │ information         │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │     MEMORY          │
                 │                     │
                 │ Retain experience   │
                 │ and knowledge       │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │   AGENT RUNTIME     │
                 │                     │
                 │ Plan → Act →        │
                 │ Observe → Evaluate  │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │     OUTCOME         │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │    EVALUATION       │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │       MEMORY        │
                 │                     │
                 │ Update knowledge    │
                 └─────────────────────┘
```

---

# The Three Reviews

| Review | Core question | Primary papers |
|---|---|---|
| **Retrieval + Evidence** | How do we ensure the agent reasons from the right, sufficient, verifiable information? | Self-RAG, CRAG, ALCE, RAGTruth |
| **Agent Reliability + Evaluation** | How do we know an agent trajectory is reliable, safe, and improving? | AgentBench, ReAct, Reflexion, AgentDojo |
| **Agent Memory** | How should an agent retain, retrieve, update, and learn from experience without contaminating future reasoning? | MemGPT, MemoryBank, A-MEM, Reflexion, Memory Poisoning |

---

# Architectural conclusions

### 1. Retrieval should be self-critical

```text
Retrieve
 ↓
Evaluate retrieval
 ↓
Correct / expand
 ↓
Construct context
 ↓
Generate
 ↓
Verify
```

### 2. Agents should be evaluated as trajectories

```text
Input
 ↓
Trajectory
 ↓
Tool interactions
 ↓
State transitions
 ↓
Intermediate decisions
 ↓
Outcome
```

### 3. Memory should be a controlled knowledge system

```text
Experience
 ↓
Evaluate
 ↓
Structure
 ↓
Link
 ↓
Track provenance / confidence
 ↓
Store
 ↓
Retrieve
 ↓
Update / invalidate
```

---

# Technology selection — deferred

The literature review should inform the architecture before technology is selected.

Do **not** yet assume:

- LangGraph
- Temporal
- Mem0
- Supermemory
- LangFuse
- Vector databases
- LLMOps platforms

The next stage is to translate the literature findings into the **low-level design of these three components**, and only then evaluate candidate technologies against the resulting requirements.
