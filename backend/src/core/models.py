"""
Core data models for the 6-Layer Organizational Intelligence Engine.

Architecture Layers:
  Layer 1: INGESTION       → Pull raw data from any source
  Layer 2: NORMALIZATION   → Clean, standardize, format-unify
  Layer 3: CHUNKING        → Semantic segmentation with overlap + metadata
  Layer 4: DISTILLATION    → AI-powered extraction, noise filtering, classification
  Layer 5: DEDUPLICATION   → Similarity-based merge/update/discard
  Layer 6: SYNTHESIS       → Skill assembly, tool provisioning, agent routing
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime, timezone


# ─── Enums ────────────────────────────────────────────────────────

class ProcessingLayer(str, Enum):
    """Tracks which pipeline layer data has been processed through."""
    RAW = "raw"                   # Layer 1: Just ingested
    NORMALIZED = "normalized"     # Layer 2: Cleaned and standardized
    CHUNKED = "chunked"           # Layer 3: Semantically segmented
    DISTILLED = "distilled"       # Layer 4: AI-extracted operational signals
    DEDUPLICATED = "deduplicated" # Layer 5: Merged with canonical store
    SYNTHESIZED = "synthesized"   # Layer 6: Compiled into skills


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


class Department(str, Enum):
    MARKETING = "marketing"
    ENGINEERING = "engineering"
    SALES = "sales"
    OPS = "ops"
    SHARED = "shared"


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

    # Layer 1
    raw_items_ingested: int = 0
    # Layer 2
    items_after_normalization: int = 0
    # Layer 3
    chunks_created: int = 0
    # Layer 4
    chunks_after_distillation: int = 0
    noise_filtered: int = 0
    # Layer 5
    chunks_added: int = 0
    chunks_updated: int = 0
    chunks_discarded: int = 0
    # Layer 6
    skills_recompiled: int = 0
    agents_notified: int = 0

    errors: List[str] = Field(default_factory=list)
