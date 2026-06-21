import os
from typing import List, Optional, Any
from pydantic import BaseModel
from core.models import SkillDef, SkillMetadata
try:
    from providers.router import ProviderRouter
    from providers import AIRequest
    _ROUTER_AVAILABLE = True
except ImportError:
    _ROUTER_AVAILABLE = False

# Phase 3: hash cache
try:
    from providers.hash_cache import HashCache
    _PHASE3_AVAILABLE = True
except ImportError:
    _PHASE3_AVAILABLE = False

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
    def __init__(self, router=None, cache=None, db_path=None):
        if router:
            self.router = router
        elif _ROUTER_AVAILABLE:
            self.router = ProviderRouter()
        else:
            self.router = None
        self.client = self.router

        self._cache = cache
        if self._cache is None and _PHASE3_AVAILABLE and db_path:
            self._cache = HashCache(db_path=db_path)

    def determine_tools(self, skill: SkillDef) -> List[str]:
        """Analyzes a skill definition and determines required MCP servers."""
        import asyncio
        import json as json_mod

        if not self.client:
            return self._mock_determine(skill)

        # Build context
        skill_text = f"Name: {skill.name}\nDescription: {skill.description}\nPrerequisites: {skill.prerequisites}\nSteps: {skill.steps}"
        
        # ── Phase 3: cache check ──────────────────────────────────────────────
        cache_key = None
        if self._cache is not None:
            cache_key = HashCache.make_key(
                prompt=skill_text,
                provider="router",
                model="tool-prov-v1",
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                try:
                    result = ToolRequirement(**cached)
                    return result.mcp_servers
                except Exception:
                    pass

        try:
            request = AIRequest(
                purpose="classify",
                messages=[
                    {"role": "system", "content": f"You are an AI tool provisioner. Analyze the skill definition and select the necessary MCP servers from this list: {', '.join(AVAILABLE_MCP_SERVERS)}. Return only the names of the required servers."},
                    {"role": "user", "content": skill_text}
                ],
                response_schema=ToolRequirement,
                temperature=0.3,
            )

            try:
                response = asyncio.run(self.router.complete(request))
            except RuntimeError:
                try:
                    import nest_asyncio
                    nest_asyncio.apply()
                    loop = asyncio.get_event_loop()
                    response = loop.run_until_complete(self.router.complete(request))
                except ImportError:
                    return self._mock_determine(skill)

            if response.error:
                return self._mock_determine(skill)

            mcp_servers = []
            if response.parsed and hasattr(response.parsed, 'mcp_servers'):
                mcp_servers = response.parsed.mcp_servers
            elif response.content:
                try:
                    data = json_mod.loads(response.content)
                    result = ToolRequirement(**data)
                    mcp_servers = result.mcp_servers
                except Exception:
                    pass

            if mcp_servers:
                # ── Phase 3: write to cache ───────────────────────────────────────────
                if self._cache is not None and cache_key:
                    self._cache.set(cache_key, {"mcp_servers": mcp_servers, "reasoning": "cached"})
                return mcp_servers

            return self._mock_determine(skill)
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
