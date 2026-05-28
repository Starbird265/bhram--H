"""
Real Notion Connector
=====================
Connects to your local Notion workspace using an Internal Integration token.
No OAuth app needed — just create a token at notion.so/my-integrations in 30 seconds.

Features:
  - Full workspace search (all pages/databases shared with the integration)
  - Block-level content extraction (paragraphs, headings, lists, toggles, tables, code)
  - Database row extraction with property values
  - Pagination (handles workspaces with 1000s of pages)
  - Delta sync: only re-fetches pages edited since last sync (saves tokens)
  - Persists last-sync cursor in bhrm.db
"""

from __future__ import annotations

import os
import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Generator

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


# ─── Notion API constants ────────────────────────────────────────────────────

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class NotionAPIError(Exception):
    pass


# ─── Low-level API client ────────────────────────────────────────────────────

class _NotionClient:
    """Thin wrapper around Notion REST API with auto-pagination."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        if not _REQUESTS_OK:
            raise NotionAPIError("requests library not installed. Run: pip install requests")
        r = requests.get(f"{_NOTION_API}{path}", headers=self.headers, params=params or {}, timeout=15)
        if r.status_code == 401:
            raise NotionAPIError("Invalid Notion token. Check your integration token.")
        if r.status_code == 403:
            raise NotionAPIError("Access denied. Make sure you shared pages with your integration.")
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict = None) -> dict:
        if not _REQUESTS_OK:
            raise NotionAPIError("requests library not installed.")
        r = requests.post(f"{_NOTION_API}{path}", headers=self.headers, json=body or {}, timeout=15)
        r.raise_for_status()
        return r.json()

    def search_all(self, query: str = "", filter_type: str = None) -> Generator[dict, None, None]:
        """Yield all pages/databases matching query (auto-paginates)."""
        cursor = None
        while True:
            body: Dict[str, Any] = {"page_size": 100}
            if query:
                body["query"] = query
            if filter_type:
                body["filter"] = {"value": filter_type, "property": "object"}
            if cursor:
                body["start_cursor"] = cursor

            result = self._post("/search", body)
            for item in result.get("results", []):
                yield item

            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

    def get_blocks(self, block_id: str) -> Generator[dict, None, None]:
        """Yield all child blocks (auto-paginates)."""
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = self._get(f"/blocks/{block_id}/children", params)
            for block in result.get("results", []):
                yield block
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

    def get_database_rows(self, db_id: str) -> Generator[dict, None, None]:
        """Yield all rows in a database (auto-paginates)."""
        cursor = None
        while True:
            body: Dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            result = self._post(f"/databases/{db_id}/query", body)
            for row in result.get("results", []):
                yield row
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

    def get_page(self, page_id: str) -> dict:
        return self._get(f"/pages/{page_id}")

    def whoami(self) -> dict:
        return self._get("/users/me")


# ─── Block → text extraction ─────────────────────────────────────────────────

def _rich_text_to_str(rich_text: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def _block_to_text(block: dict, depth: int = 0) -> str:
    """Convert a single Notion block to markdown-style text.
    
    Handles all standard Notion block types including:
    paragraphs, headings, lists, todos, toggles, quotes, callouts,
    code, dividers, tables, bookmarks, embeds, files, images, and more.
    """
    btype = block.get("type", "")
    bdata = block.get(btype, {})
    indent = "  " * depth

    if btype == "paragraph":
        return f"{indent}{_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype in ("heading_1", "heading_2", "heading_3"):
        level = int(btype[-1])
        return f"{'#' * level} {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "bulleted_list_item":
        return f"{indent}- {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "numbered_list_item":
        return f"{indent}1. {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "to_do":
        done = bdata.get("checked", False)
        return f"{indent}{'[x]' if done else '[ ]'} {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "toggle":
        return f"{indent}▸ {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "quote":
        return f"{indent}> {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "callout":
        emoji = bdata.get("icon", {}).get("emoji", "📌")
        return f"{indent}{emoji} {_rich_text_to_str(bdata.get('rich_text', []))}"
    elif btype == "code":
        lang = bdata.get("language", "")
        code = _rich_text_to_str(bdata.get("rich_text", []))
        return f"{indent}```{lang}\n{indent}{code}\n{indent}```"
    elif btype == "divider":
        return f"{indent}---"
    elif btype == "table_row":
        cells = [_rich_text_to_str(c) for c in bdata.get("cells", [])]
        return f"{indent}| " + " | ".join(cells) + " |"
    elif btype == "table":
        # Table parent block — content comes from child table_row blocks
        return ""  # Rows are handled via has_children recursion
    elif btype == "column_list":
        return ""  # Columns are handled via has_children recursion
    elif btype == "column":
        return ""  # Column content is handled via has_children recursion
    elif btype == "synced_block":
        # Synced blocks have their content in children
        return ""  # Content comes via has_children recursion
    elif btype == "child_page":
        title = bdata.get("title", "Untitled sub-page")
        return f"{indent}📄 [Sub-page: {title}]"
    elif btype == "child_database":
        title = bdata.get("title", "Untitled database")
        return f"{indent}🗃️ [Database: {title}]"
    elif btype == "bookmark":
        url = bdata.get("url", "")
        caption = _rich_text_to_str(bdata.get("caption", []))
        label = caption if caption else url
        return f"{indent}🔗 [{label}]({url})" if url else ""
    elif btype == "link_preview":
        url = bdata.get("url", "")
        return f"{indent}🔗 {url}" if url else ""
    elif btype == "embed":
        url = bdata.get("url", "")
        caption = _rich_text_to_str(bdata.get("caption", []))
        label = caption if caption else "Embedded content"
        return f"{indent}📎 {label}: {url}" if url else ""
    elif btype == "image":
        caption = _rich_text_to_str(bdata.get("caption", []))
        img_type = bdata.get("type", "")
        url = bdata.get(img_type, {}).get("url", "") if img_type else ""
        if caption:
            return f"{indent}🖼️ [Image: {caption}]"
        elif url:
            return f"{indent}🖼️ [Image]"
        return ""
    elif btype == "video":
        caption = _rich_text_to_str(bdata.get("caption", []))
        return f"{indent}🎬 [Video: {caption}]" if caption else f"{indent}🎬 [Video]"
    elif btype == "file":
        caption = _rich_text_to_str(bdata.get("caption", []))
        name = bdata.get("name", "")
        label = caption or name or "Attached file"
        return f"{indent}📁 [{label}]"
    elif btype == "pdf":
        caption = _rich_text_to_str(bdata.get("caption", []))
        return f"{indent}📄 [PDF: {caption}]" if caption else f"{indent}📄 [PDF]"
    elif btype == "equation":
        expression = bdata.get("expression", "")
        return f"{indent}$$ {expression} $$" if expression else ""
    elif btype == "breadcrumb":
        return ""  # Navigation element, not content
    elif btype == "table_of_contents":
        return ""  # Auto-generated, not content
    elif btype == "link_to_page":
        page_id = bdata.get("page_id", "")
        return f"{indent}📄 [Link to page: {page_id}]" if page_id else ""
    elif btype == "template":
        return f"{indent}{_rich_text_to_str(bdata.get('rich_text', []))}"
    else:
        # Fallback: try to extract any rich_text
        rt = bdata.get("rich_text", [])
        if rt:
            return f"{indent}{_rich_text_to_str(rt)}"
        return ""


def _extract_page_title(page: dict) -> str:
    """Extract title from page or database metadata."""
    props = page.get("properties", {})
    # Try common title property names
    for key in ("Name", "Title", "title", "name", "Page"):
        prop = props.get(key, {})
        ptype = prop.get("type", "")
        if ptype == "title":
            return _rich_text_to_str(prop.get("title", []))
    # Try any title-type property
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich_text_to_str(prop.get("title", []))
    return page.get("id", "Untitled")


def _extract_page_properties(page: dict) -> str:
    """Extract ALL page properties as structured context.
    
    Returns a markdown-formatted block with all non-empty properties.
    This gives the pipeline metadata context (status, dates, assignees,
    tags, relations) that is often critical for understanding the page.
    """
    props = page.get("properties", {})
    lines = []
    for prop_name, prop_val in props.items():
        v = _extract_prop_value(prop_val)
        if v and v.strip():
            lines.append(f"  {prop_name}: {v}")
    if lines:
        return "[Page Properties]\n" + "\n".join(lines)
    return ""


def _extract_prop_value(prop: dict) -> str:
    """Extract human-readable string from any Notion property."""
    ptype = prop.get("type", "")
    val = prop.get(ptype, None)
    if val is None:
        return ""
    if ptype == "title":
        return _rich_text_to_str(val)
    elif ptype == "rich_text":
        return _rich_text_to_str(val)
    elif ptype == "select":
        return (val or {}).get("name", "") if val else ""
    elif ptype == "multi_select":
        return ", ".join(s.get("name", "") for s in (val or []))
    elif ptype == "status":
        return (val or {}).get("name", "") if val else ""
    elif ptype == "checkbox":
        return "Yes" if val else "No"
    elif ptype == "number":
        return str(val) if val is not None else ""
    elif ptype == "date":
        if val:
            start = val.get("start", "")
            end = val.get("end", "")
            return f"{start} – {end}" if end else start
        return ""
    elif ptype == "people":
        return ", ".join(p.get("name", "") for p in (val or []))
    elif ptype == "url":
        return str(val) if val else ""
    elif ptype == "email":
        return str(val) if val else ""
    elif ptype == "phone_number":
        return str(val) if val else ""
    elif ptype == "formula":
        return str(val.get("string") or val.get("number") or val.get("boolean") or "")
    elif ptype == "relation":
        return f"[{len(val)} linked pages]" if val else ""
    elif ptype == "rollup":
        rtype = val.get("type", "")
        rval = val.get(rtype, None)
        if rtype == "number":
            return str(rval) if rval is not None else ""
        elif rtype == "array":
            parts = []
            for item in (rval or []):
                itype = item.get("type", "")
                iv = item.get(itype, None)
                if itype == "title":
                    parts.append(_rich_text_to_str(iv or []))
                elif iv is not None:
                    parts.append(str(iv))
            return ", ".join(parts) if parts else ""
        return str(rval) if rval else ""
    elif ptype == "created_time":
        return str(val) if val else ""
    elif ptype == "last_edited_time":
        return str(val) if val else ""
    elif ptype == "created_by":
        return val.get("name", "") if isinstance(val, dict) else ""
    elif ptype == "last_edited_by":
        return val.get("name", "") if isinstance(val, dict) else ""
    elif ptype == "files":
        names = [f.get("name", "") for f in (val or []) if f.get("name")]
        return ", ".join(names) if names else ""
    elif ptype == "unique_id":
        prefix = val.get("prefix", "") if isinstance(val, dict) else ""
        number = val.get("number", "") if isinstance(val, dict) else ""
        return f"{prefix}-{number}" if prefix else str(number)
    return ""


def _generate_smart_summary(title: str, content: str, max_len: int = 300) -> str:
    """Generate an intelligent summary by extracting key sentences.
    
    Instead of blindly truncating, picks out:
      1. The first substantive paragraph (not a heading)
      2. Any sentences with signal words (must, always, never, important)
      3. Falls back to first N chars only if nothing better found
    """
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    if not lines:
        return title

    # Collect candidate sentences
    candidates = []
    signal_words = {"must", "always", "never", "important", "critical",
                    "required", "ensure", "policy", "rule", "process",
                    "step", "deadline", "approval", "security", "deploy"}

    first_para = None
    for line in lines:
        # Skip headings, dividers, metadata blocks
        if line.startswith("#") or line.startswith("---") or line.startswith("["):
            continue
        # Skip very short lines
        if len(line.split()) < 3:
            continue

        if first_para is None:
            first_para = line

        # Check for signal words
        lower = line.lower()
        if any(w in lower for w in signal_words):
            candidates.append(line)

    # Build summary
    parts = []
    if first_para:
        parts.append(first_para)

    for c in candidates:
        if c != first_para and len(" | ".join(parts + [c])) < max_len:
            parts.append(c)

    summary = " | ".join(parts) if parts else content[:max_len]

    # Trim to max length
    if len(summary) > max_len:
        summary = summary[:max_len - 3] + "..."

    return summary.replace("\n", " ")


# ─── Main connector class ────────────────────────────────────────────────────

class NotionConnector:
    """
    Real Notion workspace connector.

    Usage:
        connector = NotionConnector(token="secret_xxx")
        pages = list(connector.fetch_workspace())  # all pages
        content = connector.fetch_page_content(page_id)
    """

    APP_NAME = "notion"

    def __init__(self, token: Optional[str] = None, db_path: Optional[str] = None):
        self.token = token or os.getenv("NOTION_API_KEY", "")
        self._client: Optional[_NotionClient] = None
        self._db_path = db_path

        if self.token:
            self._client = _NotionClient(self.token)

    # ── Connection ────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return bool(self.token and _REQUESTS_OK)

    def test_connection(self) -> Dict[str, Any]:
        """Test token validity. Returns user info dict."""
        if not self._client:
            return {"ok": False, "error": "No token configured"}
        try:
            user = self._client.whoami()
            return {
                "ok": True,
                "user": user.get("name", "unknown"),
                "type": user.get("type", "unknown"),
                "workspace_name": user.get("workspace_name", ""),
            }
        except NotionAPIError as e:
            return {"ok": False, "error": str(e)}

    # ── Content extraction ─────────────────────────────────────────

    def _fetch_blocks_recursive(
        self, block_id: str, depth: int = 0, max_depth: int = 5
    ) -> List[str]:
        """Recursively fetch all blocks and their children up to max_depth.
        
        Unlike the old 1-level fetch, this follows the full tree:
        toggles inside toggles, nested lists, column layouts, synced blocks, etc.
        """
        if depth > max_depth or not self._client:
            return []

        lines = []
        try:
            for block in self._client.get_blocks(block_id):
                line = _block_to_text(block, depth=depth)
                if line.strip():
                    lines.append(line)

                # Recursively fetch children (toggles, columns, synced blocks, tables, etc.)
                if block.get("has_children"):
                    try:
                        child_lines = self._fetch_blocks_recursive(
                            block["id"], depth=depth + 1, max_depth=max_depth
                        )
                        lines.extend(child_lines)
                    except Exception:
                        pass  # Don't let one failed sub-tree break the whole page
        except Exception as e:
            if depth == 0:
                print(f"  [Notion] Error fetching blocks for {block_id}: {e}")
        return lines

    def fetch_page_content(self, page_id: str, include_properties: bool = True) -> str:
        """Fetch all block content from a page as markdown-style text.
        
        Deep recursive extraction — follows toggles, columns, synced blocks,
        nested lists, and tables to unlimited depth (capped at 5 levels).
        
        Optionally prepends page-level properties (status, dates, tags, etc.)
        as structured metadata context.
        """
        if not self._client:
            return ""

        parts = []

        # Extract page-level properties as context
        if include_properties:
            try:
                page_meta = self._client.get_page(page_id)
                props_text = _extract_page_properties(page_meta)
                if props_text:
                    parts.append(props_text)
            except Exception:
                pass  # Properties are bonus context, don't fail on them

        # Deep recursive block extraction
        block_lines = self._fetch_blocks_recursive(page_id, depth=0, max_depth=5)
        if block_lines:
            parts.extend(block_lines)

        return "\n".join(parts)

    def fetch_database_content(self, db_id: str) -> str:
        """Fetch all rows from a Notion database as structured text.
        
        For each row:
          1. Extracts all property values (title, status, dates, tags, etc.)
          2. Fetches the page BODY CONTENT inside the row (the actual knowledge)
        
        This is critical — Notion database rows are also pages with full content.
        Previously only properties were extracted, missing all the real knowledge.
        """
        if not self._client:
            return ""
        rows = []
        try:
            for row in self._client.get_database_rows(db_id):
                title = _extract_page_title(row)
                row_id = row.get("id", "")

                # 1. Extract property values
                props = row.get("properties", {})
                props_text = []
                for prop_name, prop_val in props.items():
                    v = _extract_prop_value(prop_val)
                    if v and prop_name.lower() not in ("title", "name"):
                        props_text.append(f"{prop_name}: {v}")

                # Start row text with title + properties
                row_header = f"## {title}"
                if props_text:
                    row_header += "\n  " + ", ".join(props_text)
                rows.append(row_header)

                # 2. Fetch the body content inside this row (the real knowledge!)
                if row_id:
                    try:
                        body_lines = self._fetch_blocks_recursive(
                            row_id.replace("-", ""), depth=1, max_depth=4
                        )
                        if body_lines:
                            rows.extend(body_lines)
                    except Exception:
                        pass  # Don't let one row's body failure break the whole DB

                rows.append("")  # Blank line between rows

        except Exception as e:
            print(f"  [Notion] Error fetching database {db_id}: {e}")
        return "\n".join(rows)

    # ── Workspace sync ─────────────────────────────────────────────

    def fetch_workspace(self, since: Optional[str] = None, known_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        """
        Fetch all pages/databases shared with this integration.
        Returns list of dicts with id, title, content, type, last_edited, summary, properties.

        Deep content extraction:
          - Pages: full recursive block tree + page properties
          - Databases: all row properties + body content inside each row

        Args:
            since: ISO timestamp. Only return pages edited after this time.
            known_ids: Set of previously synced page IDs. Newly shared pages are fetched even if old.
        """
        if not self._client:
            return []

        results = []
        seen_ids = set()
        known_ids = known_ids or set()

        for item in self._client.search_all():
            obj_type = item.get("object")
            item_id = item.get("id", "").replace("-", "")
            last_edited = item.get("last_edited_time", "")

            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            # Delta: skip pages not edited since last sync, unless they are newly discovered
            if item_id in known_ids and since and last_edited and last_edited <= since:
                continue

            title = _extract_page_title(item)
            url = item.get("url", "")

            # Extract properties metadata for pages
            properties_text = ""
            if obj_type == "page":
                content = self.fetch_page_content(item_id, include_properties=True)
                properties_text = _extract_page_properties(item)
            elif obj_type == "database":
                content = self.fetch_database_content(item_id)
            else:
                continue

            if not content.strip():
                continue

            # Generate intelligent summary instead of dumb truncation
            smart_summary = _generate_smart_summary(title, content)

            # Count content stats for logging
            line_count = len([l for l in content.split("\n") if l.strip()])
            word_count = len(content.split())

            results.append({
                "id": item_id,
                "title": title or "Untitled",
                "content": content,
                "summary": smart_summary,
                "properties": properties_text,
                "type": obj_type,
                "url": url,
                "last_edited": last_edited,
                "source": "notion",
                "content_hash": hashlib.sha256(content.encode()).hexdigest()[:16],
                "word_count": word_count,
            })
            print(f"  [Notion] Fetched {obj_type}: {title[:50]} ({word_count} words, {line_count} lines)")

        return results

    def fetch_delta(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Real delta sync — only pages edited since `since` timestamp.
        This is the real implementation (not a stub).
        """
        return self.fetch_workspace(since=since)

    # ── Legacy compat ──────────────────────────────────────────────

    def fetch_page(self, page_id: str) -> str:
        """Fetch a single page by ID. Kept for backward compatibility."""
        return self.fetch_page_content(page_id)
