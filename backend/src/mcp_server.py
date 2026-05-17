"""
BHRM Organizational Intelligence — MCP Server

Exposes the distilled knowledge base to AI agents via the Model Context Protocol.

Transport modes:
  - stdio:  For local agent integration (Claude Desktop, etc.)
  - sse:    For remote/network agents (HTTP-based)

Tools:
  - search_knowledge:       Semantic keyword search across all chunks
  - get_department_rules:   Get all rules for a specific department
  - get_skill:              Retrieve a compiled SKILL.md by name
  - list_skills:            List all available skills
  - list_departments:       List departments with chunk counts
  - report_failure:         Inject failure feedback into the pipeline
  - run_pipeline:           Trigger a full 6-layer pipeline run
  - get_pipeline_status:    Check if the pipeline is running + view logs

Resources:
  - skill://{department}/{skill_name}  — Read compiled SKILL.md files
  - chunks://{department}              — Read raw chunks for a department

Prompts:
  - department_briefing:    Generate a full briefing for a department
  - task_context:           Assemble relevant context for a specific task
"""

import os
import sys
import json
import asyncio
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# ─── Path Setup ──────────────────────────────────────────────────
# Ensure the src directory is on the path so we can import project modules
SRC_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from core.models import Department, KnowledgeType, SourceType
from storage.filesystem_store import FilesystemStore
from storage.skill_registry import SkillRegistry
from smart_layer.agent_connector import AgentConnector
from generators.skill_generator import SkillGenerator

# ─── Constants ───────────────────────────────────────────────────
DB_PATH = str(BACKEND_DIR / "database")
KB_PATH = str(BACKEND_DIR / "knowledge_base")

# ─── MCP Server Init ────────────────────────────────────────────
mcp = FastMCP(
    name="bhrm-intelligence",
    instructions=(
        "You are connected to the BHRM Organizational Intelligence Engine. "
        "This server provides access to distilled company knowledge organized "
        "by department (engineering, marketing, sales, ops, shared). "
        "Use the tools to search rules, retrieve skills, and report failures. "
        "Always check department-specific rules before making decisions."
    ),
)

# Pipeline state (shared with run_pipeline tool)
_pipeline_state = {
    "is_running": False,
    "last_run_log": "",
    "last_run_at": None,
}


# ═════════════════════════════════════════════════════════════════
# TOOLS
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def search_knowledge(
    query: str,
    department: Optional[str] = None,
    knowledge_type: Optional[str] = None,
    min_confidence: float = 0.0,
) -> str:
    """
    Search the organizational knowledge base by keywords.

    Args:
        query: Keywords to search for (e.g., "deployment", "stripe webhook")
        department: Filter by department (engineering, marketing, sales, ops, shared)
        knowledge_type: Filter by type (SOP, DECISION, POLICY, FAILURE_PATTERN, EDGE_CASE, SECURITY_RULE, etc.)
        min_confidence: Minimum confidence score (0.0 to 1.0)
    """
    store = FilesystemStore(db_path=DB_PATH)

    if department:
        try:
            dept = Department(department.lower())
            chunks = store.get_all_by_department(dept)
        except ValueError:
            return f"Error: Unknown department '{department}'. Valid: engineering, marketing, sales, ops, shared"
    else:
        chunks = store.get_all()

    # Filter by knowledge type
    if knowledge_type:
        try:
            kt = KnowledgeType(knowledge_type.upper())
            chunks = [c for c in chunks if c.knowledge_type == kt]
        except ValueError:
            valid = ", ".join(t.value for t in KnowledgeType)
            return f"Error: Unknown knowledge_type '{knowledge_type}'. Valid: {valid}"

    # Filter by confidence
    chunks = [c for c in chunks if c.metadata.confidence_score >= min_confidence]

    # Keyword search
    query_terms = set(query.lower().split())
    scored = []
    for chunk in chunks:
        searchable = f"{chunk.title} {chunk.content} {chunk.summary} {' '.join(chunk.tags)}".lower()
        matches = sum(1 for term in query_terms if term in searchable)
        if matches > 0:
            scored.append((matches, chunk))

    scored.sort(key=lambda x: (-x[0], -x[1].metadata.confidence_score))
    results = scored[:10]

    if not results:
        return f"No results found for '{query}'." + (
            f" (filtered to department={department})" if department else ""
        )

    lines = [f"Found {len(results)} result(s) for '{query}':\n"]
    for rank, (score, c) in enumerate(results, 1):
        lines.append(
            f"  {rank}. [{c.department.value.upper()}] [{c.knowledge_type.value}] "
            f"{c.title}\n"
            f"     Confidence: {c.metadata.confidence_score:.2f} | "
            f"Source: {c.source_identifier}\n"
            f"     {c.content[:300]}{'...' if len(c.content) > 300 else ''}\n"
        )

    return "\n".join(lines)


