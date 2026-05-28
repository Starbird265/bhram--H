# Organizational Intelligence Engine: 11-Layer Architecture

## Executive Summary
The Organizational Intelligence Engine is an advanced 11-layer automated pipeline that transforms unstructured organizational data (Slack, Notion, local files, GitHub) into high-fidelity AI agent instructions (`SKILL.md`). Inspired by OpenAI's organizational brain pattern and Anthropic's SKILL.md standard, the engine is designed to adapt, learn from operational failures, resolve internal contradictions, and continuously update agent directives via the Model Context Protocol (MCP).

Version 2.12 introduces an **8-Tier SQLite Memory Architecture** (adding SESSION memory), **automatic knowledge expiry**, **user/agent profile aggregation**, **strategic hardening** (sovereignty, health, ROI), a **Tauri Desktop Control Plane**, and dynamic **AI Provider Routing** (Claude/OpenAI/Ollama).

---

## The 11-Layer Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                       11-LAYER PIPELINE                         │
│                                                                 │
│  ┌─────────────┐  Raw data from Slack, Notion, GitHub, files   │
│  │  LAYER 1    │  Pull channel history, wiki pages,            │
│  │  INGESTION  │  local .md/.json files                        │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Strip HTML, Slack artifacts,                 │
│  │  LAYER 2    │  conversational noise, emoji,                 │
│  │  NORMALIZE  │  pleasantries → clean text                    │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Recursive text splitting with                │
│  │  LAYER 3    │  overlap windows + source position            │
│  │  CHUNKING   │  metadata tracking                            │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  AI batch processing (5 chunks/call):         │
│  │  LAYER 4    │  Noise filter → Signal extract →              │
│  │  DISTILL    │  Type classify → Confidence score             │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Similarity scoring (title + content +        │
│  │  LAYER 5    │  tags + dept/type), bounded AI dedup,         │
│  │  DEDUP      │  ADD / UPDATE / DISCARD decisions             │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  PII & Secret detection using Presidio/Rules. │
│  │  LAYER 7    │  Redact sensitive chunks (SensitivityLevel)   │
│  │  PRIVACY    │  prior to persistent storage.                 │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Decompose complex claims into pure           │
│  │  LAYER 8    │  AtomicKnowledgeUnits and isolate             │
│  │  ATOMIC SEG.│  actionable imperatives from context.         │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Flag contradictions against existing memory. │
│  │  LAYER 9    │  Auto-approve safe updates, quarantine        │
│  │  CONFLICT   │  CONTESTED units for manual UI approval.      │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Skill assembly, MCP tool provisioning,       │
│  │  LAYER 6    │  SKILL.md generation, and categorization.     │
│  │  SYNTHESIS  │  Runs after conflict resolution guarantees.   │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Write to the 8-Tier SQLite database and      │
│  │  LAYER 10   │  update the local vector cosine index for     │
│  │  MEMORY IDX │  blazing-fast semantic retrieval.             │
│  └──────┬──────┘                                               │
│         │                                                       │
│  ┌──────▼──────┐  Context injection, tool gating, agent        │
│  │  LAYER 11   │  hot-reloading, and API provisioning for      │
│  │  ORCHESTR.  │  connected MCP clients and A2A delegates.     │
│  └─────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
```

*(Note: Layer numbering reflects historical engine evolution. Layer 6 Synthesis now intentionally executes after Layer 9 Conflict Resolution to guarantee only approved knowledge enters a skill).*

---

## Detailed Layer Breakdown

### Layer 1: Ingestion (`ingestion/`)
- **Connectors**: Slack, Notion, GitHub, Linear, Local Files.
- **Resilience**: Falls back to mock data or cached records when API limits are hit or keys are missing.

### Layer 2: Normalization (`pipeline/normalize.py`)
5-stage cleaning pipeline:
1. HTML tag stripping
2. Slack artifact normalization (`<@U123>` → `[user]`, `<#C123|name>` → `#name`)
3. Conversational noise removal (10+ patterns: pleasantries, emoji reactions, off-topic chat)
4. Markdown simplification (bold/italic removal, link text extraction)
5. Whitespace normalization

### Layer 3: Chunking (`pipeline/chunking.py`)
- Recursive text splitting with separator hierarchy: `# → ## → ### → ¶ → sentence → word`
- Configurable `max_tokens` and `overlap_tokens`
- Specialized grouper that preserves message thread boundaries

### Layer 4: Distillation (`pipeline/distill.py`)
- **Batch processing**: 5 chunks per AI call via `ProviderRouter`
- Structured outputs with a `DistillationBatch` Pydantic schema
- 12-type knowledge ontology: SOP, DECISION, POLICY, FAILURE_PATTERN, EDGE_CASE, etc.

### Layer 5: Deduplication (`pipeline/deduplicate.py`)
- Pre-filters by multi-signal similarity scoring (title 30%, content 50%, tags 10%, dept/type 10%)
- Only sends top-5 candidates to AI to minimize token spend
- Auto-discards exact matches

### Layer 7: Privacy Scan (`pipeline/privacy_scan.py`)
- **Sensitivity Levels**: PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED.
- Detects PII, API keys, passwords, and secrets.
- Redacts sensitive text before it reaches canonical memory.

### Layer 8: Atomic Segmentation (`pipeline/atomic_segmentation.py`)
- Decomposes dense paragraphs into singular `AtomicKnowledgeUnit`s.
- Separates context from direct imperatives (e.g., "Always use `uv`" vs "Because it's faster").

