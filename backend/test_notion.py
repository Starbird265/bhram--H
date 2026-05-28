#!/usr/bin/env python3
"""Quick Notion connection test — run after sharing pages with your integration."""
import os, sys

env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "ingestion"))
from notion_connector import NotionConnector

connector = NotionConnector()
result = connector.test_connection()
print(f"Connection: {'✅ OK' if result['ok'] else '❌ FAILED'}")
if result.get("ok"):
    print(f"Bot user : {result['user']}")
    print(f"Workspace: {result.get('workspace_name') or '(name hidden — normal for internal integrations)'}")
    print("\nScanning for shared pages...")
    pages = connector.fetch_workspace()
    print(f"\nFound {len(pages)} page(s) with content\n")
    for p in pages:
        print(f"  [{p['type']:8}] {p['title'][:60]}")
        print(f"             {len(p['content'])} chars | last edited: {p['last_edited'][:10]}")
else:
    print(f"Error: {result['error']}")
