"""
Organizational Intelligence Engine — Unified API Server

Consolidates ALL endpoints into a single FastAPI app:
  - Configuration management (data sources, API keys)
  - Pipeline execution (run the 10-layer loop)
  - Webhook ingestion (real-time Slack/Notion push)
  - Agent feedback loop (failure memory injection)
  - Dynamic runtime assembly (on-demand skill composition)
  - Skill/chunk/agent management
"""

import os
import sys
import io
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from storage.filesystem_store import FilesystemStore
from storage.sqlite_store import SQLiteStore
from storage.skill_registry import SkillRegistry
from smart_layer.agent_connector import AgentConnector
from smart_layer.assembler import SkillAssembler
from smart_layer.tool_provisioner import ToolProvisioner
from generators.skill_generator import SkillGenerator
from main import run_orchestration_loop
from pipeline.normalize import TextNormalizer
from pipeline.chunking import SemanticChunker
from pipeline.distill import KnowledgeDistiller
from pipeline.deduplicate import KnowledgeDeduplicator
from core.models import SourceType, Department, KnowledgeType, UnitStatus, SessionFact
from ingestion.connector_manager import ConnectorManager

# Phase 4 imports
from ingestion.webhook_server import router as webhook_router
from middleware import RateLimiter, connector_limiter
from core.acl import ACLStore, ACLRule
from core.search import VectorStore

# Phase 5: External Agent Connectivity
from middleware.webhook_security import build_authenticated_headers, verify_signature
from agents.webhook_dispatcher import WebhookDispatcher
import logging

# Dedicated Connection Log
conn_logger = logging.getLogger("ConnectionLog")
if not conn_logger.handlers:
    conn_handler = logging.FileHandler("connection.log")
    conn_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    conn_logger.addHandler(conn_handler)
    conn_logger.setLevel(logging.INFO)

# Global webhook dispatcher instance (started in lifespan)
_webhook_dispatcher: Optional[WebhookDispatcher] = None

# ─── Startup lifespan: pre-load embeddings + auto-index ──────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Runs once at startup before accepting requests."""
    _db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
    _kb = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge_base")

    # 1. Pre-load embedding model in background thread (non-blocking)
    import threading
    def _load_embeddings():
        try:
            from core.search import _get_embed_model
            model = _get_embed_model()
            if model:
                print("  [Startup] Embedding model loaded ✓")
        except Exception as e:
            print(f"  [Startup] Embedding model unavailable: {e}")
    threading.Thread(target=_load_embeddings, daemon=True).start()

    # 2. Deduplicate atomic_units (keep oldest per claim+dept)
    try:
        import sqlite3 as _sq
        _conn = _sq.connect(os.path.join(_db, "bhrm.db"))
        _before = _conn.execute("SELECT COUNT(*) FROM atomic_units").fetchone()[0]
        _conn.execute("""
            DELETE FROM atomic_units WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM atomic_units
                GROUP BY LOWER(TRIM(claim)), department
            )
        """)
        _conn.commit()
        _after = _conn.execute("SELECT COUNT(*) FROM atomic_units").fetchone()[0]
        _conn.close()
        if _before != _after:
            print(f"  [Startup] Deduped units: {_before} → {_after} ✓")
    except Exception as e:
        print(f"  [Startup] Dedup skipped: {e}")

    # 3. Auto-index all unindexed chunks into search
    try:
        _vs = VectorStore(db_path=_db)
        _count = _vs.index_from_store(db_path=_db)
        if _count > 0:
            print(f"  [Startup] Search: indexed {_count} chunks ✓")
        else:
            print("  [Startup] Search index already up to date ✓")
    except Exception as e:
        print(f"  [Startup] Search indexing skipped: {e}")

    # 4. Start SkillFileWatcher for hot-reload (Layer 11)
    _watcher = None
    try:
        from agents.registry import AgentRegistry
        from agents.skill_watcher import SkillFileWatcher, AgentReloadBus
        _reload_bus = AgentReloadBus()
        _registry = AgentRegistry(db_path=_db)
        _watcher = SkillFileWatcher(
            watch_dir=_kb,
            reload_bus=_reload_bus,
            registry=_registry,
            debounce_seconds=5,
        )
        _watcher.start()
        print(f"  [Startup] SkillFileWatcher started on: {_kb} ✓")
    except Exception as e:
        print(f"  [Startup] SkillFileWatcher skipped: {e}")

    # 5. Start WebhookDispatcher for reliable outbound delivery
    global _webhook_dispatcher
    _webhook_dispatcher = WebhookDispatcher(max_retries=5, base_delay=1.0)
    _webhook_dispatcher.start()
    print("  [Startup] WebhookDispatcher started ✓")
    conn_logger.info("WebhookDispatcher started during API startup")

    # 6.5. Run expiry sweep (clean stale session facts + expired units)
    try:
        from memory import MemoryManager as _MM
        _mem = _MM(SQLiteStore(db_path=_db))
        _expiry = _mem.run_expiry_sweep()
        _total_expired = _expiry.get('total_cleaned', 0)
        if _total_expired > 0:
            print(f"  [Startup] Expiry sweep: {_total_expired} stale items cleaned ✓")
        else:
            print("  [Startup] Expiry sweep: nothing to clean ✓")
    except Exception as e:
        print(f"  [Startup] Expiry sweep skipped: {e}")

    # 6. Mount MCP SSE server at /mcp (unified port, shared auth)
    try:
        from mcp_server import mcp as _mcp_server
        _mcp_sse_app = _mcp_server.sse_app(mount_path="/mcp")
        app.mount("/mcp", _mcp_sse_app)
        print("  [Startup] MCP SSE server mounted at /mcp ✓")
    except Exception as e:
        print(f"  [Startup] MCP SSE mount skipped: {e}")

    yield  # App runs here

    # Cleanup
    if _watcher:
        _watcher.stop()
    if _webhook_dispatcher:
        _webhook_dispatcher.stop()
    print("  [Shutdown] Cortex stopping cleanly.")


app = FastAPI(
    title="Cortex AI — Organizational Intelligence Engine",
    lifespan=_lifespan,
)


# ─── API Key Auth ─────────────────────────────────────────────────────────────
# Set CORTEX_API_KEY env var to enable auth. Unset = open (dev mode).
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_CORTEX_API_KEY = os.getenv("CORTEX_API_KEY", "")

async def _require_api_key(key: Optional[str] = Depends(_API_KEY_HEADER)):
    """Dependency: validates X-API-Key header when CORTEX_API_KEY is set."""
    if not _CORTEX_API_KEY:
        return  # Dev mode — no key required
    if key != _CORTEX_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Set X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

# Bug 1 fix: proper HTTP middleware — works for ALL routes regardless of
# how they are registered. The old add_api_route monkey-patch was bypassed
# by include_router(). A middleware intercepts every HTTP request.
# Open routes (no auth): /api/health, /webhooks/*, /docs, /openapi.json, /redoc
@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    # Always pass through CORS preflight — browser sends OPTIONS with no auth headers
    if request.method == "OPTIONS":
        return await call_next(request)

    # Open routes — no auth needed for reads. The dashboard polls these constantly.
    _OPEN_PREFIXES = (
        "/api/health", "/api/status", "/api/connectors", "/api/database",
        "/api/logs", "/api/memory", "/api/skills", "/api/agents",
        "/api/search", "/api/config", "/api/notion",
        "/api/sync", "/api/providers", "/api/run",
        "/webhooks", "/docs", "/openapi.json", "/redoc",
        "/.well-known",
        "/mcp",  # MCP SSE transport — has its own auth layer
    )

    key = os.getenv("CORTEX_API_KEY", "")
    if key and request.url.path.startswith("/api/"):
        if not any(request.url.path.startswith(p) for p in _OPEN_PREFIXES):
            provided = request.headers.get("X-API-Key", "")
            if provided != key:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing X-API-Key header"},
                )
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phase 4: rate limiter + webhook router
app.add_middleware(RateLimiter)
app.include_router(webhook_router)

base_dir = os.path.dirname(os.path.dirname(__file__))

# Mutable application state
app_state = {
    "data_sources": [os.path.join(base_dir, "raw_data")],
    "anthropic_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "ollama_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "privacy_mode": False,
    "slack_channel": "C1234_ENGINEERING",
    "is_running": False,
    "execution_logs": []
}

# Shared connector manager instance
connector_mgr = ConnectorManager(db_path=os.path.join(base_dir, "database"))


# ═══════════════════════════════════════════════════════════════
# CONFIGURATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

class ConfigModel(BaseModel):
    data_sources: List[str]
    anthropic_key: str
    ollama_url: Optional[str] = "http://localhost:11434"
    privacy_mode: Optional[bool] = False
    slack_channel: str


@app.get("/api/config")
def get_config():
    return {
        "data_sources": app_state["data_sources"],
        "anthropic_key": "********" if app_state["anthropic_key"] else "",
        "ollama_url": app_state["ollama_url"],
        "privacy_mode": app_state["privacy_mode"],
        "slack_channel": app_state["slack_channel"]
    }


@app.post("/api/config")
def update_config(config: ConfigModel):
    app_state["data_sources"] = config.data_sources
    if config.anthropic_key and config.anthropic_key != "********":
        app_state["anthropic_key"] = config.anthropic_key
        os.environ["ANTHROPIC_API_KEY"] = config.anthropic_key
    if config.ollama_url:
        app_state["ollama_url"] = config.ollama_url
        os.environ["OLLAMA_BASE_URL"] = config.ollama_url
    app_state["privacy_mode"] = config.privacy_mode if config.privacy_mode is not None else False
    app_state["slack_channel"] = config.slack_channel
    return {"status": "success", "message": "Configuration updated successfully."}


# ═══════════════════════════════════════════════════════════════
# STATUS & STATS ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/status")
def get_status():
    db_connected = any(os.path.exists(p) for p in app_state["data_sources"])
    return {
        "status": "online",
        "anthropic_api_key_configured": bool(app_state["anthropic_key"]),
        "db_connected": db_connected,
        "is_running": app_state["is_running"],
        "privacy_mode": app_state.get("privacy_mode", False),
    }


@app.get("/api/database/stats")
def get_db_stats():
    try:
        db_path = os.path.join(base_dir, "database")
        store = FilesystemStore(db_path=db_path)
        registry = SkillRegistry(db_path=db_path)
        all_chunks = store.get_all()

        # Compute department breakdown
        dept_counts = {}
        type_counts = {}
        for chunk in all_chunks:
            dept_counts[chunk.department.value] = dept_counts.get(chunk.department.value, 0) + 1
            type_counts[chunk.knowledge_type.value] = type_counts.get(chunk.knowledge_type.value, 0) + 1

        return {
            "total_chunks": len(all_chunks),
            "total_skills": len(registry.list_all_skills()),
            "department_breakdown": dept_counts,
            "type_breakdown": type_counts,
        }
    except Exception as e:
        return {"total_chunks": 0, "total_skills": 0, "error": str(e)}


