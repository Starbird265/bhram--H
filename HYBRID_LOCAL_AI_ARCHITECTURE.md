# Hybrid Local AI Architecture Research Note

> [!NOTE]  
> **Status: FULLY IMPLEMENTED (v2.11)**  
> The hybrid architecture detailed in this document has been fully implemented. The system now utilizes a Tauri Desktop app, a 7-tier SQLite memory architecture, an 11-layer pipeline, and an intelligent `ProviderRouter`.

## Why this change matters

The current engine already has the right core: an 11-layer pipeline that ingests local and online data, normalizes it, chunks it, distills operational knowledge, deduplicates it, and compiles `SKILL.md` files. The missing piece was a local control plane: a desktop GUI that can safely see the user's machine, connect to local apps, run offline when needed, and route selected work to online AI providers only when policy allows it.

The target architecture is not "AI does everything." It is a hybrid system where deterministic code owns data movement, auditability, permissions, storage, and repeatability, while AI helps with bounded tasks such as generating filter scripts, classifying chunks, extracting structured fields, resolving ambiguous conflicts, and synthesizing skill documents.

## Research anchors

- OpenAI Structured Outputs support schema-constrained responses, which fits the existing Pydantic-based distillation and skill synthesis pipeline: https://platform.openai.com/docs/guides/structured-outputs
- OpenAI recommends the Responses API for new text-generation projects, and it supports structured data workflows: https://platform.openai.com/docs/guides/text
- Anthropic's Claude tool-use API supports client tools with JSON input schemas, which maps cleanly to local operations executed by our app rather than by the model: https://docs.anthropic.com/en/docs/build-with-claude/tool-use
- Anthropic's MCP connector for the Messages API requires HTTP/SSE style remote servers and cannot directly connect to local STDIO servers, so local Claude Desktop or Claude Code integration remains separate from cloud Claude API integration: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
- Claude Code supports local STDIO MCP servers, which is suitable for local machine tools and private project context: https://docs.anthropic.com/en/docs/claude-code/mcp
- MCP's architecture distinguishes local STDIO servers from remote HTTP transports, which supports a split local/offline and online integration model: https://modelcontextprotocol.io/docs/learn/architecture
- Tauri capabilities can limit desktop filesystem permissions by window and platform, which is useful for a local GUI that touches user files: https://tauri.app/es/reference/acl/capability/
- Ollama exposes local chat and embedding APIs, which gives the offline provider path a practical local runtime target: https://docs.ollama.com/api/chat and https://docs.ollama.com/api/embed

## Current repo state (Fully Implemented)

What exists now:

- A FastAPI backend in `backend/src/api.py`.
- An 11-layer orchestration loop in `backend/src/main.py`.
- Local folder, Obsidian, GitHub, Slack, and Notion connector management in `backend/src/ingestion/`.
- A central `AtomicKnowledgeUnit` model in `backend/src/core/models.py`.
- AI-assisted distillation and skill assembly routed through `ProviderRouter` (OpenAI, Claude, Ollama, Rule-based).
- 7-tier SQLite memory storage in `backend/src/memory/` and `backend/src/storage/`.
- An MCP server for Claude Desktop or other local MCP clients in `backend/src/mcp_server.py`.
- A Tauri desktop control plane in `src-tauri/`.
- A web dashboard in `frontend/` wrapped by Tauri.
- Layer 11 Agent Orchestration in `backend/src/agents/`.

## The Hybrid Architecture

The system uses three cooperating layers:

1. **Desktop control plane (Tauri)**

   A local desktop GUI owns user consent, local file/app access, provider settings, queue visibility, and manual review. Tauri was chosen for its strict native capability boundaries and smaller footprint.

2. **Local orchestration service (FastAPI Sidecar)**

   FastAPI acts as the local worker service. The desktop app manages its lifecycle as a secure sidecar. It runs connectors, the 11-layer pipeline stages, 7-tier memory, local MCP, job queue, audit logs, and provider routing.

3. **AI provider router**

   A single `ProviderRouter` interface for:
   - **OpenAI**: For structured extraction, synthesis, high-quality transformations, and online reasoning.
   - **Claude**: For tool-use reasoning, review, long-context synthesis, and workflows that benefit from Anthropic's MCP ecosystem.
   - **Ollama**: Local runtime for private/offline classification, embedding, first-pass filtering, and fallback summarization.
   - **Rule-based**: Local deterministic logic and guaranteed offline operation.

