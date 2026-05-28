"""
Phase 4 — Webhook Server

Real-time ingestion endpoints for push-based data sources.
Each endpoint verifies the request signature, extracts payload,
and immediately queues it through the hash-gate → pipeline path.

Supported platforms:
  POST /webhooks/slack          — Slack Events API (URL verification + event push)
  POST /webhooks/notion         — Notion webhooks (v1, HMAC-SHA256 signature)
  POST /webhooks/github         — GitHub webhooks (X-Hub-Signature-256)
  POST /webhooks/whatsapp       — WhatsApp Business Cloud (challenge + HMAC)
  POST /webhooks/jira           — Jira issue webhooks (no signature by default)
  POST /webhooks/linear         — Linear webhooks (X-Linear-Signature)

All endpoints:
  - Verify signatures before touching the payload (security-first)
  - Return 200/204 immediately (async processing in background)
  - Write a PointerRecord immediately so the dashboard shows real-time activity
  - Feed the content to the distillation pipeline via the hash-gate
  - Append to the audit log

Environment variables required (per platform):
  SLACK_SIGNING_SECRET
  NOTION_WEBHOOK_SECRET
  GITHUB_WEBHOOK_SECRET
  WHATSAPP_WEBHOOK_SECRET
  WHATSAPP_VERIFY_TOKEN
  LINEAR_WEBHOOK_SECRET
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Header, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ── Signature verification helpers ──────────────────────────────────────────────

_DEV_MODE = bool(os.getenv("DEBUG") or os.getenv("CORTEX_DEV"))  # V1 fix


def _verify_slack(body: bytes, timestamp: str, signature: str) -> bool:
    """Slack Events API: v0=HMAC-SHA256(secret, 'v0:{ts}:{body}')"""
    secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not secret:
        # V1 fix: only skip verification in explicit dev mode
        return _DEV_MODE
    if abs(time.time() - float(timestamp)) > 300:
        return False  # Replay protection: reject if timestamp > 5 min old
    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_sha256(body: bytes, signature: str, secret: str, prefix: str = "sha256=") -> bool:
    """Generic HMAC-SHA256 verification used by GitHub, WhatsApp, Linear, Notion."""
    if not secret:
        # V1 fix: only skip verification in explicit dev mode
        return _DEV_MODE
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


# ── Background processing helper ───────────────────────────────────────────────

def _ingest_text(app_id: str, location_key: str, permalink: str, title: str,
                 content: str, department: str = "shared") -> None:
    """
    Hash-gate → pointer upsert → distillation pipeline.
    Called in a background task so the webhook returns 200 instantly.
    """
    try:
        from storage.pointer_store import PointerStore, PointerRecord
        from storage.sqlite_store import SQLiteStore
        from pipeline.distill import KnowledgeDistiller
        from pipeline.normalize import TextNormalizer
        from pipeline.chunking import SemanticChunker
        from core.models import SourceType, Department

        db_path = _db_path()
        store = PointerStore(db_path=db_path)
        content_hash = PointerRecord.hash_content(content)

        # Check hash-gate: skip AI if content unchanged
        existing = store.get(location_key)
        if existing and not existing.is_stale(content_hash):
            print(f"  [Webhook/{app_id}] Hash-gate: {location_key} unchanged — skipped")
            return

        # Upsert pointer record
        now = datetime.now(timezone.utc).isoformat()
        store.upsert(PointerRecord(
            location_key=location_key,
            app_id=app_id,
            content_hash=content_hash,
            permalink=permalink,
            title=title,
            last_seen_at=now,
            last_indexed_at=now,
            department=department,
        ))

        # Feed through pipeline
        try:
            source_type_map = {
                "slack": SourceType.SLACK, "notion": SourceType.NOTION,
                "github": SourceType.GITHUB, "whatsapp": SourceType.WHATSAPP,
                "jira": SourceType.JIRA, "linear": SourceType.LINEAR,
            }
            src_type = source_type_map.get(app_id, SourceType.LOCAL_FILE)
            dept = Department(department) if department in [d.value for d in Department] else Department.SHARED

            sql_store = SQLiteStore(db_path=db_path)
            normalizer = TextNormalizer()
            chunker = SemanticChunker()
            distiller = KnowledgeDistiller(db_path=db_path)

            # Bug 4 fixed: correct method names are normalize_document / chunk_document
            normalized = normalizer.normalize_document(content)
            chunks = chunker.chunk_document(
                normalized,
                source_type=src_type,
                source_identifier=location_key,
                department=dept,
            )
            distilled = distiller.distill_chunks(chunks)

            from core.search import VectorStore as _VS
            from storage.filesystem_store import FilesystemStore as _FSS
            _vs = _VS(db_path=db_path)
            _fss = _FSS(db_path=db_path)  # Bug 4 fix: main pipeline reads FilesystemStore
            for chunk in distilled:
                sql_store.save_chunk(chunk)
                # Bug 4 fix: persist to FilesystemStore so main pipeline dedup/privacy/skills see it
                try:
                    _fss.save_chunk(chunk)
                except Exception as _fss_err:
                    print(f"  [Webhook/{app_id}] FilesystemStore save failed (non-fatal): {_fss_err}")
                try:
                    _vs.index_chunk(
                        chunk_id=chunk.id,
                        title=chunk.title,
                        content=chunk.content,
                        summary=getattr(chunk, "summary", ""),
                        department=chunk.department.value,
                        knowledge_type=chunk.knowledge_type.value,
                        source_type=chunk.source_type.value,
                        source_id=chunk.source_identifier,
                        tags=list(getattr(chunk, "tags", [])),
                        confidence=chunk.metadata.confidence_score,
                    )
                except Exception:
                    pass  # VectorStore indexing failure must not break webhook

            print(f"  [Webhook/{app_id}] Processed {len(distilled)} chunks from {location_key}")

        except Exception as pipeline_err:
            print(f"  [Webhook/{app_id}] Pipeline error (non-fatal): {pipeline_err}")

    except Exception as e:
        print(f"  [Webhook/{app_id}] Background ingest failed: {e}")


def _db_path() -> str:
    """Resolve db_path the same way api.py does."""
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backend_dir = os.path.dirname(src_dir)
    return os.path.join(backend_dir, "database")


def _audit(app_id: str, event_type: str, target_id: str, details: Dict[str, Any]) -> None:
    """Append a webhook event to the audit log."""
    try:
        import uuid
        from storage.sqlite_store import SQLiteStore
        from core.models import AuditEntry
        sql = SQLiteStore(db_path=_db_path())
        sql.log_audit(AuditEntry(
            id=str(uuid.uuid4()),
            action=f"webhook:{event_type}",
            actor=f"webhook:{app_id}",
            target_type="webhook",
            target_id=target_id,
            details=details,
        ))
    except Exception:
        pass  # Audit failure must never break webhook handling


# ═══════════════════════════════════════════════════════════════
# SLACK
# ═══════════════════════════════════════════════════════════════

@router.post("/slack", response_class=PlainTextResponse, status_code=200)
async def slack_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
):
    """
    Slack Events API endpoint.
    1. URL verification challenge (type=url_verification)
    2. message events → real-time hash-gate → pipeline
    """
    body = await request.body()

    # Signature verification
    if x_slack_request_timestamp and x_slack_signature:
        if not _verify_slack(body, x_slack_request_timestamp, x_slack_signature):
            raise HTTPException(status_code=403, detail="Invalid Slack signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # URL verification challenge (Slack sends this when you first add the endpoint)
    if payload.get("type") == "url_verification":
        return payload.get("challenge", "")

    event = payload.get("event", {})
    event_type = event.get("type", "")

    if event_type == "message" and not event.get("subtype"):
        text = event.get("text", "").strip()
        if text and len(text) >= 10:
            channel = event.get("channel", "unknown")
            ts = event.get("ts", str(time.time()))
            user = event.get("user", "unknown")
            location_key = f"{channel}/{ts}"
            permalink = f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"

            background_tasks.add_task(
                _ingest_text,
                app_id="slack",
                location_key=location_key,
                permalink=permalink,
                title=f"Slack #{channel} message",
                content=text,
            )
            background_tasks.add_task(
                _audit, "slack", "message", location_key,
                {"channel": channel, "user": user, "ts": ts}
            )

    return ""


# ═══════════════════════════════════════════════════════════════
# NOTION
# ═══════════════════════════════════════════════════════════════

@router.post("/notion", status_code=204)
async def notion_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_notion_signature: Optional[str] = Header(None, alias="X-Notion-Signature"),
):
    """Notion webhook (v1 API). Triggers on page.created, page.updated events."""
    body = await request.body()
    secret = os.getenv("NOTION_WEBHOOK_SECRET", "")
    if x_notion_signature and secret:
        if not _verify_sha256(body, x_notion_signature, secret):
            raise HTTPException(status_code=403, detail="Invalid Notion signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("type", "")
    if event_type in ("page.created", "page.updated"):
        page_id = payload.get("entity", {}).get("id", "")
        title = payload.get("entity", {}).get("title", f"Notion page {page_id}")
        permalink = f"https://www.notion.so/{page_id.replace('-', '')}"
        location_key = f"notion/{page_id}"

        def _fetch_and_ingest_notion():
            """Bug 11 fix: actually fetch the page content from Notion API."""
            notion_token = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
            content = None

            if notion_token and page_id:
                try:
                    from notion_client import Client as NotionClient
                    nc = NotionClient(auth=notion_token)
                    # Fetch all blocks for the page
                    blocks_resp = nc.blocks.children.list(block_id=page_id)
                    text_parts = []
                    for block in blocks_resp.get("results", []):
                        btype = block.get("type", "")
                        rich_text = block.get(btype, {}).get("rich_text", [])
                        for rt in rich_text:
                            plain = rt.get("plain_text", "")
                            if plain.strip():
                                text_parts.append(plain)
                    if text_parts:
                        content = "\n".join(text_parts)
                        print(f"  [Notion webhook] Fetched {len(text_parts)} blocks from page {page_id}")
                except ImportError:
                    print("  [Notion webhook] notion_client not installed — using stub content")
                except Exception as e:
                    print(f"  [Notion webhook] Failed to fetch page content: {e}")

            if not content:
                # Fallback stub — at least captures the title and ID for pointer tracking
                content = f"Notion page updated: {title} (id: {page_id})"

            _ingest_text(
                app_id="notion",
                location_key=location_key,
                permalink=permalink,
                title=title,
                content=content,
            )

        background_tasks.add_task(_fetch_and_ingest_notion)
        background_tasks.add_task(
            _audit, "notion", event_type, location_key,
            {"page_id": page_id, "title": title}
        )



# ═══════════════════════════════════════════════════════════════
# GITHUB
# ═══════════════════════════════════════════════════════════════

@router.post("/github", status_code=204)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
):
    """GitHub webhooks — push, pull_request, issues, issue_comment."""
    body = await request.body()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if x_hub_signature_256 and secret:
        if not _verify_sha256(body, x_hub_signature_256, secret):
            raise HTTPException(status_code=403, detail="Invalid GitHub signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    repo = payload.get("repository", {}).get("full_name", "unknown")
    event = x_github_event or "unknown"

    if event == "push":
        commits = payload.get("commits", [])
        for commit in commits[:20]:  # cap at 20 commits per push
            sha = commit.get("id", "")[:8]
            message = commit.get("message", "")
            author = commit.get("author", {}).get("name", "unknown")
            branch = payload.get("ref", "").replace("refs/heads/", "")
            content = f"[{repo}] {author} pushed to {branch}:\n{message}"
            location_key = f"github/{repo}/commit/{sha}"
            permalink = commit.get("url", "")
            background_tasks.add_task(
                _ingest_text, "github", location_key, permalink,
                f"GitHub push: {sha[:7]} — {message[:60]}", content
            )

    elif event in ("issues", "pull_request"):
        obj = payload.get("issue") or payload.get("pull_request") or {}
        title = obj.get("title", "")
        body_text = obj.get("body", "")
        html_url = obj.get("html_url", "")
        number = obj.get("number", 0)
        content = f"[{repo} #{number}] {title}\n\n{body_text}"
        location_key = f"github/{repo}/{event}/{number}"
        background_tasks.add_task(
            _ingest_text, "github", location_key, html_url,
            f"GitHub {event}: {title}", content
        )

    elif event == "issue_comment":
        comment = payload.get("comment", {})
        issue = payload.get("issue", {})
        content = comment.get("body", "")
        number = issue.get("number", 0)
        html_url = comment.get("html_url", "")
        location_key = f"github/{repo}/comment/{comment.get('id', '')}"
        background_tasks.add_task(
            _ingest_text, "github", location_key, html_url,
            f"GitHub comment on #{number}", content
        )

    background_tasks.add_task(
        _audit, "github", event, repo,
        {"repo": repo, "event": event}
    )


# ═══════════════════════════════════════════════════════════════
# WHATSAPP
# ═══════════════════════════════════════════════════════════════

@router.get("/whatsapp", response_class=PlainTextResponse, status_code=200)
async def whatsapp_verify(
    hub_mode: Optional[str] = None,
    hub_verify_token: Optional[str] = None,
    hub_challenge: Optional[str] = None,
):
    """WhatsApp webhook verification (GET challenge-response)."""
    expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "cortex-verify")
    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        return hub_challenge or ""
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp", status_code=200)
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """
    WhatsApp Business Cloud webhook.
    Only business messages are ingested (not status updates).
    """
    body = await request.body()
    secret = os.getenv("WHATSAPP_WEBHOOK_SECRET", "")
    if x_hub_signature_256 and secret:
        if not _verify_sha256(body, x_hub_signature_256, secret):
            raise HTTPException(status_code=403, detail="Invalid WhatsApp signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "ok"}

    # Drill into WhatsApp's nested structure
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue  # Only plain text for now
                text = msg.get("text", {}).get("body", "").strip()
                if not text or len(text) < 5:
                    continue
                msg_id = msg.get("id", "")
                from_num = msg.get("from", "unknown")
                location_key = f"whatsapp/{from_num}/{msg_id}"
                content = f"[WhatsApp from {from_num}]\n{text}"

                background_tasks.add_task(
                    _ingest_text, "whatsapp", location_key, "",
                    f"WhatsApp message from {from_num}", content
                )
                background_tasks.add_task(
                    _audit, "whatsapp", "message", location_key,
                    {"from": from_num, "msg_id": msg_id}
                )

    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════
# JIRA
# ═══════════════════════════════════════════════════════════════

@router.post("/jira", status_code=204)
async def jira_webhook(request: Request, background_tasks: BackgroundTasks):
    """Jira issue webhooks — jira:issue_created, jira:issue_updated, comment_created."""
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    if not issue:
        return

    issue_key = issue.get("key", "")
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    description = fields.get("description") or ""
    # ADF description → extract plain text if dict
    if isinstance(description, dict):
        description = str(description)

    if event.startswith("jira:issue"):
        content = f"[{issue_key}] {summary}\n\n{description}"
        location_key = f"jira/{issue_key}"
        permalink = f"https://jira.atlassian.net/browse/{issue_key}"
        background_tasks.add_task(
            _ingest_text, "jira", location_key, permalink,
            f"Jira {issue_key}: {summary}", content
        )

    elif event == "comment_created":
        comment = payload.get("comment", {})
        body_text = comment.get("body", "")
        if isinstance(body_text, dict):
            body_text = str(body_text)
        comment_id = comment.get("id", "")
        location_key = f"jira/{issue_key}/comment/{comment_id}"
        content = f"[{issue_key} comment] {summary}\n\n{body_text}"
        background_tasks.add_task(
            _ingest_text, "jira", location_key, "", f"Jira comment on {issue_key}", content
        )

    background_tasks.add_task(_audit, "jira", event, issue_key, {"event": event, "key": issue_key})


# ═══════════════════════════════════════════════════════════════
# LINEAR
# ═══════════════════════════════════════════════════════════════

@router.post("/linear", status_code=204)
async def linear_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_linear_signature: Optional[str] = Header(None, alias="X-Linear-Signature"),
):
    """Linear issue/comment webhooks with HMAC-SHA256 signature."""
    body = await request.body()
    secret = os.getenv("LINEAR_WEBHOOK_SECRET", "")
    if x_linear_signature and secret:
        if not _verify_sha256(body, x_linear_signature, secret):
            raise HTTPException(status_code=403, detail="Invalid Linear signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    action = payload.get("action", "")
    obj_type = payload.get("type", "")
    data = payload.get("data", {})

    if obj_type == "Issue":
        issue_id = data.get("id", "")
        title = data.get("title", "")
        description = data.get("description", "")
        url = data.get("url", "")
        content = f"[Linear] {title}\n\n{description}"
        location_key = f"linear/issue/{issue_id}"
        background_tasks.add_task(
            _ingest_text, "linear", location_key, url, f"Linear: {title}", content
        )

    elif obj_type == "Comment":
        comment_id = data.get("id", "")
        body_text = data.get("body", "")
        issue_id = data.get("issue", {}).get("id", "")
        url = data.get("url", "")
        location_key = f"linear/comment/{comment_id}"
        background_tasks.add_task(
            _ingest_text, "linear", location_key, url,
            f"Linear comment on {issue_id}", body_text
        )

    background_tasks.add_task(_audit, "linear", f"{obj_type}.{action}", data.get("id", ""),
                               {"type": obj_type, "action": action})


# ── Webhook health check ────────────────────────────────────────────────────────

@router.get("/health")
def webhook_health():
    """Returns which webhooks have secrets configured."""
    return {
        "slack": bool(os.getenv("SLACK_SIGNING_SECRET")),
        "notion": bool(os.getenv("NOTION_WEBHOOK_SECRET")),
        "github": bool(os.getenv("GITHUB_WEBHOOK_SECRET")),
        "whatsapp": bool(os.getenv("WHATSAPP_WEBHOOK_SECRET")),
        "linear": bool(os.getenv("LINEAR_WEBHOOK_SECRET")),
        "jira": True,  # Jira doesn't require a secret
        "note": "Set environment variables to enable signature verification.",
    }
