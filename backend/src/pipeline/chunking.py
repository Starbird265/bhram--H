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
        """Recursively splits text while tracking section headers and positions."""
        chunks = self._recursive_split(text, self.separators)
        result = []
        char_offset = 0

        for chunk_text in chunks:
            # Try to extract a section header from the chunk
            header = None
            header_match = re.match(r'^(#{1,3})\s+(.+?)$', chunk_text, re.MULTILINE)
            if header_match:
                header = header_match.group(2).strip()

            start = text.find(chunk_text, char_offset)
            if start == -1:
                start = char_offset
            end = start + len(chunk_text)

            result.append(RawChunk(
                text=chunk_text,
                section_header=header,
                start_char=start,
                end_char=end
            ))
            char_offset = end

        return result

    def _recursive_split(self, text: str, separators: List[str]) -> List[str]:
        """Core recursive splitting algorithm."""
        final_chunks = []

        # Base case: text is within limits
        if len(text.split()) <= self.max_tokens:
            return [text]

        # Find the best separator to split on
        separator = separators[0] if separators else ""
        for sep in separators:
            if sep in text:
                separator = sep
                break

        # If no separator found, just return the text (fail open)
        if not separator:
            return [text]

        # Split the text
        splits = text.split(separator)

        # Merge splits iteratively until we hit max_tokens
        current_chunk = ""
        for split in splits:
            if not split.strip():
                continue

            # If the current split itself is too large, recurse with finer separators
            if len(split.split()) > self.max_tokens:
                if current_chunk:
                    final_chunks.append(current_chunk.strip())
                    current_chunk = ""

                next_seps = separators[separators.index(separator) + 1:] if separator in separators else []
                final_chunks.extend(self._recursive_split(split, next_seps))
                continue

            # If adding this split exceeds the chunk limit, save and start new
            potential_chunk = current_chunk + (separator if current_chunk else "") + split
            if len(potential_chunk.split()) > self.max_tokens:
                if current_chunk:
                    final_chunks.append(current_chunk.strip())
                current_chunk = split
            else:
                current_chunk = potential_chunk

        if current_chunk:
            final_chunks.append(current_chunk.strip())

        return final_chunks

    def _add_overlap(self, chunks: List[RawChunk]) -> List[RawChunk]:
        """
        Adds overlap windows between adjacent chunks for context preservation.
        Takes the last N tokens from chunk[i] and prepends to chunk[i+1].
        """
        if len(chunks) <= 1 or self.overlap_tokens <= 0:
            return chunks

        for i in range(1, len(chunks)):
            prev_words = chunks[i - 1].text.split()
            if len(prev_words) > self.overlap_tokens:
                overlap_text = " ".join(prev_words[-self.overlap_tokens:])
                chunks[i].text = f"[...context] {overlap_text}\n\n{chunks[i].text}"

        return chunks
