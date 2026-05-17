from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import os

from core.models import Department, SourceType
from pipeline.normalize import TextNormalizer
from pipeline.chunking import SemanticChunker
from ingestion.notion_connector import NotionConnector
from ingestion.slack_connector import SlackConnector
from pipeline.distill import KnowledgeDistiller
from pipeline.deduplicate import KnowledgeDeduplicator
from storage.filesystem_store import FilesystemStore
from storage.skill_registry import SkillRegistry
from smart_layer.assembler import SkillAssembler
from smart_layer.tool_provisioner import ToolProvisioner
from generators.skill_generator import SkillGenerator

app = FastAPI(title="Organizational Intelligence Layer", description="Organizational Brain & Skill Provisioning API")

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "database")
KB_PATH = os.path.join(BASE_DIR, "knowledge_base")

# Initialize shared components
store = FilesystemStore(db_path=DB_PATH)
registry = SkillRegistry(db_path=DB_PATH)
distiller = KnowledgeDistiller()
deduplicator = KnowledgeDeduplicator()
assembler = SkillAssembler()
provisioner = ToolProvisioner()
generator = SkillGenerator(base_output_dir=KB_PATH)


class SlackIngestRequest(BaseModel):
    channel_id: str
    channel_name: str

class AgentFeedbackRequest(BaseModel):
    skill_name: str
    department: str
    issue_description: str
    suggested_fix: str

class DynamicAssemblyRequest(BaseModel):
    task_description: str
    department: str

def run_pipeline_for_messages(messages: list[str], channel_name: str, department: Department):
    """Background task to process messages, update DB, and recompile skills."""
    print(f"Starting background distillation for {len(messages)} messages...")
    
    # 1. Distill
    new_chunks = distiller.distill_slack_messages(messages, channel_name=channel_name)
    if not new_chunks:
        return
        
    # 2. Deduplicate
    existing_chunks = store.get_all()
    for chunk in new_chunks:
        # Override the mock department with the requested one
        chunk.department = department
        action, processed_chunk = deduplicator.evaluate_chunk(chunk, existing_chunks)
        if action in ["ADD", "UPDATE"] and processed_chunk:
            store.save_chunk(processed_chunk)
            
    # 3. Recompile
    # For now, we naively recompile all skills in that department
    dept_chunks = store.get_all_by_department(department)
    if dept_chunks:
        skill_name = f"{department.value}-onboarding"
        assembled = assembler.assemble_skill(
            skill_name=skill_name,
            description=f"Core compiled rules for {department.value}",
            chunks=dept_chunks,
            department=department
        )
        provisioned = provisioner.provision_skill(assembled)
        generator.save_skill(provisioned, department)
        registry.register_skill(skill_name, [c.id for c in dept_chunks])
        print(f"Successfully recompiled {skill_name}")


class NotionIngestRequest(BaseModel):
    page_id: str
    department: str

def run_pipeline_for_document(document: str, source_identifier: str, department: Department):
    """Background task to process a large document (e.g. from Notion)."""
    print(f"Starting background distillation for Notion document {source_identifier}...")
    
    # 1. Normalize
    cleaned_doc = TextNormalizer.normalize_document(document)
    
    # 2. Chunking
    chunker = SemanticChunker(max_tokens=1000)
    sections = chunker.chunk_by_markdown_headers(cleaned_doc)
    print(f"   Document split into {len(sections)} semantic sections.")
    
    # 3. Distill each chunk
    all_new_chunks = []
    for section in sections:
        # Mocking the source type as Notion here
        extracted = distiller.distill_document(section, source_identifier, SourceType.NOTION)
        if extracted:
            all_new_chunks.extend(extracted)
            
    if not all_new_chunks:
        return
        
    # 4. Deduplicate & Save
    existing_chunks = store.get_all()
    for chunk in all_new_chunks:
        chunk.department = department
        action, processed_chunk = deduplicator.evaluate_chunk(chunk, existing_chunks)
        if action in ["ADD", "UPDATE"] and processed_chunk:
            store.save_chunk(processed_chunk)
            
    # 5. Recompile
    dept_chunks = store.get_all_by_department(department)
    if dept_chunks:
        skill_name = f"{department.value}-onboarding"
        assembled = assembler.assemble_skill(
            skill_name=skill_name,
            description=f"Core compiled rules for {department.value}",
            chunks=dept_chunks,
            department=department
        )
        provisioned = provisioner.provision_skill(assembled)
        generator.save_skill(provisioned, department)
        registry.register_skill(skill_name, [c.id for c in dept_chunks])
        print(f"Successfully recompiled {skill_name}")


