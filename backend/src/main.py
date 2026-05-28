"""
BHRM Organizational Intelligence Engine — 10-Layer Orchestrator

The main pipeline that runs all 10 layers in sequence:
  Layer 1:  INGESTION          → Pull raw data from Slack, Notion, local files
  Layer 2:  NORMALIZATION      → Clean, standardize, format-unify
  Layer 3:  CHUNKING           → Semantic segmentation with overlap + metadata
  Layer 4:  DISTILLATION       → AI-powered extraction, noise filtering, classification
  Layer 5:  DEDUPLICATION      → Similarity-based merge/update/discard
  Layer 6:  SYNTHESIS          → Skill assembly, tool provisioning, agent routing
  Layer 7:  PRIVACY SCAN       → PII/secret detection, sensitivity labeling
  Layer 8:  ATOMIC SEGMENTATION → Decompose chunks into AtomicKnowledgeUnits
  Layer 9:  CONFLICT RESOLUTION → Merge identical, flag contradictions
  Layer 10: MEMORY INDEXING    → Write to 8-tier memory architecture
"""

import os
import uuid
import hashlib
from typing import List, Optional
from datetime import datetime, timezone
from contextlib import contextmanager

from core.models import (
    Department, SourceType, ProcessingLayer, PipelineRunStats,
    KnowledgeChunk, KnowledgeType, KnowledgeMetadata,
    AtomicKnowledgeUnit, SensitivityLevel, UnitStatus, MemoryTier,
    SourceRef, PrivacyMode,
)

# Layer 1: Ingestion
from ingestion.slack_connector import SlackConnector
from ingestion.notion_connector import NotionConnector
from ingestion.parsers import LocalFileParser
from ingestion.connector_manager import ConnectorManager

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

# Layer 7: Privacy Scan (NEW)
from pipeline.privacy_scanner import PrivacyScanner

# Layers 8-10: Hybrid Architecture (NEW)
from storage.sqlite_store import SQLiteStore
from memory import MemoryManager
from providers.router import ProviderRouter
from smart_layer.validators import SkillValidator


import threading as _threading


class _TimeoutError(Exception):
    """Raised when a pipeline step exceeds its timeout."""
    pass


@contextmanager
def _timeout(seconds: int, label: str = "operation"):
    """Thread-safe timeout context manager.

    Uses threading.Thread + join(timeout=) instead of SIGALRM so it works
    correctly from FastAPI worker threads (SIGALRM only works on the main thread
    and raises ValueError: signal only works in main thread otherwise).
    """
    result = [None]       # [exception] or [None] on success
    finished = _threading.Event()

    def _target(fn_and_args):
        fn, args, kwargs = fn_and_args
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as exc:
            result[0] = exc
        finally:
            finished.set()

    # We can't wrap arbitrary "with" body in a thread, so we use a simpler
    # mechanism: set a flag after `seconds` and check it on __exit__.
    _expired = [False]

    def _alarm():
        if not finished.wait(seconds):
            _expired[0] = True
            finished.set()  # unblock anything waiting

    timer = _threading.Thread(target=_alarm, daemon=True)
    timer.start()
    try:
        yield
    finally:
        finished.set()  # signal that we're done
        if _expired[0]:
            raise _TimeoutError(f"{label} timed out after {seconds}s")


