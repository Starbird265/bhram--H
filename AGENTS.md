# AGENTS.md — Developer Instructions

Welcome, Agent. This file contains instructions and tips for working with the Cortex AI codebase.

## 1. 11-Layer Pipeline
Always respect the 11-layer architecture when adding new processing logic. New layers should be inserted into the sequence in `backend/src/main.py`.

## 2. Memory Tiers
Data flows through 8 specific memory tiers defined in `core/models.py`. Use the `MemoryManager` in `backend/src/memory/` to interact with these tiers rather than querying SQLite directly whenever possible.

## 3. CLI Usage
Use the `click` CLI in `main.py` for testing the pipeline:
```bash
python main.py --sources ./my_data --verbose
```

## 4. Connectivity Logs
Monitor `connection.log` for debugging issues with Slack, Notion, or External Agent handshakes.

## 5. Verification
Before submitting changes, ensure you have:
1. Checked for layer numbering consistency.
2. Verified that Pydantic models match the SQLite schema.
3. Tested the CLI with mock data.
