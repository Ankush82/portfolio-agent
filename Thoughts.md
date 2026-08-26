This is the Capabalities abstraction and the components for the Agentic System
User & Portfolio
Capabilities
Onboard User
Connect Portfolio
Read Portfolio
Track Portfolio State
Analyze Portfolio Exposure
The abstraction is:
Portfolio is the user's current financial state.

2. Source System
News, company reports, filings, presentations, earnings releases, management announcements, etc. all become Sources.
Capabilities
Register Source
Discover Sources
Ingest Source
Update Source
Validate Source
Retrieve Source
So:
Source
├── News
├── Filing
├── Report
├── Presentation
├── Earnings Release
├── Management Announcement
└── Market Data
These aren't separate capabilities anymore.

3. Document & Data Processing
Anything coming from a source needs to become usable information.
Capabilities
Parse
Normalize
Extract
Transform
Structure
For example:
PDF
↓
Parse
↓
Extract
↓
Structured Financial Information

4. Entity & Knowledge Model
This gives the system a common vocabulary.
Capabilities
Resolve Entity
Link Entities
Represent Relationship
Update Knowledge
For example:
TCS
↓
Company
↓
IT Sector
↓
Tata Group
↓
Customers / Competitors / Suppliers
This is where company, stock, person, sector, index, etc. become entities, rather than separate concepts scattered across the system.

5. Event System
This is another major abstraction.
Everything that happens becomes an Event.
Event
├── Earnings
├── Acquisition
├── Management Change
├── Regulatory Action
├── Guidance Change
├── Product Launch
├── Macro Event
└── Market Movement
Capabilities
Detect Event
Classify Event
Link Event to Entities
Update Event
Retrieve Event
That's enough.

6. Observation System
This is slightly different from Events.
An Observation is something the system has observed.
For example:
Revenue = ₹10,000 Cr
Stock price = ₹1,450
Margin = 21%
Capabilities
Observe
Compare Observations
Detect Change
Detect Anomaly
This abstraction becomes extremely useful because news, market data, financial metrics, and portfolio changes can all produce observations.

7. Context & Memory
Instead of separating short-term, long-term, episodic, semantic and procedural memory as different components, I would initially abstract them as Memory.
Capabilities
Store Memory
Retrieve Memory
Update Memory
Search Memory
Consolidate Memory
Then internally you can have different memory types.
Memory
├── Episodic
├── Semantic
├── Procedural
└── User-specific
But the system-level abstraction is simply Memory.

8. Analysis System
This is where observations, events, entities and memory are turned into interpretations.
Capabilities
Analyze
Compare
Infer
Estimate Impact
Generate Hypothesis
For example:
Event
+
Company Context
+
Historical Memory
+
Market Context
       ↓
     Analyze
       ↓
Impact Hypothesis

9. Evidence System
This should be a distinct abstraction because your financial system needs grounded reasoning.
Capabilities
Collect Evidence
Link Evidence
Evaluate Evidence
Verify Claim
Resolve Evidence Conflict
Everything ultimately becomes:
Claim
  ↓
Evidence
  ↓
Verification

10. Decision / Recommendation System
I would not call this "Trading" yet.
The system takes analysis and determines what is worth surfacing to the user.
Capabilities
Assess Significance
Rank Significance
Assess Confidence
Determine Actionability
Generate Recommendation
For example:
Analysis
  ↓
Significance
  ↓
Confidence
  ↓
Actionability
  ↓
Recommendation

11. Agent Runtime
This is the generic machinery that allows agents to operate.
Capabilities
Plan
Execute
Observe
Manage State
Delegate
Coordinate
Recover
This is where agentic behavior actually lives.

12. Tool System
Again, don't create separate capabilities for every API.
Abstract tools as:
Tool
├── Market API
├── News API
├── Broker API
├── Search
├── Database
└── Document Parser
Capabilities
Discover Tool
Select Tool
Execute Tool
Validate Tool Result
Fallback Tool

13. Policy & Safety
Capabilities
Authorize
Enforce Policy
Assess Risk
Escalate
Require Approval
This component determines what the system is allowed to do.

14. Notification / Interaction
Capabilities
Generate Notification
Prioritize Notification
Deliver Notification
Collect Feedback

15. Evaluation & Learning
I'd combine these initially.
Capabilities
Evaluate
Replay
Measure Outcome
Collect Feedback
Update System Knowledge
The underlying abstractions:
Evaluation
Simulation
Feedback
Learning

16. Observability & Audit
Capabilities
Trace
Monitor
Record
Audit
