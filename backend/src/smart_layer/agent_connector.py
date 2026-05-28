import json
import os
import requests
from pathlib import Path
from typing import List, Dict

class AgentConnector:
    """
    Manages the binding between AI Agents and Distilled Skills.
    This creates the closed-loop architecture where updated skills
    are automatically injected into agent context windows or tools.
    """
    def __init__(self, db_path: str = "database"):
        self.db_path = Path(db_path)
        self.registry_file = self.db_path / "agent_registry.json"
        
        if not self.registry_file.exists():
            with open(self.registry_file, "w") as f:
                # Default agents
                json.dump({
                    "code_reviewer": {"skills": []},
                    "architect": {"skills": []},
                    "debugger": {"skills": []},
                    "sec_auditor": {"skills": []}
                }, f)

    def get_agent_skills(self, agent_name: str) -> List[str]:
        """Returns the list of skills bound to the given agent."""
        with open(self.registry_file, "r") as f:
            data = json.load(f)
        return data.get(agent_name, {}).get("skills", [])

    def bind_skill_to_agent(self, agent_name: str, skill_name: str) -> bool:
        """
        Binds a generated skill to an agent. 
        In a real runtime, this would trigger a prompt-rebuild or tool-reconfiguration.
        """
        with open(self.registry_file, "r") as f:
            data = json.load(f)
            
        if agent_name not in data:
            data[agent_name] = {"skills": []}
            
        if skill_name not in data[agent_name]["skills"]:
            data[agent_name]["skills"].append(skill_name)
            
            with open(self.registry_file, "w") as f:
                json.dump(data, f, indent=2)
            return True
        return False

    def remove_skill_from_agent(self, agent_name: str, skill_name: str):
        with open(self.registry_file, "r") as f:
            data = json.load(f)
            
        if agent_name in data and skill_name in data[agent_name]["skills"]:
            data[agent_name]["skills"].remove(skill_name)
            with open(self.registry_file, "w") as f:
                json.dump(data, f, indent=2)

    def trigger_agent_reload(self, agent_name: str):
        """
        Triggers an actual webhook to reload the agent's context 
        window to absorb newly bound knowledge.
        Uses HMAC-signed headers for authenticated delivery.
        """
        skills = self.get_agent_skills(agent_name)
        print(f"[AgentConnector] Triggering hot-reload for agent '{agent_name}'.")
        print(f"[AgentConnector] Agent '{agent_name}' is now armed with skills: {skills}")
        
        webhook_url = os.getenv(f"AGENT_WEBHOOK_{agent_name.upper()}")
        if webhook_url:
            try:
                from middleware.webhook_security import build_authenticated_headers
                payload = {"agent": agent_name, "active_skills": skills, "event": "skill_reload"}
                headers = build_authenticated_headers(payload=payload)

                print(f"[AgentConnector] Sending signed payload to {webhook_url}...")
                response = requests.post(
                    webhook_url, json=payload, headers=headers, timeout=5
                )
                if response.status_code == 200:
                    print(f"[AgentConnector] Agent '{agent_name}' successfully reloaded.")
                else:
                    print(f"[AgentConnector] Failed to reload agent '{agent_name}'. Status: {response.status_code}")
            except Exception as e:
                print(f"[AgentConnector] Webhook error for agent '{agent_name}': {e}")
        else:
            print(f"[AgentConnector] No webhook configured for agent '{agent_name}'. Skipping live reload.")
