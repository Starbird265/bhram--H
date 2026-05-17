# Organizational Intelligence Engine: 6-Layer Architecture

## Executive Summary
The Organizational Intelligence Engine is a 6-layer automated pipeline that transforms unstructured organizational data (Slack, Notion, local files) into high-fidelity AI agent instructions (`SKILL.md`). Inspired by OpenAI's organizational brain pattern and Anthropic's SKILL.md standard, the engine is designed to adapt, learn from operational failures, and continuously update agent directives without model retraining.

---

## The 6-Layer Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    6-LAYER PIPELINE                         │
│                                                             │
│  ┌─────────────┐  Raw data from Slack, Notion, files       │
│  │  LAYER 1    │  Pull channel history, wiki pages,        │
│  │  INGESTION  │  local .md/.json files                    │
│  └──────┬──────┘                                           │
│         │                                                   │
│  ┌──────▼──────┐  Strip HTML, Slack artifacts,             │
│  │  LAYER 2    │  conversational noise, emoji,             │
│  │  NORMALIZE  │  pleasantries → clean text                │
│  └──────┬──────┘                                           │
│         │                                                   │
│  ┌──────▼──────┐  Recursive text splitting with            │
│  │  LAYER 3    │  overlap windows + source position        │
│  │  CHUNKING   │  metadata tracking                       │
│  └──────┬──────┘                                           │
│         │                                                   │
│  ┌──────▼──────┐  AI batch processing (5 chunks/call):     │
│  │  LAYER 4    │  Noise filter → Signal extract →          │
│  │  DISTILL    │  Type classify → Confidence score         │
│  └──────┬──────┘                                           │
│         │                                                   │
│  ┌──────▼──────┐  Similarity scoring (title + content +    │
│  │  LAYER 5    │  tags + dept/type), bounded AI dedup,     │
│  │  DEDUP      │  ADD / UPDATE / DISCARD decisions         │
│  └──────┬──────┘                                           │
│         │                                                   │
│  ┌──────▼──────┐  Skill assembly, MCP tool provisioning,   │
│  │  LAYER 6    │  SKILL.md generation, agent routing,      │
│  │  SYNTHESIS  │  hot-reload notification                  │
│  └─────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Ingestion (`ingestion/`)
- **Slack Connector** (`slack_connector.py`): Pulls channel histories via Slack API
- **Notion Connector** (`notion_connector.py`): Fetches wiki pages via Notion API
- **Local File Parser** (`parsers.py`): Scans directories for `.md` and `.json` files
- Falls back to mock data when API keys are not configured

## Layer 2: Normalization (`pipeline/normalize.py`)
5-stage cleaning pipeline:
1. HTML tag stripping
2. Slack artifact normalization (`<@U123>` → `[user]`, `<#C123|name>` → `#name`)
3. Conversational noise removal (10+ patterns: pleasantries, emoji reactions, off-topic chat)
4. Markdown simplification (bold/italic removal, link text extraction)
5. Whitespace normalization

**Key feature**: Preserves lines with operational signal keywords even if they start with noise.

## Layer 3: Chunking (`pipeline/chunking.py`)
- Recursive text splitting with separator hierarchy: `# → ## → ### → ¶ → sentence → word`
- Configurable `max_tokens` (default: 1000) and `overlap_tokens` (default: 100)
- Source position tracking (file path, section header, Slack message indices)
- Specialized Slack message grouper that preserves message boundaries

## Layer 4: Distillation (`pipeline/distill.py`)
- **Batch processing**: 5 chunks per AI call (not 1-by-1)
- OpenAI structured outputs with `DistillationBatch` schema
- 12-type knowledge ontology: SOP, DECISION, POLICY, FAILURE_PATTERN, EDGE_CASE, APPROVAL_FLOW, TOOL_WORKFLOW, CUSTOMER_CONTEXT, ESCALATION, GLOSSARY, PREFERENCE, SECURITY_RULE
- Auto-department classification from content keywords
- Confidence scoring: 0.0-1.0 based on definitiveness
- Mock fallback: Rule-based extraction with keyword matching

## Layer 5: Deduplication (`pipeline/deduplicate.py`)
- Pre-filters by multi-signal similarity scoring (title 30%, content 50%, tags 10%, dept/type 10%)
- Only sends top-5 candidates to AI (not entire DB)
- Auto-discards near-exact matches (>0.9 similarity)
- Batch evaluation with self-duplication prevention
- Heuristic fallback: Content length comparison for UPDATE decisions

## Layer 6: Synthesis (`smart_layer/`, `generators/`)
- **Skill Assembly**: Confidence-gated (≥0.7), categorized by knowledge type
- **AI Synthesis**: When available, AI rewrites chunks into coherent skill documents
- **Tool Provisioning**: Maps skills to MCP servers (GitHub, Vercel, Notion, Slack, etc.)
- **SKILL.md Generation**: Valid YAML frontmatter + structured markdown
- **Agent Routing**: Auto-determines target agent by department and skill content
- **Hot Reload**: Webhook notification to agents when skills are updated

---

## The Failure-Memory Loop

1. An engineer notes an issue: *"IMPORTANT: Do not restart auth-service without notifying #sec-ops first."*
2. The Engine ingests this and classifies it as a `FAILURE_PATTERN`
3. The chunk is deduplicated and saved to the canonical database
4. The SkillAssembler recompiles the affected skill
5. The failure pattern is injected into the **Edge Cases & What NOT to do** section
6. Next time an agent runs, it reads the updated `SKILL.md` and avoids the mistake

The `/api/feedback` endpoint enables agents to inject corrections directly.

---

## Setup

### Prerequisites
Create a `.env` file (or set environment variables):
```bash
OPENAI_API_KEY=your_openai_api_key_here      # Required for AI processing
SLACK_BOT_TOKEN=your_slack_bot_token_here      # Optional: for live Slack
NOTION_API_KEY=your_notion_api_key_here        # Optional: for live Notion
```

### Running
```bash
# Install dependencies
pip install -r backend/requirements.txt

# Run the 6-layer pipeline (CLI)
cd backend/src && python main.py

# Run the web dashboard
cd backend/src && uvicorn api:app --reload --port 8000
```

### API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | System health + connection status |
| GET | `/api/config` | Current configuration |
| POST | `/api/config` | Update configuration |
| POST | `/api/run` | Execute 6-layer pipeline |
| GET | `/api/logs` | Pipeline execution logs |
| GET | `/api/database/stats` | Chunk/skill counts + breakdowns |
| GET | `/api/skills` | List all generated skills |
| GET | `/api/chunks` | List all canonical chunks |
| POST | `/api/agents/assign` | Bind skill to agent |
| POST | `/api/webhooks/ingest` | Real-time data push |
| POST | `/api/feedback` | Agent failure feedback |
| POST | `/api/assemble` | Dynamic runtime skill assembly |
| GET | `/api/health` | Simple health check |