@mcp.tool()
def get_department_rules(department: str) -> str:
    """
    Get ALL operational rules, SOPs, policies, and edge cases for a department.
    Use this before making any department-specific decisions.

    Args:
        department: One of: engineering, marketing, sales, ops, shared
    """
    try:
        dept = Department(department.lower())
    except ValueError:
        return f"Error: Unknown department '{department}'. Valid: engineering, marketing, sales, ops, shared"

    store = FilesystemStore(db_path=DB_PATH)
    chunks = store.get_all_by_department(dept)

    if not chunks:
        return f"No knowledge found for department '{department}'."

    # Sort by type then confidence
    chunks.sort(key=lambda c: (c.knowledge_type.value, -c.metadata.confidence_score))

    lines = [f"═══ {department.upper()} DEPARTMENT RULES ({len(chunks)} items) ═══\n"]
    current_type = None

    for c in chunks:
        if c.knowledge_type.value != current_type:
            current_type = c.knowledge_type.value
            lines.append(f"\n── {current_type} ──")

        conf_marker = "🟢" if c.metadata.confidence_score >= 0.8 else "🟡" if c.metadata.confidence_score >= 0.6 else "🔴"
        lines.append(
            f"  {conf_marker} {c.title} (conf: {c.metadata.confidence_score:.2f})\n"
            f"     {c.content}\n"
        )

    return "\n".join(lines)


@mcp.tool()
def get_skill(skill_name: str) -> str:
    """
    Retrieve a compiled SKILL.md document by name.
    Skills are pre-assembled knowledge packages optimized for agent consumption.

    Args:
        skill_name: Name of the skill (e.g., "engineering-operational-knowledge")
    """
    registry = SkillRegistry(db_path=DB_PATH)
    all_skills = registry.list_all_skills()

    if skill_name not in all_skills:
        # Try fuzzy match
        matches = [s for s in all_skills if skill_name.lower() in s.lower()]
        if matches:
            return f"Skill '{skill_name}' not found. Did you mean: {', '.join(matches)}?"
        return f"Skill '{skill_name}' not found. Available: {', '.join(all_skills)}"

    # Find the SKILL.md file
    for dept_dir in Path(KB_PATH).iterdir():
        if dept_dir.is_dir():
            skill_dir = dept_dir / skill_name
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                return skill_file.read_text(encoding="utf-8")

    return f"Skill '{skill_name}' is registered but its SKILL.md file was not found on disk."


@mcp.tool()
def list_skills() -> str:
    """List all compiled skills available in the knowledge base."""
    registry = SkillRegistry(db_path=DB_PATH)
    skills = registry.list_all_skills()

    if not skills:
        return "No skills have been compiled yet. Run the pipeline first."

    # Enrich with file locations
    lines = [f"Available Skills ({len(skills)}):\n"]
    for skill_name in sorted(skills):
        chunk_ids = registry.get_chunks_for_skill(skill_name)
        # Find department
        dept_found = "unknown"
        for dept_dir in Path(KB_PATH).iterdir():
            if dept_dir.is_dir() and (dept_dir / skill_name / "SKILL.md").exists():
                dept_found = dept_dir.name
                break
        lines.append(f"  • {skill_name}  [{dept_found}]  ({len(chunk_ids)} source chunks)")

    return "\n".join(lines)


@mcp.tool()
def list_departments() -> str:
    """List all departments and their knowledge chunk counts."""
    store = FilesystemStore(db_path=DB_PATH)
    all_chunks = store.get_all()

    dept_stats: dict[str, dict] = {}
    for c in all_chunks:
        d = c.department.value
        if d not in dept_stats:
            dept_stats[d] = {"count": 0, "types": set(), "avg_conf": 0.0, "confs": []}
        dept_stats[d]["count"] += 1
        dept_stats[d]["types"].add(c.knowledge_type.value)
        dept_stats[d]["confs"].append(c.metadata.confidence_score)

    if not dept_stats:
        return "No knowledge chunks found. Run the pipeline to ingest data."

    lines = [f"Departments ({len(dept_stats)}):\n"]
    for dept, stats in sorted(dept_stats.items()):
        avg = sum(stats["confs"]) / len(stats["confs"])
        lines.append(
            f"  {dept.upper()}: {stats['count']} chunks | "
            f"Avg confidence: {avg:.2f} | "
            f"Types: {', '.join(sorted(stats['types']))}"
        )

    lines.append(f"\nTotal: {len(all_chunks)} chunks across {len(dept_stats)} departments")
    return "\n".join(lines)


