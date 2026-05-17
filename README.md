# bhram--H — Organizational Intelligence Engine

A 6-layer automated pipeline that ingests unstructured company data from Slack, Notion, and local files, distills it into actionable knowledge chunks, and compiles them into agent-ready `SKILL.md` documents. Built with FastAPI, OpenAI, and the Model Context Protocol (MCP).

---

## What It Does

The system solves a specific problem: operational knowledge lives scattered across Slack threads, Notion wikis, local markdown files, and JSON configs. Engineers share deployment rules in chat, marketing documents brand guidelines in wikis, and critical edge cases get buried in message threads.

This engine pulls all of that, strips the noise, classifies the signal, deduplicates against what it already knows, and compiles the result into structured skill files that AI agents can consume directly.

---

## Architecture

The pipeline runs 6 layers sequentially. Each layer transforms the data and passes it forward.

```
Raw Data (Slack, Notion, Files)
        │
   ┌────▼────┐
   │ INGEST  │  Pull channel histories, wiki pages, local .md/.json
   └────┬────┘
        │
   ┌────▼────────┐
   │ NORMALIZE   │  Strip HTML, Slack artifacts, emoji, pleasantries
   └────┬────────┘
        │
   ┌────▼────┐
   │ CHUNK   │  Recursive text splitting with overlap + position metadata
   └────┬────┘
        │
   ┌────▼────────┐
   │ DISTILL     │  AI batch processing: noise filter → signal extract → classify
   └────┬────────┘
        │
   ┌────▼────┐
   │ DEDUP   │  Similarity scoring, bounded AI comparison, ADD/UPDATE/DISCARD
   └────┬────┘
        │
   ┌────▼──────────┐
   │ SYNTHESIZE    │  Compile SKILL.md, provision MCP tools, route to agents
   └───────────────┘
```

---

## Project Structure

```
bhrm--H/
├── backend/
│   ├── src/
│   │   ├── main.py                     # 6-layer orchestrator (CLI entry point)
│   │   ├── api.py                      # FastAPI server (web dashboard + REST API)
│   │   ├── mcp_server.py               # MCP server for AI agent integration
│   │   ├── core/
│   │   │   └── models.py               # Pydantic models (KnowledgeChunk, SkillDef, enums)
│   │   ├── ingestion/
│   │   │   ├── slack_connector.py       # Slack API channel history puller
│   │   │   ├── notion_connector.py      # Notion API page fetcher
│   │   │   └── parsers.py              # Local .md/.json file scanner
│   │   ├── pipeline/
│   │   │   ├── normalize.py            # 5-stage text cleaning (HTML, Slack, noise, markdown, whitespace)
│   │   │   ├── chunking.py             # Recursive semantic splitter with overlap windows
│   │   │   ├── distill.py              # AI batch distillation (5 chunks/call, 12-type ontology)
│   │   │   └── deduplicate.py          # Multi-signal similarity scoring + AI dedup
│   │   ├── smart_layer/
│   │   │   ├── assembler.py            # Confidence-gated skill assembly
│   │   │   ├── classifier.py           # Department + type auto-classification
│   │   │   ├── tool_provisioner.py     # MCP server mapping (GitHub, Vercel, Notion, Slack)
│   │   │   └── agent_connector.py      # Agent binding + hot-reload triggers
│   │   ├── generators/
│   │   │   └── skill_generator.py      # YAML frontmatter + markdown SKILL.md writer
│   │   └── storage/
│   │       ├── filesystem_store.py     # JSON-file-based chunk persistence
│   │       └── skill_registry.py       # Skill ↔ chunk mapping registry
│   ├── raw_data/                       # Sample data (engineering, marketing)
│   ├── database/                       # Generated canonical chunk store (gitignored)
│   ├── knowledge_base/                 # Generated SKILL.md files (gitignored)
│   ├── requirements.txt
│   └── claude_desktop_config.json      # MCP server config for Claude Desktop
├── frontend/
│   ├── index.html                      # Cortex AI dashboard
│   ├── styles.css                      # Glassmorphism dark-mode UI
│   └── app.js                          # Dashboard logic (config, pipeline, agents)
├── ORGANIZATIONAL_INTELLIGENCE_ENGINE.md   # Detailed architecture documentation
└── .gitignore
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend API | FastAPI + Uvicorn |
| AI Processing | OpenAI GPT-4o-mini (structured outputs) |
| Agent Protocol | Model Context Protocol (MCP) via `fastmcp` |
| Data Models | Pydantic v2 |
| Ingestion | Slack SDK, Notion Client, local file parsers |
| Frontend | Vanilla HTML/CSS/JS (glassmorphism dark theme) |
| Storage | JSON files on disk (filesystem store) |

---

## Knowledge Types

The distillation layer classifies every chunk into one of 12 types:

`SOP` · `DECISION` · `POLICY` · `FAILURE_PATTERN` · `EDGE_CASE` · `APPROVAL_FLOW` · `TOOL_WORKFLOW` · `CUSTOMER_CONTEXT` · `ESCALATION` · `GLOSSARY` · `PREFERENCE` · `SECURITY_RULE`

Departments: `engineering` · `marketing` · `sales` · `ops` · `shared`

---

## Setup

### Prerequisites

- Python 3.10+
- An OpenAI API key (for AI-powered distillation; falls back to rule-based extraction without one)

### Install

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in `backend/` or export directly:

```bash
OPENAI_API_KEY=sk-...          # Required for AI distillation (Layer 4)
SLACK_BOT_TOKEN=xoxb-...       # Optional — for live Slack ingestion
NOTION_API_KEY=ntn_...         # Optional — for live Notion ingestion
```

Without API keys, the connectors fall back to mock data, so the pipeline still runs end-to-end.

---

## Usage

### Run the Pipeline (CLI)

```bash
cd backend/src
python main.py
```

This runs all 6 layers in sequence: ingest → normalize → chunk → distill → dedup → synthesize. Output is printed to the terminal with per-layer stats.

### Run the Web Dashboard

```bash
cd backend/src
uvicorn api:app --reload --port 8000
```

Then open `http://localhost:8000`. The dashboard provides:
- Configuration management (data sources, API keys, Slack channel)
- One-click pipeline execution with live terminal output
- Knowledge base stats (chunk counts, department breakdowns)
- Agent provisioning (bind skills to agents, trigger hot-reload)

