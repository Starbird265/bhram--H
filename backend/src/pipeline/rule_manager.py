"""
Dynamic Rule Manager — Self-Training Rule Engine.

The AI teaches the rule-based engine over time:
  1. When the rule-based extractor misses a signal, AI processes it
  2. AI returns the signal + suggested keywords/phrases that identify it
  3. Those keywords are saved to dynamic_rules.json
  4. Next time, the rule-based engine catches it — no AI needed

Over time, the rule-based engine handles more and more cases,
and the AI is called less and less. The AI trains its replacement.

Storage: database/dynamic_rules.json
Format:
{
    "rules": [
        {"keyword": "recalibrate", "source": "ai_suggested", "created_at": "...", "hit_count": 0},
        ...
    ],
    "stats": {"total_rules": N, "total_ai_calls_saved": M}
}
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Set


class DynamicRuleManager:
    """Manages the self-evolving keyword ruleset that the AI populates."""

    def __init__(self, db_path: str):
        self._file = Path(db_path) / "dynamic_rules.json"
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._rules: List[Dict[str, Any]] = []
        self._keyword_set: Set[str] = set()
        self._stats: Dict[str, int] = {"total_rules": 0, "total_ai_calls_saved": 0}
        self._load()

    def _load(self):
        """Load rules from disk."""
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._rules = data.get("rules", [])
                self._stats = data.get("stats", {"total_rules": 0, "total_ai_calls_saved": 0})
                self._keyword_set = {r["keyword"].lower() for r in self._rules}
            except Exception:
                self._rules = []
                self._keyword_set = set()

    def _save(self):
        """Persist rules to disk."""
        self._stats["total_rules"] = len(self._rules)
        data = {"rules": self._rules, "stats": self._stats}
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print(f"  [RuleManager] Save failed (non-fatal): {e}")

    def get_keywords(self) -> List[str]:
        """Return all learned keywords for the rule-based extractor."""
        with self._lock:
            return list(self._keyword_set)

    def add_rules(self, keywords: List[str], source: str = "ai_suggested") -> int:
        """Add new keywords from AI suggestions. Returns count of new rules added."""
        added = 0
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            for kw in keywords:
                kw_lower = kw.strip().lower()
                # Skip empty, too short, or already known
                if len(kw_lower) < 3 or kw_lower in self._keyword_set:
                    continue
                # Skip generic words that would match everything
                _GENERIC = {
                    "the", "and", "for", "that", "this", "with", "from", "have",
                    "are", "was", "were", "been", "will", "not", "but", "all",
                    "can", "had", "her", "his", "one", "our", "out", "day",
                }
                if kw_lower in _GENERIC:
                    continue

                self._rules.append({
                    "keyword": kw_lower,
                    "source": source,
                    "created_at": now,
                    "hit_count": 0,
                })
                self._keyword_set.add(kw_lower)
                added += 1

            if added > 0:
                self._save()

        return added

    def record_hit(self, keyword: str):
        """Record that a dynamic rule was used to catch a signal."""
        kw_lower = keyword.strip().lower()
        with self._lock:
            for rule in self._rules:
                if rule["keyword"] == kw_lower:
                    rule["hit_count"] = rule.get("hit_count", 0) + 1
                    break
            self._stats["total_ai_calls_saved"] = self._stats.get("total_ai_calls_saved", 0) + 1
            self._save()

    def get_stats(self) -> Dict[str, Any]:
        """Return stats about the dynamic rule engine."""
        with self._lock:
            top_rules = sorted(self._rules, key=lambda r: r.get("hit_count", 0), reverse=True)[:10]
            return {
                "total_rules": len(self._rules),
                "total_ai_calls_saved": self._stats.get("total_ai_calls_saved", 0),
                "top_rules": [
                    {"keyword": r["keyword"], "hits": r.get("hit_count", 0), "source": r.get("source")}
                    for r in top_rules
                ],
            }