@mcp.tool()
def report_failure(
    skill_name: str,
    department: str,
    issue_description: str,
    suggested_fix: str,
) -> str:
    """
    Report a failure or incorrect behavior from a skill.
    This injects the correction back into the pipeline's Failure Memory Loop.

    Args:
        skill_name: Which skill failed (e.g., "engineering-operational-knowledge")
        department: Department the skill belongs to
        issue_description: What went wrong
        suggested_fix: How it should be corrected
    """
    try:
        dept = Department(department.lower())
    except ValueError:
        return f"Error: Unknown department '{department}'."

    # Build the correction document
    raw_correction = (
        f"FAILURE REPORT for {skill_name}:\n"
        f"What failed: {issue_description}\n"
        f"Correction: {suggested_fix}"
    )

    # Run it through the mini-pipeline
    from pipeline.normalize import TextNormalizer
    from pipeline.chunking import SemanticChunker
    from pipeline.distill import KnowledgeDistiller
    from pipeline.deduplicate import KnowledgeDeduplicator

    normalizer = TextNormalizer()
    cleaned = normalizer.normalize_document(raw_correction)

    chunker = SemanticChunker(max_tokens=1000, overlap_tokens=100)
    chunks = chunker.chunk_document(
        cleaned, SourceType.LOCAL_FILE, f"feedback-{skill_name}", dept
    )

    if not chunks:
        return "Feedback received but no actionable content was extracted."

    distiller = KnowledgeDistiller()
    distilled = distiller.distill_chunks(chunks)

    if not distilled:
        return "Feedback processed but filtered as noise. Try providing more specific details."

    store = FilesystemStore(db_path=DB_PATH)
    deduplicator = KnowledgeDeduplicator()
    existing = store.get_all()

    saved = 0
    for chunk in distilled:
        action, processed = deduplicator.evaluate_chunk(chunk, existing)
        if action in ["ADD", "UPDATE"] and processed:
            store.save_chunk(processed)
            saved += 1

    return (
        f"Failure feedback processed successfully.\n"
        f"  Signals extracted: {len(distilled)}\n"
        f"  Chunks saved: {saved}\n"
        f"  Note: Run the pipeline to recompile the '{skill_name}' skill with this correction."
    )


@mcp.tool()
def run_pipeline() -> str:
    """
    Trigger a full 6-layer pipeline run (ingestion → synthesis).
    This reprocesses all data sources and recompiles skills.
    Returns immediately — use get_pipeline_status to monitor progress.
    """
    if _pipeline_state["is_running"]:
        return "Pipeline is already running. Use get_pipeline_status to check progress."

    import threading
    import io

    def _run():
        _pipeline_state["is_running"] = True
        _pipeline_state["last_run_log"] = ""

        old_stdout = sys.stdout
        sys.stdout = capture = io.StringIO()

        try:
            from main import run_orchestration_loop
            run_orchestration_loop()
        except Exception as e:
            print(f"\n[ERROR] Pipeline failed: {e}")
        finally:
            sys.stdout = old_stdout
            _pipeline_state["last_run_log"] = capture.getvalue()
            _pipeline_state["last_run_at"] = datetime.now(timezone.utc).isoformat()
            _pipeline_state["is_running"] = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return "Pipeline started in background. Use get_pipeline_status() to monitor."


@mcp.tool()
def get_pipeline_status() -> str:
    """Check if the pipeline is running and view the last run's output."""
    status = "RUNNING" if _pipeline_state["is_running"] else "IDLE"
    last_at = _pipeline_state["last_run_at"] or "never"
    log = _pipeline_state["last_run_log"] or "(no logs yet)"

    # Truncate log if very long
    if len(log) > 3000:
        log = log[:1500] + "\n\n... [truncated] ...\n\n" + log[-1500:]

    return f"Status: {status}\nLast run: {last_at}\n\n--- Pipeline Output ---\n{log}"


@mcp.tool()
def get_agent_bindings() -> str:
    """Show which skills are bound to which agents."""
    connector = AgentConnector(db_path=DB_PATH)
    registry_file = connector.registry_file

    if not registry_file.exists():
        return "No agent registry found."

    with open(registry_file, "r") as f:
        data = json.load(f)

    lines = [f"Agent Bindings ({len(data)} agents):\n"]
    for agent, info in sorted(data.items()):
        skills = info.get("skills", [])
        if skills:
            lines.append(f"  🤖 {agent}: {', '.join(skills)}")
        else:
            lines.append(f"  🤖 {agent}: (no skills bound)")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════
# RESOURCES
# ═════════════════════════════════════════════════════════════════

