"""
Storage Layer: Filesystem Store
Thread-safe JSON-based storage for KnowledgeChunks.
Uses file locking to prevent data corruption from concurrent writes.
"""

import json
import os
import fcntl
from pathlib import Path
from typing import List
from core.models import KnowledgeChunk, Department


class FilesystemStore:
    def __init__(self, db_path: str = "database"):
        self.db_dir = Path(db_path)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_file = self.db_dir / "chunks.json"

        if not self.chunks_file.exists():
            with open(self.chunks_file, "w") as f:
                json.dump([], f)

    def _load_all(self) -> List[KnowledgeChunk]:
        with open(self.chunks_file, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)  # Shared lock for reading
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return [KnowledgeChunk(**item) for item in data]

    def _save_all(self, chunks: List[KnowledgeChunk]):
        with open(self.chunks_file, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # Exclusive lock for writing
            try:
                json.dump([c.model_dump(mode='json') for c in chunks], f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def save_chunk(self, chunk: KnowledgeChunk):
        chunks = self._load_all()
        for i, c in enumerate(chunks):
            if c.id == chunk.id:
                chunks[i] = chunk
                self._save_all(chunks)
                return
        chunks.append(chunk)
        self._save_all(chunks)

    def get_all_by_department(self, department: Department) -> List[KnowledgeChunk]:
        return [c for c in self._load_all() if c.department.value == department.value]

    def get_all(self) -> List[KnowledgeChunk]:
        return self._load_all()

    def delete_chunk(self, chunk_id: str):
        chunks = self._load_all()
        chunks = [c for c in chunks if c.id != chunk_id]
        self._save_all(chunks)
