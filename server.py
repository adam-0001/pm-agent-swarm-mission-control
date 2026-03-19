"""
PM Agent Swarm - Mission Control Backend
Tornado server with WebSocket support for real-time agent orchestration.
Supports mock mode (realistic simulation), live mode (actual Anthropic API), and local mode (Ollama).

Usage:
    python server.py          # Mock mode (no API key needed)
    python server.py --live   # Live mode (requires ANTHROPIC_API_KEY in .env)
    python server.py --local  # Local mode (requires Ollama running on localhost:11434)

Then open http://localhost:8000 in your browser.

Author: Adam Ketefian | MKTG 454 Ad Hoc Project | UW Foster School of Business
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Any, List

import tornado.ioloop
import tornado.web
import tornado.websocket

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "1048576"))  # 1 MB default

# Load .env file for API keys
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # dotenv not installed, rely on shell environment

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("mission-control")


# ---------------------------------------------------------------------------
# Enums & Data Classes
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISING = "revising"
    COMPLETED = "completed"


AGENT_CONFIGS = {
    "discovery": {
        "agent_id": "discovery", "name": "Discovery Agent", "icon": "D",
        "role": "Customer Research Lead",
        "description": "Synthesizes customer interviews into structured pain-point reports, user segments, and automation opportunities.",
        "tools": ["Interview Analyzer", "Insight Synthesizer", "Segment Mapper"],
    },
    "competitive": {
        "agent_id": "competitive", "name": "Competitive Analyst", "icon": "C",
        "role": "Market Intelligence",
        "description": "Analyzes competitor products, pricing, and positioning. Produces SWOT matrices and feature gap analyses.",
        "tools": ["Market Scanner", "Feature Gap Analyzer", "Threat Detector"],
    },
    "roadmap": {
        "agent_id": "roadmap", "name": "Roadmap Agent", "icon": "R",
        "role": "Prioritization Expert",
        "description": "Applies RICE scoring to rank features, maps dependencies, and suggests quarterly roadmap groupings.",
        "tools": ["RICE Scorer", "Dependency Mapper", "Timeline Planner"],
    },
    "prd": {
        "agent_id": "prd", "name": "PRD Writer", "icon": "P",
        "role": "Documentation Specialist",
        "description": "Generates structured PRDs with user stories, acceptance criteria, success metrics, and traceability.",
        "tools": ["PRD Generator", "User Story Builder", "Acceptance Criteria"],
    },
    "validator": {
        "agent_id": "validator", "name": "Validator Agent", "icon": "V",
        "role": "Quality Assurance",
        "description": "Audits all agent outputs for factual accuracy, internal consistency, and hallucinations. Tags claims as VERIFIED, NEEDS REVIEW, or FLAG.",
        "tools": ["Fact Checker", "Consistency Auditor", "Hallucination Detector"],
    },
    "synthesis": {
        "agent_id": "synthesis", "name": "Synthesis", "icon": "S",
        "role": "Executive Summary",
        "description": "Combines all agent outputs into a cohesive strategic brief.",
        "tools": ["Report Generator"],
    },
}


# ---------------------------------------------------------------------------
# Agent System Prompts (used by LiveAgentExecutor and LocalAgentExecutor)
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPTS = {
    "discovery": """You are a Customer Discovery Agent for a product management team. Analyze the provided customer interviews, feedback, and research data.

Your output MUST be structured as markdown with these sections:
# Customer Discovery Insights
## Key Pain Points (Ranked by Frequency)
For each pain point: name, evidence quotes, confidence level (HIGH/MEDIUM/LOW)
## User Segments
A markdown table with columns: Segment | Representatives | Core Need
## Automation Opportunities
Numbered list of opportunities with brief descriptions
## Unmet Needs
Bullet list of needs not addressed by current tools

Be specific. Cite actual quotes from the provided data. Tag confidence levels.""",

    "competitive": """You are a Competitive Analysis Agent. Analyze the competitive landscape based on the provided context.

Your output MUST be structured as markdown with these sections:
# Competitive Landscape Analysis
## SWOT Matrix
A markdown table with Internal/External rows and Positive/Negative columns
## Feature Gap Analysis
A markdown table comparing capabilities across competitors with checkmarks/X marks
## Strategic Opportunities
Numbered list with bold titles and descriptions
## Competitive Threats [NEEDS REVIEW]
Bullet list of threats

Tag claims as [VERIFIED] (from provided data), [INFERRED] (analytical conclusion), or [NEEDS REVIEW].""",

    "roadmap": """You are a Roadmap Prioritization Agent. Based on the discovery insights and competitive analysis from previous agents, create a prioritized feature roadmap.

Your output MUST be structured as markdown with these sections:
# Prioritized Feature Roadmap
## RICE-Scored Feature Backlog
A markdown table with columns: Rank | Feature | Reach | Impact | Confidence | Effort | RICE Score
Score each 1-10, RICE = (Reach × Impact × Confidence) / Effort
## Quarterly Groupings
### Q1, Q2, Q3 sections with bullet lists
## Dependencies
Dependency chains using arrow notation

Flag any assumptions with [FLAG: ASSUMPTION].""",

    "prd": """You are a PRD Writing Agent. Based on the highest-priority feature from the roadmap, write a comprehensive Product Requirements Document.

Your output MUST be structured as markdown with these sections:
# Product Requirements Document
## Feature: [Name of highest-priority feature]
### Problem Statement
Reference discovery findings with [Traced to: Discovery Agent]
### Target User
Primary and secondary personas
### User Stories
US-1, US-2, US-3 format with Acceptance Criteria (AC-1, AC-2, etc.)
### Success Metrics
Markdown table with: Metric | Target | Measurement
### Technical Considerations
Bullet list
### Open Questions [NEEDS HUMAN REVIEW]
Numbered list
### Traceability
Table linking requirements to source findings""",

    "validator": """You are a Validation Agent. Audit ALL outputs from the previous agents (Discovery, Competitive, Roadmap, PRD) for accuracy and consistency.

Your output MUST be structured as markdown with these sections:
# Validation Report
## Pipeline Confidence Score: X / 10
## 1. Factual Accuracy Audit
For each agent's output, tag claims as:
- [VERIFIED] — directly supported by provided data
- [NEEDS REVIEW] — reasonable but not data-backed
- [FLAG: POSSIBLE HALLUCINATION] — stated as fact but not in source data
## 2. Internal Consistency Check
Bullet list checking cross-agent alignment
## 3. Items Requiring Human PM Review
Numbered list
## 4. Improvement Suggestions
Bullet list per agent
## Recommendation
APPROVED, APPROVED WITH NOTES, or NEEDS REVISION with confidence score""",

    "synthesis": """You are a Synthesis Agent. Combine ALL outputs from the previous agents into a single cohesive executive summary.

