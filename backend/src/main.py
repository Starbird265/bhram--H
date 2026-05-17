"""
BHRM Organizational Intelligence Engine — 6-Layer Orchestrator

The main pipeline that runs all 6 layers in sequence:
  Layer 1: INGESTION       → Pull raw data from Slack, Notion, local files
  Layer 2: NORMALIZATION   → Clean, standardize, format-unify
  Layer 3: CHUNKING        → Semantic segmentation with overlap + metadata
  Layer 4: DISTILLATION    → AI-powered extraction, noise filtering, classification
  Layer 5: DEDUPLICATION   → Similarity-based merge/update/discard
  Layer 6: SYNTHESIS       → Skill assembly, tool provisioning, agent routing
"""

import os
import uuid
from typing import List, Optional

from core.models import Department, SourceType, PipelineRunStats, KnowledgeChunk

# Layer 1: Ingestion
from ingestion.slack_connector import SlackConnector
from ingestion.notion_connector import NotionConnector
from ingestion.parsers import LocalFileParser

# Layer 2: Normalization
from pipeline.normalize import TextNormalizer

# Layer 3: Chunking
from pipeline.chunking import SemanticChunker

# Layer 4: Distillation
from pipeline.distill import KnowledgeDistiller

# Layer 5: Deduplication
from pipeline.deduplicate import KnowledgeDeduplicator

# Layer 6: Synthesis
from storage.filesystem_store import FilesystemStore
from storage.skill_registry import SkillRegistry
from smart_layer.assembler import SkillAssembler
from smart_layer.tool_provisioner import ToolProvisioner
from smart_layer.agent_connector import AgentConnector
from generators.skill_generator import SkillGenerator


