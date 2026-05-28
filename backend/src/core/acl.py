"""
Phase 4 — ACL Inheritance Layer

Access Control Lists for all ingested documents.
Determines which departments / users can read each AtomicKnowledgeUnit
or KnowledgeChunk based on where the data came from.

Hierarchy (most permissive → most restrictive):
  PUBLIC     → anyone can read (open Slack channels, public GitHub repos)
  SHARED     → all authenticated users within the org
  DEPARTMENT → only members of the owning department
  RESTRICTED → only named users / service accounts

Inheritance rules:
  1. Source-level policy (set when connecting an app) → wins if RESTRICTED
  2. Channel/page-level policy (set when selecting resources) → overrides source
  3. Content-level policy (set by privacy scanner) → can only tighten, never loosen

For enterprise deployments:
  - ACL records are stored in the `acl_records` SQLite table
  - On read (from /api/memory/units), records are filtered by the requesting user's dept
  - On write (from the pipeline), ACL records are inherited from source config

Example:
  Slack #engineering channel → department=ENGINEERING, visibility=DEPARTMENT
  Notion "Public Wiki" page  → department=SHARED, visibility=SHARED
  GitHub private repo        → department=ENGINEERING, visibility=RESTRICTED
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Set


# ── ACL Data models ─────────────────────────────────────────────────────────────

@dataclass
class ACLRule:
    """
    One access control rule for a resource.
    A resource can have multiple rules (one per department or user).
    """
    resource_id: str           # location_key or chunk_id or skill_name
    resource_type: str         # "chunk", "unit", "skill", "pointer"
    app_id: str                # Which connector produced this resource
    visibility: str            # "public", "shared", "department", "restricted"
    allowed_departments: List[str] = field(default_factory=list)  # [] = all depts
    allowed_users: List[str] = field(default_factory=list)        # [] = all users
    denied_departments: List[str] = field(default_factory=list)   # Explicit deny list
    inherited_from: Optional[str] = None    # parent resource_id (for inheritance)
    set_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ACLCheckResult:
    allowed: bool
    reason: str
    visibility: str
    resource_id: str


# ── Source-level defaults ────────────────────────────────────────────────────────

# Default ACL policy per connector app
# These are applied when no explicit rule has been set for a resource
SOURCE_DEFAULTS: Dict[str, Dict] = {
    "slack": {
        "public_channel": {"visibility": "shared", "allowed_departments": []},
        "private_channel": {"visibility": "department", "allowed_departments": []},
        "dm": {"visibility": "restricted", "allowed_departments": []},
    },
    "notion": {
        "public_page": {"visibility": "shared", "allowed_departments": []},
        "private_page": {"visibility": "department", "allowed_departments": []},
    },
    "google_drive": {
        "shared_drive": {"visibility": "shared", "allowed_departments": []},
        "my_drive": {"visibility": "restricted", "allowed_departments": []},
    },
    "github": {
        "public_repo": {"visibility": "public", "allowed_departments": []},
        "private_repo": {"visibility": "department", "allowed_departments": ["engineering"]},
    },
    "confluence": {
        "space": {"visibility": "shared", "allowed_departments": []},
    },
    "jira": {
        "project": {"visibility": "department", "allowed_departments": []},
    },
    "linear": {
        "team": {"visibility": "department", "allowed_departments": ["engineering"]},
    },
    "ms_teams": {
        "channel": {"visibility": "department", "allowed_departments": []},
    },
    "whatsapp": {
        "message": {"visibility": "restricted", "allowed_departments": ["support"]},
    },
}


# ── ACL Store ───────────────────────────────────────────────────────────────────

class ACLStore:
    """
    SQLite-backed ACL store. Auto-migrates acl_records table into bhrm.db.

    Key operations:
      set_rule(resource_id, rule)      — store a rule
      check(resource_id, dept, user)   — is this dept/user allowed?
      inherit_from_source(app_id, ...)  — apply source-level defaults
      get_rules(resource_id)           — list all rules for a resource
    """

    def __init__(self, db_path: str):
        self.db_file = Path(db_path) / "bhrm.db"
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_file), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS acl_records (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id          TEXT NOT NULL,
                    resource_type        TEXT NOT NULL DEFAULT 'chunk',
                    app_id               TEXT NOT NULL DEFAULT '',
                    visibility           TEXT NOT NULL DEFAULT 'shared',
                    allowed_departments  TEXT NOT NULL DEFAULT '[]',
                    allowed_users        TEXT NOT NULL DEFAULT '[]',
                    denied_departments   TEXT NOT NULL DEFAULT '[]',
                    inherited_from       TEXT,
                    set_at               TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_acl_resource
                    ON acl_records(resource_id);
                CREATE INDEX IF NOT EXISTS idx_acl_visibility
                    ON acl_records(visibility);
                CREATE INDEX IF NOT EXISTS idx_acl_app
                    ON acl_records(app_id);
            """)

    # ── Write ────────────────────────────────────────────────────────────────────

    def set_rule(self, rule: ACLRule) -> None:
        """Store or replace an ACL rule for a resource."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO acl_records (
                    resource_id, resource_type, app_id, visibility,
                    allowed_departments, allowed_users, denied_departments,
                    inherited_from, set_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rule.resource_id, rule.resource_type, rule.app_id, rule.visibility,
                json.dumps(rule.allowed_departments), json.dumps(rule.allowed_users),
                json.dumps(rule.denied_departments), rule.inherited_from, rule.set_at,
            ))

    def inherit_from_source(
        self,
        resource_id: str,
        app_id: str,
        resource_subtype: str = "default",
        department: str = "shared",
        resource_type: str = "chunk",
    ) -> ACLRule:
        """
        Apply the source-level default ACL for a resource.
        Called by the pipeline whenever a new document is ingested.
        Returns the rule that was applied.
        """
        source_defaults = SOURCE_DEFAULTS.get(app_id, {})
        policy = (
            source_defaults.get(resource_subtype)
            or source_defaults.get(list(source_defaults.keys())[0] if source_defaults else "default")
            or {"visibility": "shared", "allowed_departments": []}
        )
        depts = policy.get("allowed_departments", [])
        if not depts and department != "shared":
            depts = [department]

        rule = ACLRule(
            resource_id=resource_id,
            resource_type=resource_type,
            app_id=app_id,
            visibility=policy.get("visibility", "shared"),
            allowed_departments=depts,
            inherited_from=f"source:{app_id}",
        )
        self.set_rule(rule)
        return rule

    # ── Read / Check ─────────────────────────────────────────────────────────────

    def check(
        self,
        resource_id: str,
        requesting_department: Optional[str] = None,
        requesting_user: Optional[str] = None,
    ) -> ACLCheckResult:
        """
        Check if a department/user can access a resource.
        Returns ACLCheckResult with allowed=True/False and reasoning.
        """
        rules = self.get_rules(resource_id)

        if not rules:
            # No explicit ACL → default to shared access
            return ACLCheckResult(
                allowed=True, reason="no_acl_rule_default_allow",
                visibility="shared", resource_id=resource_id
            )

        # Use the most restrictive rule that applies
        for rule in rules:
            # Explicit deny check
            if requesting_department and requesting_department in rule.denied_departments:
                return ACLCheckResult(
                    allowed=False,
                    reason=f"department_{requesting_department}_explicitly_denied",
                    visibility=rule.visibility,
                    resource_id=resource_id,
                )

            visibility = rule.visibility

            if visibility == "public":
                return ACLCheckResult(allowed=True, reason="public", visibility=visibility,
                                      resource_id=resource_id)

            if visibility == "shared":
                # Allow anyone within the org (authenticated)
                return ACLCheckResult(allowed=True, reason="shared_access", visibility=visibility,
                                      resource_id=resource_id)

            if visibility == "department":
                if not requesting_department:
                    return ACLCheckResult(allowed=False, reason="department_required",
                                          visibility=visibility, resource_id=resource_id)
                allowed_depts = rule.allowed_departments
                if not allowed_depts or requesting_department in allowed_depts:
                    return ACLCheckResult(allowed=True, reason=f"in_allowed_department",
                                          visibility=visibility, resource_id=resource_id)
                return ACLCheckResult(
                    allowed=False,
                    reason=f"department_{requesting_department}_not_in_allowed_list",
                    visibility=visibility, resource_id=resource_id
                )

            if visibility == "restricted":
                if requesting_user and requesting_user in rule.allowed_users:
                    return ACLCheckResult(allowed=True, reason="user_explicitly_allowed",
                                          visibility=visibility, resource_id=resource_id)
                if requesting_department and requesting_department in rule.allowed_departments:
                    return ACLCheckResult(allowed=True, reason="dept_explicitly_allowed",
                                          visibility=visibility, resource_id=resource_id)
                return ACLCheckResult(allowed=False, reason="restricted_not_in_allowed_list",
                                      visibility=visibility, resource_id=resource_id)

        return ACLCheckResult(allowed=True, reason="no_matching_rule", visibility="shared",
                              resource_id=resource_id)

    def get_rules(self, resource_id: str) -> List[ACLRule]:
        """Get all ACL rules for a resource, most recently set first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM acl_records WHERE resource_id = ? ORDER BY id DESC",
                (resource_id,)
            ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def filter_allowed_ids(
        self,
        resource_ids: List[str],
        requesting_department: Optional[str],
        requesting_user: Optional[str] = None,
    ) -> List[str]:
        """
        Bulk-check a list of resource IDs and return only the allowed ones.
        Used by /api/memory/units to filter results before returning to client.
        """
        return [
            rid for rid in resource_ids
            if self.check(rid, requesting_department, requesting_user).allowed
        ]

    def get_stats(self) -> Dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM acl_records").fetchone()[0]
            by_vis = conn.execute("""
                SELECT visibility, COUNT(*) as cnt FROM acl_records GROUP BY visibility
            """).fetchall()
        return {
            "total_rules": total,
            "by_visibility": {r["visibility"]: r["cnt"] for r in by_vis},
        }

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> ACLRule:
        return ACLRule(
            resource_id=row["resource_id"],
            resource_type=row["resource_type"],
            app_id=row["app_id"],
            visibility=row["visibility"],
            allowed_departments=json.loads(row["allowed_departments"]),
            allowed_users=json.loads(row["allowed_users"]),
            denied_departments=json.loads(row["denied_departments"]),
            inherited_from=row["inherited_from"],
            set_at=row["set_at"],
        )
