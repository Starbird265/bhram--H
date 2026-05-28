"""
Shared pytest fixtures for the 6-Layer Intelligence Engine test suite.
All tests run with sys.path pre-configured so imports work without installation.
"""

import sys
import os
import uuid
from datetime import datetime, timezone

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
# Makes `from pipeline.distill import ...` work without installing the package.
BACKEND_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend", "src"))
sys.path.insert(0, BACKEND_SRC)

from core.models import (
    KnowledgeChunk, KnowledgeType, SourceType, Department,
    KnowledgeMetadata, ProcessingLayer,
)


# ── Factories ────────────────────────────────────────────────────────────────

def make_chunk(
    content: str,
    knowledge_type: KnowledgeType = KnowledgeType.SOP,
    department: Department = Department.ENGINEERING,
    source_type: SourceType = SourceType.SLACK,
    source_identifier: str = "#eng-test",
    confidence: float = 0.8,
    title: str = "Test Chunk",
    processing_layer: ProcessingLayer = ProcessingLayer.CHUNKED,
) -> KnowledgeChunk:
    """Creates a minimal KnowledgeChunk for testing."""
    return KnowledgeChunk(
        id=str(uuid.uuid4()),
        department=department,
        knowledge_type=knowledge_type,
        source_type=source_type,
        source_identifier=source_identifier,
        title=title,
        content=content,
        summary=content[:80],
        tags=[],
        metadata=KnowledgeMetadata(
            confidence_score=confidence,
            source_reliability=1.0,
            verification_count=0,
            last_confirmed_at=datetime.now(timezone.utc),
            source_refs=[source_identifier],
        ),
        processing_layer=processing_layer,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ── Core fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def high_signal_messages():
    """Real-world-style Slack messages with clear operational signals."""
    return [
        "Don't deploy payments after 6 PM because Stripe webhooks fail silently.",
        "IMPORTANT: Never restart auth-service without notifying #sec-ops first.",
        "Make sure to tag #devops in the PR if it touches the DB schema.",
        "Always run migrations in staging before production.",
        "Use `helm upgrade --atomic` or rollback immediately if health check fails.",
    ]


@pytest.fixture
def noise_messages():
    """Pure conversational noise with zero operational signal."""
    return [
        "Hey everyone!",
        "Thanks!",
        "+1",
        "ok sounds good",
        "grabbing lunch, brb",
        "lol nice",
        "Happy Friday everyone!",
    ]


@pytest.fixture
def mixed_messages(high_signal_messages, noise_messages):
    """Mix of signal + noise to test filtering accuracy."""
    return high_signal_messages + noise_messages


@pytest.fixture
def compound_rule_chunk():
    """A chunk with a compound rule joined by 'and' — must split into 2 signals."""
    return make_chunk(
        content="Never deploy after 6 PM and always tag #devops in the PR.",
        title="Compound Deployment Rule",
    )


@pytest.fixture
def engineering_chunk():
    return make_chunk(
        content="Always run `terraform plan` before `terraform apply` in production.",
        title="Terraform Safety Rule",
        department=Department.ENGINEERING,
        knowledge_type=KnowledgeType.SOP,
        confidence=0.85,
    )


@pytest.fixture
def security_chunk():
    return make_chunk(
        content="Never commit secrets to the repository. Use AWS Secrets Manager.",
        title="Secrets Management Policy",
        department=Department.ENGINEERING,
        knowledge_type=KnowledgeType.SECURITY_RULE,
        confidence=0.95,
    )


@pytest.fixture
def low_confidence_modal_chunk():
    """Should be promoted by refine_chunks from 0.5 → 0.7 (modal verb present)."""
    return make_chunk(
        content="You should always run tests before pushing to main.",
        title="Testing Requirement",
        confidence=0.5,
        processing_layer=ProcessingLayer.DISTILLED,
    )


@pytest.fixture
def near_duplicate_pair():
    """Two chunks that are very similar — the second should be DISCARD or UPDATE."""
    chunk_a = make_chunk(
        content="Never deploy to production without a rollback plan.",
        title="Deployment Safety",
        confidence=0.9,
        processing_layer=ProcessingLayer.DEDUPLICATED,
    )
    chunk_b = make_chunk(
        content="Do not deploy to production without a tested rollback plan.",
        title="Deployment Safety Rule",
        confidence=0.9,
        processing_layer=ProcessingLayer.DISTILLED,
    )
    return chunk_a, chunk_b


@pytest.fixture
def distinct_chunks():
    """Two chunks with completely different content — both should be ADD."""
    chunk_a = make_chunk(
        content="Always run terraform plan before terraform apply.",
        title="Terraform Plan First",
        confidence=0.85,
        processing_layer=ProcessingLayer.DEDUPLICATED,
    )
    chunk_b = make_chunk(
        content="Customer PII must never be logged in production logs.",
        title="PII Logging Policy",
        department=Department.OPS,
        knowledge_type=KnowledgeType.POLICY,
        confidence=0.95,
        processing_layer=ProcessingLayer.DISTILLED,
    )
    return chunk_a, chunk_b
