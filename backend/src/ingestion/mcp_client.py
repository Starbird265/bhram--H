"""
MCP Client for bhram--H pipeline.

Replaces all custom connector API code by spawning official MCP server packages
(e.g. @notionhq/notion-mcp-server) and calling their tools.

This is the same mechanism Claude Desktop uses — we just do it inside our pipeline.

Usage:
    client = MCPConnector.from_config("notion", mcp_config)
    pages = client.call_tool_sync("search", {"query": ""})
"""

import asyncio
import json
import os
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Tool name maps per connector ──────────────────────────────────
# Maps our generic operation names to each MCP server's actual tool names.
TOOL_MAP: dict[str, dict[str, str]] = {
    "notion": {
        "search":      "notion_search",
        "get_page":    "notion_retrieve_block_children",
        "list":        "notion_list_databases",
    },
    "slack": {
        "history":     "slack_get_channel_history",
        "channels":    "slack_list_channels",
        "replies":     "slack_get_thread_replies",
    },
    "github": {
        "get_file":    "get_file_contents",
        "search":      "search_repositories",
        "issues":      "list_issues",
        "prs":         "list_pull_requests",
        "readme":      "get_file_contents",
    },
    "google_drive": {
        "search":      "search",
        "read_file":   "read_file",
    },
    "linear": {
        "issues":      "list_issues",
        "get_issue":   "get_issue",
        "projects":    "list_projects",
        "teams":       "list_teams",
    },
}


class MCPConnector:
    """
    Spawns an official MCP server as a subprocess and calls its tools.

    Single class replaces: notion_connector.py, slack_connector.py,
    github_connector.py, google_drive_connector.py, linear_connector.py.

    Each call:
      1. Starts the MCP server process (via stdio transport)
      2. Initializes the MCP session
      3. Calls the tool
      4. Returns the text result
      5. Closes the session (process exits)
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str]):
        self.command = command
        self.args = args
        self.env = {**os.environ, **env}  # merge with current env

    @classmethod
    def from_config(cls, app_id: str, mcp_config: dict) -> Optional["MCPConnector"]:
        """
        Build an MCPConnector from an mcp_config.json entry.

        mcp_config format (same as Claude Desktop):
        {
          "command": "npx",
          "args": ["-y", "@notionhq/notion-mcp-server"],
          "env": {"OPENAPI_MCP_HEADERS": "..."}
        }
        """
        servers = mcp_config.get("mcpServers", {})
        server_cfg = servers.get(app_id)
        if not server_cfg:
            return None
        return cls(
            command=server_cfg["command"],
            args=server_cfg.get("args", []),
            env=server_cfg.get("env", {}),
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Async: spawn MCP server, call tool, return text result."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            raise RuntimeError("mcp package not installed. Run: pip install mcp>=1.27.0")

        server_params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
        )

        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)

                    # Extract text from result content
                    if result.content:
                        parts = []
                        for item in result.content:
                            if hasattr(item, "text"):
                                parts.append(item.text)
                            elif isinstance(item, dict):
                                parts.append(item.get("text", str(item)))
                        return "\n".join(parts)
                    return ""

        except Exception as e:
            logger.error(f"[MCPClient] Tool call failed ({tool_name}): {e}")
            return ""

    def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Sync wrapper for use in pipeline (non-async context)."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in async context (e.g. FastAPI) — use thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, self.call_tool(tool_name, arguments))
                    return future.result(timeout=60)
            else:
                return loop.run_until_complete(self.call_tool(tool_name, arguments))
        except Exception as e:
            logger.error(f"[MCPClient] sync call failed: {e}")
            return ""

    async def list_tools(self) -> list[str]:
        """List all tools exposed by this MCP server (for debugging)."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            return []

        server_params = StdioServerParameters(
            command=self.command, args=self.args, env=self.env
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]


# ── High-level ingestion functions ────────────────────────────────

def ingest_via_mcp_notion(connector: "MCPConnector", page_ids: list[str] = None) -> list[str]:
    """
    Fetch Notion content via official MCP server.
    Returns list of raw text strings (one per page).
    """
    results = []

    if not page_ids:
        # Search workspace for all shared pages
        raw = connector.call_tool_sync("notion_search", {"query": ""})
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
            page_ids = [r["id"] for r in data.get("results", []) if r.get("object") == "page"]
        except (json.JSONDecodeError, KeyError):
            logger.warning("[Notion MCP] Could not parse search results")
            return []

    for page_id in page_ids[:20]:  # cap at 20
        raw = connector.call_tool_sync("notion_retrieve_block_children", {
            "block_id": page_id.replace("-", ""),
        })
        if raw.strip():
            results.append(raw)

    return results


def ingest_via_mcp_slack(connector: "MCPConnector", channel_ids: list[str] = None) -> list[str]:
    """
    Fetch Slack messages via official MCP server.
    Returns list of raw text strings (one per channel).
    """
    results = []

    if not channel_ids:
        raw = connector.call_tool_sync("slack_list_channels", {"limit": 100})
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
            channel_ids = [c["id"] for c in data.get("channels", []) if not c.get("is_archived")]
        except (json.JSONDecodeError, KeyError):
            return []

    for channel_id in channel_ids[:10]:  # cap at 10
        raw = connector.call_tool_sync("slack_get_channel_history", {
            "channel_id": channel_id,
            "limit": 200,
        })
        if raw.strip():
            results.append(raw)

    return results


def ingest_via_mcp_github(connector: "MCPConnector", repos: list[str] = None) -> list[str]:
    """
    Fetch GitHub content via official MCP server.
    Returns list of raw text strings (READMEs, issues).
    """
    results = []

    if not repos:
        raw = connector.call_tool_sync("search_repositories", {
            "query": "user:@me sort:updated",
        })
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
            repos = [r["full_name"] for r in data.get("items", [])][:5]
        except (json.JSONDecodeError, KeyError):
            return []

    for repo in repos:
        owner, name = repo.split("/") if "/" in repo else (repo, repo)
        readme = connector.call_tool_sync("get_file_contents", {
            "owner": owner, "repo": name, "path": "README.md",
        })
        if readme.strip():
            results.append(f"# {repo} README\n{readme}")

    return results


def ingest_via_mcp_linear(connector: "MCPConnector", team_ids: list[str] = None) -> list[str]:
    """
    Fetch Linear issues via official MCP server.
    Returns list of raw text strings.
    """
    results = []

    args: dict[str, Any] = {"first": 50, "orderBy": "updatedAt"}
    if team_ids:
        args["teamId"] = team_ids[0]  # Linear MCP supports one team per call

    raw = connector.call_tool_sync("list_issues", args)
    if raw.strip():
        results.append(raw)

    return results
