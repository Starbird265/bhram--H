"""
One-time migration: Import existing chunks.json into SQLite.

Usage:
    python -m storage.migrate_json_to_sqlite [--db-path database]

Reads chunks.json from the db_path directory and imports all
KnowledgeChunks into the new SQLite store. Safe to run multiple
times — uses INSERT OR IGNORE so duplicates are skipped.
"""

import os
import sys
import json
import argparse
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import KnowledgeChunk
from storage.sqlite_store import SQLiteStore


def migrate(db_path: str):
    """Migrate chunks.json → bhrm.db"""
    chunks_file = Path(db_path) / "chunks.json"

    if not chunks_file.exists():
        print(f"No chunks.json found at {chunks_file}. Nothing to migrate.")
        return 0

    # Load existing JSON data
    with open(chunks_file, "r") as f:
        raw_data = json.load(f)

    if not raw_data:
        print("chunks.json is empty. Nothing to migrate.")
        return 0

    print(f"Found {len(raw_data)} chunks in chunks.json")

    # Initialize SQLite store
    store = SQLiteStore(db_path=db_path)

    # Import each chunk
    imported = 0
    skipped = 0
    errors = 0

    for item in raw_data:
        try:
            chunk = KnowledgeChunk(**item)
            # Check if already exists
            existing = store.get_chunk_by_id(chunk.id)
            if existing:
                skipped += 1
                continue
            store.save_chunk(chunk)
            imported += 1
        except Exception as e:
            errors += 1
            print(f"  Error importing chunk: {e}")

    print(f"\nMigration complete:")
    print(f"  Imported: {imported}")
    print(f"  Skipped (already in DB): {skipped}")
    print(f"  Errors: {errors}")
    print(f"  SQLite DB: {store.db_file}")

    # Rename old file as backup
    if imported > 0:
        backup = chunks_file.with_suffix(".json.bak")
        chunks_file.rename(backup)
        print(f"  Backed up original to: {backup}")

    return imported


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate chunks.json to SQLite")
    parser.add_argument(
        "--db-path", default=str(Path(__file__).resolve().parent.parent.parent / "database"),
        help="Path to the database directory"
    )
    args = parser.parse_args()
    migrate(args.db_path)
