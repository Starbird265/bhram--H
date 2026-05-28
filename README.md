# Cortex AI — Organizational Intelligence Engine (v2.11)

An 11-layer automated pipeline and hybrid desktop application that ingests unstructured company data from Slack, Notion, GitHub, and local files, distills it into atomic operational rules, resolves contradictions, and compiles them into agent-ready `SKILL.md` documents. 

Built with a **Tauri/Rust Desktop Control Plane**, a **7-Tier SQLite Memory Architecture**, a **FastAPI** backend, an **AI Provider Router** (OpenAI, Claude, Ollama), and the **Model Context Protocol (MCP)**.

---

## What It Does

The system solves a specific problem: operational knowledge lives scattered across Slack threads, Notion wikis, local markdown files, and JSON configs. Engineers share deployment rules in chat, marketing documents brand guidelines in wikis, and critical edge cases get buried in message threads.

This engine pulls all of that, strips the noise, classifies the signal, deduplicates against what it already knows, resolves conflicts, and compiles the result into structured skill files that AI agents can consume directly—without ever sending your sensitive data to public cloud providers.

---

## The 11-Layer Architecture

The pipeline runs 11 sequential layers, ensuring knowledge is clean, verified, and safe before any AI agent sees it.

```text
Raw Data (Slack, Notion, GitHub, Files)
        │
   ┌────▼────┐
   │ 1. INGESTION        │  Pull channel histories, wiki pages, local files, linear tickets
   └────┬────┘
        │
   ┌────▼────────────┐
   │ 2. NORMALIZATION    │  Strip HTML, Slack artifacts, emoji, and pleasantries
   └────┬────────────┘
        │
   ┌────▼────┐
   │ 3. CHUNKING         │  Line-aware semantic splitting with overlap + source metadata
   └────┬────┘
        │
   ┌────▼────────────┐
   │ 4. DISTILLATION     │  AI batch processing: filter noise → extract signal → classify
   └────┬────────────┘
        │
   ┌────▼────┐
   │ 5. DEDUPLICATION    │  Fast-pass hashing + semantic similarity scoring (ADD/UPDATE/DISCARD)
   └────┬────┘
        │
   ┌────▼────────────┐
   │ 7. PRIVACY SCAN     │  PII & Secret detection. Restricted data is redacted or kept local
   └────┬────────────┘
        │
   ┌────▼────────────┐
   │ 8. ATOMIC SEGMENT   │  Decompose claims into pure `AtomicKnowledgeUnits` + imperatives
   └────┬────────────┘
        │
   ┌────▼────────────┐
   │ 9. CONFLICT RESOLVE │  Flag contradictions against canonical memory (Auto-approve vs CONTESTED)
   └────┬────────────┘
        │
   ┌────▼────────────┐
   │ 6. SYNTHESIS        │  Compile approved atomic units into `SKILL.md` & map MCP tools
   └────┬────────────┘
        │
   ┌────▼────────────┐
   │ 10. MEMORY INDEXING │  Write to 7-tier SQLite architecture & update vector cosine index
   └────┬────────────┘
        │
   ┌────▼────────────┐
   │ 11. AGENT ORCHESTR. │  Context injection, tool gating, and agent hot-reloading
   └─────────────┘
```

*(Note: Layer 6 Synthesis executes after Conflict Resolution to guarantee only approved knowledge enters a skill).*

---

## Project Structure