@app.get("/api/audit/integrity")
def get_audit_integrity():
    try:
        import sqlite3 as _sq3
        import hashlib
        from main import _split_into_claims

        db_path = os.path.join(base_dir, "database")
        
        # 1. Load all chunks from chunks.json (Filesystem)
        fs_store = FilesystemStore(db_path=db_path)
        fs_chunks = fs_store.get_all()
        total_fs_chunks = len(fs_chunks)

        # 2. Simulate active claim splitting and compute deterministic IDs
        active_ids = set()
        active_claims_map = {}
        for chunk in fs_chunks:
            claims = _split_into_claims(chunk.content)
            for claim_text in claims:
                if len(claim_text.strip()) < 10:
                    continue
                _claim_key = f"{claim_text.strip().lower()}|{chunk.department.value}"
                _det_id = f"aku-{hashlib.sha256(_claim_key.encode()).hexdigest()[:12]}"
                active_ids.add(_det_id)
                active_claims_map[_det_id] = {
                    "id": _det_id,
                    "claim": claim_text.strip(),
                    "department": chunk.department.value,
                    "source_identifier": chunk.source_identifier
                }

        total_active_claims = len(active_ids)

        # 3. Fetch all chunks and atomic units in SQLite
        db_file = os.path.join(db_path, "bhrm.db")
        conn = _sq3.connect(db_file)
        conn.row_factory = _sq3.Row
        
        db_chunks = conn.execute("SELECT id, title, source_identifier FROM chunks").fetchall()
        total_db_chunks = len(db_chunks)
        db_chunk_ids = {row["id"] for row in db_chunks}

        db_units = conn.execute("SELECT id, claim, department, source_identifier, status, created_at FROM atomic_units").fetchall()
        total_db_units = len(db_units)
        
        conn.close()

        # 4. Identify orphans in SQLite atomic_units
        orphaned_units = []
        for unit in db_units:
            uid = unit["id"]
            if uid not in active_ids:
                orphaned_units.append({
                    "id": uid,
                    "claim": unit["claim"],
                    "department": unit["department"],
                    "source_identifier": unit["source_identifier"],
                    "status": unit["status"],
                    "created_at": unit["created_at"]
                })

        # 5. Identify historical chunks in chunks.json not in SQLite chunks table
        missing_chunks = []
        for chunk in fs_chunks:
            if chunk.id not in db_chunk_ids:
                missing_chunks.append({
                    "id": chunk.id,
                    "title": chunk.title,
                    "source_identifier": chunk.source_identifier
                })

        return {
            "total_filesystem_chunks": total_fs_chunks,
            "total_sqlite_chunks": total_db_chunks,
            "total_sqlite_atomic_units": total_db_units,
            "total_active_simulated_claims": total_active_claims,
            "total_orphaned_units": len(orphaned_units),
            "total_missing_chunks": len(missing_chunks),
            "orphaned_units": orphaned_units,
            "missing_chunks": missing_chunks
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/audit/prune")
def prune_orphaned_units():
    try:
        import sqlite3 as _sq3
        import hashlib
        from main import _split_into_claims

        db_path = os.path.join(base_dir, "database")
        
        # Load all chunks from chunks.json
        fs_store = FilesystemStore(db_path=db_path)
        fs_chunks = fs_store.get_all()

        # Simulate active claim splitting and compute deterministic IDs
        active_ids = set()
        for chunk in fs_chunks:
            claims = _split_into_claims(chunk.content)
            for claim_text in claims:
                if len(claim_text.strip()) < 10:
                    continue
                _claim_key = f"{claim_text.strip().lower()}|{chunk.department.value}"
                _det_id = f"aku-{hashlib.sha256(_claim_key.encode()).hexdigest()[:12]}"
                active_ids.add(_det_id)

        # Connect and delete orphans
        db_file = os.path.join(db_path, "bhrm.db")
        conn = _sq3.connect(db_file)
        
        before = conn.execute("SELECT COUNT(*) FROM atomic_units").fetchone()[0]
        
        if active_ids:
            placeholders = ",".join("?" for _ in active_ids)
            conn.execute(f"DELETE FROM atomic_units WHERE id NOT IN ({placeholders})", list(active_ids))
        else:
            conn.execute("DELETE FROM atomic_units")
            
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM atomic_units").fetchone()[0]
        conn.close()

        return {
            "success": True,
            "pruned_count": before - after,
            "remaining_count": after
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ═══════════════════════════════════════════════════════════════
# SKILLS & AGENTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/skills")
def get_skills():
    db_path = os.path.join(base_dir, "database")
    registry = SkillRegistry(db_path=db_path)
    return {"skills": registry.list_all_skills()}


@app.get("/api/chunks")
def get_chunks(department: Optional[str] = None):
    """List all chunks, optionally filtered by department."""
    db_path = os.path.join(base_dir, "database")
    store = FilesystemStore(db_path=db_path)

    if department:
        try:
            dept = Department(department.lower())
            chunks = store.get_all_by_department(dept)
        except ValueError:
            chunks = store.get_all()
    else:
        chunks = store.get_all()

    return {
        "chunks": [
            {
                "id": c.id,
                "title": c.title,
                "department": c.department.value,
                "knowledge_type": c.knowledge_type.value,
                "confidence": c.metadata.confidence_score,
                "summary": c.summary[:200],
                "source": c.source_identifier,
                "processing_layer": c.processing_layer.value,
            }
            for c in chunks
        ]
    }


# ═══════════════════════════════════════════════════════════════
# ATOMIC UNITS (10-LAYER PIPELINE MEMORY)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/memory/units")
def get_atomic_units(
    department: Optional[str] = None,
    status: Optional[str] = None,
    requesting_dept: Optional[str] = None,
):
    """
    Return atomic knowledge units from the 10-layer pipeline memory.

    Query params:
      department:      filter by department slug (e.g. 'engineering', 'hr').
                       Pass 'all' or omit to return every department.
      status:          filter by unit status ('active', 'approved', 'contested', 'rejected').
      requesting_dept: ACL filter — only return units allowed for this department.
    """
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)

    stat_enum = None
    if status:
        try:
            stat_enum = UnitStatus(status.lower())
        except ValueError:
            pass

    # dept=all (or no dept) → collect from every department
    if not department or department.lower() == "all":
        all_units = []
        for dept in Department:
            all_units.extend(store.get_units_by_department(dept, status=stat_enum))
        units = all_units
    else:
        try:
            dept = Department(department.lower())
        except ValueError:
            return {"error": f"Invalid department '{department}'. Valid values: {[d.value for d in Department]} or 'all'"}
        units = store.get_units_by_department(dept, status=stat_enum)

    # ACL filter: if requesting_dept provided, only return allowed units
    if requesting_dept:
        acl_store = ACLStore(db_path=db_path)
        unit_ids = [u.id for u in units]
        allowed_ids = set(acl_store.filter_allowed_ids(unit_ids, requesting_department=requesting_dept))
        units = [u for u in units if u.id in allowed_ids]

    return {
        "units": [
            {
                "id": u.id,
                "claim": u.claim,
                "instruction": u.instruction,
                "department": u.department.value,
                "confidence": u.confidence_score,
                "status": u.status.value,
                "memory_tier": u.memory_tier.value,
                "source": u.source_identifier,
                "conflicts": u.conflicts_with,
                "created_at": u.created_at.isoformat()
            }
            for u in units
        ],
        "total": len(units),
        "department_filter": department or "all",
        "acl_filtered": requesting_dept is not None,
    }

@app.get("/api/memory/conflicts")
def get_conflicts():
    """
    List all contested knowledge units that need human resolution.
    Returns each conflict with the unit that caused it, so you can approve/reject.
    """
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)
    # Query contested units directly (avoids loading all units into memory)
    import sqlite3 as _sq3
    _conn = _sq3.connect(os.path.join(db_path, "bhrm.db"))
    _conn.row_factory = _sq3.Row
    _rows = _conn.execute(
        "SELECT id FROM atomic_units WHERE status = 'contested'"
    ).fetchall()
    _conn.close()
    contested = [u for uid in _rows if (u := store.get_unit_by_id(uid["id"]))]
    result = []
    for unit in contested:
        conflicting_units = []
        for cid in unit.conflicts_with:
            cu = store.get_unit_by_id(cid)
            if cu:
                conflicting_units.append({
                    "id": cu.id,
                    "claim": cu.claim,
                    "status": cu.status.value,
                    "confidence": cu.confidence_score,
                })
        result.append({
            "id": unit.id,
            "claim": unit.claim,
            "instruction": unit.instruction,
            "department": unit.department.value,
            "confidence": unit.confidence_score,
            "source": unit.source_identifier,
            "conflicts_with": conflicting_units,
            "resolution": "POST /api/memory/units/{id}/approve or /reject",
        })

    return {
        "total_conflicts": len(result),
        "conflicts": result,
        "tip": "Approve the correct version, reject the wrong one. Approved units go to canonical memory.",
    }


@app.post("/api/memory/units/{unit_id}/approve")
def approve_unit(unit_id: str):
    """Approve a knowledge unit — promotes it to canonical memory tier."""
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)
    unit = store.get_unit_by_id(unit_id)
    if not unit:
        raise HTTPException(status_code=404, detail=f"Unit {unit_id} not found")

    # Promote to canonical
    unit.status = UnitStatus.APPROVED
    from core.models import MemoryTier
    unit.memory_tier = MemoryTier.CANONICAL
    store.save_unit(unit)

    # Log audit trail
    from core.models import AuditEntry
    import uuid as _uuid
    store.log_audit(AuditEntry(
        id=_uuid.uuid4().hex[:12],
        action="unit_approved", target_type="unit",
        target_id=unit_id, actor="api_user",
        details={"claim": unit.claim[:100]},
    ))

    return {
        "status": "success",
        "action": "approved",
        "unit_id": unit_id,
        "memory_tier": "canonical",
        "claim": unit.claim,
        "message": f"Unit approved and moved to canonical memory.",
    }


@app.post("/api/memory/units/{unit_id}/reject")
def reject_unit(unit_id: str):
    """Reject a knowledge unit — marks it as rejected, removes from active use."""
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)
    unit = store.get_unit_by_id(unit_id)
    if not unit:
        raise HTTPException(status_code=404, detail=f"Unit {unit_id} not found")

    unit.status = UnitStatus.REJECTED
    store.save_unit(unit)

    from core.models import AuditEntry
    import uuid as _uuid
    store.log_audit(AuditEntry(
        id=_uuid.uuid4().hex[:12],
        action="unit_rejected", target_type="unit",
        target_id=unit_id, actor="api_user",
        details={"claim": unit.claim[:100]},
    ))

    return {
        "status": "success",
        "action": "rejected",
        "unit_id": unit_id,
        "claim": unit.claim,
        "message": f"Unit rejected and removed from active knowledge.",
    }


class AgentAssignModel(BaseModel):
    agent_name: str
    skill_name: str


@app.post("/api/agents/assign")
def assign_agent_legacy(data: AgentAssignModel):
    """Legacy endpoint — kept for backward compat. Use POST /api/agents/{id}/bind."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    bound = reg.bind_skill(data.agent_name, data.skill_name)
    if bound:
        return {"status": "success", "message": f"Bound '{data.skill_name}' to '{data.agent_name}'."}
    return {"status": "success", "message": f"'{data.skill_name}' already bound to '{data.agent_name}'."}


# ═══════════════════════════════════════════════════════════════
# LAYER 11 — AGENT ORCHESTRATION API
# ═══════════════════════════════════════════════════════════════

@app.get("/api/agents/reload-events")
def get_reload_events():
    """Get recent skill reload events across all agents."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    events = reg.get_reload_events(limit=50)
    return {"events": events, "total": len(events)}