def run_orchestration_loop(
    data_sources: Optional[List[str]] = None,
    slack_channel: str = "C1234_ENGINEERING",
    slack_channel_name: str = "#engineering-deployments"
):
    """
    Runs the complete 10-layer pipeline.

    Args:
        data_sources: List of paths to scan for raw data files.
                      Defaults to the backend/raw_data directory.
        slack_channel: Slack channel ID to pull from.
        slack_channel_name: Display name for the Slack channel.
    """
    run_id = str(uuid.uuid4())[:8]
    stats = PipelineRunStats(run_id=run_id)

    print(f"╔══════════════════════════════════════════════════════════════╗")
    print(f"║  10-LAYER ORGANIZATIONAL INTELLIGENCE PIPELINE (Run: {run_id}) ║")
    print(f"╚══════════════════════════════════════════════════════════════╝")

    base_dir = os.path.dirname(os.path.dirname(__file__))

    if data_sources is None:
        data_sources = [os.path.join(base_dir, "raw_data")]

    # ─── Init shared components ──────────────────────────────────
    db_path = os.path.join(base_dir, "database")

    # Legacy store (backward compat, layers 1-6)
    store = FilesystemStore(db_path=db_path)
    registry = SkillRegistry(db_path=db_path)

    # New hybrid architecture components (layers 7-10)
    sqlite_store = SQLiteStore(db_path=db_path)
    memory = MemoryManager(sqlite_store)
    # Bug 7 fix: flush stale working units from previous runs before starting a new run.
    # Without this, old CONTESTED/PENDING units pollute conflict detection every run.
    memory.flush_working_memory()
    print("  [Memory] Flushed stale working units from previous run")

    # Run expiry sweep at pipeline start (clean stale session facts + expired units)
    expiry_result = memory.run_expiry_sweep()
    if expiry_result["total_cleaned"] > 0:
        print(f"  [Memory] Expiry sweep: {expiry_result['total_cleaned']} stale items cleaned")
    else:
        print("  [Memory] Expiry sweep: nothing to clean")
    router = ProviderRouter()
    scanner = PrivacyScanner()
    validator = SkillValidator(min_units=2, min_coverage=0.4)

    # Search index — wired directly so every chunk save updates search immediately
    from core.search import VectorStore as _VectorStore
    _vs = _VectorStore(db_path=db_path)

    # Log provider availability
    providers = router.get_available_providers()
    print(f"\n  AI Providers: {', '.join(p['name'] for p in providers if p['available'])}")

    # Log memory stats
    mem_stats = memory.get_stats()
    print(f"  Memory: {mem_stats['atomic_units_canonical']} canonical units, "
          f"{mem_stats['source_refs']} sources, {mem_stats['audit_entries']} audit entries")

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

    # 1c. Connected App Ingestion (via ConnectorManager)
    base_db_path = os.path.join(base_dir, "database")
    conn_mgr = ConnectorManager(db_path=base_db_path)
    connected_chunks = []
    try:
        with _timeout(30, "ConnectorManager.ingest_all_connected"):
            connected_chunks = conn_mgr.ingest_all_connected()
    except _TimeoutError as te:
        print(f"  ⚠ {te} — skipping connected apps this run")
    except Exception as e:
        print(f"  ⚠ Connected app ingestion failed: {e} — continuing with local data")

    source_summary = {}  # Track {source_name: chunk_count}
    if connected_chunks:
        from core.models import SourceType as _ST, Department as _Dept, KnowledgeType as _KT
        from core.models import KnowledgeMetadata as _KMeta, ProcessingLayer as _PL
        import uuid as _uuid
        normalized_connected = []
        for item in connected_chunks:
            if hasattr(item, "source_type"):  # already a KnowledgeChunk
                normalized_connected.append(item)
            else:  # RawDocument — wrap it
                content = getattr(item, "content", str(item))
                loc = getattr(item, "location_key", "unknown")
                src_app = getattr(item, "source_app", "unknown")
                src_type = getattr(_ST, src_app.upper(), _ST.LOCAL_FILE)
                from pipeline.normalize import TextNormalizer as _TN
                from pipeline.chunking import SemanticChunker as _SC
                _normed = _TN().normalize_document(content)
                _chunks = _SC().chunk_document(_normed, src_type, loc, _Dept.SHARED)
                normalized_connected.extend(_chunks)
        all_raw_chunks.extend(normalized_connected)
        stats.raw_items_ingested += len(normalized_connected)
        source_summary["connected_apps"] = len(normalized_connected)
        print(f"\n  [Apps] {len(normalized_connected)} chunks from connected apps")
    else:
        source_summary["connected_apps"] = 0
        print(f"\n  [Apps] 0 chunks from connected apps (none connected or failed)")

    # Also add filesystem paths from connectors as data sources
    connector_paths = conn_mgr.get_connected_data_paths()
    for cp in connector_paths:
        if cp not in data_sources and os.path.isdir(cp):
            print(f"\n  [Apps] Scanning connected path: {cp}")
            parser = LocalFileParser(raw_data_dir=cp)
            extra_chunks = parser.ingest_directory()
            all_raw_chunks.extend(extra_chunks)
            stats.raw_items_ingested += len(extra_chunks)

    # Track source breakdown
    source_summary["slack"] = len(raw_messages)
    source_summary["local_files"] = len(all_raw_chunks) - source_summary.get("connected_apps", 0)

    # Bug 18 fix: use deterministic ID = hash(source_identifier + content_hash)
    for chunk in all_raw_chunks:
        content_hash = hashlib.sha256(chunk.content.encode()).hexdigest()[:16]
        stable_id = "src-" + hashlib.sha256(
            (chunk.source_identifier + content_hash).encode()
        ).hexdigest()[:12]
        ref = SourceRef(
            id=stable_id,
            source_type=chunk.source_type,
            source_identifier=chunk.source_identifier,
            file_hash=content_hash,
            byte_size=len(chunk.content.encode()),
            privacy_mode=_get_source_privacy(chunk.source_type),
        )
        memory.record_source(ref)

    print(f"\n  ✓ Layer 1 Complete: {stats.raw_items_ingested} items ingested")
    print(f"  Source breakdown: {source_summary}")

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

    # Chunk file-based content
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
    ai_mode = "AI" if distiller.client else "Mock (rule-based)"
    print(f"  Processing mode: {ai_mode}")
    print(f"  Batch size: {distiller.BATCH_SIZE} chunks/call")

    distilled_chunks = distiller.distill_chunks(all_chunks)
    stats.chunks_after_distillation = len(distilled_chunks)
    stats.noise_filtered = max(0, len(all_chunks) - len(distilled_chunks))

    for chunk in distilled_chunks[:5]:
        print(f"  [{chunk.knowledge_type.value}] {chunk.title} (conf: {chunk.metadata.confidence_score:.2f})")
    if len(distilled_chunks) > 5:
        print(f"  ... and {len(distilled_chunks) - 5} more signals")

    print(f"\n  ✓ Layer 4 Complete: {stats.chunks_after_distillation} signals, {stats.noise_filtered} noise filtered")

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

            # Also save to SQLite store for new architecture
            sqlite_store.save_chunk(processed_chunk)

            # ── Root fix: index into search immediately on every save ──
            try:
                _vs.index_chunk(
                    chunk_id=processed_chunk.id,
                    title=processed_chunk.title,
                    content=processed_chunk.content,
                    summary=getattr(processed_chunk, 'summary', ''),
                    department=processed_chunk.department.value,
                    knowledge_type=processed_chunk.knowledge_type.value,
                    source_type=processed_chunk.source_type.value,
                    source_id=processed_chunk.source_identifier,
                    tags=list(getattr(processed_chunk, 'tags', [])),
                    confidence=processed_chunk.metadata.confidence_score,
                )
            except Exception as _e:
                pass  # Search index failure never blocks pipeline

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
    # LAYER 5.5: PRIVACY SCAN — PII/secret detection & sensitivity
    # Bug 16 fix: runs BEFORE synthesis so sensitive data is flagged
    # before AI skill compilation and agent routing ever touches it.
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 5.5: PRIVACY SCAN                │")
    print("└─────────────────────────────────────────┘")

    all_deduped_chunks = store.get_all()
    flagged_count = 0
    _sensitive_chunk_ids: set = set()

    for chunk in all_deduped_chunks:
        scan_result = scanner.scan(chunk.content)
        stats.items_privacy_scanned += 1

        if scan_result.has_pii:
            flagged_count += 1
            stats.items_flagged_sensitive += 1
            _sensitive_chunk_ids.add(chunk.id)
            print(f"  ⚠ PII in '{chunk.title}': {scan_result.categories_found} → {scan_result.suggested_sensitivity.value}")

            memory.log_decision(
                action="privacy_scan",
                target_type="chunk",
                target_id=chunk.id,
                details=scan_result.to_dict(),
            )

    print(f"\n  ✓ Layer 5.5 Complete: {stats.items_privacy_scanned} scanned, {stats.items_flagged_sensitive} flagged")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 8: ATOMIC SEGMENTATION — Decompose into AtomicKnowledgeUnits
    # Only process non-sensitive chunks (Bug 5 fix: _sensitive_chunk_ids used here)
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 8: ATOMIC SEGMENTATION           │")
    print("└─────────────────────────────────────────┘")

    safe_chunks = [c for c in all_deduped_chunks if c.id not in _sensitive_chunk_ids]
    print(f"  Segmenting {len(safe_chunks)} safe chunks ({len(_sensitive_chunk_ids)} sensitive filtered out)")

    for chunk in safe_chunks:
        # Extract atomic units from each chunk
        units = _decompose_to_atomic_units(chunk, scanner)
        for unit in units:
            memory.save_working_unit(unit)
            stats.atomic_units_created += 1

    print(f"\n  ✓ Layer 8 Complete: {stats.atomic_units_created} atomic units created")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 9: CONFLICT RESOLUTION — Flag contradictions
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 9: CONFLICT RESOLUTION           │")
    print("└─────────────────────────────────────────┘")

    working_units = memory.get_working_units()
    canonical_units = memory.get_canonical_units()

    for unit in working_units:
        conflict_found = False

        # Check for conflicts with existing canonical units
        for existing in canonical_units:
            if existing.department != unit.department:
                continue
            if existing.knowledge_type != unit.knowledge_type:
                continue

            # Simple conflict detection: same topic but different instruction
            if (_text_similarity(existing.claim, unit.claim) > 0.7 and
                _text_similarity(existing.instruction, unit.instruction) < 0.5):
                # Potential conflict
                unit.conflicts_with.append(existing.id)
                unit.status = UnitStatus.CONTESTED
                conflict_found = True
                stats.conflicts_detected += 1
                print(f"  ⚠ Conflict: '{unit.claim[:50]}...' vs '{existing.claim[:50]}...'")

                memory.log_decision(
                    action="conflict_detected",
                    target_type="unit",
                    target_id=unit.id,
                    details={"conflicts_with": existing.id},
                )

        if not conflict_found:
            # No conflicts — auto-approve if confidence is high enough
            if unit.confidence_score >= 0.7:
                memory.promote_to_canonical(unit)
                stats.conflicts_resolved += 1
            else:
                # Save as-is in working memory for manual review
                memory.save_working_unit(unit)
        else:
            memory.save_working_unit(unit)

    print(f"\n  ✓ Layer 9 Complete: {stats.conflicts_detected} conflicts, {stats.conflicts_resolved} auto-resolved")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 6: SYNTHESIS — Skill compilation, provisioning, routing
    # Bug 6 fix: runs AFTER Layers 8+9 so approved AtomicKnowledgeUnits
    # are used as the primary source for skill content (not raw chunks).
    # Bug 5 fix: sensitive chunks are excluded from all synthesis.
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 6: SYNTHESIS                     │")
    print("└─────────────────────────────────────────┘")

    assembler = SkillAssembler()
    provisioner = ToolProvisioner()
    generator = SkillGenerator(base_output_dir=os.path.join(base_dir, "knowledge_base"))
    connector = AgentConnector(db_path=os.path.join(base_dir, "database"))

    # Recompile skills per department using approved canonical units
    departments_with_data = set(c.department for c in safe_chunks)

    for dept in departments_with_data:
        # Bug 6 fix: prefer canonical AtomicKnowledgeUnits from Layer 9
        approved_units = memory.get_canonical_units(department=dept)
        if approved_units:
            # Convert AtomicKnowledgeUnits → KnowledgeChunk format for assembler
            from core.models import KnowledgeMetadata as _KMeta, ProcessingLayer as _PL
            from datetime import datetime as _dt, timezone as _tz
            dept_chunks = []
            for u in approved_units:
                raw_pool = store.get_all_by_department(dept)
                src_type = raw_pool[0].source_type if raw_pool else SourceType.LOCAL_FILE
                dept_chunks.append(KnowledgeChunk(
                    id=u.id,
                    department=u.department,
                    knowledge_type=u.knowledge_type,
                    source_type=src_type,
                    source_identifier=getattr(u, "source_ref", f"atomic:{u.id}") or f"atomic:{u.id}",
                    title=u.claim[:80],
                    content=f"{u.claim}\n\n{u.instruction}" if getattr(u, "instruction", "") else u.claim,
                    summary=u.claim[:120],
                    tags=list(getattr(u, "tags", [])),
                    metadata=_KMeta(confidence_score=u.confidence_score),
                    processing_layer=_PL.DISTILLED,
                    created_at=_dt.now(_tz.utc),
                    updated_at=_dt.now(_tz.utc),
                ))
            print(f"\n  [Synthesis] {dept.value}: {len(dept_chunks)} canonical units (Layer 9 approved)")
        else:
            # Fallback: use raw safe chunks when no canonical units exist yet
            dept_chunks = [c for c in store.get_all_by_department(dept)
                           if c.id not in _sensitive_chunk_ids]
            print(f"\n  [Synthesis] {dept.value}: {len(dept_chunks)} raw chunks (no canonical units yet)")

        if not dept_chunks:
            print(f"  [Synthesis] Skipping {dept.value} — all chunks flagged sensitive or empty")
            continue

        skill_name = f"{dept.value}-operational-knowledge"
        print(f"  [Compiling] {skill_name} from {len(dept_chunks)} chunks...")

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

        # 6c. Save SKILL.md + CONTEXT.md
        path = generator.save_skill(provisioned_skill, dept)
        ctx_path = generator.save_context(provisioned_skill, dept)
        registry.register_skill(skill_name, [c.id for c in dept_chunks])
        print(f"  ✓ Saved: {path}")
        print(f"  ✓ Context: {ctx_path}")
        print(f"    MCP Tools: {mcp_tools}")

        # 6d. Route to appropriate agent
        target_agent = _determine_agent(provisioned_skill)
        connector.bind_skill_to_agent(target_agent, provisioned_skill.name)
        connector.trigger_agent_reload(target_agent)
        print(f"    Agent: {target_agent}")

        stats.skills_recompiled += 1
        stats.agents_notified += 1

    print(f"\n  ✓ Layer 6 Complete: {stats.skills_recompiled} skills compiled")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 10: MEMORY INDEXING — Write to memory tiers
    # ═══════════════════════════════════════════════════════════════
    print("\n┌─────────────────────────────────────────┐")
    print("│  LAYER 10: MEMORY INDEXING              │")
    print("└─────────────────────────────────────────┘")

    # Perform final cleanup
    memory.run_expiry_sweep()

    final_stats = memory.get_stats()
    stats.units_indexed = final_stats["atomic_units_canonical"]

    print(f"  Canonical units:     {final_stats['atomic_units_canonical']}")
    print(f"  Working (pending):   {final_stats['atomic_units_working']}")
    print(f"  Expired units:       {final_stats.get('atomic_units_expired', 0)}")
    print(f"  Session facts:       {final_stats.get('session_facts', 0)}")
    print(f"  Source refs:         {final_stats['source_refs']}")
    print(f"  Audit entries:       {final_stats['audit_entries']}")
    print(f"  Unresolved failures: {final_stats['unresolved_failures']}")

    # Log completion in audit
    memory.log_decision(
        action="pipeline_complete",
        target_type="run",
        target_id=run_id,
        details={
            "layers_completed": 10,
            "chunks_processed": stats.chunks_created,
            "units_created": stats.atomic_units_created,
            "conflicts_found": stats.conflicts_detected,
        },
    )

    print(f"\n  ✓ Layer 10 Complete: {stats.units_indexed} units indexed in canonical memory")

    # ═══════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  10-LAYER PIPELINE COMPLETE                                ║")
    print(f"╠══════════════════════════════════════════════════════════════╣")
    print(f"║  L1  Raw items ingested:     {stats.raw_items_ingested:>5}                           ║")
    print(f"║  L2  After normalization:    {stats.items_after_normalization:>5}                           ║")
    print(f"║  L3  Chunks created:         {stats.chunks_created:>5}                           ║")
    print(f"║  L4  Signals extracted:      {stats.chunks_after_distillation:>5}                           ║")
    print(f"║  L4  Noise filtered:         {stats.noise_filtered:>5}                           ║")
    print(f"║  L5  Added / Updated / Drop: {stats.chunks_added:>3} / {stats.chunks_updated:>3} / {stats.chunks_discarded:>3}                    ║")
    print(f"║  L6  Skills compiled:        {stats.skills_recompiled:>5}                           ║")
    print(f"║  L7  Privacy flagged:        {stats.items_flagged_sensitive:>5}                           ║")
    print(f"║  L8  Atomic units created:   {stats.atomic_units_created:>5}                           ║")
    print(f"║  L9  Conflicts detected:     {stats.conflicts_detected:>5}                           ║")
    print(f"║  L10 Units in canonical:     {stats.units_indexed:>5}                           ║")
    print(f"╚══════════════════════════════════════════════════════════════╝")

    return stats


