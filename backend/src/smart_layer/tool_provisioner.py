import os
from typing import List
from pydantic import BaseModel
from core.models import SkillDef, SkillMetadata
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

class ToolRequirement(BaseModel):
    mcp_servers: List[str]
    reasoning: str

AVAILABLE_MCP_SERVERS = [
    "notion",
    "slack",
    "github",
    "linear",
    "vercel",
    "google_drive",
    "jira"
]

class ToolProvisioner:
    def __init__(self):
        self.client = OpenAI() if OpenAI and os.getenv("OPENAI_API_KEY") else None

    def determine_tools(self, skill: SkillDef) -> List[str]:
        """Analyzes a skill definition and determines required MCP servers."""
        if not self.client:
            return self._mock_determine(skill)

        # Build context
        skill_text = f"Name: {skill.name}\nDescription: {skill.description}\nPrerequisites: {skill.prerequisites}\nSteps: {skill.steps}"
        
        try:
            response = self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": f"You are an AI tool provisioner. Analyze the skill definition and select the necessary MCP servers from this list: {', '.join(AVAILABLE_MCP_SERVERS)}. Return only the names of the required servers."},
                    {"role": "user", "content": skill_text}
                ],
                response_format=ToolRequirement,
            )
            return response.choices[0].message.parsed.mcp_servers
        except Exception as e:
            print(f"Tool provisioning failed: {e}. Falling back to mock.")
            return self._mock_determine(skill)

    def _mock_determine(self, skill: SkillDef) -> List[str]:
        servers = set()
        text = str(skill.model_dump()).lower()
        
        if "github" in text or "pull request" in text or "merge" in text or "branch" in text:
            servers.add("github")
        if "vercel" in text or "deploy" in text:
            servers.add("vercel")
        if "notion" in text or "wiki" in text:
            servers.add("notion")
        if "slack" in text or "message" in text:
            servers.add("slack")
            
        return list(servers)
        
    def provision_skill(self, skill: SkillDef) -> SkillDef:
        """Injects required MCP servers into the skill metadata."""
        servers = self.determine_tools(skill)
        
        if servers:
            # The SKILL.md spec usually supports a single primary mcp-server in metadata, 
            # or a comma-separated list. We'll join them.
            mcp_str = ", ".join(servers)
            
            if not skill.metadata:
                from core.models import SkillMetadata
                skill.metadata = SkillMetadata(author="knowledge-layer", mcp_server=mcp_str)
            else:
                skill.metadata.mcp_server = mcp_str
                
        return skill