@app.get("/api/agents")
def list_agents():
    """List all registered agents with their profiles, skills, and status."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    agents = reg.list_all()
    return {
        "agents": [
            {
                "agent_id": a.agent_id,
                "display_name": a.display_name,
                "icon": a.icon,
                "role": a.role.value,
                "department": a.department,
                "description": a.description,
                "bound_skills": a.bound_skills,
                "skill_count": len(a.bound_skills),
                "tools_allowlist": a.tools_allowlist,
                "tools_denylist": a.tools_denylist,
                "sensitivity_ceiling": a.sensitivity_ceiling,
                "auto_reload": a.auto_reload_on_skill_change,
                "last_reloaded_at": a.last_reloaded_at.isoformat() if a.last_reloaded_at else None,
                "context_ready": a.context_ready,
                "can_delegate_to": a.can_delegate_to,
                "auto_bind_departments": a.auto_bind_departments,
                "created_at": a.created_at.isoformat(),
                "webhook_url": getattr(a, 'webhook_url', None),
            }
            for a in agents
        ],
        "total": len(agents),
        "summary": reg.get_summary(),
    }


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    """Get a single agent's full profile."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    agent = reg.get(agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_id}' not found."})
    return {
        "agent_id": agent.agent_id,
        "display_name": agent.display_name,
        "icon": agent.icon,
        "role": agent.role.value,
        "department": agent.department,
        "description": agent.description,
        "bound_skills": agent.bound_skills,
        "tools_allowlist": agent.tools_allowlist,
        "tools_denylist": agent.tools_denylist,
        "mcp_servers": agent.mcp_servers,
        "sensitivity_ceiling": agent.sensitivity_ceiling,
        "max_context_tokens": agent.max_context_tokens,
        "online_llm_allowed": agent.online_llm_allowed,
        "auto_reload": agent.auto_reload_on_skill_change,
        "reload_debounce_seconds": agent.reload_debounce_seconds,
        "can_delegate_to": agent.can_delegate_to,
        "accepts_tasks_from": agent.accepts_tasks_from,
        "auto_bind_departments": agent.auto_bind_departments,
        "last_reloaded_at": agent.last_reloaded_at.isoformat() if agent.last_reloaded_at else None,
        "context_ready": agent.context_ready,
        "webhook_url": agent.webhook_url,
        "auth_token": agent.auth_token,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


class CreateAgentRequest(BaseModel):
    agent_id: str
    display_name: str
    icon: Optional[str] = "🤖"
    role: Optional[str] = "specialist"
    department: Optional[str] = "shared"
    description: Optional[str] = ""
    tools_allowlist: Optional[List[str]] = []
    tools_denylist: Optional[List[str]] = []
    sensitivity_ceiling: Optional[str] = "internal"
    max_context_tokens: Optional[int] = 8000
    auto_bind_departments: Optional[List[str]] = []


@app.post("/api/agents")
def create_agent(data: CreateAgentRequest):
    """Create a new agent."""
    from agents.registry import AgentRegistry
    from core.models import AgentProfile, AgentRole
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))

    # Check if agent already exists
    existing = reg.get(data.agent_id)
    if existing:
        return JSONResponse(status_code=409, content={"error": f"Agent '{data.agent_id}' already exists."})

    try:
        role = AgentRole(data.role)
    except ValueError:
        role = AgentRole.SPECIALIST

    profile = AgentProfile(
        agent_id=data.agent_id,
        display_name=data.display_name,
        icon=data.icon or "🤖",
        role=role,
        department=data.department or "shared",
        description=data.description or "",
        tools_allowlist=data.tools_allowlist or [],
        tools_denylist=data.tools_denylist or [],
        sensitivity_ceiling=data.sensitivity_ceiling or "internal",
        max_context_tokens=data.max_context_tokens or 8000,
        auto_bind_departments=data.auto_bind_departments or [],
    )
    reg.register(profile)
    return {"status": "success", "message": f"Agent '{data.agent_id}' created.", "agent_id": data.agent_id}


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Delete an agent from the registry."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    deleted = reg.delete(agent_id)
    if deleted:
        return {"status": "success", "message": f"Agent '{agent_id}' deleted."}
    return JSONResponse(status_code=404, content={"error": f"Agent '{agent_id}' not found."})


class SkillBindRequest(BaseModel):
    skill_name: str


@app.post("/api/agents/{agent_id}/bind")
def bind_skill_to_agent(agent_id: str, data: SkillBindRequest):
    """Bind a skill to an agent."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    bound = reg.bind_skill(agent_id, data.skill_name)
    if bound:
        return {"status": "success", "message": f"Skill '{data.skill_name}' bound to '{agent_id}'."}
    return {"status": "success", "message": f"Skill '{data.skill_name}' already bound to '{agent_id}'."}


@app.post("/api/agents/{agent_id}/unbind")
def unbind_skill_from_agent(agent_id: str, data: SkillBindRequest):
    """Unbind a skill from an agent."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    removed = reg.unbind_skill(agent_id, data.skill_name)
    if removed:
        return {"status": "success", "message": f"Skill '{data.skill_name}' unbound from '{agent_id}'."}
    return JSONResponse(status_code=404, content={"error": f"Skill '{data.skill_name}' not bound to '{agent_id}'."})


@app.post("/api/agents/{agent_id}/reload")
def reload_agent(agent_id: str):
    """Trigger context rebuild + hot-reload for an agent."""
    from agents.registry import AgentRegistry
    from agents.context_assembler import AgentContextAssembler
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))

    agent = reg.get(agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_id}' not found."})

    # Assemble context
    kb_dir = os.path.join(base_dir, "knowledge_base")
    assembler = AgentContextAssembler(knowledge_base_dir=kb_dir)
    context = assembler.assemble(agent)

    # Save assembled context
    ctx_dir = os.path.join(base_dir, "database", "agent_contexts")
    os.makedirs(ctx_dir, exist_ok=True)
    ctx_path = os.path.join(ctx_dir, f"{agent_id}.md")
    with open(ctx_path, "w", encoding="utf-8") as f:
        f.write(context)

    # Record reload
    reg.record_reload(agent_id, ctx_path, success=True)

    return {
        "status": "success",
        "message": f"Context rebuilt for '{agent_id}'.",
        "context_length": len(context),
        "context_tokens_approx": len(context) // 4,
        "context_path": ctx_path,
    }


@app.get("/api/agents/{agent_id}/context")
def get_agent_context(agent_id: str):
    """Get the assembled context document for an agent."""
    from agents.registry import AgentRegistry
    from agents.context_assembler import AgentContextAssembler
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))

    agent = reg.get(agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_id}' not found."})

    kb_dir = os.path.join(base_dir, "knowledge_base")
    assembler = AgentContextAssembler(knowledge_base_dir=kb_dir)

    # Try to serve from cache first
    ctx_path = os.path.join(base_dir, "database", "agent_contexts", f"{agent_id}.md")
    if os.path.isfile(ctx_path):
        with open(ctx_path, "r", encoding="utf-8") as f:
            cached_context = f.read()
        return {
            "agent_id": agent_id,
            "context": cached_context,
            "source": "cached",
            "length": len(cached_context),
            "tokens_approx": len(cached_context) // 4,
        }

    # Build fresh
    context = assembler.assemble(agent)
    return {
        "agent_id": agent_id,
        "context": context,
        "source": "assembled_live",
        "length": len(context),
        "tokens_approx": len(context) // 4,
    }