CRITICAL RULES:
- Produce a COMPLETE, SELF-CONTAINED document. Do NOT ask the user any questions.
- Do NOT end with "Do you want me to...", "Should I...", "Let me know if...", or any request for further input.
- Do NOT offer to generate additional documents. Your output IS the final deliverable.
- End with the Recommended Next Steps section, nothing else.

Your output MUST be structured as markdown:
# Executive Summary
## Strategic Brief
One-paragraph overview of the product opportunity.
## Key Findings
Top 3-5 bullet points from discovery and competitive analysis.
## Recommended Roadmap
Brief summary of the prioritized features from the roadmap agent.
## First Feature: [Name]
Summary of the PRD for the top-priority feature.
## Confidence & Risk Assessment
Key findings from the validator.
## Recommended Next Steps
Numbered list of 3-5 immediate actions for the PM team.

This should read as a single, cohesive strategic document — not a collection of separate reports. Do not include any conversational text or follow-up questions.""",
}


# ---------------------------------------------------------------------------
# Mock Agent Outputs (realistic PM content for demo mode)
# ---------------------------------------------------------------------------

MOCK_OUTPUTS = {
    "discovery": """# Customer Discovery Insights

## Key Pain Points (Ranked by Frequency)

### 1. Cross-Tool Status Fragmentation (5/5 interviews)
**Evidence:** Every interviewee described manually aggregating status across 2-4 tools.
- Sarah: "I spend 2 hours every Monday manually creating a status update by pulling from all three [tools]."
- Marcus: "I have to go through Slack threads, Jira boards, and meeting notes to piece together what happened."
**Confidence: HIGH** — Universal pain point across company sizes.

### 2. Signal Buried in Noise (3/5 interviews)
**Evidence:** Critical information gets lost in communication channels.
- Priya: "The signal was there — our sales team had flagged it in Slack three months earlier, but it got buried."
- James: "Most of the patterns are obvious but I still have to do the work."
**Confidence: HIGH** — Strong evidence from senior PMs.

### 3. Manual Data Synthesis (4/5 interviews)
**Evidence:** PMs spend significant time on data collection vs. decision-making.
- Aisha: "I'd love it if the data collection part was automated...so I can focus on the actual decision-making."
- James: "I manually tag and categorize about 200 tickets a month."
**Confidence: HIGH** — Clear automation opportunity.

## User Segments
| Segment | Representatives | Core Need |
|---------|----------------|-----------|
| Startup PM (IC) | Sarah, James | Reduce manual overhead with limited resources |
| Mid-Market PM | Marcus | Better stakeholder communication tools |
| Enterprise PM Leader | Priya, Aisha | Strategic signal detection across large orgs |

## Automation Opportunities
1. **Automated Status Aggregation** — Pull from Jira/Asana/Slack, generate weekly update
2. **Signal Detection** — Monitor channels for competitive/customer signals, surface proactively
3. **Support Ticket Synthesis** — Auto-categorize and surface top pain points weekly
4. **Competitive Alert System** — Track competitor moves, flag relevant changes

## Unmet Needs
- No tool currently offers a unified "PM command center" across all work tools
- PMs want AI to handle data collection but retain decision authority
- Need for proactive alerting, not just reactive reporting""",

    "competitive": """# Competitive Landscape Analysis

## SWOT Matrix — Unified PM Command Center

| | Positive | Negative |
|---|---------|----------|
| **Internal** | **Strengths:** Novel unified approach, AI-native architecture, PM-specific workflows | **Weaknesses:** No existing user base, unproven at scale, limited integrations at launch |
| **External** | **Opportunities:** No competitor offers true cross-tool synthesis, PMs actively seeking consolidation | **Threats:** Asana/Notion adding AI features rapidly, potential platform lock-in resistance |

## Feature Gap Analysis
| Capability | Asana | Linear | Notion | Monday.com | Our Opportunity |
|-----------|-------|--------|--------|-----------|----------------|
| Cross-tool status aggregation | ❌ | ❌ | ❌ | ❌ | **UNIQUE** [VERIFIED] |
| AI-powered summarization | ✅ (2025) | ✅ (basic) | ✅ | ✅ (acquired) | Table stakes [VERIFIED] |
| Competitive monitoring | ❌ | ❌ | ❌ | ❌ | **UNIQUE** [INFERRED] |
| Support ticket synthesis | ❌ | ❌ | ❌ | ❌ | **UNIQUE** [INFERRED] |
| Engineering workflow | ❌ (weak) | ✅ (strong) | ⚠️ | ❌ (weak) | Not our focus [VERIFIED] |

## Strategic Opportunities
1. **Blue Ocean: PM Command Center** — No competitor offers unified cross-tool intelligence
2. **Competitive Moat: Integration Depth** — Deep bidirectional integrations (not just read)
3. **Wedge Market: Startup PMs** — Highest pain, fastest adoption, viral potential

## Competitive Threats [NEEDS REVIEW]
- Asana Intelligence expanding rapidly — could add cross-tool features
- Notion's flexibility means users may build DIY solutions
- Linear's developer love could expand into PM workflows

**Source Verification:** Claims tagged [VERIFIED] are directly from provided competitor data. Claims tagged [INFERRED] are analytical conclusions drawn by this agent.""",

    "roadmap": """# Prioritized Feature Roadmap

## RICE-Scored Feature Backlog

| Rank | Feature | Reach | Impact | Confidence | Effort | RICE Score |
|------|---------|-------|--------|------------|--------|------------|
| 1 | Automated Status Aggregation | 9 | 9 | 9 | 4 | **182** |
| 2 | Stakeholder Update Generator | 8 | 6 | 7 | 3 | **112** |
| 3 | Integration SDK (Jira/Asana/Slack) | 10 | 8 | 9 | 9 | **80** |
| 4 | Competitive Alert System | 7 | 8 | 7 | 5 | **78** |
| 5 | Support Ticket Synthesis | 8 | 7 | 8 | 6 | **75** |
| 6 | Signal Detection Engine | 6 | 9 | 6 | 7 | **46** |
| 7 | Enterprise SSO & Compliance | 4 | 6 | 8 | 5 | **38** |
| 8 | PRD Auto-Generator | 5 | 7 | 5 | 8 | **22** |

⚠️ **[FLAG: ASSUMPTION]** Effort scores are estimates. Actual engineering assessment required.

## Quarterly Groupings

### Q1 — Quick Wins & Foundation
- **Integration SDK** (RICE: 80) — Required foundation for all other features
- **Automated Status Aggregation** (RICE: 182) — Highest impact, depends on integrations
- **Stakeholder Update Generator** (RICE: 112) — Quick win, builds on status data

### Q2 — Core Intelligence
- **Support Ticket Synthesis** (RICE: 75) — Builds on integration layer
- **Competitive Alert System** (RICE: 78) — Independent module, high strategic value

