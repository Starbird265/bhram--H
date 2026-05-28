"""
Skill Validators — Multi-gate validation for skill generation.

Every skill must pass all validators before publication.
Gates:
  1. Schema validation (frontmatter, structure)
  2. Source coverage (minimum atomic units)
  3. Conflict check (no unresolved contradictions)
  4. Dedup check (no duplicate steps/instructions)
  5. Completeness check (SOPs + edge cases + prerequisites)
"""

from typing import List, Dict, Any, Optional, Tuple
from core.models import AtomicKnowledgeUnit, SkillDef, UnitStatus




class ValidatorResult:
    """Result of a single validation check."""
    def __init__(self, name: str, passed: bool, message: str = "", details: Optional[Dict] = None):
        self.name = name
        self.passed = passed
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict:
        return {
            "validator": self.name,
            "passed": self.passed,
            "message": self.message,
            "details": self.details,
        }


class SkillValidationReport:
    """Aggregated report from all validators."""
    def __init__(self):
        self.results: List[ValidatorResult] = []

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> List[ValidatorResult]:
        return [r for r in self.results if not r.passed]

    def add(self, result: ValidatorResult):
        self.results.append(result)

    def to_dict(self) -> Dict:
        return {
            "all_passed": self.all_passed,
            "total_checks": len(self.results),
            "failures": len(self.failures),
            "results": [r.to_dict() for r in self.results],
        }


class SkillValidator:
    """Runs all validation gates on a skill before publication."""

    def __init__(self, min_units: int = 3, min_coverage: float = 0.6):
        self.min_units = min_units
        self.min_coverage = min_coverage

    def validate(
        self,
        skill: SkillDef,
        source_units: List[AtomicKnowledgeUnit],
    ) -> SkillValidationReport:
        """Run all validators and return a comprehensive report."""
        report = SkillValidationReport()

        report.add(self._check_schema(skill))
        report.add(self._check_source_coverage(skill, source_units))
        report.add(self._check_conflicts(source_units))
        report.add(self._check_duplicate_steps(skill))
        report.add(self._check_completeness(skill))

        return report

    def _check_schema(self, skill: SkillDef) -> ValidatorResult:
        """Gate 1: Validate skill structure and frontmatter."""
        issues = []

        if not skill.name or len(skill.name) < 3:
            issues.append("Name too short (min 3 chars)")
        if not skill.description or len(skill.description) < 10:
            issues.append("Description too short (min 10 chars)")
        if not skill.overview or len(skill.overview) < 20:
            issues.append("Overview too short (min 20 chars)")
        if not skill.steps:
            issues.append("No steps defined")

        return ValidatorResult(
            name="schema",
            passed=len(issues) == 0,
            message="; ".join(issues) if issues else "Schema valid",
            details={"issues": issues},
        )

    def _check_source_coverage(
        self, skill: SkillDef, units: List[AtomicKnowledgeUnit]
    ) -> ValidatorResult:
        """Gate 2: Ensure minimum number of source atomic units."""
        approved = [u for u in units if u.status == UnitStatus.APPROVED]
        has_enough = len(approved) >= self.min_units

        return ValidatorResult(
            name="source_coverage",
            passed=has_enough,
            message=f"{len(approved)} approved units (min {self.min_units})",
            details={
                "approved_units": len(approved),
                "total_units": len(units),
                "minimum_required": self.min_units,
            },
        )

    def _check_conflicts(self, units: List[AtomicKnowledgeUnit]) -> ValidatorResult:
        """Gate 3: Check for unresolved contradictions among source units."""
        contested = [u for u in units if u.status == UnitStatus.CONTESTED]
        conflicting = [u for u in units if u.conflicts_with]

        # Unresolved = contested units OR units with active conflict links
        unresolved = len(contested)
        for u in conflicting:
            active_conflicts = [
                cid for cid in u.conflicts_with
                if any(other.id == cid and other.status != UnitStatus.SUPERSEDED for other in units)
            ]
            unresolved += len(active_conflicts)

        return ValidatorResult(
            name="conflict_check",
            passed=unresolved == 0,
            message=f"{unresolved} unresolved conflicts" if unresolved else "No conflicts",
            details={
                "contested_units": len(contested),
                "unresolved_conflicts": unresolved,
            },
        )

    def _check_duplicate_steps(self, skill: SkillDef) -> ValidatorResult:
        """Gate 4: Check for duplicate or near-duplicate steps."""
        if not skill.steps:
            return ValidatorResult(name="dedup_steps", passed=True, message="No steps to check")

        # Simple similarity: normalize and compare
        normalized = [s.lower().strip() for s in skill.steps]
        duplicates = []
        for i, step_a in enumerate(normalized):
            for j, step_b in enumerate(normalized[i+1:], i+1):
                # Exact or near-exact duplicate
                if step_a == step_b:
                    duplicates.append((i, j))
                elif len(step_a) > 20 and len(step_b) > 20:
                    # Check if one is a substring of the other
                    if step_a in step_b or step_b in step_a:
                        duplicates.append((i, j))

        return ValidatorResult(
            name="dedup_steps",
            passed=len(duplicates) == 0,
            message=f"{len(duplicates)} duplicate step pairs" if duplicates else "No duplicates",
            details={"duplicate_pairs": duplicates},
        )

    def _check_completeness(self, skill: SkillDef) -> ValidatorResult:
        """Gate 5: Check that skill covers required sections."""
        coverage = {
            "has_steps": bool(skill.steps),
            "has_overview": bool(skill.overview),
            "has_prerequisites": bool(skill.prerequisites),
            "has_edge_cases": bool(skill.edge_cases),
            "has_examples": bool(skill.examples),
        }

        covered = sum(1 for v in coverage.values() if v)
        total = len(coverage)
        score = covered / total

        return ValidatorResult(
            name="completeness",
            passed=score >= self.min_coverage,
            message=f"Coverage {score:.0%} (min {self.min_coverage:.0%})",
            details={"coverage_score": score, "sections": coverage},
        )
