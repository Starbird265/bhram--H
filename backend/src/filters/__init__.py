"""
Declarative Filter DSL — Models and Engine.

Instead of using eval/exec on AI-generated Python, we define
a safe, structured filter language:

  FilterRule:    One condition (field + operator + value)
  FilterRecipe:  Named collection of rules with AND/OR logic + action

AI generates FilterRecipe JSON → we validate against the schema →
dry-run on data → user approves → save to registry.
"""

import re
import json
import uuid
from typing import List, Optional, Dict, Any, Union, Literal
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from core.models import AtomicKnowledgeUnit, KnowledgeChunk


# ─── Filter DSL Models ───────────────────────────────────────────

class FilterRule(BaseModel):
    """One filter condition."""
    field: Literal[
        "content", "title", "claim", "instruction",
        "tags", "department", "knowledge_type",
        "source_type", "source_identifier", "scope",
        "confidence_score", "sensitivity_level", "status",
        "entities", "tools_required",
    ]
    operator: Literal[
        "contains", "not_contains",
        "equals", "not_equals",
        "gt", "lt", "gte", "lte",
        "regex", "in", "not_in",
        "exists", "not_exists",
    ]
    value: Union[str, float, int, List[str], None] = None
    case_sensitive: bool = False


class FilterRecipe(BaseModel):
    """Named filter with multiple conditions and an action."""
    id: str = Field(default_factory=lambda: f"filter-{uuid.uuid4().hex[:8]}")
    name: str
    description: str = ""
    version: int = 1
    conditions: List[FilterRule]
    logic: Literal["AND", "OR"] = "AND"
    action: Literal["KEEP", "DROP", "MOVE", "TAG", "FLAG_REVIEW"] = "KEEP"
    action_params: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "user"  # "user" or "ai"


class FilterResult(BaseModel):
    """Result of running a filter on a dataset."""
    recipe_id: str
    recipe_name: str
    total_items: int
    matched_items: int
    action: str
    matched_ids: List[str]
    sample_matches: List[Dict[str, Any]] = Field(default_factory=list)


# ─── Filter Engine ───────────────────────────────────────────────

