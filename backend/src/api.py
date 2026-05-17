"""
Organizational Intelligence Engine — Unified API Server

Consolidates ALL endpoints into a single FastAPI app:
  - Configuration management (data sources, API keys)
  - Pipeline execution (run the 6-layer loop)
  - Webhook ingestion (real-time Slack/Notion push)
  - Agent feedback loop (failure memory injection)
  - Dynamic runtime assembly (on-demand skill composition)
  - Skill/chunk/agent management
"""

import os
import sys
import io
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

from storage.filesystem_store import FilesystemStore
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
from core.models import SourceType, Department, KnowledgeType

app = FastAPI(title="Cortex AI — Organizational Intelligence Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

base_dir = os.path.dirname(os.path.dirname(__file__))

# Mutable application state
app_state = {
    "data_sources": [os.path.join(base_dir, "raw_data")],
    "openai_key": os.getenv("OPENAI_API_KEY", ""),
    "slack_channel": "C1234_ENGINEERING",
    "is_running": False,
    "execution_logs": []
}


# ═══════════════════════════════════════════════════════════════
# CONFIGURATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

class ConfigModel(BaseModel):
    data_sources: List[str]
    openai_key: str
    slack_channel: str


@app.get("/api/config")
def get_config():
    return {
        "data_sources": app_state["data_sources"],
        "openai_key": "********" if app_state["openai_key"] else "",
        "slack_channel": app_state["slack_channel"]
    }


@app.post("/api/config")
def update_config(config: ConfigModel):
    app_state["data_sources"] = config.data_sources
    if config.openai_key and config.openai_key != "********":
        app_state["openai_key"] = config.openai_key
        os.environ["OPENAI_API_KEY"] = config.openai_key
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
        "openai_api_key_configured": bool(app_state["openai_key"]),
        "db_connected": db_connected,
        "is_running": app_state["is_running"],
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


class AgentAssignModel(BaseModel):
    agent_name: str
    skill_name: str


@app.post("/api/agents/assign")
def assign_agent(data: AgentAssignModel):
    db_path = os.path.join(base_dir, "database")
    connector = AgentConnector(db_path=db_path)
    bound = connector.bind_skill_to_agent(data.agent_name, data.skill_name)

    if bound:
        connector.trigger_agent_reload(data.agent_name)
        return {"status": "success", "message": f"Successfully bound '{data.skill_name}' to '{data.agent_name}' and triggered hot-reload."}
    else:
        return {"status": "success", "message": f"'{data.skill_name}' is already bound to '{data.agent_name}'."}


# ═══════════════════════════════════════════════════════════════
# PIPELINE EXECUTION
# ═══════════════════════════════════════════════════════════════

def execute_pipeline():
    """Background task that runs the 6-layer pipeline."""
    app_state["is_running"] = True
    app_state["execution_logs"].clear()

    old_stdout = sys.stdout
    sys.stdout = mystdout = io.StringIO()

    try:
        run_orchestration_loop(
            data_sources=app_state["data_sources"],
            slack_channel=app_state["slack_channel"]
        )
    except Exception as e:
        print(f"\n[ERROR] Pipeline failed: {str(e)}")
    finally:
        sys.stdout = old_stdout
        app_state["execution_logs"].append(mystdout.getvalue())
        app_state["is_running"] = False


@app.post("/api/run")
def run_pipeline(background_tasks: BackgroundTasks):
    if app_state["is_running"]:
        return {"status": "error", "message": "Pipeline is already running."}

    background_tasks.add_task(execute_pipeline)
    return {"status": "success", "message": "6-layer pipeline started."}


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
    distiller = KnowledgeDistiller()
    distilled = distiller.distill_chunks(chunks)

    if not distilled:
        return {"status": "success", "message": "Content processed. All noise, no signals extracted."}

    # Layer 5: Dedup & Save
    db_path = os.path.join(base_dir, "database")
    store = FilesystemStore(db_path=db_path)
    deduplicator = KnowledgeDeduplicator()
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
    db_path = os.path.join(base_dir, "database")
    store = FilesystemStore(db_path=db_path)
    return {"status": "ok", "canonical_chunks": len(store.get_all())}


# ═══════════════════════════════════════════════════════════════
# STATIC FILES (Frontend)
# ═══════════════════════════════════════════════════════════════

frontend_dir = os.path.join(os.path.dirname(base_dir), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