### Run the MCP Server

For local integration with Claude Desktop or other MCP-compatible agents:

```bash
cd backend/src
python mcp_server.py --transport stdio
```

For network access:

```bash
python mcp_server.py --transport sse --port 8001
```

**Claude Desktop config** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "bhrm-intelligence": {
      "command": "python3",
      "args": ["backend/src/mcp_server.py"]
    }
  }
}
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | System health + connection status |
| `GET` | `/api/config` | Current configuration |
| `POST` | `/api/config` | Update data sources, API keys |
| `POST` | `/api/run` | Execute full 6-layer pipeline (background) |
| `GET` | `/api/logs` | Pipeline execution logs |
| `GET` | `/api/database/stats` | Chunk/skill counts + breakdowns |
| `GET` | `/api/skills` | List all generated skills |
| `GET` | `/api/chunks` | List all canonical chunks (filterable by department) |
| `POST` | `/api/agents/assign` | Bind a skill to an agent |
| `POST` | `/api/webhooks/ingest` | Real-time data push (Slack/Notion events) |
| `POST` | `/api/feedback` | Agent failure feedback (injects into Failure Memory Loop) |
| `POST` | `/api/assemble` | Dynamic runtime skill assembly |
| `GET` | `/api/health` | Simple health check |

---

## MCP Tools

When connected via MCP, agents have access to these tools:

| Tool | Purpose |
|------|---------|
| `search_knowledge` | Keyword search across all chunks (filterable by department, type, confidence) |
| `get_department_rules` | Get all rules for a specific department |
| `get_skill` | Retrieve a compiled SKILL.md by name |
| `list_skills` | List all available skills |
| `list_departments` | List departments with chunk counts |
| `report_failure` | Inject failure feedback into the pipeline |
| `run_pipeline` | Trigger a full pipeline run |
| `get_pipeline_status` | Check pipeline status + view logs |
| `get_agent_bindings` | Show which skills are bound to which agents |

---

## The Failure Memory Loop

This is the core feedback mechanism:

1. An engineer notes: *"Do not restart auth-service without notifying #sec-ops first."*
2. The engine ingests this, classifies it as `FAILURE_PATTERN`
3. The chunk is deduplicated and saved to the canonical store
4. The `SkillAssembler` recompiles the affected department's skill
5. The failure pattern appears in the "Edge Cases & What NOT to do" section
6. Next time an agent reads the skill, it avoids the mistake

Agents can also inject corrections directly via the `/api/feedback` endpoint or the `report_failure` MCP tool.

---

## License

Proprietary. All rights reserved.

© 2026 Gaurav Singh