def run_orchestration_loop(
    data_sources: Optional[List[str]] = None,
    slack_channel: str = "C1234_ENGINEERING",
    slack_channel_name: str = "#engineering-deployments"
):
    """
    Runs the complete 6-layer pipeline.
    
    Args:
        data_sources: List of paths to scan for raw data files.
                      Defaults to the backend/raw_data directory.
        slack_channel: Slack channel ID to pull from.
        slack_channel_name: Display name for the Slack channel.
    """
    run_id = str(uuid.uuid4())[:8]
    stats = PipelineRunStats(run_id=run_id)

    print(f"╔══════════════════════════════════════════════════════════════╗")
    print(f"║  6-LAYER ORGANIZATIONAL INTELLIGENCE PIPELINE  (Run: {run_id})  ║")
    print(f"╚══════════════════════════════════════════════════════════════╝")

    base_dir = os.path.dirname(os.path.dirname(__file__))

    if data_sources is None:
        data_sources = [os.path.join(base_dir, "raw_data")]

    # Init shared components
    store = FilesystemStore(db_path=os.path.join(base_dir, "database"))
    registry = SkillRegistry(db_path=os.path.join(base_dir, "database"))

    # ═══════════════════════════════════════════════════════════════
    # LAYER 1: INGESTION — Pull raw data from all sources
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 1: INGESTION                     │")
    print("└─────────────────────────────────────────┘")

    all_raw_messages = []
    all_raw_chunks = []

    # 1a. Slack Ingestion
    print("\n  [Slack] Pulling channel history...")
    slack = SlackConnector()
    raw_messages = slack.fetch_channel_history(channel_id=slack_channel)
    stats.raw_items_ingested += len(raw_messages)
    all_raw_messages = raw_messages
    for msg in raw_messages[:5]:
        print(f"    → {msg[:80]}{'...' if len(msg) > 80 else ''}")
    if len(raw_messages) > 5:
        print(f"    ... and {len(raw_messages) - 5} more messages")

    # 1b. Local File Ingestion
    for source_path in data_sources:
        if os.path.isdir(source_path):
            print(f"\n  [Files] Scanning: {source_path}")
            parser = LocalFileParser(raw_data_dir=source_path)
            file_chunks = parser.ingest_directory()
            all_raw_chunks.extend(file_chunks)
            stats.raw_items_ingested += len(file_chunks)

    print(f"\n  ✓ Layer 1 Complete: {stats.raw_items_ingested} items ingested")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 2: NORMALIZATION — Clean and standardize
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 2: NORMALIZATION                 │")
    print("└─────────────────────────────────────────┘")

    normalizer = TextNormalizer()

    # Normalize Slack messages
    normalized_slack = normalizer.normalize_slack_messages(raw_messages)
    original_lines = sum(1 for m in raw_messages if m.strip())
    normalized_lines = len([l for l in normalized_slack.split("\n") if l.strip()])
    noise_removed = original_lines - normalized_lines
    print(f"  [Slack] {original_lines} messages → {normalized_lines} signals ({noise_removed} noise filtered)")

    # Normalize file-based chunks
    for chunk in all_raw_chunks:
        chunk.content = normalizer.normalize_document(chunk.content)

    stats.items_after_normalization = normalized_lines + len(all_raw_chunks)
    print(f"\n  ✓ Layer 2 Complete: {stats.items_after_normalization} items after normalization")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 3: CHUNKING — Semantic segmentation with metadata
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 3: CHUNKING                      │")
    print("└─────────────────────────────────────────┘")

    chunker = SemanticChunker(max_tokens=1000, overlap_tokens=100)

    # Chunk Slack messages (specialized grouper)
    slack_chunks = chunker.chunk_slack_messages(
        messages=[l.lstrip("- ") for l in normalized_slack.split("\n") if l.strip()],
        source_identifier=slack_channel_name,
        department=Department.ENGINEERING
    )
    print(f"  [Slack] → {len(slack_chunks)} semantic chunks")

    # Chunk file-based content (already chunked at file level, but may need sub-chunking)
    file_chunks = []
    for raw_chunk in all_raw_chunks:
        sub_chunks = chunker.chunk_document(
            text=raw_chunk.content,
            source_type=raw_chunk.source_type,
            source_identifier=raw_chunk.source_identifier,
            department=raw_chunk.department
        )
        file_chunks.extend(sub_chunks)
    print(f"  [Files] → {len(file_chunks)} semantic chunks")

    all_chunks = slack_chunks + file_chunks
    stats.chunks_created = len(all_chunks)
    print(f"\n  ✓ Layer 3 Complete: {stats.chunks_created} total chunks")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 4: DISTILLATION — AI-powered extraction & classification
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 4: DISTILLATION                  │")
    print("└─────────────────────────────────────────┘")

    distiller = KnowledgeDistiller()
    ai_mode = "AI (GPT-4o-mini)" if distiller.client else "Mock (rule-based)"
    print(f"  Processing mode: {ai_mode}")
    print(f"  Batch size: {distiller.BATCH_SIZE} chunks/call")

    distilled_chunks = distiller.distill_chunks(all_chunks)
    stats.chunks_after_distillation = len(distilled_chunks)
    stats.noise_filtered = len(all_chunks) - len(distilled_chunks)

    for chunk in distilled_chunks[:5]:
        print(f"  [{chunk.knowledge_type.value}] {chunk.title} (conf: {chunk.metadata.confidence_score:.2f})")
    if len(distilled_chunks) > 5:
        print(f"  ... and {len(distilled_chunks) - 5} more signals")

    print(f"\n  ✓ Layer 4 Complete: {stats.chunks_after_distillation} signals extracted, {stats.noise_filtered} noise filtered")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 5: DEDUPLICATION — Merge with canonical store
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 5: DEDUPLICATION                 │")
    print("└─────────────────────────────────────────┘")

    deduplicator = KnowledgeDeduplicator()
    existing_chunks = store.get_all()
    print(f"  Existing canonical chunks: {len(existing_chunks)}")
    print(f"  New chunks to evaluate: {len(distilled_chunks)}")

    results = deduplicator.evaluate_batch(distilled_chunks, existing_chunks)

    for action, processed_chunk in results:
        if action in ["ADD", "UPDATE"] and processed_chunk:
            store.save_chunk(processed_chunk)
            if action == "ADD":
                stats.chunks_added += 1
            else:
                stats.chunks_updated += 1
            print(f"  [{action}] '{processed_chunk.title}'")
        else:
            stats.chunks_discarded += 1
            print(f"  [DISCARD] (redundant)")

    print(f"\n  ✓ Layer 5 Complete: +{stats.chunks_added} added, ~{stats.chunks_updated} updated, -{stats.chunks_discarded} discarded")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 6: SYNTHESIS — Skill compilation, provisioning, routing
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 6: SYNTHESIS                     │")
    print("└─────────────────────────────────────────┘")

    assembler = SkillAssembler()
    provisioner = ToolProvisioner()
    generator = SkillGenerator(base_output_dir=os.path.join(base_dir, "knowledge_base"))
    connector = AgentConnector(db_path=os.path.join(base_dir, "database"))

    # Recompile skills for each department that has chunks
    departments_with_data = set()
    all_canonical = store.get_all()
    for chunk in all_canonical:
        departments_with_data.add(chunk.department)

    for dept in departments_with_data:
        dept_chunks = store.get_all_by_department(dept)
        if not dept_chunks:
            continue

        skill_name = f"{dept.value}-operational-knowledge"
        print(f"\n  [Compiling] {skill_name} from {len(dept_chunks)} chunks...")

        # 6a. Assemble
        assembled_skill = assembler.assemble_skill(
            skill_name=skill_name,
            description=f"Core operational knowledge and rules for the {dept.value} department. Use when handling {dept.value}-related tasks.",
            chunks=dept_chunks,
            department=dept
        )

        # 6b. Provision tools
        provisioned_skill = provisioner.provision_skill(assembled_skill)
        mcp_tools = provisioned_skill.metadata.mcp_server if provisioned_skill.metadata else "None"

        # 6c. Save SKILL.md
        path = generator.save_skill(provisioned_skill, dept)
        registry.register_skill(skill_name, [c.id for c in dept_chunks])
        print(f"  ✓ Saved: {path}")
        print(f"    MCP Tools: {mcp_tools}")

        # 6d. Route to appropriate agent
        target_agent = _determine_agent(provisioned_skill)
        connector.bind_skill_to_agent(target_agent, provisioned_skill.name)
        connector.trigger_agent_reload(target_agent)
        print(f"    Agent: {target_agent}")

        stats.skills_recompiled += 1
        stats.agents_notified += 1

    # ═══════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  PIPELINE COMPLETE                                         ║")
    print(f"╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Raw items ingested:    {stats.raw_items_ingested:>5}                                ║")
    print(f"║  After normalization:   {stats.items_after_normalization:>5}                                ║")
    print(f"║  Chunks created:        {stats.chunks_created:>5}                                ║")
    print(f"║  Signals extracted:     {stats.chunks_after_distillation:>5}                                ║")
    print(f"║  Noise filtered:        {stats.noise_filtered:>5}                                ║")
    print(f"║  Added to DB:           {stats.chunks_added:>5}                                ║")
    print(f"║  Updated in DB:         {stats.chunks_updated:>5}                                ║")
    print(f"║  Discarded:             {stats.chunks_discarded:>5}                                ║")
    print(f"║  Skills compiled:       {stats.skills_recompiled:>5}                                ║")
    print(f"╚══════════════════════════════════════════════════════════════╝")

    return stats


def _determine_agent(skill) -> str:
    """Determine the best agent for a skill based on department and content."""
    if skill.department == Department.ENGINEERING:
        name_lower = skill.name.lower()
        if "security" in name_lower:
            return "sec_auditor"
        elif "deploy" in name_lower or "architecture" in name_lower:
            return "architect"
        return "code_reviewer"
    elif skill.department == Department.MARKETING:
        return "content_creator"
    elif skill.department == Department.SALES:
        return "sales_agent"
    else:
        return "code_reviewer"


if __name__ == "__main__":
    run_orchestration_loop()
