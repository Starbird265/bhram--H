import json
from pathlib import Path
from typing import List, Dict

class SkillRegistry:
    def __init__(self, db_path: str = "database"):
        self.registry_file = Path(db_path) / "skill_registry.json"
        if not self.registry_file.exists():
            with open(self.registry_file, "w") as f:
                json.dump({}, f)

    def register_skill(self, skill_name: str, chunk_ids: List[str]):
        """Maps a skill to the knowledge chunks it was built from."""
        with open(self.registry_file, "r") as f:
            data = json.load(f)
            
        data[skill_name] = chunk_ids
        
        with open(self.registry_file, "w") as f:
            json.dump(data, f, indent=2)

    def get_chunks_for_skill(self, skill_name: str) -> List[str]:
        with open(self.registry_file, "r") as f:
            data = json.load(f)
        return data.get(skill_name, [])
        
    def get_skills_affected_by_chunk(self, chunk_id: str) -> List[str]:
        """If a chunk updates, which skills need to be recompiled?"""
        with open(self.registry_file, "r") as f:
            data = json.load(f)
            
        affected_skills = []
        for skill_name, chunk_ids in data.items():
            if chunk_id in chunk_ids:
                affected_skills.append(skill_name)
        return affected_skills

    def list_all_skills(self) -> List[str]:
        with open(self.registry_file, "r") as f:
            data = json.load(f)
        return list(data.keys())