@app.post("/ingest/notion")
async def ingest_notion(request: NotionIngestRequest, background_tasks: BackgroundTasks):
    """Webhook endpoint for Notion to push document updates."""
    notion = NotionConnector()
    document_content = notion.fetch_page(page_id=request.page_id)
    
    dept = Department.ENGINEERING
    if request.department.lower() == "marketing":
        dept = Department.MARKETING
        
    background_tasks.add_task(run_pipeline_for_document, document_content, request.page_id, dept)
    return {"status": "accepted", "message": f"Processing Notion page {request.page_id} in background."}


@app.post("/ingest/slack")
async def ingest_slack(request: SlackIngestRequest, background_tasks: BackgroundTasks):
    """Webhook endpoint for Slack to push data into the intelligence layer."""
    slack = SlackConnector()
    messages = slack.fetch_channel_history(channel_id=request.channel_id)
    
    dept = Department.ENGINEERING
    if "marketing" in request.channel_name.lower():
        dept = Department.MARKETING
        
    background_tasks.add_task(run_pipeline_for_messages, messages, request.channel_name, dept)
    return {"status": "accepted", "message": f"Processing {len(messages)} messages in background."}


@app.post("/feedback")
async def receive_agent_feedback(feedback: AgentFeedbackRequest, background_tasks: BackgroundTasks):
    """Endpoint for Agents/Humans to report broken workflows (Failure Memory Loop)."""
    print(f"AGENT FEEDBACK RECEIVED for {feedback.skill_name}: {feedback.issue_description}")
    
    # 1. Synthesize the human correction into a raw document
    raw_correction = f"FAILURE REPORT for {feedback.skill_name}:\nWhat failed: {feedback.issue_description}\nCorrection/Rule to never do again: {feedback.suggested_fix}"
    
    try:
        dept = Department(feedback.department.lower())
    except ValueError:
        dept = Department.SHARED
        
    # 2. Inject it back into Layer 1 to be distilled into a FAILURE_PATTERN in Layer 2
    background_tasks.add_task(run_pipeline_for_document, raw_correction, f"feedback-{feedback.skill_name}", dept)
    
    return {"status": "processing", "message": "Feedback injected into the Failure Memory Loop for distillation."}

@app.post("/assemble")
async def dynamic_runtime_assembly(request: DynamicAssemblyRequest):
    """Runtime skill composition based on a specific task prompt."""
    try:
        dept = Department(request.department.lower())
    except ValueError:
        dept = Department.SHARED
        
    # 1. Fetch all chunks (In production, use Vector DB semantic search here)
    all_chunks = store.get_all_by_department(dept)
    
    # 2. Filter dynamically based on task keywords + confidence score
    keywords = set(request.task_description.lower().split())
    relevant_chunks = []
    
    from core.models import KnowledgeType
    for c in all_chunks:
        if c.metadata.confidence_score >= 0.7:
            chunk_words = set(c.content.lower().split())
            # Always include security rules, or include if keywords match
            if c.knowledge_type == KnowledgeType.SECURITY_RULE or keywords.intersection(chunk_words):
                relevant_chunks.append(c)
                
    # 3. Assemble temporary skill payload in memory (Do not persist to Layer 3 disk)
    if not relevant_chunks:
        relevant_chunks = [c for c in all_chunks if c.metadata.confidence_score >= 0.7]
        
    assembled = assembler.assemble_skill(
        skill_name="dynamic-runtime-context",
        description=f"Auto-assembled runtime context for task: {request.task_description}",
        chunks=relevant_chunks,
        department=dept
    )
    
    # 4. Generate the payload
    payload = generator.generate_skill_md(assembled, dept)
    
    return {"status": "success", "runtime_payload": payload}

@app.get("/health")
async def health_check():
    return {"status": "ok", "canonical_chunks": len(store.get_all())}