@app.get("/api/agents/{agent_id}/agent-card")
def get_agent_card(agent_id: str):
    """Get the A2A-compatible AgentCard JSON."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    card = reg.get_agent_card(agent_id)
    if not card:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_id}' not found."})
    return card


@app.get("/api/agents/{agent_id}/delegation-targets")
def get_delegation_targets(agent_id: str):
    """Get all agents this agent can delegate to (validated both directions)."""
    from agents.registry import AgentRegistry
    from agents.delegator import AgentDelegator
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    delegator = AgentDelegator(registry=reg)
    targets = delegator.get_delegation_targets(agent_id)
    return {"agent_id": agent_id, "delegation_targets": targets}


# ═══════════════════════════════════════════════════════════════
# A2A PROTOCOL — DISCOVERY & EXTERNAL AGENT SUPPORT
# ═══════════════════════════════════════════════════════════════

@app.get("/.well-known/agent.json")
def a2a_discovery():
    """A2A Protocol discovery endpoint.

    External agents and orchestrators call this to discover
    what agents and capabilities this Cortex instance exposes.
    Standard: https://google.github.io/A2A/
    """
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    agents = reg.list_all()

    skills = []
    for a in agents:
        skills.append({
            "id": a.agent_id,
            "name": a.display_name,
            "description": a.description,
        })

    _port = os.getenv('CORTEX_PORT', '8100')
    return {
        "name": "Cortex AI — Organizational Intelligence Engine",
        "description": "Multi-agent knowledge system with 10-layer pipeline, semantic search, and agent orchestration.",
        "url": f"http://localhost:{_port}",
        "version": "2.11.0",
        "skills": skills,
        "capabilities": {
            "streaming": False,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
            },
            "webhookHmac": {
                "type": "hmac",
                "algorithm": "sha256",
                "header": "X-Cortex-Signature",
                "description": "Set CORTEX_WEBHOOK_SECRET to enable HMAC-signed outbound webhooks.",
            },
        },
        "mcp": {
            "sse_endpoint": f"http://localhost:{_port}/mcp/sse",
            "stdio_command": "python3 backend/src/mcp_server.py",
            "description": "Model Context Protocol server for AI agent tool access.",
        },
        "endpoints": {
            "register": "/api/agents/external/register",
            "submit_task": "/api/agents/{agent_id}/tasks",
            "task_result": "/api/tasks/{task_id}/result",
            "list_agents": "/api/agents",
            "agent_card": "/api/agents/{agent_id}/agent-card",
            "feedback": "/api/feedback",
            "mcp_sse": "/mcp/sse",
        },
    }


class ExternalAgentRegistration(BaseModel):
    """Registration payload for an external agent connecting to Cortex."""
    agent_id: str
    display_name: str
    description: str
    url: str                                    # The external agent's callback URL
    icon: Optional[str] = "🌐"
    role: Optional[str] = "specialist"
    department: Optional[str] = "shared"
    skills: Optional[List[Dict[str, str]]] = []  # [{id, name, description}]
    supported_input_modes: Optional[List[str]] = ["text/plain"]
    supported_output_modes: Optional[List[str]] = ["text/plain"]
    auth_token: Optional[str] = None             # Token to authenticate callbacks to this agent


@app.post("/api/agents/external/register")
def register_external_agent(data: ExternalAgentRegistration):
    """Register an external agent with Cortex.

    External agents (Claude, GPT, custom bots, etc.) call this endpoint
    to announce themselves and become available for task delegation.
    The agent must provide a callback `url` where Cortex can POST tasks.
    """
    from agents.registry import AgentRegistry
    from core.models import AgentProfile, AgentRole
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))

    # Check for ID collision with internal agents
    existing = reg.get(data.agent_id)
    if existing and not getattr(existing, 'webhook_url', None):
        return JSONResponse(
            status_code=409,
            content={"error": f"Agent ID '{data.agent_id}' is reserved for an internal agent. Use a unique ID."}
        )

    try:
        role = AgentRole(data.role)
    except ValueError:
        role = AgentRole.SPECIALIST

    profile = AgentProfile(
        agent_id=data.agent_id,
        display_name=data.display_name,
        icon=data.icon or "🌐",
        role=role,
        department=data.department or "shared",
        description=data.description,
        webhook_url=data.url,                    # External callback URL
        auth_token=data.auth_token,              # Bearer token for outbound calls
        tools_allowlist=["cortex_search"],        # Minimal default permissions
        tools_denylist=[],
        sensitivity_ceiling="internal",
        max_context_tokens=8000,
        accepts_tasks_from=["orchestrator"],      # Orchestrator can delegate to it
        context_ready=True,                       # External agents manage own context
    )
    reg.register(profile)
    conn_logger.info(f"External agent registered: {data.agent_id} ({data.display_name}) at {data.url}")

    # Also make orchestrator aware it can delegate to this agent
    orchestrator = reg.get("orchestrator")
    if orchestrator and data.agent_id not in orchestrator.can_delegate_to:
        orchestrator.can_delegate_to.append(data.agent_id)
        reg.register(orchestrator)

    return {
        "status": "registered",
        "message": f"External agent '{data.display_name}' registered successfully.",
        "agent_id": data.agent_id,
        "agent_card_url": f"/api/agents/{data.agent_id}/agent-card",
        "task_submission_url": f"/api/agents/{data.agent_id}/tasks",
    }


class TaskSubmission(BaseModel):
    """A task submitted to an agent (by another agent or external system)."""
    task_type: Optional[str] = "query"
    description: str
    payload: Optional[Dict[str, Any]] = {}
    from_agent_id: Optional[str] = None
    callback_url: Optional[str] = None           # Where to POST results when done


@app.post("/api/agents/{agent_id}/tasks")
def submit_task_to_agent(agent_id: str, task: TaskSubmission):
    """Submit a task to a specific agent.

    This is the main entry point for agent-to-agent communication.
    External agents, the orchestrator, or the dashboard can all
    submit tasks here. The task is queued and processed async.
    """
    from agents.registry import AgentRegistry
    from agents.delegator import AgentDelegator, DelegatedTask
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))

    target = reg.get(agent_id)
    if not target:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_id}' not found."})

    # Authorization: if from_agent_id is set, validate delegation permission
    if task.from_agent_id:
        source = reg.get(task.from_agent_id)
        if not source:
            return JSONResponse(status_code=404, content={"error": f"Source agent '{task.from_agent_id}' not found."})

        delegator = AgentDelegator(registry=reg)
        if not delegator.can_delegate(task.from_agent_id, agent_id):
            return JSONResponse(
                status_code=403,
                content={"error": f"Agent '{task.from_agent_id}' is not authorized to delegate to '{agent_id}'."}
            )

    # Log the task
    import uuid as _uuid
    task_id = str(_uuid.uuid4())[:12]
    reg.log_delegation(
        from_agent_id=task.from_agent_id or "external",
        to_agent_id=agent_id,
        task_type=task.task_type or "query",
        outcome="received",
    )

    # If agent has a webhook (external agent), forward via dispatcher
    if target.webhook_url:
        conn_logger.info(f"Dispatching task {task_id} to external agent {agent_id} at {target.webhook_url}")
        webhook_payload = {
            "task_id": task_id,
            "task_type": task.task_type,
            "description": task.description,
            "payload": task.payload,
            "callback_url": task.callback_url or f"/api/tasks/{task_id}/result",
        }
        headers = build_authenticated_headers(
            agent_auth_token=getattr(target, 'auth_token', None),
            payload=webhook_payload,
        )

        if _webhook_dispatcher:
            _webhook_dispatcher.enqueue(
                url=target.webhook_url,
                payload=webhook_payload,
                headers=headers,
                task_id=task_id,
                agent_id=agent_id,
            )
            return {
                "status": "dispatched",
                "task_id": task_id,
                "message": f"Task dispatched to external agent '{agent_id}' via retry queue.",
                "webhook_url": target.webhook_url,
                "callback_url": f"/api/tasks/{task_id}/result",
            }
        else:
            # Fallback: direct POST (no retry)
            try:
                import requests as _req
                resp = _req.post(
                    target.webhook_url, json=webhook_payload,
                    headers=headers, timeout=10,
                )
                return {
                    "status": "forwarded",
                    "task_id": task_id,
                    "message": f"Task forwarded to external agent '{agent_id}'.",
                    "agent_response_status": resp.status_code,
                }
            except Exception as e:
                return {
                    "status": "forward_failed",
                    "task_id": task_id,
                    "message": f"Failed to forward task to '{agent_id}': {e}",
                }

    # Internal agent: queue the task (for now, return acknowledgment)
    return {
        "status": "queued",
        "task_id": task_id,
        "message": f"Task queued for internal agent '{agent_id}'. Processing will be triggered on next pipeline run or manual reload.",
        "agent_id": agent_id,
        "task_type": task.task_type,
    }


@app.post("/api/agents/delegate")
def delegate_task(data: dict):
    """Trigger a delegation from one agent to another.

    Body: {from_agent_id, to_agent_id, task_type, description, unit_id?}
    """
    from agents.registry import AgentRegistry
    from agents.delegator import AgentDelegator, DelegatedTask, DelegationNotPermittedError
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    delegator = AgentDelegator(registry=reg)

    from_id = data.get("from_agent_id", "")
    to_id = data.get("to_agent_id", "")
    if not from_id or not to_id:
        return JSONResponse(status_code=400, content={"error": "from_agent_id and to_agent_id are required."})

    source = reg.get(from_id)
    if not source:
        return JSONResponse(status_code=404, content={"error": f"Source agent '{from_id}' not found."})

    task = DelegatedTask(
        task_type=data.get("task_type", "delegation"),
        description=data.get("description", ""),
        unit_id=data.get("unit_id"),
    )

    try:
        result = delegator.delegate(source, to_id, task)
        return {
            "status": "success",
            "task_id": result.task_id,
            "delegation_status": result.status,
            "message": f"Task delegated from '{from_id}' to '{to_id}'.",
        }
    except DelegationNotPermittedError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})




# ═══════════════════════════════════════════════════════════════
# TASK RESULT CALLBACK — External agents POST results here
# ═══════════════════════════════════════════════════════════════

class TaskResultPayload(BaseModel):
    """Payload an external agent sends when it completes a task."""
    agent_id: str
    status: str = "completed"           # "completed" or "failed"
    result: Optional[str] = None
    error: Optional[str] = None


@app.post("/api/tasks/{task_id}/result")
def receive_task_result(task_id: str, payload: TaskResultPayload):
    """Receive a task completion result from an external agent.

    External agents call this endpoint after processing a delegated task.
    The result is stored in the task_results table and the delegation
    outcome is updated. Failed tasks are injected into the Failure Memory Loop.
    """
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))

    # Store the result
    conn_logger.info(f"Received result for task {task_id} from agent {payload.agent_id} (status: {payload.status})")
    reg.save_task_result(
        task_id=task_id,
        agent_id=payload.agent_id,
        status=payload.status,
        result_text=payload.result,
        error_text=payload.error,
    )

    # Update delegation outcome
    reg.update_delegation_outcome(task_id, payload.status)

    # If failed, inject into Failure Memory Loop
    if payload.status == "failed" and payload.error:
        try:
            raw_correction = (
                f"EXTERNAL AGENT FAILURE (task {task_id}):\n"
                f"Agent: {payload.agent_id}\n"
                f"Error: {payload.error}"
            )
            from core.models import Department as _Dept
            webhook_payload = WebhookPayload(
                source="external_agent_failure",
                channel_or_page=f"task-{task_id}",
                content=raw_correction,
                department="shared",
            )
            handle_webhook_ingestion(webhook_payload)
        except Exception as e:
            print(f"[TaskResult] Failed to inject failure: {e}")

    return {
        "status": "accepted",
        "task_id": task_id,
        "message": f"Result from '{payload.agent_id}' recorded ({payload.status}).",
    }


@app.get("/api/tasks/{task_id}/result")
def get_task_result(task_id: str):
    """Retrieve the result of a previously submitted task."""
    from agents.registry import AgentRegistry
    reg = AgentRegistry(db_path=os.path.join(base_dir, "database"))
    result = reg.get_task_result(task_id)
    if not result:
        return JSONResponse(status_code=404, content={"error": f"No result for task '{task_id}'."})
    return result


@app.get("/api/webhooks/dispatcher/status")
def get_dispatcher_status():
    """Get the webhook dispatcher status and dead-lettered deliveries."""
    if not _webhook_dispatcher:
        return {"status": "not_running", "queue_size": 0, "dead_letters": []}
    return {
        "status": "running",
        "queue_size": _webhook_dispatcher.queue_size(),
        "dead_letters": _webhook_dispatcher.get_dead_letters(limit=20),
    }


# ═══════════════════════════════════════════════════════════════
# APP CONNECTORS
# ═══════════════════════════════════════════════════════════════

class ConnectRequest(BaseModel):
    credentials: Optional[Dict[str, str]] = None


@app.get("/api/connectors")
def list_connectors():
    """List all available connectors and their connection status."""
    return {"connectors": connector_mgr.get_all_connectors()}


@app.post("/api/connectors/{app_id}/connect")
def connect_app(app_id: str, body: Optional[ConnectRequest] = None, background_tasks: BackgroundTasks = None):
    """
    Connect an app. Tier 1 apps auto-detect, Tier 2 apps need credentials.
    On success, auto-triggers a background sync so data flows immediately.
    """
    creds = body.credentials if body else None
    result = connector_mgr.connect(app_id, creds)

    # Auto-sync on successful connect — only sync THIS app, not all
    if result.get("status") == "connected" and background_tasks:
        SYNC_CONNECTORS = {"notion", "slack", "google_drive", "confluence", "jira", "linear", "ms_teams"}
        if app_id in SYNC_CONNECTORS:
            _sync_app_id = app_id  # capture for closure
            def _auto_sync():
                import time as _t
                _t.sleep(1)  # Let the response return first
                try:
                    print(f"\n[AutoSync] {_sync_app_id} connected \u2014 starting initial sync...")
                    chunks = connector_mgr.ingest_single(_sync_app_id)
                    if chunks:
                        print(f"[AutoSync] Got {len(chunks)} chunks from {_sync_app_id}, running pipeline...")
                        from main import run_orchestration_loop
                        run_orchestration_loop()
                    else:
                        print(f"[AutoSync] No chunks from {_sync_app_id} yet.")
                except Exception as e:
                    print(f"[AutoSync] Error: {e}")
            background_tasks.add_task(_auto_sync)
            result["syncing"] = True
            result["message"] += " Initial sync started in background."

    return result



@app.post("/api/connectors/{app_id}/disconnect")
def disconnect_app(app_id: str):
    """Disconnect an app and remove its stored credentials."""
    return connector_mgr.disconnect(app_id)


@app.post("/api/connectors/{app_id}/test")
def test_connector(app_id: str, body: ConnectRequest):
    """Validate credentials without saving them."""
    return connector_mgr.test_connection(app_id, body.credentials or {})


# ═══════════════════════════════════════════════════════════════
# PIPELINE EXECUTION
# ═══════════════════════════════════════════════════════════════

def execute_pipeline():
    """Background task that runs the 10-layer knowledge pipeline.

    Layers: Ingest → Normalize → Chunk → Distill → Dedup →
            Synthesize → Privacy → Atomic → Conflict → Memory
    """
    import threading

    app_state["is_running"] = True
    app_state["execution_logs"].clear()

    class LiveLogStream:
        """Thread-safe stdout wrapper that captures output to a list in real time."""
        def __init__(self, original_stdout, log_list):
            self.original = original_stdout
            self.log_list = log_list
            self._lock = threading.Lock()

        def write(self, s):
            if s:  # Skip empty writes
                self.original.write(s)
                with self._lock:
                    self.log_list.append(s)

        def flush(self):
            self.original.flush()

        # Forward any attribute lookups to the original stdout
        def __getattr__(self, name):
            return getattr(self.original, name)

    old_stdout = sys.stdout
    sys.stdout = LiveLogStream(sys.stdout, app_state["execution_logs"])

    try:
        run_orchestration_loop(
            data_sources=app_state["data_sources"],
            slack_channel=app_state["slack_channel"]
        )
    except Exception as e:
        print(f"\n[ERROR] Pipeline failed: {str(e)}")
    finally:
        sys.stdout = old_stdout
        app_state["is_running"] = False

    # Bug fix: reindex AFTER pipeline completes, not via a separate 5s-sleep task
    try:
        vs = VectorStore(db_path=os.path.join(base_dir, "database"))
        count = vs.index_from_store(db_path=os.path.join(base_dir, "database"))
        if count > 0:
            print(f"  [Search] Auto-indexed {count} new chunks after pipeline run.")
    except Exception as e:
        print(f"  [Search] Auto-index failed (non-fatal): {e}")


@app.post("/api/run")
def run_pipeline(background_tasks: BackgroundTasks):
    if app_state["is_running"]:
        return {"status": "error", "message": "Pipeline is already running."}

    background_tasks.add_task(execute_pipeline)

    return {"status": "success", "message": "10-layer pipeline started. Search index will auto-update."}


@app.get("/api/logs")
def get_logs():
    return {"logs": "".join(app_state["execution_logs"]), "is_running": app_state["is_running"]}


# ═══════════════════════════════════════════════════════════════
# WEBHOOK INGESTION (Real-time push from Slack/Notion)
# ═══════════════════════════════════════════════════════════════

class WebhookPayload(BaseModel):
    source: str                    # "slack" or "notion"
    channel_or_page: str           # Channel name or page ID
    content: str                   # Raw content to process
    department: Optional[str] = None  # Optional department override


@app.post("/api/webhooks/ingest")
def handle_webhook_ingestion(payload: WebhookPayload):
    """
    Real-time webhook endpoint for ingestion.
    Runs a mini-pipeline: Normalize → Chunk → Distill → Dedup → Save
    """
    app_state["execution_logs"].append(f"[Webhook] Received data from {payload.source}\n")

    source_type = SourceType.SLACK if payload.source.lower() == "slack" else SourceType.NOTION
    dept = Department.SHARED
    if payload.department:
        try:
            dept = Department(payload.department.lower())
        except ValueError:
            pass

    # Layer 2: Normalize
    normalizer = TextNormalizer()
    cleaned = normalizer.normalize_document(payload.content)

    # Layer 3: Chunk
    chunker = SemanticChunker(max_tokens=1000, overlap_tokens=100)
    chunks = chunker.chunk_document(cleaned, source_type, payload.channel_or_page, dept)

    if not chunks:
        return {"status": "success", "message": "Content processed. No actionable signals found."}

    # Layer 4: Distill
    db_path = os.path.join(base_dir, "database")
    from providers.router import ProviderRouter
    from providers.hash_cache import HashCache
    router = ProviderRouter()
    cache = HashCache(db_path=db_path)

    distiller = KnowledgeDistiller(router=router, cache=cache, db_path=db_path)
    distilled = distiller.distill_chunks(chunks)

    if not distilled:
        return {"status": "success", "message": "Content processed. All noise, no signals extracted."}

    # Layer 5: Dedup & Save
    store = FilesystemStore(db_path=db_path)
    deduplicator = KnowledgeDeduplicator(router=router, cache=cache, db_path=db_path)
    existing = store.get_all()

    saved = 0
    for chunk in distilled:
        action, processed = deduplicator.evaluate_chunk(chunk, existing)
        if action in ["ADD", "UPDATE"] and processed:
            store.save_chunk(processed)
            saved += 1
            app_state["execution_logs"].append(f"[Webhook] [{action}] {processed.title}\n")

    return {"status": "success", "message": f"Ingested and saved {saved} chunks from {len(distilled)} signals."}


# ═══════════════════════════════════════════════════════════════
# AGENT FEEDBACK LOOP (Failure Memory)
# ═══════════════════════════════════════════════════════════════

class AgentFeedbackRequest(BaseModel):
    skill_name: str
    department: str
    issue_description: str
    suggested_fix: str


@app.post("/api/feedback")
def receive_agent_feedback(feedback: AgentFeedbackRequest, background_tasks: BackgroundTasks):
    """
    Endpoint for agents/humans to report broken workflows.
    Injects the correction back into the pipeline as a FAILURE_PATTERN.
    """
    print(f"[Feedback] Received for {feedback.skill_name}: {feedback.issue_description}")

    # Synthesize the correction into a raw document
    raw_correction = (
        f"FAILURE REPORT for {feedback.skill_name}:\n"
        f"What failed: {feedback.issue_description}\n"
        f"Correction: {feedback.suggested_fix}"
    )

    try:
        dept = Department(feedback.department.lower())
    except ValueError:
        dept = Department.SHARED

    # Inject back into the pipeline via webhook handler
    payload = WebhookPayload(
        source="feedback",
        channel_or_page=f"feedback-{feedback.skill_name}",
        content=raw_correction,
        department=dept.value
    )
    handle_webhook_ingestion(payload)

    return {"status": "processing", "message": "Feedback injected into the Failure Memory Loop for distillation."}


# ═══════════════════════════════════════════════════════════════
# DYNAMIC RUNTIME ASSEMBLY
# ═══════════════════════════════════════════════════════════════

class DynamicAssemblyRequest(BaseModel):
    task_description: str
    department: str


@app.post("/api/assemble")
def dynamic_runtime_assembly(request: DynamicAssemblyRequest):
    """
    Runtime skill composition: Given a task description, dynamically
    assembles a skill payload from relevant chunks without persisting to disk.
    """
    try:
        dept = Department(request.department.lower())
    except ValueError:
        dept = Department.SHARED

    db_path = os.path.join(base_dir, "database")
    store = FilesystemStore(db_path=db_path)
    all_chunks = store.get_all_by_department(dept)

    # Filter by confidence + keyword relevance
    keywords = set(request.task_description.lower().split())
    relevant_chunks = []

    for c in all_chunks:
        if c.metadata.confidence_score >= 0.7:
            chunk_words = set(c.content.lower().split())
            # Always include security rules, or include if keywords match
            if c.knowledge_type == KnowledgeType.SECURITY_RULE or keywords.intersection(chunk_words):
                relevant_chunks.append(c)

    if not relevant_chunks:
        relevant_chunks = [c for c in all_chunks if c.metadata.confidence_score >= 0.7][:10]

    # Assemble in-memory skill
    assembler = SkillAssembler()
    assembled = assembler.assemble_skill(
        skill_name="dynamic-runtime-context",
        description=f"Auto-assembled runtime context for task: {request.task_description}",
        chunks=relevant_chunks,
        department=dept
    )

    # Generate the SKILL.md payload (but don't save to disk)
    generator = SkillGenerator(base_output_dir=os.path.join(base_dir, "knowledge_base"))
    payload = generator.generate_skill_md(assembled, dept)

    return {"status": "success", "runtime_payload": payload}


# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    """
    Full system health — checks every critical subsystem.
    All AI providers are auto-detected — no hardcoded models.
    """
    db_path = os.path.join(base_dir, "database")
    store = FilesystemStore(db_path=db_path)
    chunks = store.get_all()

    # ── AI providers (fully dynamic) ──────────────────────────────
    ai_info = {}
    try:
        from providers.router import ProviderRouter
        router_inst = ProviderRouter()
        providers = router_inst.get_available_providers()
        for p in providers:
            ai_info[p["name"]] = {
                "connected": p.get("available", False),
                "model": p.get("model", "unknown"),
                "cost": p.get("cost", "unknown"),
            }
    except Exception as e:
        ai_info["error"] = str(e)

    any_ai = any(v.get("connected") for k, v in ai_info.items() if k != "error")

    # ── Search check ──────────────────────────────────────────────
    search_stats = {"total_indexed": 0, "embeddings_available": False}
    try:
        vs = VectorStore(db_path=db_path)
        search_stats = vs.get_stats()
    except Exception:
        pass

    # ── Notion check ──────────────────────────────────────────────
    notion_connected = bool(_get_notion_token())

    # ── Auth mode ─────────────────────────────────────────────────
    auth_enabled = bool(_CORTEX_API_KEY)

    setup_needed = []
    if not any_ai:
        setup_needed.append("AI: Install ollama (brew install ollama && ollama pull llama3.2 && ollama serve) or set ANTHROPIC_API_KEY / OPENAI_API_KEY")
    if not search_stats.get("embeddings_available"):
        setup_needed.append("Embeddings: loading in background (will be ready in ~10s)")
    if not notion_connected:
        setup_needed.append("Notion: POST /api/notion/connect with your token")
    if not auth_enabled:
        setup_needed.append("Auth: set CORTEX_API_KEY env var to lock down API")

    # ── Self-training stats ───────────────────────────────────────
    rule_stats = {}
    try:
        from pipeline.rule_manager import DynamicRuleManager
        rm = DynamicRuleManager(db_path=db_path)
        rule_stats = rm.get_stats()
    except Exception:
        pass

    return {
        "status": "ok",
        "canonical_chunks": len(chunks),
        "search": {
            "indexed": search_stats.get("total_indexed", 0),
            "embeddings_ready": search_stats.get("embeddings_available", False),
            "by_department": search_stats.get("by_department", {}),
        },
        "ai": ai_info,
        "self_training": rule_stats,
        "preferred_provider": os.getenv("CORTEX_PREFERRED_PROVIDER", "auto"),
        "connectors": {
            "notion": notion_connected,
        },
        "auth_enabled": auth_enabled,
        "setup_needed": setup_needed,
    }


# ═══════════════════════════════════════════════════════════════
# NOTION — ONE-CLICK CONNECT + REAL SYNC
# ═══════════════════════════════════════════════════════════════

class NotionConnectRequest(BaseModel):
    token: str  # Internal Integration token from notion.so/my-integrations


def _get_notion_token() -> Optional[str]:
    """Read persisted Notion token — CredentialStore first, then .env fallback."""
    # V2 fix: prefer CredentialStore (encrypted) over plaintext .env
    try:
        from core.credential_store import CredentialStore
        cred_store = CredentialStore(db_path=os.path.join(base_dir, "database"))
        creds = cred_store.get_credentials("notion")
        if creds and creds.get("api_key"):
            return creds["api_key"]
    except Exception:
        pass  # Fall through to .env fallback

    # Fallback: .env file (backward compat)
    env_file = os.path.join(base_dir, ".env")
    if os.path.exists(env_file):
        for line in open(env_file).readlines():
            if line.startswith("NOTION_API_KEY="):
                return line.strip().split("=", 1)[1]
    return os.getenv("NOTION_API_KEY", "") or None


def _save_notion_token(token: str):
    """Persist Notion token to CredentialStore (primary) + env var (runtime)."""
    # V2 fix: store in CredentialStore instead of only .env
    try:
        from core.credential_store import CredentialStore
        cred_store = CredentialStore(db_path=os.path.join(base_dir, "database"))
        existing_creds = cred_store.get_credentials("notion") or {}
        existing_creds["api_key"] = token
        cred_store.save_credentials("notion", existing_creds)
    except Exception as e:
        print(f"[Notion] CredentialStore save failed, falling back to .env: {e}")
        # Fallback: write to .env so nothing breaks
        env_file = os.path.join(base_dir, ".env")
        lines = []
        if os.path.exists(env_file):
            lines = [l for l in open(env_file).readlines() if not l.startswith("NOTION_API_KEY=")]
        lines.append(f"NOTION_API_KEY={token}\n")
        with open(env_file, "w") as f:
            f.writelines(lines)
    # Always set live in process so all code paths see the token immediately
    os.environ["NOTION_API_KEY"] = token


@app.post("/api/notion/connect")
async def notion_connect(req: NotionConnectRequest):
    """
    One-click Notion connection.
    Validates the token against Notion API, then persists it.
    Frontend just calls this with the token pasted from notion.so/my-integrations.
    """
    from ingestion.notion_connector import NotionConnector, NotionAPIError
    connector = NotionConnector(token=req.token.strip())
    result = connector.test_connection()
    if not result["ok"]:
        conn_logger.error(f"Notion connection failed: {result['error']}")
        raise HTTPException(status_code=400, detail=result["error"])
    _save_notion_token(req.token.strip())
    conn_logger.info(f"Notion connected successfully for workspace: {result.get('workspace_name')}")
    return {
        "connected": True,
        "user": result.get("user"),
        "workspace": result.get("workspace_name"),
        "message": "Notion connected. Run /api/notion/sync to pull your workspace.",
    }


@app.get("/api/notion/status")
async def notion_status():
    """Check whether Notion is connected and how many pages were last synced."""
    token = _get_notion_token()
    if not token:
        return {
            "connected": False,
            "message": "Not connected. POST to /api/notion/connect with your token.",
            "how_to_get_token": "1. Go to notion.so/my-integrations  2. Click '+ New integration'  3. Copy the token",
        }
    from ingestion.notion_connector import NotionConnector
    connector = NotionConnector(token=token)
    result = connector.test_connection()
    return {
        "connected": result["ok"],
        "user": result.get("user"),
        "workspace": result.get("workspace_name"),
        "error": result.get("error"),
    }


@app.post("/api/notion/sync")
async def notion_sync(background_tasks: BackgroundTasks):
    """
    Pull all Notion pages shared with the integration into the knowledge pipeline.
    Runs in background — returns immediately, processes async.
    """
    token = _get_notion_token()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Notion not connected. POST to /api/notion/connect first.",
        )

    def _do_sync():
        from ingestion.notion_connector import NotionConnector
        from core.credential_store import CredentialStore
        import json as _json
        from datetime import datetime, timezone

        db_path = os.path.join(base_dir, "database")
        cred_store = CredentialStore(db_path=db_path)
        creds = cred_store.get_credentials("notion") or {}

        # Delta sync: only fetch pages edited since last cursor
        since = creds.get("sync_cursor") or None
        if since:
            print(f"\n[Notion Sync] Delta sync — fetching pages edited since {since}")
        else:
            print("\n[Notion Sync] Full sync — no cursor found, fetching all pages...")

        connector = NotionConnector(token=token, db_path=db_path)
        pages = connector.fetch_workspace(since=since)

        if not pages:
            if since:
                print("[Notion Sync] No pages changed since last sync. Done.")
            else:
                print("[Notion Sync] No pages found. Make sure you shared pages with your integration.")
            return

        # Write pages to raw_data for the pipeline to pick up
        raw_dir = os.path.join(base_dir, "raw_data", "notion")
        os.makedirs(raw_dir, exist_ok=True)

        for page in pages:
            # Write each page as a .md file
            safe_title = "".join(c for c in page["title"] if c.isalnum() or c in " -_")[:60]
            fname = f"{page['id'][:8]}_{safe_title}.md".replace(" ", "_")
            fpath = os.path.join(raw_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(f"# {page['title']}\n\n")
                f.write(f"Source: {page.get('url', 'notion')}\n")
                f.write(f"Last edited: {page.get('last_edited', '')}\n\n")
                f.write(page["content"])
            print(f"  [Notion Sync] Saved: {fname}")

        # Persist sync cursor: max last_edited across fetched pages
        last_edited_times = [p.get("last_edited", "") for p in pages if p.get("last_edited")]
        if last_edited_times:
            new_cursor = max(last_edited_times)
            creds["sync_cursor"] = new_cursor
            cred_store.save_credentials("notion", creds)
            print(f"[Notion Sync] Sync cursor updated → {new_cursor}")

        print(f"[Notion Sync] Saved {len(pages)} pages. Running pipeline...")

        # Run the 10-layer pipeline to ingest everything
        from main import run_orchestration_loop
        run_orchestration_loop()

        # Auto-index search after pipeline
        vs = VectorStore(db_path=db_path)
        count = vs.index_from_store(db_path=db_path)
        print(f"[Notion Sync] Search indexed {count} chunks. Done.")

    background_tasks.add_task(_do_sync)
    return {
        "status": "syncing",
        "message": "Pulling Notion workspace in background. Check /api/health for progress.",
    }


# ═══════════════════════════════════════════════════════════════
# PROVIDER STATUS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/providers/status")
def get_providers_status():
    """Real-time status of all AI providers — fully dynamic discovery.
    No hardcoded model names. Everything auto-detected."""
    try:
        from providers.router import ProviderRouter
        router_inst = ProviderRouter()
        providers = router_inst.get_available_providers()
        return {
            "providers": providers,
            "preferred_provider": os.getenv("CORTEX_PREFERRED_PROVIDER", "auto"),
        }
    except Exception as _e:
        return {"providers": [], "error": str(_e)}


class ProviderConfigRequest(BaseModel):
    preferred_provider: Optional[str] = None  # "ollama", "claude", "openai", or None for auto


@app.post("/api/providers/config")
def set_provider_config(body: ProviderConfigRequest):
    """Set the preferred AI provider at runtime.
    Pass null/empty to reset to auto-routing.
    """
    pref = body.preferred_provider
    if pref:
        valid = {"ollama", "claude", "openai"}
        if pref.lower() not in valid:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid provider. Choose from: {sorted(valid)}"}
            )
        os.environ["CORTEX_PREFERRED_PROVIDER"] = pref.lower()
    else:
        os.environ.pop("CORTEX_PREFERRED_PROVIDER", None)

    return {
        "status": "ok",
        "preferred_provider": pref or "auto",
        "message": f"Provider preference set to '{pref or 'auto'}'. Takes effect on next request.",
    }


@app.get("/api/providers/routing-debug")
def get_routing_debug():
    """Debug endpoint showing how the router would handle different task types."""
    from providers.model_router import route, TaskType, describe_routing
    results = []
    for task in TaskType:
        results.append(describe_routing(
            task=task, content_length=500, sensitivity=None
        ))
    return {"routing_decisions": results}


@app.get("/api/providers/rules-stats")
def get_rules_stats():
    """Stats about the self-training rule engine."""
    try:
        from pipeline.rule_manager import DynamicRuleManager
        db_path = os.path.join(base_dir, "database")
        rm = DynamicRuleManager(db_path=db_path)
        return rm.get_stats()
    except Exception as e:
        return {"error": str(e)}



# ═══════════════════════════════════════════════════════════════
# OAUTH — ONE-CLICK CONNECT
# ═══════════════════════════════════════════════════════════════

class OAuthConnectRequest(BaseModel):
    client_id: str
    client_secret: str
    extra: Optional[Dict[str, Any]] = None


@app.get("/api/oauth/config")
def get_oauth_app_config():
    """
    Return the list of OAuth-capable apps with their env-configured client IDs.
    Frontend uses this to show/hide the 'Connect' button per app.
    """
    from ingestion.oauth_manager import OAUTH_CONFIGS
    result = []
    for app_id, config in OAUTH_CONFIGS.items():
        env_prefix = app_id.upper().replace("-", "_")
        client_id = os.getenv(f"OAUTH_{env_prefix}_CLIENT_ID", "")
        result.append({
            "app_id": app_id,
            "name": app_id.replace("_", " ").title(),
            "configured": bool(client_id),
            "scopes": config.get("scopes", []),
        })
    return {"apps": result}


@app.post("/api/oauth/connect/{app_id}")
async def oauth_connect(app_id: str, body: OAuthConnectRequest, background_tasks: BackgroundTasks):
    """
    Trigger the OAuth browser flow for an app.
    Opens a browser tab → user approves → tokens stored in keyring.
    Returns immediately; the browser flow happens in a background thread.
    """
    from ingestion.oauth_manager import OAuthManager
    db_path = os.path.join(base_dir, "database")
    manager = OAuthManager(credential_dir=db_path)

    def run_flow():
        result = manager.start_flow(
            app_id=app_id,
            client_id=body.client_id,
            client_secret=body.client_secret,
            extra_config=body.extra,
        )
        status = "connected" if result.success else "failed"
        print(f"  [OAuth/{app_id}] {status}: {result.message}")

    background_tasks.add_task(run_flow)
    return {"status": "flow_started", "app_id": app_id,
            "message": f"Browser opened for {app_id}. Approve in the browser tab."}


@app.get("/api/oauth/status")
def get_oauth_status():
    """
    Show which apps are currently connected (have stored tokens).
    Used to render ✓/✗ badges on the connectors panel.
    """
    from ingestion.oauth_manager import OAuthManager, OAUTH_CONFIGS
    db_path = os.path.join(base_dir, "database")
    manager = OAuthManager(credential_dir=db_path)
    statuses = {}
    for app_id in OAUTH_CONFIGS:
        statuses[app_id] = manager.is_connected(app_id)
    return {"connected": statuses}


@app.delete("/api/oauth/disconnect/{app_id}")
def oauth_disconnect(app_id: str):
    """Revoke and delete stored tokens for an app."""
    from ingestion.oauth_manager import OAuthManager
    db_path = os.path.join(base_dir, "database")
    manager = OAuthManager(credential_dir=db_path)
    success = manager.revoke(app_id)
    return {"success": success, "app_id": app_id}


@app.get("/api/connectors/resources/{app_id}")
def list_connector_resources(app_id: str):
    """
    List selectable resources for an app (channels, pages, folders, repos).
    Called after connect to let the user pick what to sync.
    Returns up to 200 resources.
    """
    connector_map = {
        "slack": ("ingestion.connectors.slack_connector", "SlackConnector", "bot_token"),
        "notion": ("ingestion.connectors.notion_connector", "NotionConnector", "api_key"),
        "google_drive": ("ingestion.connectors.google_drive_connector", "GoogleDriveConnector", "access_token"),
        "confluence": ("ingestion.connectors.confluence_connector", "ConfluenceConnector", "access_token"),
        "jira": ("ingestion.connectors.jira_connector", "JiraConnector", "access_token"),
        "linear": ("ingestion.connectors.linear_connector", "LinearConnector", "access_token"),
        "ms_teams": ("ingestion.connectors.ms_teams_connector", "MSTeamsConnector", "access_token"),
        "github": ("ingestion.connectors.github_connector", "GitHubConnector", "access_token"),
    }
    if app_id not in connector_map:
        raise HTTPException(status_code=404, detail=f"Unknown app: {app_id}")

    module_path, class_name, token_key = connector_map[app_id]

    # Try OAuth token first, fall back to API key creds
    from ingestion.oauth_manager import OAuthManager
    db_path = os.path.join(base_dir, "database")
    oauth_manager = OAuthManager(credential_dir=db_path)
    token = oauth_manager._load_token_set(app_id)
    token_str = token.access_token if token else None

    # Fall back to API key creds stored in ConnectorManager
    if not token_str:
        mgr = ConnectorManager(db_path=os.path.join(base_dir, "database"))
        creds = mgr.cred_store.get_credentials(app_id)
        if creds:
            token_str = creds.get(token_key) or creds.get("api_key") or creds.get("bot_token")

    if not token_str:
        raise HTTPException(status_code=401, detail=f"No credentials stored for {app_id}. Connect first.")

    try:
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        connector = cls(**{token_key: token_str})
        resources = connector.list_resources()
        return {
            "app_id": app_id,
            "resources": [
                {
                    "id": r.id, "name": r.name,
                    "type": r.resource_type,
                    "description": r.description,
                    "is_private": r.is_private,
                    "member_count": r.member_count,
                }
                for r in resources[:200]
            ],
            "total": len(resources),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list resources: {e}")



# [catch-all moved to end of file]


# ═══════════════════════════════════════════════════════════════
# PHASE 2 — POINTER MEMORY
# ═══════════════════════════════════════════════════════════════

@app.get("/api/memory/pointers")
def get_pointers(app_id: Optional[str] = None, limit: int = 200):
    """Browse the pointer address book — location_key, hash, permalink, title only."""
    from storage.pointer_store import PointerStore
    db_path = os.path.join(base_dir, "database")
    store = PointerStore(db_path=db_path)
    if app_id:
        records = store.list_by_app(app_id, limit=limit)
    else:
        stats = store.get_stats()
        records = []
        app_count = max(len(stats.get("by_app", [])), 1)
        for app_info in stats.get("by_app", []):
            records.extend(store.list_by_app(app_info["app_id"], limit=limit // app_count))
    return {
        "pointers": [
            {"location_key": r.location_key, "app_id": r.app_id,
             "content_hash": r.content_hash, "permalink": r.permalink,
             "title": r.title, "last_seen_at": r.last_seen_at,
             "last_indexed_at": r.last_indexed_at, "department": r.department,
             "byte_size": r.byte_size}
            for r in records[:limit]
        ],
        "total": store.count(app_id=app_id),
        "note": "Only addresses stored. No raw content.",
    }


@app.get("/api/memory/pointer-stats")
def get_pointer_stats():
    """Token-saving dashboard: docs seen/skipped/processed, skip rate %, cost saved USD."""
    from ingestion.sync_manager import SyncManager
    db_path = os.path.join(base_dir, "database")
    manager = SyncManager(db_path=db_path)
    return manager.get_dashboard()


# ═══════════════════════════════════════════════════════════════
# PHASE 3 — SYNC + CACHE + MODEL ROUTING
# ═══════════════════════════════════════════════════════════════

class SyncRunRequest(BaseModel):
    selected_resources: Optional[List[str]] = None


async def _sync_tier1(app_id: str, background_tasks: BackgroundTasks = None):
    """Handle sync for Tier 1 connectors (no token needed — CLI/filesystem-based)."""

    # Verify connector is actually connected
    mgr = ConnectorManager(db_path=os.path.join(base_dir, "database"))
    all_connectors = mgr.get_all_connectors()
    conn = next((c for c in all_connectors if c["id"] == app_id), None)
    if not conn or not conn.get("connected"):
        raise HTTPException(status_code=400,
            detail=f"{app_id} is not connected. Click CONNECT first.")

    def _run_github():
        from ingestion.github_connector import GitHubConnector
        from storage.sqlite_store import SQLiteStore

        gh = GitHubConnector()
        if not gh.is_authenticated():
            print(f"  [Sync] GitHub CLI not authenticated — skipping")
            return

        print(f"  [Sync] Running GitHub CLI sync...")
        chunks = gh.ingest(limit_repos=10)
        if chunks:
            sql = SQLiteStore(db_path=os.path.join(base_dir, "database"))
            for chunk in chunks:
                sql.save_chunk(chunk)
            print(f"  [Sync] GitHub: saved {len(chunks)} chunks")
        else:
            print(f"  [Sync] GitHub: no new chunks")

    def _run_folder():
        mgr_inner = ConnectorManager(db_path=os.path.join(base_dir, "database"))
        chunks = mgr_inner.ingest_all_connected()
        if chunks:
            from storage.sqlite_store import SQLiteStore
            sql = SQLiteStore(db_path=os.path.join(base_dir, "database"))
            for chunk in chunks:
                sql.save_chunk(chunk)
            print(f"  [Sync] {app_id}: saved {len(chunks)} chunks")

    if app_id == "github":
        if background_tasks:
            background_tasks.add_task(_run_github)
        else:
            _run_github()
    elif app_id in ("localfolder", "obsidian"):
        if background_tasks:
            background_tasks.add_task(_run_folder)
        else:
            _run_folder()

    return {"status": "sync_started", "message": f"{app_id} sync started (Tier 1 — CLI mode)"}


@app.post("/api/sync/run/{app_id}")
async def trigger_sync(app_id: str, body: Optional[SyncRunRequest] = None,
                       background_tasks: BackgroundTasks = None):
    """Delta sync for one connector. Hash-gates unchanged docs before any AI call."""
    from ingestion.sync_manager import SyncManager
    from ingestion.oauth_manager import OAuthManager

    # ── Tier 1 connectors: CLI-based, no token needed ──
    tier1_ids = {"github", "localfolder", "obsidian"}
    if app_id in tier1_ids:
        return await _sync_tier1(app_id, background_tasks)

    # ── Tier 2 connectors: require a token ──
    db_path = os.path.join(base_dir, "database")
    oauth_manager = OAuthManager(credential_dir=db_path)
    token_set = oauth_manager._load_token_set(app_id)
    token = token_set.access_token if token_set else None

    if not token:
        mgr = ConnectorManager(db_path=os.path.join(base_dir, "database"))
        creds = mgr.cred_store.get_credentials(app_id)
        if creds:
            token = (creds.get("access_token") or creds.get("api_key") or
                     creds.get("bot_token") or creds.get("system_token"))

    if not token:
        raise HTTPException(status_code=401,
            detail=f"No credentials for {app_id}. Connect it first from the Connectors panel.")

    connector_map = {
        "slack": ("ingestion.connectors.slack_connector", "SlackConnector", "bot_token"),
        "notion": ("ingestion.connectors.notion_connector", "NotionConnector", "api_key"),
        "google_drive": ("ingestion.connectors.google_drive_connector", "GoogleDriveConnector", "access_token"),
        "confluence": ("ingestion.connectors.confluence_connector", "ConfluenceConnector", "access_token"),
        "jira": ("ingestion.connectors.jira_connector", "JiraConnector", "access_token"),
        "linear": ("ingestion.connectors.linear_connector", "LinearConnector", "access_token"),
        "ms_teams": ("ingestion.connectors.ms_teams_connector", "MSTeamsConnector", "access_token"),
        "github": ("ingestion.connectors.github_connector", "GitHubConnector", "access_token"),
        "whatsapp": ("ingestion.connectors.whatsapp_connector", "WhatsAppConnector", "system_token"),
    }
    if app_id not in connector_map:
        raise HTTPException(status_code=404, detail=f"Unknown connector: {app_id}")

    module_path, class_name, token_key = connector_map[app_id]

    def _run():
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        connector = cls(**{token_key: token})
        resources = (body.selected_resources or []) if body else []

        if resources:
            # Bug 20 fix: ms_teams set_channels expects dicts {team_id, channel_id},
            # not raw strings. Parse "team_id/channel_id" strings into dicts.
            def _as_teams_dicts(res_list):
                result = []
                for r in res_list:
                    if isinstance(r, dict):
                        result.append(r)
                    elif "/" in str(r):
                        parts = str(r).split("/", 1)
                        result.append({"team_id": parts[0], "channel_id": parts[1]})
                    else:
                        result.append({"team_id": r, "channel_id": r})
                return result

            for setter in ("set_channels", "set_pages", "set_repos", "set_teams",
                           "set_folders", "set_projects", "set_spaces"):
                if hasattr(connector, setter):
                    payload = _as_teams_dicts(resources) if setter == "set_channels" else resources
                    getattr(connector, setter)(payload)
                    break

        # Bug 7 fix: pass pipeline_fn so fetched docs are actually processed.
        # Without this, SyncManager hash-gates and records docs but never distills them.
        def _pipeline_fn(raw_docs):
            from pipeline.normalize import TextNormalizer
            from pipeline.chunking import SemanticChunker
            from pipeline.distill import KnowledgeDistiller
            from storage.sqlite_store import SQLiteStore
            from storage.filesystem_store import FilesystemStore
            from core.search import VectorStore
            from core.models import SourceType, Department

            normalizer = TextNormalizer()
            chunker = SemanticChunker()
            _db = os.path.join(base_dir, "database")
            distiller = KnowledgeDistiller(db_path=_db)
            sql = SQLiteStore(db_path=_db)
            fs = FilesystemStore(db_path=_db)
            vs = VectorStore(db_path=_db)

            src_type = getattr(SourceType, app_id.upper(), SourceType.LOCAL_FILE)
            dept = Department.SHARED

            for doc in raw_docs:
                try:
                    text = getattr(doc, "content", str(doc))
                    normed = normalizer.normalize_document(text)
                    loc = getattr(doc, "location_key", "unknown")
                    chunks = chunker.chunk_document(normed, src_type, loc, dept)
                    distilled = distiller.distill_chunks(chunks)
                    for chunk in distilled:
                        sql.save_chunk(chunk)
                        fs.save_chunk(chunk)  # Bug fix: also write to chunks.json
                        try:
                            vs.index_chunk(
                                chunk_id=chunk.id, title=chunk.title,
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
                            pass
                except Exception as e:
                    print(f"  [SyncPipeline/{app_id}] Error on {getattr(doc, 'location_key', '?')}: {e}")

        sync_mgr = SyncManager(db_path=os.path.join(base_dir, "database"))
        result = sync_mgr.run_sync(app_id=app_id, connector=connector, pipeline_fn=_pipeline_fn)
        print(f"  [Sync/{app_id}] fetched={result.docs_fetched} skipped={result.docs_skipped} "
              f"processed={result.docs_processed} saved≈{result.tokens_saved_estimate:,} tokens")


    if background_tasks:
        background_tasks.add_task(_run)
        return {"status": "sync_started", "app_id": app_id,
                "message": f"Delta sync started for {app_id}."}
    _run()
    return {"status": "sync_complete", "app_id": app_id}


@app.get("/api/cache/stats")
def get_cache_stats():
    """Hash cache: hit rate, entry counts, estimated tokens and USD saved."""
    from providers.hash_cache import HashCache
    db_path = os.path.join(base_dir, "database")
    cache = HashCache(db_path=db_path)
    stats = cache.stats()
    stats["estimated_tokens_saved"] = stats["total_hits"] * 500
    stats["estimated_cost_saved_usd"] = round(stats["total_hits"] * 500 / 1_000_000 * 3.0, 4)
    return stats


@app.get("/api/providers/route-debug")
def route_debug(task: str = "distill", content_length: int = 500, sensitivity: Optional[str] = None):
    """Explain which AI provider is chosen for a given task, showing all routing reasons."""
    from providers.model_router import describe_routing, TaskType
    from providers.ollama_adapter import OllamaAdapter
    try:
        task_enum = TaskType(task)
    except ValueError:
        raise HTTPException(status_code=400,
            detail=f"Invalid task. Valid: {[t.value for t in TaskType]}")
    ollama = OllamaAdapter()
    return describe_routing(
        task=task_enum,
        content_length=content_length,
        sensitivity=sensitivity,
        ollama_available=ollama.is_available,
        claude_available=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


# ═══════════════════════════════════════════════════════════════
# PHASE 4 — SEMANTIC SEARCH
# ═══════════════════════════════════════════════════════════════

@app.get("/api/search")
def semantic_search(
    q: str = Query(..., description="Search query"),
    dept: Optional[str] = None,
    knowledge_type: Optional[str] = None,
    limit: int = 10,
    mode: str = "hybrid",
    min_confidence: float = 0.0,
    requesting_dept: Optional[str] = None,
):
    """
    Hybrid semantic + keyword search across all indexed knowledge.
    mode: 'hybrid' (recommended), 'semantic', or 'keyword'
    ACL-filtered: pass ?requesting_dept=engineering to only get allowed results.
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    if limit > 50:
        limit = 50

    db_path = os.path.join(base_dir, "database")
    vector_store = VectorStore(db_path=db_path)
    results = vector_store.search(
        query=q,
        department=dept if dept and dept != "all" else None,
        knowledge_type=knowledge_type,
        limit=limit * 2,  # over-fetch for ACL filtering
        mode=mode,
        min_confidence=min_confidence,
    )

    # ACL filter
    if requesting_dept:
        acl = ACLStore(db_path=db_path)
        results = [
            r for r in results
            if acl.check(r.chunk_id, requesting_dept).allowed
        ]

    results = results[:limit]

    return {
        "query": q,
        "mode": mode,
        "total": len(results),
        "results": [
            {
                "chunk_id": r.chunk_id,
                "title": r.title,
                "summary": r.summary,
                "content_preview": r.content[:300],
                "department": r.department,
                "knowledge_type": r.knowledge_type,
                "source_type": r.source_type,
                "source_identifier": r.source_identifier,
                "tags": r.tags,
                "confidence_score": r.confidence_score,
                "semantic_score": r.semantic_score,
                "keyword_score": r.keyword_score,
                "hybrid_score": r.hybrid_score,
                "permalink": r.permalink,
            }
            for r in results
        ],
        "search_stats": vector_store.get_stats(),
    }