class FilterEngine:
    """Evaluates FilterRecipes against data — safe, no eval/exec."""

    def apply_to_units(
        self, recipe: FilterRecipe, units: List[AtomicKnowledgeUnit]
    ) -> FilterResult:
        """Apply a filter recipe to atomic knowledge units."""
        matched = []
        samples = []

        for unit in units:
            if self._matches(recipe, unit):
                matched.append(unit.id)
                if len(samples) < 5:
                    samples.append({
                        "id": unit.id,
                        "claim": unit.claim[:100],
                        "department": unit.department.value,
                        "confidence": unit.confidence_score,
                    })

        return FilterResult(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            total_items=len(units),
            matched_items=len(matched),
            action=recipe.action,
            matched_ids=matched,
            sample_matches=samples,
        )

    def apply_to_chunks(
        self, recipe: FilterRecipe, chunks: List[KnowledgeChunk]
    ) -> FilterResult:
        """Apply a filter recipe to knowledge chunks."""
        matched = []
        samples = []

        for chunk in chunks:
            if self._matches(recipe, chunk):
                matched.append(chunk.id)
                if len(samples) < 5:
                    samples.append({
                        "id": chunk.id,
                        "title": chunk.title[:100],
                        "department": chunk.department.value,
                    })

        return FilterResult(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            total_items=len(chunks),
            matched_items=len(matched),
            action=recipe.action,
            matched_ids=matched,
            sample_matches=samples,
        )

    def dry_run(
        self, recipe: FilterRecipe, data: List[Any]
    ) -> FilterResult:
        """Preview filter results without applying the action."""
        if data and isinstance(data[0], AtomicKnowledgeUnit):
            return self.apply_to_units(recipe, data)
        elif data and isinstance(data[0], KnowledgeChunk):
            return self.apply_to_chunks(recipe, data)
        return FilterResult(
            recipe_id=recipe.id, recipe_name=recipe.name,
            total_items=0, matched_items=0, action=recipe.action,
            matched_ids=[], sample_matches=[],
        )

    # ─── Rule evaluation ─────────────────────────────────────────

    def _matches(self, recipe: FilterRecipe, item: Any) -> bool:
        """Check if an item matches all/any conditions in the recipe."""
        results = [self._evaluate_rule(rule, item) for rule in recipe.conditions]

        if recipe.logic == "AND":
            return all(results)
        else:  # OR
            return any(results)

    def _evaluate_rule(self, rule: FilterRule, item: Any) -> bool:
        """Evaluate a single filter rule against an item."""
        # Get the field value from the item
        value = self._get_field_value(rule.field, item)

        if rule.operator == "exists":
            return value is not None and value != "" and value != []
        if rule.operator == "not_exists":
            return value is None or value == "" or value == []

        if value is None:
            return False

        return self._compare(rule.operator, value, rule.value, rule.case_sensitive)

    def _get_field_value(self, field: str, item: Any) -> Any:
        """Extract a field value from an item (unit or chunk)."""
        # Direct attribute access
        if hasattr(item, field):
            val = getattr(item, field)
            # Convert enums to their string value
            if hasattr(val, 'value'):
                return val.value
            return val

        # Nested access for metadata fields
        if hasattr(item, 'metadata') and hasattr(item.metadata, field):
            return getattr(item.metadata, field)

        return None

    def _compare(
        self, operator: str, field_val: Any, rule_val: Any,
        case_sensitive: bool
    ) -> bool:
        """Perform the comparison operation — all safe, no eval."""
        # String operations
        if operator == "contains":
            return self._str_contains(field_val, rule_val, case_sensitive)
        elif operator == "not_contains":
            return not self._str_contains(field_val, rule_val, case_sensitive)
        elif operator == "equals":
            return self._str_equals(field_val, rule_val, case_sensitive)
        elif operator == "not_equals":
            return not self._str_equals(field_val, rule_val, case_sensitive)
        elif operator == "regex":
            return self._regex_match(field_val, rule_val, case_sensitive)

        # Numeric operations
        elif operator == "gt":
            return float(field_val) > float(rule_val)
        elif operator == "lt":
            return float(field_val) < float(rule_val)
        elif operator == "gte":
            return float(field_val) >= float(rule_val)
        elif operator == "lte":
            return float(field_val) <= float(rule_val)

        # Collection operations
        elif operator == "in":
            if isinstance(rule_val, list):
                return str(field_val) in [str(v) for v in rule_val]
            return str(field_val) == str(rule_val)
        elif operator == "not_in":
            if isinstance(rule_val, list):
                return str(field_val) not in [str(v) for v in rule_val]
            return str(field_val) != str(rule_val)

        return False

    def _str_contains(self, field_val: Any, search: Any, case_sensitive: bool) -> bool:
        """Check if field contains the search string."""
        fv = str(field_val)
        sv = str(search)
        if isinstance(field_val, list):
            # For list fields (tags, entities), check if search is in any item
            items = [str(i) for i in field_val]
            if not case_sensitive:
                return any(sv.lower() in item.lower() for item in items)
            return any(sv in item for item in items)
        if not case_sensitive:
            return sv.lower() in fv.lower()
        return sv in fv

    def _str_equals(self, field_val: Any, target: Any, case_sensitive: bool) -> bool:
        """Check string equality."""
        fv = str(field_val)
        tv = str(target)
        if not case_sensitive:
            return fv.lower() == tv.lower()
        return fv == tv

    def _regex_match(self, field_val: Any, pattern: Any, case_sensitive: bool) -> bool:
        """Safe regex match — compiled pattern, no eval."""
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            return bool(re.search(str(pattern), str(field_val), flags))
        except re.error:
            return False


# ─── Filter Registry ─────────────────────────────────────────────

class FilterRegistry:
    """Stores and manages filter recipes (JSON file-based)."""

    def __init__(self, storage_path: str = "database"):
        import os
        self._path = os.path.join(storage_path, "filter_recipes.json")
        os.makedirs(storage_path, exist_ok=True)
        if not os.path.exists(self._path):
            with open(self._path, "w") as f:
                json.dump([], f)

    def save(self, recipe: FilterRecipe):
        """Save or update a filter recipe."""
        recipes = self._load_all()
        # Update existing or append
        found = False
        for i, r in enumerate(recipes):
            if r["id"] == recipe.id:
                recipes[i] = json.loads(recipe.model_dump_json())
                found = True
                break
        if not found:
            recipes.append(json.loads(recipe.model_dump_json()))
        self._save_all(recipes)

    def get(self, recipe_id: str) -> Optional[FilterRecipe]:
        """Get a recipe by ID."""
        for r in self._load_all():
            if r["id"] == recipe_id:
                return FilterRecipe(**r)
        return None

    def list_all(self) -> List[FilterRecipe]:
        """Get all saved recipes."""
        return [FilterRecipe(**r) for r in self._load_all()]

    def delete(self, recipe_id: str):
        """Delete a recipe by ID."""
        recipes = [r for r in self._load_all() if r["id"] != recipe_id]
        self._save_all(recipes)

    def _load_all(self) -> List[Dict]:
        try:
            with open(self._path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save_all(self, data: List[Dict]):
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2, default=str)