### Q3 — Advanced & Enterprise
- **Signal Detection Engine** (RICE: 46) — Complex ML, needs data from Q1-Q2
- **Enterprise SSO & Compliance** (RICE: 38) — Gates enterprise sales
- **PRD Auto-Generator** (RICE: 22) — Builds on all prior data

## Dependencies
- Integration SDK → Status Aggregation → Update Generator → Signal Detection
- Integration SDK → Ticket Synthesis (parallel with above)
- Competitive Alert System (independent)
- Enterprise SSO (independent, gates enterprise deals)""",

    "prd": """# Product Requirements Document
## Feature: Automated Status Aggregation

### Problem Statement
Product Managers spend 1.5–2 hours per week manually collecting status updates from multiple tools (Jira, Asana, Slack, spreadsheets) to create stakeholder reports. This is the #1 pain point identified across all 5 customer interviews, affecting PMs at every company size.
[Traced to: Discovery Agent — Pain Point #1]

### Target User
**Primary:** Product Managers (IC level) at startups and mid-market companies who manage cross-functional teams using 2+ project management tools.
**Secondary:** Engineering Managers who need to report team status to leadership.

### User Stories

**US-1:** As a PM, I want to connect my Jira, Asana, and Slack accounts so that status data flows into one dashboard automatically.
- **AC-1:** User can authenticate with OAuth to Jira, Asana, and Slack within 3 clicks each
- **AC-2:** Initial sync completes within 60 seconds for projects with <1,000 items
- **AC-3:** Ongoing sync occurs every 15 minutes (configurable)

**US-2:** As a PM, I want to see a unified weekly status summary so that I don't have to manually aggregate updates.
- **AC-1:** Summary auto-generates every Monday at 9am (configurable)
- **AC-2:** Summary includes: completed items, in-progress items, blocked items, key metrics
- **AC-3:** Summary is editable before sharing

**US-3:** As a PM, I want to share the status update with stakeholders via email or Slack so that I can distribute it without copy-pasting.
- **AC-1:** One-click share to configured Slack channel
- **AC-2:** Email distribution list configurable in settings
- **AC-3:** Shared update includes read receipts

### Success Metrics
| Metric | Target | Measurement |
|--------|--------|-------------|
| Time saved per PM per week | >1 hour | Self-reported survey + usage analytics |
| Weekly active usage | 70% of connected users | Product analytics |
| Status update accuracy | >90% user satisfaction | In-app feedback |
| Integration success rate | >95% | Error monitoring |

### Technical Considerations
- **Integration Architecture:** OAuth 2.0 for all third-party connections
- **Data Storage:** User data encrypted at rest (AES-256), in transit (TLS 1.3)
- **Rate Limiting:** Respect API rate limits for Jira (429 handling), Asana, Slack
- **Scalability:** Must handle PMs with up to 50 projects across 3 tools

### Open Questions [NEEDS HUMAN REVIEW]
1. Should we support Notion and Monday.com in v1, or defer to v2?
2. What's the right default cadence — weekly or daily summaries?
3. How do we handle conflicting status between tools?
4. Privacy: Can we aggregate status across private Slack channels?

### Traceability
| Requirement | Source |
|-------------|--------|
| Cross-tool aggregation | Discovery: Pain Point #1 (5/5 interviews) |
| Stakeholder distribution | Discovery: Pain Point #1 (Marcus interview) |
| Proactive generation | Discovery: Unmet Need — "PM command center" |
| Integration breadth | Competitive Analysis: Blue Ocean opportunity |""",

    "validator": """# Validation Report

## Pipeline Confidence Score: 8.4 / 10

## 1. Factual Accuracy Audit

### Discovery Insights
- [VERIFIED] Cross-tool fragmentation — directly supported by 5/5 interview quotes ✅
- [VERIFIED] Manual synthesis time estimates — directly from interview data ✅
- [VERIFIED] User segment definitions — grounded in interview demographics ✅
- [NEEDS REVIEW] "No unified PM command center" — broad claim, may be tools we haven't analyzed ⚠️

### Competitive Analysis
- [VERIFIED] Asana Intelligence launch in 2025 — from provided competitor data ✅
- [VERIFIED] Linear's engineering focus — from provided data ✅
- [NEEDS REVIEW] "Notion DIY solutions" — reasonable inference but not data-backed ⚠️
- [FLAG: POSSIBLE HALLUCINATION] Monday.com AI acquisition — stated as fact but not in source data ❌

### Roadmap
- [VERIFIED] RICE scoring methodology correctly applied ✅
- [NEEDS REVIEW] Effort estimates are agent-generated, not validated by engineering ⚠️
- [VERIFIED] Dependency mapping is logically consistent ✅

### PRD
- [VERIFIED] Problem statement traces to discovery findings ✅
- [VERIFIED] User stories are testable and well-formed ✅
- [VERIFIED] Success metrics are measurable ✅
- [NEEDS REVIEW] "60 second sync for <1,000 items" — needs engineering validation ⚠️

## 2. Internal Consistency Check
- ✅ PRD problem statement aligns with Discovery pain point #1
- ✅ Roadmap feature #1 matches PRD feature
- ✅ Competitive gaps support strategic differentiation claims
- ⚠️ Roadmap ranking inconsistency: Stakeholder Update Generator (RICE 112) ranks #2 but Competitive Alert (RICE 78) is #4 — **ranking appears correct after re-sort**

## 3. Items Requiring Human PM Review
1. Monday.com AI acquisition claim — verify before including
2. Effort estimates on all RICE scores — get engineering input
3. PRD sync performance targets — validate with engineering
4. Notion DIY solution inference — broader market scan needed

## 4. Improvement Suggestions
- Discovery: Add quantitative data (survey) to complement qualitative interviews
- Competitive: Expand to include emerging startups, not just established players
- Roadmap: Include risk scores alongside RICE scores
- PRD: Add wireframe descriptions for user stories

## Recommendation: APPROVED WITH NOTES
Proceed to human review with the flagged items above. Overall confidence: **8.4/10**.""",

    "synthesis": """# Executive Summary

## Strategic Brief
The PM tooling market presents a significant opportunity to build a unified command center that solves the #1 pain point affecting PMs across all company sizes: cross-tool status fragmentation. Unlike competitors (Asana, Notion, Linear, Monday.com) who operate as single-tool platforms with bolt-on AI features, our AI-native approach offers true cross-tool intelligence and synthesis. The addressable market includes 150K+ PMs at companies using 2+ project tools, with a clear beachhead in startup and mid-market segments.

## Key Findings
- **5/5 customer interviews** identified manual status aggregation as consuming 1.5-2 hours per PM per week
- **Zero competitor** offers cross-tool aggregation, competitive monitoring, or unified signal detection
- **Market timing is favorable:** PMs are actively seeking consolidation solutions and frustrated with tool sprawl
- **High-confidence user segments:** Startup PMs (pain point urgency) and Enterprise PM leaders (budget authority)
- **Clear technical differentiation:** Deep integrations and AI-powered synthesis create a defensible moat

## Recommended Roadmap
**Q1:** Build the foundation (Integration SDK + Automated Status Aggregation + Update Generator) to capture the highest-pain-point users immediately. **Q2:** Add competitive monitoring and support ticket synthesis to expand value proposition. **Q3:** Deploy advanced signal detection and enterprise compliance features to unlock enterprise segment.

The top-priority feature (Automated Status Aggregation with RICE score 182) directly addresses the #1 customer pain point and can drive early adoption and viral growth.

## First Feature: Automated Status Aggregation

Automated Status Aggregation allows PMs to connect their Jira, Asana, and Slack accounts and receive a unified, AI-generated weekly status summary automatically. The feature includes one-click sharing to stakeholders via email or Slack, eliminating the 1.5-2 hours of manual synthesis work.

**User Stories:**
- Connect and sync status data from multiple tools automatically
- Receive AI-generated weekly summaries without manual aggregation
- Share status updates with one click to configured channels

**Success Metrics:**
- 70% weekly active usage among connected users
- >1 hour time saved per PM per week
- >90% user satisfaction on summary accuracy
- >95% integration success rate

## Confidence & Risk Assessment
**Pipeline Confidence: 8.4/10.** All major outputs (Discovery, Competitive, Roadmap, PRD) are grounded in customer research with strong internal consistency.

**Flagged Items for PM Review:**
1. Monday.com AI acquisition claim requires verification
2. Engineering team must validate PRD performance targets (60-second sync)
3. Effort estimates on RICE scores need engineering input
4. Broader competitive scan recommended for emerging startups

**Overall Assessment:** Proceed to development with high confidence. Address flagged items during engineering planning phase.

## Recommended Next Steps
1. **Conduct technical feasibility review** on integration depth and sync performance targets with engineering
2. **Refine customer interviews** by running a brief survey (10-15 PMs) to validate persona priorities and feature preferences
3. **Begin SDK development** for Jira/Asana/Slack integration layer — this gates all downstream features
4. **Schedule founder/investor conversations** to lock in Series A thesis and go-to-market strategy
5. **Document competitive research** as a shared playbook so sales and marketing can reference positioning in early conversations""",
}

MOCK_PLAN = [
    {"agent": "discovery", "focus": "Analyze interview transcripts for recurring pain points, user segments, and automation opportunities"},
    {"agent": "competitive", "focus": "Build SWOT matrix and feature gap analysis across Asana, Linear, Notion, Monday.com"},
    {"agent": "roadmap", "focus": "Apply RICE scoring framework and create quarterly feature groupings"},
    {"agent": "prd", "focus": "Draft PRD for highest-priority feature with user stories and acceptance criteria"},
    {"agent": "validator", "focus": "Audit all outputs for factual accuracy, internal consistency, and hallucinations"},
]


# ---------------------------------------------------------------------------
# Mock Agent Executor
# ---------------------------------------------------------------------------

class MockAgentExecutor:
    """Streams realistic agent outputs with thinking steps and metadata."""

    def __init__(self, broadcast_fn):
        self.broadcast = broadcast_fn

    # Agent-specific thinking steps and metadata
    AGENT_THINKING = {
        "discovery": {
            "steps": [
                "Analyzing 5 interview transcripts from diverse PM roles...",
                "Identifying recurring pain points across segments and company sizes...",
                "Cross-referencing interview quotes with product usage data...",
                "Mapping user segments by seniority and company stage...",
            ],
            "tools": ["Interview Analyzer", "Insight Synthesizer"],
            "sources": ["5 PM interview transcripts", "Product usage analytics"],
        },
        "competitive": {
            "steps": [
                "Scanning market landscape for PM-focused tools and platforms...",
                "Building feature comparison matrix across Asana, Linear, Notion, Monday.com...",
                "Analyzing competitive positioning and SWOT dynamics...",
                "Identifying blue ocean opportunities and market gaps...",
            ],
            "tools": ["Market Scanner", "Feature Gap Analyzer"],
            "sources": ["Competitor product pages", "G2/Capterra reviews", "Industry reports"],
        },
        "roadmap": {
            "steps": [
                "Applying RICE scoring framework to feature backlog...",
                "Computing reach × impact × confidence ÷ effort for each feature...",
                "Mapping feature dependencies and sequencing...",
                "Grouping features into quarterly delivery timeline...",
            ],
            "tools": ["RICE Scorer", "Dependency Mapper", "Timeline Planner"],
            "sources": ["Discovery insights", "Competitive analysis", "Engineering estimates"],
        },
        "prd": {
            "steps": [
                "Selecting highest-priority feature from prioritized roadmap...",
                "Structuring user stories with INVEST criteria (Independent, Negotiable, Valuable, Estimable, Small, Testable)...",
                "Defining measurable acceptance criteria for each user story...",
                "Creating traceability matrix linking back to customer needs...",
            ],
            "tools": ["PRD Generator", "User Story Builder", "Acceptance Criteria Engine"],
            "sources": ["Discovery findings", "Roadmap prioritization", "Product vision"],
        },
        "validator": {
            "steps": [
                "Cross-referencing claims against source interview data and competitive research...",
                "Checking internal consistency across all agent outputs...",
                "Tagging assertions as VERIFIED, NEEDS REVIEW, or FLAG...",
                "Computing overall confidence score based on evidence quality...",
            ],
            "tools": ["Fact Checker", "Consistency Auditor", "Hallucination Detector"],
            "sources": ["All pipeline outputs", "Source interviews", "Competitive data"],
        },
        "synthesis": {
            "steps": [
                "Reviewing all prior agent outputs for integration...",
                "Synthesizing key findings across Discovery, Competitive, Roadmap, and PRD...",
                "Structuring executive summary with strategic brief, findings, and recommendations...",
                "Validating coherence and actionability of final document...",
            ],
            "tools": ["Report Generator", "Document Synthesizer"],
            "sources": ["Discovery insights", "Competitive analysis", "Roadmap", "PRD", "Validation report"],
        },
    }

    async def run_agent(self, agent_id: str, output_callback, rejection_feedback=None, context=None, prior_outputs=None):
        # Broadcast agent detail metadata
        thinking_meta = self.AGENT_THINKING.get(agent_id, {})
        await self.broadcast({
            "type": "agent_detail",
            "agent_id": agent_id,
            "tools_active": thinking_meta.get("tools", []),
            "context_sources": thinking_meta.get("sources", []),
            "model": "claude-opus-4-6"
        })

        # Broadcast thinking steps
        thinking_steps = thinking_meta.get("steps", [])
        for i, step in enumerate(thinking_steps):
            await self.broadcast({
                "type": "agent_thinking",
                "agent_id": agent_id,
                "step": step,
                "step_index": i,
                "total_steps": len(thinking_steps)
            })
            await asyncio.sleep(0.8)

        # Broadcast thinking complete
        await self.broadcast({
            "type": "thinking_complete",
            "agent_id": agent_id
        })

        # Thinking delay before main output
        await asyncio.sleep(2.0 if rejection_feedback is None else 1.2)

        base = MOCK_OUTPUTS.get(agent_id, "")
        if rejection_feedback:
            full = f"[REVISION — Feedback applied: {rejection_feedback}]\n\n{base}"
        else:
            full = base

        # Stream word-by-word (faster than char-by-char, still looks live)
        words = full.split(" ")
        for i, word in enumerate(words):
            token = word if i == 0 else " " + word
            await output_callback(token)
            # Small pauses at sentence ends for readability
            if any(word.endswith(p) for p in (".", "!", "?", "\n")):
                await asyncio.sleep(0.04)
            else:
                await asyncio.sleep(0.018)

        await asyncio.sleep(0.5)  # post-processing


# ---------------------------------------------------------------------------
# Live Agent Executor (uses Anthropic API)
# ---------------------------------------------------------------------------

class LiveAgentExecutor:
    """Streams real agent outputs using Anthropic API with Claude Sonnet."""

    def __init__(self, broadcast_fn):
        self.broadcast = broadcast_fn
        if AsyncAnthropic is None:
            logger.error("anthropic SDK not installed. Install with: pip install anthropic")
            self.client = None
        else:
            self.client = AsyncAnthropic()

    async def run_agent(self, agent_id: str, output_callback, rejection_feedback=None, context=None, prior_outputs=None):
        if self.client is None:
            logger.error("Anthropic client not available, falling back to mock mode")
            await self.broadcast({"type": "error", "message": "Anthropic SDK not installed"})
            return

        try:
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

            # Broadcast agent detail metadata
            await self.broadcast({
                "type": "agent_detail",
                "agent_id": agent_id,
                "tools_active": ["API Streaming", "Claude Sonnet"],
                "context_sources": [],
                "model": model
            })

            # Broadcast single thinking step
            await self.broadcast({
                "type": "agent_thinking",
                "agent_id": agent_id,
                "step": f"Processing with {model}...",
                "step_index": 0,
                "total_steps": 1
            })

            await asyncio.sleep(0.5)

            await self.broadcast({
                "type": "thinking_complete",
                "agent_id": agent_id
            })

            await asyncio.sleep(1.0)

            # Build the user message from context and prior outputs
            user_message = self._build_user_message(agent_id, context, prior_outputs, rejection_feedback)

            # Get system prompt for this agent
            system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_id, "You are a helpful assistant.")

            # Stream from Claude
            async with self.client.messages.stream(
                model=model,
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                async for text in stream.text_stream:
                    await output_callback(text)

        except Exception as e:
            logger.exception(f"Error in LiveAgentExecutor for {agent_id}")
            await self.broadcast({
                "type": "error",
                "agent_id": agent_id,
                "message": f"API error: {str(e)}"
            })

    @staticmethod
    def _run_context_header(context: dict) -> str:
        """Build a run context header with date/time and product name."""
        now = datetime.now()
        header = f"RUN CONTEXT:\n  Date: {now.strftime('%B %d, %Y')}\n  Time: {now.strftime('%I:%M %p')}\n"
        product_name = ""
        if context and context.get("product_description"):
            first_line = context["product_description"].strip().split("\n")[0][:120]
            product_name = first_line
        if product_name:
            header += f"  Product: {product_name}\n"
        return header

    def _build_user_message(self, agent_id: str, context: dict, prior_outputs: dict, rejection_feedback: str) -> str:
        """Build the user message from context and prior agent outputs."""
        if context is None:
            context = {}
        if prior_outputs is None:
            prior_outputs = {}

        parts = []

        # Add run context (date, time, product)
        parts.append(self._run_context_header(context))

        # Add rejection feedback if revising
        if rejection_feedback:
            parts.append(f"REVISION FEEDBACK: {rejection_feedback}\n")

        # Add product context
        if context.get("product_description"):
            parts.append(f"PRODUCT DESCRIPTION:\n{context['product_description']}\n")

        # Add interview data
        if context.get("interview_data"):
            parts.append(f"INTERVIEW DATA:\n{context['interview_data']}\n")

        # Add competitive notes
        if context.get("competitive_notes"):
            parts.append(f"COMPETITIVE NOTES:\n{context['competitive_notes']}\n")

        # Add additional context
        if context.get("additional_context"):
            parts.append(f"ADDITIONAL CONTEXT:\n{context['additional_context']}\n")

        # Add prior agent outputs (so later agents see earlier agents' work)
        if prior_outputs:
            parts.append("PRIOR AGENT OUTPUTS:\n")
            for prev_agent_id, output in prior_outputs.items():
                parts.append(f"--- {prev_agent_id.upper()} ---\n{output}\n")

        if len(parts) <= 1:  # only the run context header
            parts.append("Analyze the provided product and market context.")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Local Agent Executor (uses OpenAI SDK with Ollama)
# ---------------------------------------------------------------------------

class LocalAgentExecutor:
    """Streams agent outputs using OpenAI SDK to talk to Ollama's OpenAI-compatible API."""

    def __init__(self, broadcast_fn):
        self.broadcast = broadcast_fn
        if AsyncOpenAI is None:
            logger.error("openai SDK not installed. Install with: pip install openai")
            self.client = None
        else:
            self.client = AsyncOpenAI(
                base_url="http://localhost:11434/v1",
                api_key="ollama"
            )

    async def run_agent(self, agent_id: str, output_callback, rejection_feedback=None, context=None, prior_outputs=None):
        if self.client is None:
            logger.error("OpenAI client not available, falling back to mock mode")
            await self.broadcast({"type": "error", "message": "OpenAI SDK not installed"})
            return

        try:
            # Broadcast agent detail metadata
            model = os.environ.get("LOCAL_MODEL", "llama3.1")
            await self.broadcast({
                "type": "agent_detail",
                "agent_id": agent_id,
                "tools_active": ["Ollama API", model],
                "context_sources": [],
                "model": model
            })

            # Broadcast single thinking step
            await self.broadcast({
                "type": "agent_thinking",
                "agent_id": agent_id,
                "step": f"Processing with {model}...",
                "step_index": 0,
                "total_steps": 1
            })

            await asyncio.sleep(0.5)

            await self.broadcast({
                "type": "thinking_complete",
                "agent_id": agent_id
            })

            await asyncio.sleep(1.0)

            # Build the user message from context and prior outputs
            user_message = self._build_user_message(agent_id, context, prior_outputs, rejection_feedback)

            # Get system prompt for this agent
            system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_id, "You are a helpful assistant.")

            # Stream from Ollama (OpenAI-compatible format uses messages array for system prompt)
            stream = await self.client.chat.completions.create(
                model=os.environ.get("LOCAL_MODEL", "llama3.1"),
                max_tokens=8192,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                stream=True,
            )

            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    await output_callback(text)

        except Exception as e:
            logger.exception(f"Error in LocalAgentExecutor for {agent_id}")
            await self.broadcast({
                "type": "error",
                "agent_id": agent_id,
                "message": f"Ollama API error: {str(e)}"
            })

    @staticmethod
    def _run_context_header(context: dict) -> str:
        """Build a run context header with date/time and product name."""
        now = datetime.now()
        header = f"RUN CONTEXT:\n  Date: {now.strftime('%B %d, %Y')}\n  Time: {now.strftime('%I:%M %p')}\n"
        product_name = ""
        if context and context.get("product_description"):
            first_line = context["product_description"].strip().split("\n")[0][:120]
            product_name = first_line
        if product_name:
            header += f"  Product: {product_name}\n"
        return header

    def _build_user_message(self, agent_id: str, context: dict, prior_outputs: dict, rejection_feedback: str) -> str:
        """Build the user message from context and prior agent outputs."""
        if context is None:
            context = {}
        if prior_outputs is None:
            prior_outputs = {}

        parts = []

        # Add run context (date, time, product)
        parts.append(self._run_context_header(context))

        # Add rejection feedback if revising
        if rejection_feedback:
            parts.append(f"REVISION FEEDBACK: {rejection_feedback}\n")

        # Add product context
        if context.get("product_description"):
            parts.append(f"PRODUCT DESCRIPTION:\n{context['product_description']}\n")

        # Add interview data
        if context.get("interview_data"):
            parts.append(f"INTERVIEW DATA:\n{context['interview_data']}\n")

        # Add competitive notes
        if context.get("competitive_notes"):
            parts.append(f"COMPETITIVE NOTES:\n{context['competitive_notes']}\n")

        # Add additional context
        if context.get("additional_context"):
            parts.append(f"ADDITIONAL CONTEXT:\n{context['additional_context']}\n")

        # Add prior agent outputs (so later agents see earlier agents' work)
        if prior_outputs:
            parts.append("PRIOR AGENT OUTPUTS:\n")
            for prev_agent_id, output in prior_outputs.items():
                parts.append(f"--- {prev_agent_id.upper()} ---\n{output}\n")

        if len(parts) <= 1:  # only the run context header
            parts.append("Analyze the provided product and market context.")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pipeline Orchestrator