@app.post("/api/search/index")
def rebuild_search_index(background_tasks: BackgroundTasks):
    """Bulk-index all existing chunks from SQLiteStore into the vector index."""
    db_path = os.path.join(base_dir, "database")

    def _reindex():
        vs = VectorStore(db_path=db_path)
        count = vs.index_from_store(db_path=db_path)
        print(f"  [Search] Indexed {count} new chunks.")

    background_tasks.add_task(_reindex)
    return {"status": "indexing_started", "message": "Building vector index in background."}


@app.get("/api/search/stats")
def get_search_stats():
    """Vector index statistics: total indexed, embedding coverage, by department."""
    db_path = os.path.join(base_dir, "database")
    vs = VectorStore(db_path=db_path)
    return vs.get_stats()


# ═══════════════════════════════════════════════════════════════
# PHASE 4 — ACL MANAGEMENT
# ═══════════════════════════════════════════════════════════════

class ACLRuleRequest(BaseModel):
    resource_id: str
    resource_type: str = "chunk"
    visibility: str = "shared"
    allowed_departments: List[str] = []
    allowed_users: List[str] = []
    denied_departments: List[str] = []


@app.post("/api/acl/rules")
def set_acl_rule(body: ACLRuleRequest):
    """Manually set an ACL rule for a specific resource."""
    db_path = os.path.join(base_dir, "database")
    acl = ACLStore(db_path=db_path)
    rule = ACLRule(
        resource_id=body.resource_id,
        resource_type=body.resource_type,
        app_id="manual",
        visibility=body.visibility,
        allowed_departments=body.allowed_departments,
        allowed_users=body.allowed_users,
        denied_departments=body.denied_departments,
    )
    acl.set_rule(rule)
    return {"success": True, "rule": {"resource_id": body.resource_id, "visibility": body.visibility}}


