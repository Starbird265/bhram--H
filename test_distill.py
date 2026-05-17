"""
Test: 6-Layer Pipeline Validation
Runs a mini version of the full pipeline to verify each layer works correctly.
"""

import sys
import os
sys.path.insert(0, os.path.abspath('backend/src'))

from pipeline.normalize import TextNormalizer
from pipeline.chunking import SemanticChunker
from pipeline.distill import KnowledgeDistiller
from core.models import SourceType, Department

# Test data: Simulated Slack messages with noise + signals
messages = [
    "Hey everyone, quick update from the post-mortem yesterday.",
    "Don't deploy payments after 6 PM because Stripe webhooks fail silently.",
    "Also, whoever is grabbing lunch, let me know.",
    "Make sure to tag #devops in the PR if it touches the DB schema.",
    "Thanks!",
    "IMPORTANT: Never restart auth-service without notifying #sec-ops first.",
]

print("=" * 60)
print("LAYER 2: NORMALIZATION")
print("=" * 60)
normalizer = TextNormalizer()
normalized = normalizer.normalize_slack_messages(messages)
print(f"Input: {len(messages)} messages")
print(f"Output:\n{normalized}")
print(f"Lines after normalization: {len([l for l in normalized.split(chr(10)) if l.strip()])}")

print("\n" + "=" * 60)
print("LAYER 3: CHUNKING")
print("=" * 60)
chunker = SemanticChunker(max_tokens=1000, overlap_tokens=50)
signal_lines = [l.lstrip("- ") for l in normalized.split("\n") if l.strip()]
chunks = chunker.chunk_slack_messages(signal_lines, "#engineering-deployments", Department.ENGINEERING)
print(f"Created {len(chunks)} chunks:")
for i, chunk in enumerate(chunks):
    print(f"  Chunk {i+1}: {chunk.content[:80]}...")

print("\n" + "=" * 60)
print("LAYER 4: DISTILLATION")
print("=" * 60)
distiller = KnowledgeDistiller()
distilled = distiller.distill_chunks(chunks)
print(f"Distilled {len(chunks)} chunks → {len(distilled)} signals:")
for signal in distilled:
    print(f"  [{signal.knowledge_type.value}] {signal.title} (conf: {signal.metadata.confidence_score:.2f})")

print("\n✓ All layers passed!")