```text
bhrm--H/
├── backend/
│   ├── src/
│   │   ├── main.py                     # 11-layer orchestrator (CLI entry point)
│   │   ├── api.py                      # FastAPI server (REST API)
│   │   ├── mcp_server.py               # MCP server for AI agent integration
│   │   ├── core/                       # Core models, ACL, credentials, and Search engine
│   │   ├── ingestion/                  # Connectors (Slack, Notion, GitHub, local files)
│   │   ├── pipeline/                   # Normalization, Chunking, Distillation, Dedup, Privacy Scan
│   │   ├── memory/                     # 7-Tier MemoryManager implementation
│   │   ├── providers/                  # AI Router (Claude, OpenAI, Ollama, Rule-Based)
│   │   ├── storage/                    # SQLite memory storage and legacy fallbacks
│   │   ├── smart_layer/                # Skill assembly, tool provisioning, validation
│   │   ├── agents/                     # Layer 11: Agent registry, delegator, context assembler
│   │   └── generators/                 # Markdown SKILL.md generators
│   ├── database/                       # Generated SQLite DB & vector indexes (gitignored)
│   ├── knowledge_base/                 # Generated SKILL.md & CONTEXT.md files (gitignored)
│   └── requirements.txt
├── src-tauri/                          # Rust control plane for the Cortex AI Desktop App
│   ├── src/
│   │   ├── main.rs
│   │   └── lib.rs                      # Desktop permissions & FastAPI sidecar lifecycle
│   └── tauri.conf.json
├── frontend/                           # HTML/CSS/JS frontend bundled by Tauri
├── ORGANIZATIONAL_INTELLIGENCE_ENGINE.md   # Detailed architecture documentation
├── HYBRID_LOCAL_AI_ARCHITECTURE.md         # Hybrid local AI implementation details
├── backend/docs/architecture/pipeline_architecture_and_stabilization.md # Core schemas and deep-dive blueprint
└── .gitignore
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Desktop App (Control Plane)** | Tauri v2 + Rust (Native OS Integration) |
| **Backend API** | FastAPI + Uvicorn |
| **AI Provider Routing** | ProviderRouter (supports OpenAI, Claude, Ollama, local rules) |
| **Agent Protocol** | Model Context Protocol (MCP) |
| **Memory / Storage** | 7-Tier SQLite Database (with local Vector Cosine Indexing) |
| **Data Models** | Pydantic v2 |
| **Ingestion** | OAuth / Token Connectors (Slack, Notion, GitHub, local files) |
| **Frontend UI** | Vanilla HTML/CSS/JS (Glassmorphism dark theme) |

---

## The 7-Tier Memory Architecture

The system tracks every piece of data through a robust SQLite-backed 7-tier memory architecture:
1. **Working Memory**: In-flight pipeline data awaiting validation.
2. **Source Memory**: Immutable records of file metrics and source hashes.
3. **Canonical Memory**: Approved `AtomicKnowledgeUnits` used for skills.
4. **Failure Memory**: Historical developer mistakes and anti-patterns.
5. **Vector Memory**: Local embeddings for instant semantic search.
6. **Skill Memory**: Compiled SKILL.md documents and agent allocations.
7. **Audit Memory**: Permanent historical record of pipeline run stats, conflict overrides, and LLM costs.

---

## Setup & Usage

### Prerequisites
- Python 3.10+
- Rust toolchain (for the Tauri Desktop App)
- At least one AI provider API key (OpenAI, Anthropic) or local Ollama running

### Start the Cortex AI Desktop App (Recommended)

The desktop app is the main control plane. It automatically launches and manages the Python FastAPI sidecar securely.

```bash
# Terminal 1: Setup Backend
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Terminal 2: Run Desktop App
cd src-tauri
cargo tauri dev
```

### Run the Pipeline (CLI-Only Mode)
You can run the full 11-layer pipeline directly from the terminal without the UI:
```bash
cd backend/src
python main.py
```

### Connect MCP Clients (Claude Desktop / Claude Code)

Agents access the generated knowledge via the local Model Context Protocol (MCP) server. Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cortex-ai": {
      "command": "python",
      "args": ["/absolute/path/to/bhrm--H/backend/src/mcp_server.py"]
    }
  }
}
```

---

## Core API Endpoints

The FastAPI backend exposes an extensive suite of capabilities:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | System health + connection status |
| `POST` | `/api/providers/config` | Update AI provider routing rules |
| `POST` | `/api/connectors/{app_id}/connect`| Establish new OAuth/Token connections |
| `POST` | `/api/run` | Execute full 11-layer pipeline |
| `GET` | `/api/memory/units` | List atomic knowledge units (filter by tier/dept) |
| `POST` | `/api/memory/units/{id}/approve` | Manually approve a contested unit |
| `GET` | `/api/agents` | List registered AI agents |
| `POST` | `/api/agents/delegate` | A2A task delegation |
| `POST` | `/api/webhooks/ingest` | Real-time data push (Slack/GitHub events) |
| `GET` | `/api/audit/integrity` | Verify memory audit logs |

---

## License

Proprietary. All rights reserved.

© 2026 Gaurav Singh