@app.get("/api/acl/check")
def check_acl(resource_id: str, dept: Optional[str] = None, user: Optional[str] = None):
    """Check if a department/user can access a resource."""
    db_path = os.path.join(base_dir, "database")
    acl = ACLStore(db_path=db_path)
    result = acl.check(resource_id, requesting_department=dept, requesting_user=user)
    return {
        "resource_id": result.resource_id,
        "allowed": result.allowed,
        "reason": result.reason,
        "visibility": result.visibility,
    }


@app.get("/api/acl/stats")
def get_acl_stats():
    """ACL store statistics: total rules, breakdown by visibility level."""
    db_path = os.path.join(base_dir, "database")
    acl = ACLStore(db_path=db_path)
    return acl.get_stats()


# ═══════════════════════════════════════════════════════════════
# PHASE 4 — AUDIT LOG
# ═══════════════════════════════════════════════════════════════

@app.get("/api/audit/log")
def get_audit_log(target_id: Optional[str] = None, limit: int = 50):
    """
    Get the audit trail. Pass ?target_id=... to filter by resource.
    Returns who did what and when — OAuth connects, sync runs, webhook events.
    """
    if limit > 200:
        limit = 200
    db_path = os.path.join(base_dir, "database")
    sql = SQLiteStore(db_path=db_path)
    entries = sql.get_audit_trail(target_id=target_id, limit=limit)
    return {
        "entries": [
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat(),
                "action": e.action,
                "actor": e.actor,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "details": e.details,
                "provider_used": e.provider_used,
                "cost_usd": e.cost_usd,
            }
            for e in entries
        ],
        "total": len(entries),
        "limit": limit,
    }


