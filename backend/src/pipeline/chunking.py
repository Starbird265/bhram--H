"""
Layer 3: CHUNKING
Semantic segmentation with overlap windows and source metadata preservation.

Breaks large documents into atomic, AI-digestible chunks while:
  - Preserving context via configurable overlap windows
  - Tracking source position metadata (which section/message it came from)
  - Respecting semantic boundaries (headers, paragraphs, sentences)
  - Keeping chunks within token budget for downstream AI processing
"""

import re
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from dataclasses import dataclass

from core.models import (
    KnowledgeChunk, KnowledgeMetadata, SourcePosition,
    SourceType, Department, KnowledgeType, ProcessingLayer
)


@dataclass
class RawChunk:
    """Intermediate chunk before AI distillation — carries metadata about its origin."""
    text: str
    section_header: Optional[str] = None
    start_char: int = 0
    end_char: int = 0
    source_messages: List[int] = None  # indices of original messages

    def __post_init__(self):
        if self.source_messages is None:
            self.source_messages = []


class SemanticChunker:
    """
    Chunks large documents into semantic sections using recursive text splitting
    with configurable overlap for context preservation.
    
    Strategy:
      1. Try to split on biggest semantic boundary first (# headers)
      2. If chunks are still too large, recurse with finer separators (## → ### → ¶ → sentence)
      3. Add overlap window from the end of previous chunk to start of next
    """

    def __init__(self, max_tokens: int = 1000, overlap_tokens: int = 100):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.separators = ["\n# ", "\n## ", "\n### ", "\n\n", "\n", ". ", " "]

    def chunk_document(
        self,
        text: str,
        source_type: SourceType,
        source_identifier: str,
        department: Department = Department.SHARED
    ) -> List[KnowledgeChunk]:
        """
        Main entrypoint: Takes normalized text and produces KnowledgeChunks
        ready for Layer 4 (Distillation).
        """
        raw_chunks = self._split_with_metadata(text)
        raw_chunks = self._add_overlap(raw_chunks)

        knowledge_chunks = []
        for i, rc in enumerate(raw_chunks):
            if len(rc.text.split()) < 5:
                continue  # Skip trivially small chunks

            chunk = KnowledgeChunk(
                id=str(uuid.uuid4()),
                department=department,
                knowledge_type=KnowledgeType.SOP,  # Placeholder — Layer 4 will reclassify
                source_type=source_type,
                source_identifier=source_identifier,
                title=rc.section_header or f"Section {i + 1}",
                content=rc.text,
                summary="",  # Layer 4 will generate this
                tags=[],
                metadata=KnowledgeMetadata(
                    confidence_score=1.0,
                    source_reliability=1.0,
                    source_position=SourcePosition(
                        section_header=rc.section_header,
                        start_char=rc.start_char,
                        end_char=rc.end_char,
                        message_indices=rc.source_messages
                    )
                ),
                processing_layer=ProcessingLayer.CHUNKED,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            knowledge_chunks.append(chunk)

        return knowledge_chunks

    def chunk_slack_messages(
        self,
        messages: List[str],
        source_identifier: str,
        department: Department = Department.SHARED
    ) -> List[KnowledgeChunk]:
        """
        Specialized chunker for Slack messages.
        Groups messages into chunks that respect the token budget
        while preserving message boundaries and tracking indices.
        """
        chunks = []
        current_group = []
        current_indices = []
        current_word_count = 0

        for i, msg in enumerate(messages):
            msg = msg.strip()
            if not msg:
                continue

            msg_words = len(msg.split())

            # If single message exceeds budget, it becomes its own chunk
            if msg_words > self.max_tokens:
                # Flush current group first
                if current_group:
                    chunks.append(RawChunk(
                        text="\n".join(current_group),
                        source_messages=current_indices.copy(),
                        start_char=0,
                        end_char=0
                    ))
                    current_group = []
                    current_indices = []
                    current_word_count = 0

                # Add oversized message as its own chunk
                chunks.append(RawChunk(
                    text=msg,
                    source_messages=[i],
                    start_char=0,
                    end_char=0
                ))
                continue

            # If adding this message would exceed budget, flush and start new group
            if current_word_count + msg_words > self.max_tokens and current_group:
                chunks.append(RawChunk(
                    text="\n".join(current_group),
                    source_messages=current_indices.copy(),
                    start_char=0,
                    end_char=0
                ))
                # Overlap: keep last message of previous group
                overlap_msg = current_group[-1] if current_group else ""
                current_group = [overlap_msg] if overlap_msg else []
                current_indices = [current_indices[-1]] if current_indices else []
                current_word_count = len(overlap_msg.split()) if overlap_msg else 0

            current_group.append(msg)
            current_indices.append(i)
            current_word_count += msg_words

        # Flush remaining
        if current_group:
            chunks.append(RawChunk(
                text="\n".join(current_group),
                source_messages=current_indices.copy(),
                start_char=0,
                end_char=0
            ))

        # Convert to KnowledgeChunks
        knowledge_chunks = []
        for j, rc in enumerate(chunks):
            if len(rc.text.split()) < 3:
                continue

            chunk = KnowledgeChunk(
                id=str(uuid.uuid4()),
                department=department,
                knowledge_type=KnowledgeType.SOP,
                source_type=SourceType.SLACK,
                source_identifier=source_identifier,
                title=f"Slack messages (batch {j + 1})",
                content=rc.text,
                summary="",
                tags=[],
                metadata=KnowledgeMetadata(
                    confidence_score=1.0,
                    source_reliability=0.8,  # Slack is lower reliability than docs
                    source_position=SourcePosition(
                        channel_id=source_identifier,
                        message_indices=rc.source_messages
                    )
                ),
                processing_layer=ProcessingLayer.CHUNKED,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            knowledge_chunks.append(chunk)

        return knowledge_chunks

    # ─── Internal Methods ────────────────────────────────────────

    def _split_with_metadata(self, text: str) -> List[RawChunk]:
        """
        Line-aware splitting: splits text into individual lines first,
        then groups consecutive lines into chunks within the token budget.
        Never splits a single line across two chunks.
        """
        lines = text.split('\n')
        chunks = []
        current_lines = []
        current_word_count = 0
        current_header = None
        chunk_start_char = 0

        for line in lines:
            stripped = line.strip()
            # Track section headers for metadata
            header_match = re.match(r'^(#{1,3})\s+(.+?)$', stripped)
            if header_match:
                current_header = header_match.group(2).strip()

            line_words = len(stripped.split()) if stripped else 0

            # Skip completely empty lines (but don't flush — they're just separators)
            if not stripped:
                if current_lines:
                    current_lines.append('')  # preserve paragraph breaks
                continue

            # If a single line exceeds budget, it becomes its own chunk
            if line_words > self.max_tokens:
                # Flush current group first
                if current_lines:
                    chunk_text = '\n'.join(current_lines).strip()
                    if chunk_text:
                        chunks.append(RawChunk(
                            text=chunk_text,
                            section_header=current_header,
                            start_char=chunk_start_char,
                            end_char=chunk_start_char + len(chunk_text),
                        ))
                    current_lines = []
                    current_word_count = 0
                    chunk_start_char += len(chunk_text) + 1

                # Add oversized line as its own chunk
                chunks.append(RawChunk(
                    text=stripped,
                    section_header=current_header,
                    start_char=chunk_start_char,
                    end_char=chunk_start_char + len(stripped),
                ))
                chunk_start_char += len(stripped) + 1
                continue

            # If adding this line would exceed budget, flush current group
            if current_word_count + line_words > self.max_tokens and current_lines:
                chunk_text = '\n'.join(current_lines).strip()
                if chunk_text:
                    chunks.append(RawChunk(
                        text=chunk_text,
                        section_header=current_header,
                        start_char=chunk_start_char,
                        end_char=chunk_start_char + len(chunk_text),
                    ))
                chunk_start_char += len(chunk_text) + 1
                current_lines = []
                current_word_count = 0

            current_lines.append(stripped)
            current_word_count += line_words

        # Flush remaining lines
        if current_lines:
            chunk_text = '\n'.join(current_lines).strip()
            if chunk_text:
                chunks.append(RawChunk(
                    text=chunk_text,
                    section_header=current_header,
                    start_char=chunk_start_char,
                    end_char=chunk_start_char + len(chunk_text),
                ))

        return chunks

    def _add_overlap(self, chunks: List[RawChunk]) -> List[RawChunk]:
        """
        Adds line-level overlap between adjacent chunks for context preservation.
        Takes the last N lines (by word count up to overlap_tokens) from chunk[i]
        and prepends to chunk[i+1]. Never injects synthetic markers.
        """
        if len(chunks) <= 1 or self.overlap_tokens <= 0:
            return chunks

        for i in range(1, len(chunks)):
            prev_lines = chunks[i - 1].text.split('\n')
            overlap_lines = []
            overlap_words = 0

            # Walk backward through previous chunk's lines
            for line in reversed(prev_lines):
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                line_words = len(line_stripped.split())
                if overlap_words + line_words > self.overlap_tokens:
                    break
                overlap_lines.insert(0, line_stripped)
                overlap_words += line_words

            if overlap_lines:
                overlap_text = '\n'.join(overlap_lines)
                chunks[i].text = f"{overlap_text}\n{chunks[i].text}"

        return chunks
