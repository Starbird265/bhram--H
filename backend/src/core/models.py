"""
Core data models for the 11-Layer Organizational Intelligence Engine.

Architecture Layers:
  Layer 1:  INGESTION          → Pull raw data from any source
  Layer 2:  NORMALIZATION      → Clean, standardize, format-unify
  Layer 3:  CHUNKING           → Semantic segmentation with overlap + metadata
  Layer 4:  DISTILLATION       → AI-powered extraction, noise filtering, classification
  Layer 5:  DEDUPLICATION      → Similarity-based merge/update/discard
  Layer 6:  SYNTHESIS          → Skill assembly, tool provisioning, agent routing
  Layer 7:  PRIVACY_SCAN       → PII/secret detection, sensitivity labeling
  Layer 8:  ATOMIC_SEGMENT     → Decompose into AtomicKnowledgeUnits
  Layer 9:  CONFLICT_RESOLVE   → Merge identical, flag contradictions
  Layer 10: MEMORY_INDEX       → Write to 7-tier memory architecture
  Layer 11: AGENT_ORCH         → Agent registry, context injection, tool gating, hot-reload
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any, Union
from enum import Enum
from datetime import datetime, timezone


# ─── Enums ────────────────────────────────────────────────────────

class ProcessingLayer(str, Enum):
    """Tracks which pipeline layer data has been processed through."""
    RAW = "raw"                       # Layer 1: Just ingested
    NORMALIZED = "normalized"         # Layer 2: Cleaned and standardized
    CHUNKED = "chunked"               # Layer 3: Semantically segmented
    DISTILLED = "distilled"           # Layer 4: AI-extracted operational signals
    DEDUPLICATED = "deduplicated"     # Layer 5: Merged with canonical store
    SYNTHESIZED = "synthesized"       # Layer 6: Compiled into skills
    PRIVACY_SCANNED = "privacy_scanned"  # Layer 7: Sensitivity labeled
    ATOMIZED = "atomized"             # Layer 8: Decomposed to atomic units
    CONFLICT_RESOLVED = "conflict_resolved"  # Layer 9: Contradictions handled
    MEMORY_INDEXED = "memory_indexed" # Layer 10: Written to memory tiers


class KnowledgeType(str, Enum):
    SOP = "SOP"
    DECISION = "DECISION"
    POLICY = "POLICY"
    FAILURE_PATTERN = "FAILURE_PATTERN"
    EDGE_CASE = "EDGE_CASE"
    APPROVAL_FLOW = "APPROVAL_FLOW"
    TOOL_WORKFLOW = "TOOL_WORKFLOW"
    CUSTOMER_CONTEXT = "CUSTOMER_CONTEXT"
    ESCALATION = "ESCALATION"
    GLOSSARY = "GLOSSARY"
    PREFERENCE = "PREFERENCE"
    SECURITY_RULE = "SECURITY_RULE"


class SourceType(str, Enum):
    NOTION = "notion"
    SLACK = "slack"
    MARKDOWN = "markdown"
    JSON = "json"
    GITHUB = "github"
    LOCAL_FILE = "local_file"
    # Phase 1 connectors
    GOOGLE_DRIVE = "google_drive"
    CONFLUENCE = "confluence"
    JIRA = "jira"
    MS_TEAMS = "ms_teams"
    LINEAR = "linear"
    WHATSAPP = "whatsapp"


class Department(str, Enum):
    MARKETING = "marketing"
    ENGINEERING = "engineering"
    SALES = "sales"
    OPS = "ops"
    SHARED = "shared"
    HR = "hr"
    FINANCE = "finance"
    LEGAL = "legal"
    SUPPORT = "support"


class SensitivityLevel(str, Enum):
    """How sensitive this data is — controls provider routing."""
    PUBLIC = "public"             # Can go anywhere
    INTERNAL = "internal"         # Stay within org tools
    CONFIDENTIAL = "confidential" # Redact before sending to online AI
    RESTRICTED = "restricted"     # Never leave the local machine


class PrivacyMode(str, Enum):
    """Source-level privacy policy — set when connecting a source."""
    HARD_LOCAL = "hard_local"           # Only local processing (Ollama + rules)
    REDACTED_ONLINE = "redacted_online" # PII stripped, then online AI allowed
    ONLINE_ALLOWED = "online_allowed"   # Full content can go to cloud providers


class UnitStatus(str, Enum):
    """Lifecycle status of an AtomicKnowledgeUnit."""
    CANDIDATE = "candidate"     # Freshly extracted, not yet approved
    APPROVED = "approved"       # Human or auto-approved for use
    CONTESTED = "contested"     # Conflicts with another unit
    SUPERSEDED = "superseded"   # Replaced by a newer unit
    RETIRED = "retired"         # No longer applicable
    REJECTED = "rejected"       # Explicitly rejected by a human reviewer
    EXPIRED = "expired"         # Past expires_at — auto-marked by expiry sweep


class MemoryTier(str, Enum):
    """Which memory tier a piece of knowledge lives in."""
    WORKING = "working"         # Temporary, current pipeline run only
    SOURCE = "source"           # Immutable source references
    CANONICAL = "canonical"     # Approved operational knowledge
    FAILURE = "failure"         # Corrections, "never do this" rules
    VECTOR = "vector"           # Embedding index for semantic search
    SKILL = "skill"             # Compiled skill documents
    AUDIT = "audit"             # Decision trail, who approved what
    SESSION = "session"         # Per-conversation facts from agent/user interactions


# ─── Layer 1-3: Raw & Chunk Models ───────────────────────────────

class SourcePosition(BaseModel):
    """Tracks where in the original source a chunk came from."""
    file_path: Optional[str] = None
    page_id: Optional[str] = None
    channel_id: Optional[str] = None
    thread_ts: Optional[str] = None       # Slack thread timestamp
    message_indices: List[int] = Field(default_factory=list)  # Which messages in the batch
    section_header: Optional[str] = None  # Markdown header this chunk belongs to
    start_char: Optional[int] = None
    end_char: Optional[int] = None


class KnowledgeMetadata(BaseModel):
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    source_reliability: float = Field(default=1.0, ge=0.0, le=1.0)
    verification_count: int = Field(default=0)
    last_confirmed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_refs: List[str] = Field(default_factory=list)
    source_position: Optional[SourcePosition] = None


# ─── Atomic Knowledge Unit (Base Field for Skill Generation) ─────

class AtomicKnowledgeUnit(BaseModel):
    """The smallest meaningful unit of operational knowledge.

    Sits between raw KnowledgeChunks and compiled SkillDefs.
    Every piece of knowledge must pass through this model before
    it can become part of a skill. This enables:
      - Precise conflict tracking at the claim level
      - Privacy gating before any AI call
      - Auditable lineage from skill → unit → source
      - Incremental updates without rewriting whole skills
    """
    id: str

    # Core knowledge
    claim: str = Field(..., description="The core assertion in 1-2 sentences")
    instruction: str = Field(..., description="Imperative form of the claim")
    rationale: Optional[str] = Field(None, description="Why this matters")

    # Classification
    knowledge_type: KnowledgeType
    department: Department
    scope: str = Field(default="global", description="global, project:X, or team:Y")

    # Applicability
    applies_when: List[str] = Field(default_factory=list)
    does_not_apply_when: List[str] = Field(default_factory=list)

    # Provenance
    source_type: SourceType
    source_identifier: str
    source_position: Optional[SourcePosition] = None
    source_excerpt_hash: str = Field(default="", description="SHA-256 of original text")
    source_reliability: float = Field(default=1.0, ge=0.0, le=1.0)

    # Confidence & verification
    confidence_score: float = Field(default=0.6, ge=0.0, le=1.0)
    verification_count: int = Field(default=0)
    last_confirmed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Privacy
    sensitivity_level: SensitivityLevel = SensitivityLevel.INTERNAL
    online_allowed: bool = True

    # Relationships
    tags: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list, description="People, tools, services mentioned")
    tools_required: List[str] = Field(default_factory=list)
    related_units: List[str] = Field(default_factory=list, description="IDs of related units")
    conflicts_with: List[str] = Field(default_factory=list, description="IDs of contradicting units")
    supersedes: List[str] = Field(default_factory=list, description="IDs of older units this replaces")

    # Lifecycle
    status: UnitStatus = UnitStatus.CANDIDATE
    memory_tier: MemoryTier = MemoryTier.WORKING
    skill_targets: List[str] = Field(default_factory=list, description="Which skills this feeds into")
    validator_results: Dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = Field(default=None, description="Auto-expire after this timestamp")


# ─── Source Reference (Immutable) ────────────────────────────────

class SourceRef(BaseModel):
    """Immutable record of where data came from. Stored in source memory."""
    id: str
    source_type: SourceType
    source_identifier: str
    file_hash: Optional[str] = None     # SHA-256 of file contents
    acquired_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    byte_size: Optional[int] = None
    privacy_mode: PrivacyMode = PrivacyMode.HARD_LOCAL
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ─── Audit Entry ─────────────────────────────────────────────────

class AuditEntry(BaseModel):
    """Single decision record in the audit trail."""
    id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: str                          # "approve", "discard", "merge", "route", "publish"
    actor: str = "system"                # "system", "user", "provider:openai", etc.
    target_type: str                     # "chunk", "unit", "skill", "filter"
    target_id: str
    details: Dict[str, Any] = Field(default_factory=dict)
    provider_used: Optional[str] = None  # Which AI provider, if any
    cost_usd: Optional[float] = None     # Estimated cost of AI call


# ─── Layer 4: Knowledge Chunk (Central Data Unit) ────────────────

class KnowledgeChunk(BaseModel):
    """The atomic unit of organizational knowledge flowing through all 6 layers."""
    id: str
    department: Department
    knowledge_type: KnowledgeType

    source_type: SourceType
    source_identifier: str

    title: str
    content: str
    summary: str

    tags: List[str] = Field(default_factory=list)

    metadata: KnowledgeMetadata = Field(default_factory=KnowledgeMetadata)

    # Processing state
    processing_layer: ProcessingLayer = ProcessingLayer.RAW

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Layer 6: Skill Models ──────────────────────────────────────

class SkillMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    author: Optional[str] = None
    version: str = "1.0.0"
    mcp_server: Optional[str] = Field(None, alias="mcp-server")


class SkillDef(BaseModel):
    # max 64 chars, lowercase + hyphens only
    name: str = Field(..., max_length=64, pattern=r'^[a-z0-9]+(-[a-z0-9]+)*$')
    # max 1024 chars
    description: str = Field(..., max_length=1024)
    department: Department = Department.SHARED
    license: Optional[str] = None
    metadata: Optional[SkillMetadata] = None

    overview: str
    prerequisites: List[str] = Field(default_factory=list)
    steps: List[str] = Field(default_factory=list)
    examples: List[Dict[str, str]] = Field(default_factory=list)
    edge_cases: List[str] = Field(default_factory=list)

    # Provenance: which chunks built this skill
    source_chunk_ids: List[str] = Field(default_factory=list)


# ─── Pipeline Observability ─────────────────────────────────────

class PipelineRunStats(BaseModel):
    """Stats for a single pipeline run — tracks what happened at each layer."""
    run_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    # Layer 1: Ingestion
    raw_items_ingested: int = 0
    # Layer 2: Normalization
    items_after_normalization: int = 0
    # Layer 3: Chunking
    chunks_created: int = 0
    # Layer 4: Distillation
    chunks_after_distillation: int = 0
    noise_filtered: int = 0
    # Layer 5: Deduplication
    chunks_added: int = 0
    chunks_updated: int = 0
    chunks_discarded: int = 0
    # Layer 6: Synthesis
    skills_recompiled: int = 0
    agents_notified: int = 0
    # Layer 7: Privacy Scan
    items_privacy_scanned: int = 0
    items_flagged_sensitive: int = 0
    # Layer 8: Atomic Segmentation
    atomic_units_created: int = 0
    # Layer 9: Conflict Resolution
    conflicts_detected: int = 0
    conflicts_resolved: int = 0
    # Layer 10: Memory Indexing
    units_indexed: int = 0

    errors: List[str] = Field(default_factory=list)


# ─── Layer 11: Agent Orchestration Models ────────────────────────

class AgentRole(str, Enum):
    """Role classification for agents in the orchestration layer."""
    ORCHESTRATOR = "orchestrator"    # Routes tasks to specialist agents
    SPECIALIST   = "specialist"      # Deep domain expert (Engineering, Sales, etc.)
    REVIEWER     = "reviewer"        # Handles CONTESTED units from Layer 9
    SYNTHESIZER  = "synthesizer"     # Triggers and monitors Layer 6 Skill builds
    WATCHER      = "watcher"         # Monitors file system / skill reload events


class AgentProfile(BaseModel):
    """
    Single source-of-truth configuration for an AI agent.

    Stored in SQLite `agents` table and serialized to YAML for
    human editing. Every agent in the system is defined by exactly
    one AgentProfile.
    """
    # ── Identity ──────────────────────────────────────────────
    agent_id: str = Field(..., description="Unique slug, e.g. 'eng-ops-agent-01'")
    display_name: str = Field(..., description="Human-readable name")
    icon: str = Field(default="🤖", description="Emoji icon for UI display")
    role: AgentRole = AgentRole.SPECIALIST
    department: str = Field(default="shared", description="Maps to Department enum value")
    description: str = Field(default="", description="One-line description (shown in AgentCard)")

    # ── Context Configuration ─────────────────────────────────
    system_prompt_template: str = Field(
        default="",
        description="Jinja2 template — references skill files + context files"
    )
    context_files: List[str] = Field(
        default_factory=list,
        description="Paths to CONTEXT.md files this agent loads at startup"
    )
    skill_files: List[str] = Field(
        default_factory=list,
        description="Paths to SKILL.md files scoped to this agent"
    )

    # ── Tool Access Control ───────────────────────────────────
    tools_allowlist: List[str] = Field(
        default_factory=list,
        description="Explicit MCP tool names agent CAN call"
    )
    tools_denylist: List[str] = Field(
        default_factory=list,
        description="Overrides allowlist — agent CANNOT call these"
    )
    mcp_servers: List[str] = Field(
        default_factory=list,
        description="MCP server URLs this agent connects to"
    )

    # ── Skill Reload ──────────────────────────────────────────
    auto_reload_on_skill_change: bool = Field(
        default=True,
        description="Auto-rebuild context when SKILL.md changes"
    )
    reload_debounce_seconds: int = Field(
        default=5,
        description="Debounce window for hot-reload events"
    )

    # ── A2A / Inter-Agent ─────────────────────────────────────
    can_delegate_to: List[str] = Field(
        default_factory=list,
        description="agent_ids this agent may hand off tasks to"
    )
    accepts_tasks_from: List[str] = Field(
        default_factory=list,
        description="agent_ids allowed to delegate TO this agent"
    )

    # ── Bound Skills (runtime state, not persisted in YAML) ──
    bound_skills: List[str] = Field(
        default_factory=list,
        description="Names of skills currently bound to this agent"
    )

    # ── Security ──────────────────────────────────────────────
    max_context_tokens: int = Field(
        default=8000,
        description="Max tokens for assembled system prompt"
    )
    online_llm_allowed: bool = Field(
        default=True,
        description="Whether this agent may use cloud LLM providers"
    )
    sensitivity_ceiling: str = Field(
        default="internal",
        description="Max SensitivityLevel of data visible to this agent"
    )

    # ── Lifecycle ─────────────────────────────────────────────
    last_reloaded_at: Optional[datetime] = None
    context_ready: bool = False
    webhook_url: Optional[str] = Field(
        default=None,
        description="External webhook to call on reload (optional)"
    )
    auth_token: Optional[str] = Field(
        default=None,
        description="Bearer token sent with outbound webhook calls to this agent"
    )
    auto_bind_departments: List[str] = Field(
        default_factory=list,
        description="Auto-bind skills from these departments during pipeline"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Session Memory (8th Tier) ───────────────────────────────

class SessionFact(BaseModel):
    """A single fact captured during an agent/user conversation session.

    Session facts are ephemeral by default (expire after a configurable TTL)
    but can be promoted to canonical AtomicKnowledgeUnits when they prove
    valuable. This bridges the gap between transient chat context and
    permanent organizational knowledge.
    """
    id: str
    agent_id: Optional[str] = None
    user_id: Optional[str] = None
    fact: str = Field(..., description="The captured fact in 1-2 sentences")
    fact_type: str = Field(default="explicit", description="explicit | inferred | tacit")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source_conversation: Optional[str] = Field(None, description="Conversation/session ID")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = Field(None, description="Auto-expire after this timestamp")
    promoted_to_unit_id: Optional[str] = Field(None, description="ID of the canonical unit if promoted")


class UserProfile(BaseModel):
    """Aggregated profile built from session facts and canonical contributions.

    Not persisted directly — computed on-demand from session_memory and
    atomic_units tables. Provides a per-user or per-agent view of their
    knowledge contributions and expertise areas.
    """
    user_id: str
    display_name: Optional[str] = None
    departments: List[str] = Field(default_factory=list)
    expertise_areas: List[str] = Field(default_factory=list)
    session_fact_count: int = 0
    canonical_contribution_count: int = 0
    top_knowledge_types: List[str] = Field(default_factory=list)
    last_active_at: Optional[datetime] = None
    profile_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