# ═══════════════════════════════════════════════════════════════
# PHASE 4 — CONNECTOR RATE LIMIT STATUS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/connectors/rate-limit-status")
def get_rate_limit_status():
    """Shows seconds since last sync per connector (for the UI 'Sync' button cooldown)."""
    return {
        "connector_last_sync": connector_limiter.status(),
        "min_gap_seconds": connector_limiter._min_gap,
        "note": "Connectors must wait min_gap_seconds between syncs to prevent runaway loops.",
    }


# ═══════════════════════════════════════════════════════════════
# SESSION MEMORY (8th TIER)
# ═══════════════════════════════════════════════════════════════

class SessionFactRequest(BaseModel):
    fact: str
    agent_id: Optional[str] = None
    user_id: Optional[str] = None
    fact_type: Optional[str] = "explicit"
    confidence: Optional[float] = 0.8
    source_conversation: Optional[str] = None
    ttl_days: Optional[int] = 7


@app.post("/api/memory/session")
def save_session_fact(data: SessionFactRequest):
    """Save a session fact — ephemeral knowledge from agent/user conversations."""
    from memory import MemoryManager
    db_path = os.path.join(base_dir, "database")
    memory = MemoryManager(SQLiteStore(db_path=db_path))

    fact = memory.save_session_fact(
        fact=data.fact,
        agent_id=data.agent_id,
        user_id=data.user_id,
        fact_type=data.fact_type or "explicit",
        confidence=data.confidence or 0.8,
        source_conversation=data.source_conversation,
        ttl_days=data.ttl_days,
    )

    return {
        "status": "success",
        "session_fact": {
            "id": fact.id,
            "fact": fact.fact,
            "fact_type": fact.fact_type,
            "agent_id": fact.agent_id,
            "user_id": fact.user_id,
            "confidence": fact.confidence,
            "expires_at": fact.expires_at.isoformat() if fact.expires_at else None,
            "created_at": fact.created_at.isoformat(),
        },
    }


