"""
GitHub Connector — Tier 1 (Zero-Setup)

Uses the locally installed `gh` CLI (GitHub CLI) to pull data
without requiring any API tokens from the user.
If `gh` is authenticated, it can pull:
  - Repository READMEs
  - Recent issues
  - Repository metadata
"""

import subprocess
import json
import uuid
from typing import List, Optional
from datetime import datetime, timezone

from core.models import (
    KnowledgeChunk, KnowledgeType, KnowledgeMetadata,
    Department, SourceType, SourcePosition, ProcessingLayer
)


class GitHubConnector:
    """Pulls data from GitHub using the locally authenticated `gh` CLI."""

    def __init__(self):
        self._gh_available = None

    def is_authenticated(self) -> bool:
        """Check if `gh` CLI is installed and authenticated."""
        if self._gh_available is not None:
            return self._gh_available

        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10
            )
            self._gh_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._gh_available = False

        return self._gh_available

    def get_auth_user(self) -> Optional[str]:
        """Get the authenticated GitHub username."""
        if not self.is_authenticated():
            return None
        try:
            result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def list_repos(self, limit: int = 10) -> List[dict]:
        """List the user's recent repositories."""
        if not self.is_authenticated():
            return []

        try:
            result = subprocess.run(
                ["gh", "repo", "list", "--limit", str(limit), "--json",
                 "name,owner,description,updatedAt,isPrivate"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return []

    def fetch_repo_readme(self, owner: str, repo: str) -> Optional[str]:
        """Fetch the README content of a repository."""
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/readme",
                 "--jq", ".content"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                import base64
                content = result.stdout.strip()
                # GitHub API returns base64-encoded content
                try:
                    return base64.b64decode(content).decode("utf-8")
                except Exception:
                    return content
        except Exception:
            pass
        return None

    def fetch_recent_issues(self, owner: str, repo: str, limit: int = 10) -> List[dict]:
        """Fetch recent issues from a repository."""
        try:
            result = subprocess.run(
                ["gh", "issue", "list", "--repo", f"{owner}/{repo}",
                 "--limit", str(limit), "--json",
                 "title,body,labels,state,createdAt"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return []

    def ingest(self, limit_repos: int = 5) -> List[KnowledgeChunk]:
        """
        Pull data from GitHub and convert to KnowledgeChunks.
        Ingests READMEs and recent issues from the user's repos.
        """
        if not self.is_authenticated():
            print("  [GitHub] gh CLI not authenticated. Skipping.")
            return []

        user = self.get_auth_user()
        print(f"  [GitHub] Authenticated as: {user}")

        chunks = []
        repos = self.list_repos(limit=limit_repos)
        print(f"  [GitHub] Found {len(repos)} repositories")

        for repo_info in repos:
            owner = repo_info.get("owner", {}).get("login", user)
            repo_name = repo_info.get("name", "unknown")
            full_name = f"{owner}/{repo_name}"

            # Ingest README
            readme = self.fetch_repo_readme(owner, repo_name)
            if readme and len(readme.strip()) > 50:
                chunk = KnowledgeChunk(
                    id=str(uuid.uuid4()),
                    department=Department.ENGINEERING,
                    knowledge_type=KnowledgeType.SOP,
                    source_type=SourceType.GITHUB,
                    source_identifier=f"github:{full_name}/README",
                    title=f"{repo_name} — README",
                    content=readme,
                    summary=readme[:200].replace("\n", " ").strip(),
                    tags=["github", "readme", repo_name],
                    metadata=KnowledgeMetadata(
                        confidence_score=0.85,
                        source_reliability=0.9,
                        source_position=SourcePosition(file_path=f"github:{full_name}")
                    ),
                    processing_layer=ProcessingLayer.RAW,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                chunks.append(chunk)
                print(f"    [README] {full_name} ({len(readme)} chars)")

            # Ingest recent issues
            issues = self.fetch_recent_issues(owner, repo_name, limit=5)
            for issue in issues:
                body = issue.get("body", "") or ""
                title = issue.get("title", "Untitled Issue")
                if len(body.strip()) < 20:
                    continue

                issue_content = f"# {title}\n\n{body}"
                labels = [l.get("name", "") for l in issue.get("labels", [])]

                chunk = KnowledgeChunk(
                    id=str(uuid.uuid4()),
                    department=Department.ENGINEERING,
                    knowledge_type=KnowledgeType.EDGE_CASE,
                    source_type=SourceType.GITHUB,
                    source_identifier=f"github:{full_name}/issues",
                    title=f"Issue: {title[:60]}",
                    content=issue_content,
                    summary=issue_content[:200].replace("\n", " ").strip(),
                    tags=["github", "issue", repo_name] + labels,
                    metadata=KnowledgeMetadata(
                        confidence_score=0.75,
                        source_reliability=0.8,
                    ),
                    processing_layer=ProcessingLayer.RAW,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                chunks.append(chunk)

        print(f"  [GitHub] Ingested {len(chunks)} chunks total")
        return chunks