### Layer 9: Conflict Resolution (`pipeline/conflict_resolve.py`)
- Checks newly proposed AtomicKnowledgeUnits against the Canonical memory tier.
- Auto-merges minor updates.
- Flags direct contradictions as `CONTESTED`. Contested chunks require manual approval via the UI or API before Synthesis.

### Layer 6: Synthesis (`smart_layer/`, `generators/`)
- **Skill Assembly**: Confidence-gated (≥0.7), categorized by knowledge type.
- **SKILL.md Generation**: Valid YAML frontmatter + structured markdown.
- **Tool Mapping**: Maps skills to MCP servers.

### Layer 10: Memory Indexing (`memory/`, `storage/`)
- Interfaces with the `MemoryManager`.
- Routes data into the **8-Tier Memory Architecture** (Working, Source, Canonical, Failure, Vector, Skill, Audit, Session).
- Runs automatic expiry sweep at pipeline start.
- Updates the FAISS/Chroma local vector index for semantic search capabilities.

### Layer 11: Agent Orchestration (`agents/`)
- `registry.py` and `context_assembler.py` manage agent identities.
- Provisions exact sub-sets of memory directly to the agent's context window.
- Manages A2A (Agent-to-Agent) delegation.

---

## The Failure-Memory Loop

1. An engineer notes an issue: *"IMPORTANT: Do not restart auth-service without notifying #sec-ops first."*
2. The Engine ingests this, strips noise, and classifies it as a `FAILURE_PATTERN`.
3. It passes Deduplication, Privacy Scan, and Atomic Segmentation.
4. Layer 10 saves it to **Failure Memory**.
5. Layer 6 Synthesis recompiles the affected deployment skill.
6. The failure pattern is injected into the **Edge Cases & What NOT to do** section.
7. Next time an MCP Agent runs, it reads the updated `SKILL.md` and avoids the mistake.

---

## The 8-Tier Memory Architecture

Located in `backend/src/memory/`, the `MemoryManager` governs 8 storage tiers:
1. **Working**: Ephemeral pipeline state.
2. **Source**: Immutable hashes of ingested files/messages.
3. **Canonical**: Approved `AtomicKnowledgeUnits`.
4. **Failure**: Anti-patterns and developer mistakes.
5. **Vector**: Embeddings for cosine similarity search.
6. **Skill**: Generated SKILL.md string blobs.
7. **Audit**: Logs of pipeline runs, LLM costs, and conflict overrides.
8. **Session**: Per-conversation facts from agent/user interactions — ephemeral by default (7-day TTL), promotable to canonical.

### Automatic Expiry
AtomicKnowledgeUnits and session facts can have an `expires_at` timestamp. A background expiry sweep runs:
- At API server startup
- At the beginning of each pipeline run
- On-demand via `POST /api/memory/expiry/sweep`

Expired units are marked `EXPIRED` (not deleted). Expired session facts are deleted unless promoted.

### User / Agent Profiles
Profiles are computed on-demand from session facts and canonical contributions:
- `GET /api/profiles/{user_id}` — aggregated user profile
- `GET /api/profiles/agent/{agent_id}` — aggregated agent profile

---

## Strategic Hardening (`core/strategic_hardening.py`)

Four proactive risk mitigations:

1. **Data Sovereignty** (`GET /api/strategic/sovereignty`): Proves all data stays local — counts local-only vs online-allowed units, enumerates data locations.
2. **SQLite Ceiling** (`GET /api/strategic/sqlite-health`): Monitors DB size, row counts, WAL size. Flags `migration_recommended=true` when thresholds are exceeded.
3. **ROI Calculator** (`GET /api/strategic/roi`): Measurable value metrics — knowledge coverage, cost savings from rule-based routing, governance ratio.
4. **Tacit Knowledge Detection**: Pattern-based scanner for unwritten organizational knowledge ("we always do X", "everyone knows Y").

---

## Setup & Execution

### Prerequisites
```bash
# .env Configuration
OPENAI_API_KEY=your_openai_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
OLLAMA_HOST=http://localhost:11434
```

### Running the Engine
```bash
# Install dependencies
pip install -r backend/requirements.txt

# Run the 11-layer pipeline (CLI)
cd backend/src && python main.py

# Run the backend API
cd backend/src && uvicorn api:app --reload --port 8000

# Run the Tauri Desktop UI
cd src-tauri && cargo tauri dev
```

### Core API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | System health + connection status |
| POST | `/api/run` | Execute 11-layer pipeline |
| GET | `/api/memory/units` | List atomic knowledge units |
| POST | `/api/memory/units/{id}/approve` | Manually approve a contested unit |
| **POST** | **`/api/memory/session`** | **Save a session fact** |
| **GET** | **`/api/memory/session`** | **Get session facts (filter by agent/user)** |
| **POST** | **`/api/memory/session/{id}/promote`** | **Promote session fact to canonical** |
| **POST** | **`/api/memory/expiry/sweep`** | **Trigger expiry sweep** |
| **GET** | **`/api/profiles/{user_id}`** | **User profile aggregation** |
| **GET** | **`/api/profiles/agent/{agent_id}`** | **Agent profile aggregation** |
| **GET** | **`/api/strategic/sovereignty`** | **Data sovereignty report** |
| **GET** | **`/api/strategic/sqlite-health`** | **SQLite health monitor** |
| **GET** | **`/api/strategic/roi`** | **ROI metrics calculator** |
| GET | `/api/agents` | List registered AI agents |
| POST | `/api/agents/delegate` | A2A task delegation |
| POST | `/api/webhooks/ingest` | Real-time data push |
| GET | `/api/audit/integrity` | Verify memory audit logs |