# ─── Helper Functions ────────────────────────────────────────────

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


def _get_source_privacy(source_type: SourceType) -> PrivacyMode:
    """Default privacy mode based on source type."""
    # Local sources default to HARD_LOCAL (most secure)
    if source_type in (SourceType.LOCAL_FILE, SourceType.MARKDOWN, SourceType.JSON):
        return PrivacyMode.HARD_LOCAL
    # Already-cloud sources default to ONLINE_ALLOWED
    elif source_type in (SourceType.SLACK, SourceType.NOTION, SourceType.GITHUB):
        return PrivacyMode.ONLINE_ALLOWED
    return PrivacyMode.HARD_LOCAL


def _decompose_to_atomic_units(
    chunk: KnowledgeChunk,
    scanner: PrivacyScanner,
) -> List[AtomicKnowledgeUnit]:
    """Decompose a KnowledgeChunk into AtomicKnowledgeUnits.

    Uses rule-based decomposition: splits multi-rule content into
    individual claims. Each claim becomes one atomic unit.
    """
    units: List[AtomicKnowledgeUnit] = []
    content = chunk.content.strip()

    # Split content into individual claims/rules
    claims = _split_into_claims(content)

    for claim_text in claims:
        if len(claim_text.strip()) < 10:
            continue

        # Scan this claim for PII
        scan = scanner.scan(claim_text)

        # Deterministic ID: same claim+dept always produces the same ID across runs
        # This makes ON CONFLICT(id) DO UPDATE actually work for cross-run dedup
        _claim_key = f"{claim_text.strip().lower()}|{chunk.department.value}"
        _det_id = f"aku-{hashlib.sha256(_claim_key.encode()).hexdigest()[:12]}"

        unit = AtomicKnowledgeUnit(
            id=_det_id,
            claim=claim_text.strip(),
            instruction=_to_imperative(claim_text.strip()),
            knowledge_type=chunk.knowledge_type,
            department=chunk.department,
            source_type=chunk.source_type,
            source_identifier=chunk.source_identifier,
            source_excerpt_hash=hashlib.sha256(claim_text.encode()).hexdigest()[:16],
            source_reliability=chunk.metadata.source_reliability,
            confidence_score=chunk.metadata.confidence_score,
            sensitivity_level=scan.suggested_sensitivity,
            online_allowed=scan.suggested_sensitivity not in (
                SensitivityLevel.RESTRICTED, SensitivityLevel.CONFIDENTIAL
            ),
            tags=list(chunk.tags),
        )
        units.append(unit)

    return units