# ---------------------------------------------------------------------------

class PipelineOrchestrator:
    """Orchestrates the 6-agent pipeline with HITL gates, per-agent approval, and synthesis."""

    def __init__(self, broadcast_fn, context: dict = None, mode: str = "mock"):
        self.broadcast = broadcast_fn
        self.is_running = False
        self.agent_outputs: Dict[str, str] = {}
        self.context = context or {}
        self.mode = mode
        self.metrics = {"duration": 0, "active_agents": 0, "gates_passed": 0,
                        "gates_total": 3, "revisions": 0, "validator_score": 0.0}

        # Choose executor based on mode
        if mode == "live" and AsyncAnthropic is not None:
            self.executor = LiveAgentExecutor(broadcast_fn)
        elif mode == "local" and AsyncOpenAI is not None:
            self.executor = LocalAgentExecutor(broadcast_fn)
        else:
            self.executor = MockAgentExecutor(broadcast_fn)

        # HITL gate synchronisation
        self._hitl_future: Optional[asyncio.Future] = None

    # -- public API --

    async def execute_pipeline(self):
        try:
            self.is_running = True
            t0 = time.time()

            await self._log("system", "Pipeline started — orchestrating 6 agents", "system")

            # Orchestrator Planning Step
            await self._broadcast_orchestrator_plan()

            # ── Stage 1: Parallel fan-out ──
            await self._set_status("discovery", AgentStatus.RUNNING)
            await self._set_status("competitive", AgentStatus.RUNNING)
            self.metrics["active_agents"] = 2
            await self._send_metrics(t0)
            await self._log("discovery", "Started customer interview analysis", "start")
            await self._log("competitive", "Started competitive landscape scan", "start")
            await self._log("system", "Parallel fan-out: 2 agents running simultaneously", "parallel")

            await asyncio.gather(
                self._run_agent("discovery"),
                self._run_agent("competitive"),
            )

            self.metrics["active_agents"] = 0
            await self._send_metrics(t0)
            await self._log("system", "Research phase complete — 2 agents finished", "complete")

            # HITL Gate 1 — per-agent approval for discovery + competitive
            approved = await self._hitl_gate("research",
                "Research Phase",
                "Review each agent's output individually.",
                t0,
                agent_ids=["discovery", "competitive"])
            if not approved:
                return

            # ── Stage 2: Roadmap ──
            await self._set_status("roadmap", AgentStatus.RUNNING)
            self.metrics["active_agents"] = 1
            await self._send_metrics(t0)
            await self._log("roadmap", "Computing RICE scores for features…", "start")
            await self._run_agent("roadmap")
            self.metrics["active_agents"] = 0
            await self._send_metrics(t0)
            await self._log("roadmap", "Roadmap prioritisation complete", "complete")

            # HITL Gate 2 — single-agent format for roadmap
            approved = await self._hitl_gate("prioritization",
                "Prioritization Phase",
                "Validate RICE scores, quarterly groupings, and dependency mapping before PRD generation.",
                t0,
                agent_ids=["roadmap"])
            if not approved:
                return

            # ── Stage 3: PRD Writer ──
            await self._set_status("prd", AgentStatus.RUNNING)
            self.metrics["active_agents"] = 1
            await self._send_metrics(t0)
            await self._log("prd", "Generating PRD for top-priority feature…", "start")
            await self._run_agent("prd")
            self.metrics["active_agents"] = 0
            await self._send_metrics(t0)
            await self._log("prd", "PRD with user stories and acceptance criteria generated", "complete")

            # HITL Gate 3 — single-agent format for prd
            approved = await self._hitl_gate("requirements",
                "Requirements Phase",
                "Review the PRD for completeness, clarity, and alignment with customer needs before final validation.",
                t0,
                agent_ids=["prd"])
            if not approved:
                return

            # ── Stage 4: Validator ──
            await self._set_status("validator", AgentStatus.RUNNING)
            self.metrics["active_agents"] = 1
            await self._send_metrics(t0)
            await self._log("validator", "Auditing all outputs for accuracy and consistency…", "start")
            await self._run_agent("validator")
            self.metrics["active_agents"] = 0
            # Parse confidence score from validator output
            validator_output = self.agent_outputs.get("validator", "")
            score = self._extract_validator_score(validator_output)
            self.metrics["validator_score"] = score
            await self._send_metrics(t0)
            await self._log("validator", f"Validation complete — Confidence {score}/10", "complete")

            # ── Stage 5: Synthesis (no additional HITL gate) ──
            await self._set_status("synthesis", AgentStatus.RUNNING)
            self.metrics["active_agents"] = 1
            await self._send_metrics(t0)
            await self._log("synthesis", "Synthesizing all outputs into executive summary…", "start")
            await self._run_agent("synthesis")
            self.metrics["active_agents"] = 0
            await self._send_metrics(t0)
            await self._log("synthesis", "Executive summary complete", "complete")

            # Pipeline Summary
            await self.broadcast({
                "type": "pipeline_summary",
                "total_duration": int(time.time() - t0),
                "agents_completed": 6,
                "checkpoints_passed": 3,
                "revisions": self.metrics["revisions"],
                "validator_score": self.metrics["validator_score"],
                "flagged_items": 4,
                "next_steps": [
                    "Review and edit the executive summary",
                    "Share the synthesis report with stakeholders",
                    "Schedule follow-up interviews for flagged items",
                    "Begin engineering estimation for Q1 features"
                ]
            })

            # Done
            await self.broadcast({"type": "pipeline_complete", "success": True,
                                  "duration": int(time.time() - t0)})
            await self._log("system",
                "Pipeline complete — all phases succeeded. Ready for development.",
                "success")

        except Exception as e:
            logger.exception("Pipeline error")
            await self.broadcast({"type": "pipeline_complete", "success": False,
                                  "error": str(e)})
        finally:
            self.is_running = False

    async def handle_hitl_response(self, data: dict):
        if self._hitl_future and not self._hitl_future.done():
            self._hitl_future.set_result(data)

    # -- private helpers --

    async def _broadcast_orchestrator_plan(self):
        """Broadcast the orchestrator's plan for the pipeline."""
        if self.mode == "mock":
            # Use hardcoded plan for mock mode
            plan = MOCK_PLAN
        else:
            # In live/local mode, would call LLM to generate plan, but for simplicity we use hardcoded
            plan = MOCK_PLAN

        await self.broadcast({
            "type": "orchestrator_plan",
            "plan": plan
        })

    async def _run_agent(self, agent_id: str, feedback: str = None):
        self.agent_outputs[agent_id] = ""

        async def on_token(text: str):
            self.agent_outputs[agent_id] += text
            await self.broadcast({"type": "agent_output", "agent_id": agent_id,
                                  "text": text, "append": True})

        await self.executor.run_agent(
            agent_id, on_token, feedback,
            context=self.context,
            prior_outputs=self.agent_outputs
        )
        await self._set_status(agent_id, AgentStatus.COMPLETED)

    async def _hitl_gate(self, gate_id: str, stage_name: str, description: str,
                         t0: float, agent_ids: List[str] = None, _revision_count: int = 0) -> bool:
        """Pause pipeline and wait for human approval. Supports per-agent decisions. Returns True if all agents approved."""
        MAX_REVISIONS = 2

        if agent_ids is None:
            agent_ids = []

        # Tell the front-end we're waiting
        await self.broadcast({"type": "hitl_gate", "gate_id": gate_id,
                              "stage_name": stage_name, "description": description,
                              "agent_ids": agent_ids})
        await self._log("system", f"Checkpoint: {stage_name} — awaiting PM approval", "gate")

        # Wait for the front-end to respond
        loop = asyncio.get_event_loop()
        self._hitl_future = loop.create_future()
        response = await self._hitl_future
        self._hitl_future = None

        # Check if all agents are approved
        decisions = response.get("decisions", {})
        all_approved = all(decisions.get(aid, {}).get("approved", False) for aid in agent_ids)

        if all_approved:
            self.metrics["gates_passed"] += 1
            await self._send_metrics(t0)
            await self._log("system", f"Human PM approved {stage_name}", "approval")
            return True

        # ── Rejection → revision loop ──
        # Find which agents were rejected and re-run them
        agents_to_revise = []
        for agent_id in agent_ids:
            agent_decision = decisions.get(agent_id, {})
            if not agent_decision.get("approved", False):
                agents_to_revise.append(agent_id)

        feedback_map = {}
        for agent_id in agents_to_revise:
            agent_decision = decisions.get(agent_id, {})
            feedback = agent_decision.get("feedback", "Please revise.")
            feedback_map[agent_id] = feedback

        self.metrics["revisions"] += 1
        await self._send_metrics(t0)

        if _revision_count >= MAX_REVISIONS:
            await self._log("system",
                f"Max revisions ({MAX_REVISIONS}) reached for {stage_name}. Pipeline paused.",
                "rejection")
            self.is_running = False
            return False

        # Re-run rejected agents with feedback
        for agent_id in agents_to_revise:
            feedback = feedback_map[agent_id]
            await self._set_status(agent_id, AgentStatus.REVISING)
            await self._log(agent_id,
                f"Revising based on feedback: \"{feedback}\"", "revision")
            self.agent_outputs[agent_id] = ""
            # Clear old output on frontend
            await self.broadcast({"type": "agent_output", "agent_id": agent_id,
                                  "text": "", "append": False})
            await self._run_agent(agent_id, feedback)
            await self._log(agent_id, "Revision complete", "complete")

        # Re-present the gate
        return await self._hitl_gate(gate_id, stage_name, description,
                                     t0, agent_ids, _revision_count + 1)

    async def _set_status(self, agent_id: str, status: AgentStatus):
        await self.broadcast({"type": "agent_status", "agent_id": agent_id,
                              "status": status.value})

    async def _send_metrics(self, t0: float):
        self.metrics["duration"] = int(time.time() - t0)
        await self.broadcast({"type": "metrics", **self.metrics})

    @staticmethod
    def _extract_validator_score(output: str) -> float:
        """Extract the confidence score from validator output. Looks for patterns like '8.4/10' or 'Score: 8.4'."""
        import re
        # Try patterns like "8.4 / 10", "8.4/10", "Score: 8.4"
        patterns = [
            r'(\d+\.?\d*)\s*/\s*10',        # "8.4 / 10" or "8.4/10"
            r'[Ss]core[:\s]+(\d+\.?\d*)',    # "Score: 8.4"
            r'[Cc]onfidence[:\s]+(\d+\.?\d*)', # "Confidence: 8.4"
        ]
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                try:
                    score = float(match.group(1))
                    if 0 <= score <= 10:
                        return round(score, 1)
                except ValueError:
                    pass
        return 0.0  # No score found

    async def _log(self, agent_id: str, message: str, event_type: str = "system"):
        cfg = AGENT_CONFIGS.get(agent_id, {})
        icon = cfg.get("icon", "S") if agent_id != "system" else "S"
        await self.broadcast({
            "type": "log",
            "agent_id": agent_id,
            "icon": icon,
            "agent_name": cfg.get("name", "System"),
            "message": message,
            "event_type": event_type,
        })


