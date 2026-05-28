"""
Layer 1: LOCAL FILE INGESTION
Parses local markdown and JSON files from the raw_data directory
into KnowledgeChunks for processing through the pipeline.
"""

import json
import uuid
from typing import List
from pathlib import Path
from datetime import datetime, timezone

from core.models import (
    KnowledgeChunk, KnowledgeType, KnowledgeMetadata,
    Department, SourceType, SourcePosition, ProcessingLayer
)


class LocalFileParser:
    """
    Parses structured (JSON) and semi-structured (Markdown) files
    from the raw_data directory into KnowledgeChunks.
    """

    def __init__(self, raw_data_dir: str):
        self.raw_data_dir = Path(raw_data_dir)

    def parse_json(self, file_path: Path, default_department: Department) -> List[KnowledgeChunk]:
        """Parses a structured JSON file into KnowledgeChunks."""
        chunks = []
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            for i, item in enumerate(data):
                content = item.get("content", "")
                if not content.strip():
                    continue

                # Map legacy type field to KnowledgeType
                raw_type = item.get("type", "SOP").upper()
                type_map = {
                    "FACT": KnowledgeType.GLOSSARY,
                    "PROCESS": KnowledgeType.SOP,
                    "NEGATIVE_EXAMPLE": KnowledgeType.FAILURE_PATTERN,
                    "RULE": KnowledgeType.POLICY,
                }
                k_type = type_map.get(raw_type, KnowledgeType.SOP)
                # Also try direct enum match
                try:
                    k_type = KnowledgeType(raw_type)
                except ValueError:
                    pass

                dept_str = item.get("department", default_department.value)
                try:
                    dept = Department(dept_str.lower())
                except ValueError:
                    dept = default_department

                chunk = KnowledgeChunk(
                    id=item.get("id", str(uuid.uuid4())),
                    department=dept,
                    knowledge_type=k_type,
                    source_type=SourceType.JSON,
                    source_identifier=str(file_path.name),
                    title=content[:60].strip().rstrip(".,;:"),
                    content=content,
                    summary=content[:120] + ("..." if len(content) > 120 else ""),
                    tags=item.get("tags", []),
                    metadata=KnowledgeMetadata(
                        confidence_score=item.get("importance_score", 0.8),
                        source_reliability=1.0,
                        source_position=SourcePosition(file_path=str(file_path))
                    ),
                    processing_layer=ProcessingLayer.RAW,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                chunks.append(chunk)
        return chunks

    def parse_markdown(self, file_path: Path, default_department: Department) -> List[KnowledgeChunk]:
        """Parses a markdown file by splitting on H2 headers into KnowledgeChunks."""
        chunks = []
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Split by H2 headers
        sections = content.split("\n## ")

        for i, section in enumerate(sections):
            if not section.strip():
                continue

            if i == 0 and not content.startswith("## "):
                chunk_content = section.strip()
                # Extract title from H1 if present
                title = "Introduction"
                if chunk_content.startswith("# "):
                    title_line = chunk_content.split("\n")[0]
                    title = title_line.lstrip("# ").strip()
            else:
                chunk_content = "## " + section.strip()
                # Extract H2 header as title
                title_line = chunk_content.split("\n")[0]
                title = title_line.lstrip("# ").strip()

            if len(chunk_content.split()) < 5:
                continue

            # Classify type from content
            lower_content = chunk_content.lower()
            if "step" in lower_content or "process" in lower_content or any(f"{n}." in lower_content for n in range(1, 10)):
                k_type = KnowledgeType.SOP
            elif "never" in lower_content or "do not" in lower_content or "don't" in lower_content:
                k_type = KnowledgeType.FAILURE_PATTERN
            elif "policy" in lower_content or "rule" in lower_content:
                k_type = KnowledgeType.POLICY
            else:
                k_type = KnowledgeType.SOP

            chunk = KnowledgeChunk(
                id=str(uuid.uuid4()),
                department=default_department,
                knowledge_type=k_type,
                source_type=SourceType.MARKDOWN,
                source_identifier=str(file_path.name),
                title=title,
                content=chunk_content,
                summary=chunk_content[:120].replace("\n", " ") + ("..." if len(chunk_content) > 120 else ""),
                tags=[],
                metadata=KnowledgeMetadata(
                    confidence_score=0.9,
                    source_reliability=1.0,
                    source_position=SourcePosition(
                        file_path=str(file_path),
                        section_header=title
                    )
                ),
                processing_layer=ProcessingLayer.RAW,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            chunks.append(chunk)

        return chunks

    def parse_text(self, file_path: Path, default_department: Department) -> List[KnowledgeChunk]:
        """Parses a plain text file into a single KnowledgeChunk."""
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if len(content.split()) < 5:
            return []

        title = file_path.stem.replace("_", " ").replace("-", " ").title()

        chunk = KnowledgeChunk(
            id=str(uuid.uuid4()),
            department=default_department,
            knowledge_type=KnowledgeType.SOP,
            source_type=SourceType.LOCAL_FILE,
            source_identifier=str(file_path.name),
            title=title,
            content=content,
            summary=content[:120].replace("\n", " ") + ("..." if len(content) > 120 else ""),
            tags=[],
            metadata=KnowledgeMetadata(
                confidence_score=0.85,
                source_reliability=1.0,
                source_position=SourcePosition(file_path=str(file_path))
            ),
            processing_layer=ProcessingLayer.RAW,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        return [chunk]

    def ingest_directory(self, recursive: bool = True) -> List[KnowledgeChunk]:
        """
        Scans the raw_data directory and parses all supported files.
        Supports JSON, Markdown, and plain text files.
        Optionally scans recursively into subdirectories.
        """
        all_chunks = []
        if not self.raw_data_dir.exists():
            print(f"[Layer 1] Warning: Directory {self.raw_data_dir} does not exist.")
            return all_chunks

        pattern = "**/*.*" if recursive else "*.*"

        for file_path in sorted(self.raw_data_dir.glob(pattern)):
            if not file_path.is_file():
                continue
            # Skip hidden files and directories relative to raw_data_dir
            try:
                rel_parts = file_path.relative_to(self.raw_data_dir).parts
                if any(part.startswith(".") for part in rel_parts):
                    continue
            except ValueError:
                pass

            # Infer department from filename
            dept = Department.SHARED
            for d in Department:
                if d.value in file_path.name.lower():
                    dept = d
                    break

            print(f"[Layer 1] Ingesting: {file_path.name} (department: {dept.value})")

            try:
                if file_path.suffix == ".json":
                    all_chunks.extend(self.parse_json(file_path, dept))
                elif file_path.suffix in (".md", ".markdown"):
                    all_chunks.extend(self.parse_markdown(file_path, dept))
                elif file_path.suffix == ".txt":
                    all_chunks.extend(self.parse_text(file_path, dept))
                else:
                    pass  # Silently skip unsupported formats
            except Exception as e:
                print(f"[Layer 1] Error parsing {file_path.name}: {e}")

        print(f"[Layer 1] Ingested {len(all_chunks)} chunks from {self.raw_data_dir}")
        return all_chunks