@app.get("/api/memory/session")
def get_session_facts(
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    include_expired: bool = False,
):
    """Get session facts, optionally filtered by agent/user."""
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)
    facts = store.get_session_facts(
        agent_id=agent_id, user_id=user_id, include_expired=include_expired
    )
    return {
        "session_facts": [
            {
                "id": f.id,
                "fact": f.fact,
                "fact_type": f.fact_type,
                "agent_id": f.agent_id,
                "user_id": f.user_id,
                "confidence": f.confidence,
                "source_conversation": f.source_conversation,
                "expires_at": f.expires_at.isoformat() if f.expires_at else None,
                "promoted_to_unit_id": f.promoted_to_unit_id,
                "created_at": f.created_at.isoformat(),
            }
            for f in facts
        ],
        "total": len(facts),
    }


@app.post("/api/memory/session/{fact_id}/promote")
def promote_session_fact(
    fact_id: str,
    department: str = "shared",
):
    """Promote a session fact to a canonical AtomicKnowledgeUnit."""
    from memory import MemoryManager
    db_path = os.path.join(base_dir, "database")
    memory = MemoryManager(SQLiteStore(db_path=db_path))

    try:
        dept = Department(department.lower())
    except ValueError:
        dept = Department.SHARED

    unit = memory.promote_session_to_canonical(fact_id, department=dept)
    if not unit:
        raise HTTPException(status_code=404, detail=f"Session fact '{fact_id}' not found")

    return {
        "status": "success",
        "action": "promoted_to_canonical",
        "fact_id": fact_id,
        "unit_id": unit.id,
        "claim": unit.claim,
        "department": unit.department.value,
        "message": "Session fact promoted to canonical memory.",
    }


# ═══════════════════════════════════════════════════════════════
# AUTOMATIC EXPIRY
# ═══════════════════════════════════════════════════════════════

@app.post("/api/memory/expiry/sweep")
def run_expiry_sweep():
    """Trigger a manual expiry sweep — cleans stale units and expired sessions."""
    from memory import MemoryManager
    db_path = os.path.join(base_dir, "database")
    memory = MemoryManager(SQLiteStore(db_path=db_path))
    result = memory.run_expiry_sweep()
    return {
        "status": "success",
        **result,
        "message": f"Expiry sweep complete: {result['total_cleaned']} items cleaned.",
    }


# ═══════════════════════════════════════════════════════════════
# USER / AGENT PROFILES
# ═══════════════════════════════════════════════════════════════

@app.get("/api/profiles/{user_id}")
def get_user_profile(user_id: str):
    """Get an aggregated profile for a user built from session facts + canonical contributions."""
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)
    profile = store.get_user_profile(user_id)
    return {
        "user_id": profile.user_id,
        "departments": profile.departments,
        "expertise_areas": profile.expertise_areas,
        "session_fact_count": profile.session_fact_count,
        "canonical_contribution_count": profile.canonical_contribution_count,
        "top_knowledge_types": profile.top_knowledge_types,
        "last_active_at": profile.last_active_at.isoformat() if profile.last_active_at else None,
        "profile_confidence": profile.profile_confidence,
    }


@app.get("/api/profiles/agent/{agent_id}")
def get_agent_profile(agent_id: str):
    """Get an aggregated profile for an agent built from session facts."""
    db_path = os.path.join(base_dir, "database")
    store = SQLiteStore(db_path=db_path)
    profile = store.get_agent_profile(agent_id)
    return {
        "agent_id": profile.user_id,
        "expertise_areas": profile.expertise_areas,
        "session_fact_count": profile.session_fact_count,
        "canonical_contribution_count": profile.canonical_contribution_count,
        "top_knowledge_types": profile.top_knowledge_types,
        "last_active_at": profile.last_active_at.isoformat() if profile.last_active_at else None,
        "profile_confidence": profile.profile_confidence,
    }


# ═══════════════════════════════════════════════════════════════
# STRATEGIC HARDENING
# ═══════════════════════════════════════════════════════════════

@app.get("/api/strategic/sovereignty")
def get_sovereignty_report():
    """Data sovereignty report — proves all data stays local."""
    from core.strategic_hardening import get_data_sovereignty_report
    db_path = os.path.join(base_dir, "database")
    return get_data_sovereignty_report(db_path)


@app.get("/api/strategic/sqlite-health")
def get_sqlite_health():
    """SQLite ceiling monitor — flags when migration to PostgreSQL is needed."""
    from core.strategic_hardening import check_sqlite_health
    db_path = os.path.join(base_dir, "database")
    return check_sqlite_health(db_path)


@app.get("/api/strategic/roi")
def get_roi_metrics():
    """ROI calculator — measurable value metrics against Gartner's 40% cancellation risk."""
    from core.strategic_hardening import calculate_roi_metrics
    db_path = os.path.join(base_dir, "database")
    return calculate_roi_metrics(db_path)



# ═══════════════════════════════════════════════════════════════
# SPA CATCH-ALL — must be LAST route registered
# ═══════════════════════════════════════════════════════════════
# This MUST remain at the bottom. FastAPI matches routes in registration
# order; if this were earlier it would shadow all subsequent @app.get routes.
from fastapi.responses import FileResponse as _FileResponse
from pathlib import Path as _Path

_frontend_dir = _Path(os.path.dirname(base_dir)).parent / "frontend"

@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str):
    """Serve frontend SPA for any path not matched by an API route above."""
    if not _frontend_dir.exists():
        raise HTTPException(status_code=404,
            detail="Frontend not built. Run: cd frontend && npm run build")
    target = _frontend_dir / full_path
    if target.exists() and target.is_file():
        return _FileResponse(str(target))
    index = _frontend_dir / "index.html"
    if index.exists():
        return _FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Not found")