# ---------------------------------------------------------------------------
# Tornado Handlers
# ---------------------------------------------------------------------------

# Global state
clients: set = set()
orchestrator: Optional[PipelineOrchestrator] = None
live_mode: str = "mock"  # Set from CLI args in __main__ ("mock", "live", or "local")


async def broadcast_to_all(message: dict):
    """Send a JSON message to every connected WebSocket client."""
    payload = json.dumps(message)
    dead = []
    for ws in clients:
        try:
            ws.write_message(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


class WSHandler(tornado.websocket.WebSocketHandler):
    """WebSocket endpoint for real-time dashboard communication."""

    def check_origin(self, origin):
        return True  # allow local dev

    def open(self):
        clients.add(self)
        logger.info(f"WS connected  ({len(clients)} total)")
        self.write_message(json.dumps({
            "type": "connection", "status": "connected",
            "agents": AGENT_CONFIGS,
        }))

    def on_close(self):
        clients.discard(self)
        logger.info(f"WS disconnected  ({len(clients)} total)")

    async def on_message(self, raw):
        global orchestrator
        msg = json.loads(raw)
        kind = msg.get("type")

        if kind == "start_pipeline":
            # Always create a fresh orchestrator for new pipeline runs
            # This ensures no stale state from previous runs
            if orchestrator and orchestrator.is_running:
                await self.write_message(json.dumps({
                    "type": "error",
                    "message": "Pipeline is already running. Wait for completion or reset."
                }))
                return
            # Extract context from message payload
            context = msg.get("context", {})
            orchestrator = PipelineOrchestrator(broadcast_to_all, context=context, mode=live_mode)
            asyncio.ensure_future(orchestrator.execute_pipeline())

        elif kind == "reset_pipeline":
            # Force-reset pipeline state, clearing any stalled orchestrator
            if orchestrator:
                orchestrator.is_running = False
            orchestrator = None
            await broadcast_to_all({
                "type": "pipeline_reset",
                "message": "Pipeline state reset. Ready for new run."
            })
            logger.info("Pipeline state reset by client request")

        elif kind == "hitl_response":
            if orchestrator:
                await orchestrator.handle_hitl_response(msg)

        elif kind == "agent_message":
            logger.info(f"Agent msg → {msg.get('agent_id')}: {msg.get('message')}")


class IndexHandler(tornado.web.RequestHandler):
    """Serve the Mission Control dashboard."""

    def get(self):
        html_path = Path(__file__).parent / "static" / "index.html"
        if html_path.exists():
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.write(html_path.read_text(encoding="utf-8"))
        else:
            self.set_status(404)
            self.write("index.html not found - create static/index.html")


class HealthHandler(tornado.web.RequestHandler):
    def get(self):
        self.write({"status": "ok", "ts": datetime.now().isoformat()})


class UploadHandler(tornado.web.RequestHandler):
    """Handle file uploads and extract text content."""

    def post(self):
        if "file" not in self.request.files:
            self.set_status(400)
            self.write({"error": "No file uploaded"})
            return

        uploaded = self.request.files["file"][0]
        filename = uploaded["filename"].lower()
        body = uploaded["body"]

        if len(body) > MAX_UPLOAD_BYTES:
            self.set_status(413)
            self.write({
                "error": f"File too large. Max allowed is {MAX_UPLOAD_BYTES} bytes.",
                "max_upload_bytes": MAX_UPLOAD_BYTES,
            })
            return

        try:
            # Extract text based on file type
            if filename.endswith((".txt", ".md", ".csv", ".tsv")):
                text = body.decode("utf-8", errors="replace")
            elif filename.endswith(".json"):
                text = body.decode("utf-8", errors="replace")
            else:
                # For unknown types, try to decode as text
                text = body.decode("utf-8", errors="replace")

            self.write({
                "filename": uploaded["filename"],
                "text": text,
                "size": len(body)
            })
            logger.info(f"File uploaded: {uploaded['filename']} ({len(body)} bytes)")

        except Exception as e:
            logger.exception(f"Error processing upload: {uploaded['filename']}")
            self.set_status(500)
            self.write({"error": f"Failed to process file: {str(e)}"})


SESSION_FILE = Path(__file__).parent / "session_state.json"


class SessionHandler(tornado.web.RequestHandler):
    """Save and restore pipeline session state."""

    def get(self):
        """Restore last session state."""
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text())
                self.write(data)
            except Exception:
                self.set_status(404)
                self.write({"error": "No saved session"})
        else:
            self.set_status(404)
            self.write({"error": "No saved session"})

    def post(self):
        """Save current session state."""
        try:
            body = json.loads(self.request.body)
            SESSION_FILE.write_text(json.dumps(body, indent=2))
            self.write({"status": "saved"})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

    def delete(self):
        """Clear saved session."""
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        self.write({"status": "cleared"})