## Local desktop GUI responsibilities

The desktop GUI is the user's operating room:

- Connect local folders, Obsidian vaults, Git repos, PDFs, docs, spreadsheets, browser exports, and app-specific data.
- Show exactly which folders/apps are connected and what permissions they have.
- Let the user choose privacy levels per source: local-only, online-allowed, redacted-online, or manual-review.
- Show every pipeline stage as a visible job (11-layer pipeline execution).
- Provide approvals for risky actions: resolving contested chunks, deleting, rewriting, publishing skills, or sending sensitive chunks to online providers.
- Show chunk lineage from generated skill back to source file, section, message, or app record.
- Expose local MCP tools to agents.

## Data handling layers

The data handling follows the robust 11-layer architecture documented in `ORGANIZATIONAL_INTELLIGENCE_ENGINE.md`.

## Atomic field for skill generation

`AtomicKnowledgeUnit` acts as the base unit between raw chunks and `SkillDef`.

Suggested fields (now implemented in Pydantic models):

- `id`, `claim`, `instruction`, `rationale`, `knowledge_type`, `department`, `sensitivity_level`, `confidence_score`, `memory_tier`, `status`, `tools_required`, `conflicts_with`, etc.

Why this helps:
- Skills can be assembled from precise facts and instructions rather than broad chunks.
- Contradictions are tracked at the smallest meaningful level.
- Sensitive fields are blocked from online providers by Layer 7 (Privacy Scan).
- Failed agent behavior updates one atomic rule (via Layer 10 Failure Memory).

## Provider routing policy

Every AI request passes through `ProviderRouter` with an audit record.

Routing policies:
- Local-only source: use local rules plus Ollama/local embeddings only.
- Sensitive source with redaction allowed: redact locally (Layer 7), then use OpenAI or Claude.
- Public or low-risk source: online providers allowed.
- High-confidence deterministic pattern: skip AI entirely.
- Low-confidence or contradictory memory: flag for manual review (Layer 9 Conflict Resolution).

## Desktop integration with local apps

The local integrations include:
- Local folder picker with recursive scan settings.
- Obsidian vault detection.
- GitHub through local `gh` auth.
- Claude Desktop and Claude Code via local MCP configuration.

## Implementation plan (Completed)

- **Phase 1**: Desktop shell (Tauri + FastAPI sidecar) -> **DONE**
- **Phase 2**: Provider router (OpenAI, Claude, Ollama) -> **DONE**
- **Phase 3**: Atomic memory unit (`AtomicKnowledgeUnit`) -> **DONE**
- **Phase 4**: Layered memory (7-Tier SQLite + Vector) -> **DONE**
- **Phase 5**: AI-generated filters (Layer 7 Privacy Scan) -> **DONE**
- **Phase 6**: Skill generation hardening (Layer 6 Synthesis moved after Layer 9 Conflict Resolution) -> **DONE**

## Closed Questions & Implementation Decisions

- **Should local-only data ever be allowed to use online AI after redaction, or should some sources be hard-blocked forever?**
  *Decision*: Handled by `SensitivityLevel` tags in Layer 7. `RESTRICTED` data never touches online APIs. `CONFIDENTIAL` can be redacted before processing.
- **Do we want the desktop GUI to run the backend as a bundled sidecar, or connect to an already-running local FastAPI service?**
  *Decision*: Bundled sidecar. Tauri manages the FastAPI lifecycle for security and ease of use.
- **Should the first durable store remain JSON for simplicity, or move now to SQLite before memory tiers get larger?**
  *Decision*: SQLite was chosen to support the robust 7-Tier Memory architecture (`Working`, `Source`, `Canonical`, `Failure`, `Vector`, `Skill`, `Audit`).
- **Which offline runtime should be first-class: Ollama, llama.cpp, LM Studio, or a generic OpenAI-compatible local endpoint?**
  *Decision*: Ollama is used for local chat/embeddings via `ProviderRouter`.
- **Should generated filter scripts be Python-only, or should we define a safer declarative filter DSL first?**
  *Decision*: Used rule-based static evaluation (Presidio + Regex) for deterministic Privacy Scans, and simple API approvals for Conflict Resolution.