def _split_into_claims(content: str) -> List[str]:
    """Split compound content into individual claims."""
    claims = []

    # Split by newlines, bullet points, numbered items
    lines = content.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Remove bullet markers
        for prefix in ["- ", "• ", "* ", "→ "]:
            if line.startswith(prefix):
                line = line[len(prefix):]
                break

        # Remove numbered markers (1. 2. etc.)
        if len(line) > 2 and line[0].isdigit() and line[1] in ".)" :
            line = line[2:].strip()
        elif len(line) > 3 and line[:2].isdigit() and line[2] in ".)" :
            line = line[3:].strip()

        # Skip markdown headings and section markers — not knowledge claims
        if line.startswith('#') or line.startswith('---') or line.startswith('==='):
            continue
        if len(line) >= 10:
            claims.append(line)

    # If no structure was found, treat the whole content as one claim
    if not claims and len(content) >= 10:
        claims = [content]

    return claims


def _to_imperative(text: str) -> str:
    """Convert a claim to imperative form (best effort)."""
    text = text.strip()
    # Already imperative-ish
    if text.lower().startswith(("always ", "never ", "do ", "ensure ", "use ", "avoid ", "check ")):
        return text

    # Simple heuristic: if it starts with a verb-like word, it's probably imperative
    if text[0].isupper() and " " in text:
        return text

    return f"Ensure that {text.lower()}" if text else text


def _text_similarity(a: str, b: str) -> float:
    """Quick word-overlap similarity (0.0–1.0). Not meant to be precise."""
    if not a or not b:
        return 0.0
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


if __name__ == "__main__":
    run_orchestration_loop()