@mcp.resource("skill://{department}/{skill_name}")
def read_skill_resource(department: str, skill_name: str) -> str:
    """Read a compiled SKILL.md file as a resource."""
    skill_path = Path(KB_PATH) / department / skill_name / "SKILL.md"
    if not skill_path.exists():
        return f"Skill not found: {department}/{skill_name}"
    return skill_path.read_text(encoding="utf-8")


@mcp.resource("chunks://{department}")
def read_department_chunks(department: str) -> str:
    """Read all knowledge chunks for a department as structured text."""
    try:
        dept = Department(department.lower())
    except ValueError:
        return f"Unknown department: {department}"

    store = FilesystemStore(db_path=DB_PATH)
    chunks = store.get_all_by_department(dept)

    if not chunks:
        return f"No chunks for department: {department}"

    lines = [f"Knowledge chunks for {department} ({len(chunks)} items):\n"]
    for c in chunks:
        lines.append(json.dumps({
            "id": c.id,
            "title": c.title,
            "type": c.knowledge_type.value,
            "confidence": c.metadata.confidence_score,
            "content": c.content,
            "source": c.source_identifier,
            "tags": c.tags,
        }, indent=2))
        lines.append("")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════
# PROMPTS
# ═════════════════════════════════════════════════════════════════

@mcp.prompt()
def department_briefing(department: str) -> str:
    """
    Generate a comprehensive briefing for a department.
    Includes all rules, SOPs, edge cases, and failure patterns.
    """
    try:
        dept = Department(department.lower())
    except ValueError:
        return f"Unknown department: {department}"

    store = FilesystemStore(db_path=DB_PATH)
    chunks = store.get_all_by_department(dept)

    if not chunks:
        return f"No knowledge available for {department}."

    sections: dict[str, list[str]] = {}
    for c in chunks:
        kt = c.knowledge_type.value
        if kt not in sections:
            sections[kt] = []
        sections[kt].append(f"- **{c.title}** (confidence: {c.metadata.confidence_score:.2f}): {c.content}")

    prompt_lines = [
        f"You are being briefed on the {department.upper()} department's operational knowledge.",
        f"There are {len(chunks)} knowledge items across {len(sections)} categories.\n",
        "Use this information to guide all decisions related to this department.\n",
    ]

    for section_type, items in sorted(sections.items()):
        prompt_lines.append(f"## {section_type}")
        prompt_lines.extend(items)
        prompt_lines.append("")

    return "\n".join(prompt_lines)


@mcp.prompt()
def task_context(task_description: str, department: str = "shared") -> str:
    """
    Assemble relevant context for a specific task.
    Dynamically selects the most relevant knowledge chunks.
    """
    try:
        dept = Department(department.lower())
    except ValueError:
        dept = Department.SHARED

    store = FilesystemStore(db_path=DB_PATH)
    all_chunks = store.get_all_by_department(dept)

    # Also include shared rules
    if dept != Department.SHARED:
        all_chunks.extend(store.get_all_by_department(Department.SHARED))

    # Keyword relevance scoring
    keywords = set(task_description.lower().split())
    scored = []
    for c in all_chunks:
        if c.metadata.confidence_score < 0.5:
            continue
        text = f"{c.title} {c.content} {' '.join(c.tags)}".lower()
        matches = sum(1 for k in keywords if k in text)
        # Always include security rules and failure patterns
        if c.knowledge_type in (KnowledgeType.SECURITY_RULE, KnowledgeType.FAILURE_PATTERN):
            matches += 2
        if matches > 0:
            scored.append((matches, c))

    scored.sort(key=lambda x: (-x[0], -x[1].metadata.confidence_score))
    relevant = scored[:15]

    if not relevant:
        # Fallback: top confidence chunks
        all_chunks.sort(key=lambda c: -c.metadata.confidence_score)
        relevant = [(1, c) for c in all_chunks[:10]]

    lines = [
        f"Context assembled for task: \"{task_description}\"",
        f"Department: {department} | Relevant rules: {len(relevant)}\n",
        "Follow these rules when executing the task:\n",
    ]

    for _, c in relevant:
        lines.append(
            f"[{c.knowledge_type.value}] {c.title}: {c.content}"
        )

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BHRM Intelligence MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: stdio (local) or sse (network)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port for SSE transport (default: 8001)",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        mcp._mcp_server_app_url = None  # Reset if cached
        # Override the port on the server settings
        mcp.settings.port = args.port
        print(f"Starting BHRM MCP Server (SSE) on port {args.port}...")
        mcp.run(transport="sse")
    else:
        print("Starting BHRM MCP Server (stdio)...", file=sys.stderr)
        mcp.run(transport="stdio")