class ExportHandler(tornado.web.RequestHandler):
    """Export all agent outputs from the current/last pipeline run as JSON."""

    def get(self):
        global orchestrator
        if orchestrator and orchestrator.agent_outputs:
            self.write({
                "agents": orchestrator.agent_outputs,
                "timestamp": datetime.now().isoformat(),
                "duration": orchestrator.metrics.get("duration", 0)
            })
        else:
            self.set_status(404)
            self.write({"error": "No pipeline outputs available"})


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------

def make_app():
    static_dir = str(Path(__file__).parent / "static")
    return tornado.web.Application([
        (r"/", IndexHandler),
        (r"/ws", WSHandler),
        (r"/health", HealthHandler),
        (r"/upload", UploadHandler),
        (r"/session", SessionHandler),
        (r"/export", ExportHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": static_dir}),
    ])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))

    # Set live_mode flag based on CLI argument
    if "--live" in sys.argv:
        if AsyncAnthropic is None:
            logger.error("anthropic SDK not installed. Install with: pip install anthropic")
            sys.exit(1)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.error("ANTHROPIC_API_KEY not set. Add it to .env file or export it.")
            sys.exit(1)
        globals()['live_mode'] = "live"
        logger.info("Starting in LIVE mode (Anthropic API with claude-sonnet-4-6)")
    elif "--local" in sys.argv:
        if AsyncOpenAI is None:
            logger.error("openai SDK not installed. Install with: pip install openai")
            sys.exit(1)
        model = os.environ.get("LOCAL_MODEL", "llama3.1")
        globals()['live_mode'] = "local"
        logger.info(f"Starting in LOCAL mode (Ollama at localhost:11434, model: {model})")
    else:
        globals()['live_mode'] = "mock"
        logger.info("Starting in MOCK mode (realistic simulation, no API key needed)")

    app = make_app()
    app.listen(port)
    logger.info(f"Mission Control running at http://localhost:{port}")
    tornado.ioloop.IOLoop.current().start()
