
"""
Layer 11: Agent Tool Gateway

Filters MCP server tools based on agent's allowlist/denylist.
Enforces the security principle: if an agent doesn't need a tool,
it must not have it.

Rules:
  1. Deny ALWAYS wins over allow (firewall model)
  2. Empty allowlist = all tools allowed (minus denylist)
  3. Non-empty allowlist = only listed tools visible
  4. Sensitivity ceiling is enforced on tool results
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from core.models import AgentProfile, SensitivityLevel


# ── Sensitivity Level Ordering ────────────────────────────────────
_SENSITIVITY_ORDER = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


@dataclass
class MCPTool:
    """Represents a tool from an MCP server."""
    name: str
    description: str = ""
    server_url: str = ""
    parameters: Dict[str, Any] = None

    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}


@dataclass
class ToolResult:
    """Represents a result returned by a tool execution."""
    tool_name: str
    content: Any
    sensitivity_level: str = "public"


class SensitivityViolationError(Exception):
    """Raised when an agent attempts to access data above its sensitivity ceiling."""
    pass


class AgentToolGateway:
    """
    Wraps MCP server connections and filters available tools
    based on the calling agent's AgentProfile.

    This is the core security primitive for agent tool access.
    """

    def get_filtered_tools(
        self,
        agent: AgentProfile,
        available_tools: List[MCPTool],
    ) -> List[MCPTool]:
        """
        Filter tools based on agent's allowlist/denylist.

        Args:
            agent: The AgentProfile requesting tools
            available_tools: Full list of tools from MCP server(s)

        Returns:
            List of tools the agent is permitted to see/use
        """
        visible_tools = []

        for tool in available_tools:
            # Rule 1: Deny list ALWAYS wins — skip immediately
            if tool.name in agent.tools_denylist:
                continue

            # Rule 2: If allowlist is non-empty, tool must be on it
            if agent.tools_allowlist and tool.name not in agent.tools_allowlist:
                continue

            visible_tools.append(tool)

        return visible_tools

    def check_result_sensitivity(
        self,
        agent: AgentProfile,
        result: ToolResult,
    ) -> ToolResult:
        """
        Verify that a tool result's sensitivity level does not
        exceed the agent's ceiling.

        Raises SensitivityViolationError if the data is too sensitive.
        """
        agent_ceiling = _SENSITIVITY_ORDER.get(agent.sensitivity_ceiling, 1)
        result_level = _SENSITIVITY_ORDER.get(result.sensitivity_level, 0)

        if result_level > agent_ceiling:
            raise SensitivityViolationError(
                f"Agent '{agent.agent_id}' has sensitivity ceiling "
                f"'{agent.sensitivity_ceiling}' but received "
                f"'{result.sensitivity_level}' data from tool '{result.tool_name}'."
            )

        return result

    def get_tool_manifest(self, agent: AgentProfile) -> List[Dict[str, Any]]:
        """
        Build a JSON-serializable tool manifest for the agent.
        Used by the frontend to show which tools an agent has access to.
        """
        manifest = []
        for tool_name in agent.tools_allowlist:
            if tool_name not in agent.tools_denylist:
                manifest.append({
                    "name": tool_name,
                    "allowed": True,
                })

        for tool_name in agent.tools_denylist:
            manifest.append({
                "name": tool_name,
                "allowed": False,
                "reason": "denied by policy",
            })

        return manifest
