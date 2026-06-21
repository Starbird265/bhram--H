"""
BHRM Organizational Intelligence Engine — 11-Layer Orchestrator

The main pipeline that runs all 11 layers in sequence:
  Layer 1:  INGESTION          → Pull raw data from Slack, Notion, GitHub, local files
  Layer 2:  NORMALIZATION      → Clean, standardize, format-unify
  Layer 3:  CHUNKING           → Semantic segmentation with overlap + metadata
  Layer 4:  DISTILLATION       → AI-powered extraction, noise filtering, classification
  Layer 5:  DEDUPLICATION      → Similarity-based merge/update/discard
  Layer 6:  PRIVACY SCAN       → PII/secret detection, sensitivity labeling
  Layer 7:  ATOMIC SEGMENTATION → Decompose chunks into AtomicKnowledgeUnits
  Layer 8:  CONFLICT RESOLUTION → Merge identical, flag contradictions
  Layer 9:  SYNTHESIS          → Skill assembly, tool provisioning, agent routing
  Layer 10: MEMORY INDEXING    → Write to 8-tier memory architecture
  Layer 11: AGENT ORCHESTRATION → Context injection, tool gating, and agent hot-reloading
"""

import os
import uuid
import hashlib
import sys
import logging
from typing import List, Optional
from datetime import datetime, timezone
from contextlib import contextmanager

import click

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

# Layer 9: Synthesis (Reflects current logic where synthesis is after conflict resolution)
from storage.filesystem_store import FilesystemStore
from storage.skill_registry import SkillRegistry
from smart_layer.assembler import SkillAssembler
from smart_layer.tool_provisioner import ToolProvisioner
from smart_layer.agent_connector import AgentConnector
from generators.skill_generator import SkillGenerator

# Layer 6: Privacy Scan
from pipeline.privacy_scanner import PrivacyScanner

# Layers 10: Hybrid Architecture
from storage.sqlite_store import SQLiteStore
from memory import MemoryManager
from providers.router import ProviderRouter
from smart_layer.validators import SkillValidator


import threading as _threading


# ── Logging Configuration ───────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("pipeline.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("CortexAI")

# Dedicated Connection Log
conn_logger = logging.getLogger("ConnectionLog")
conn_handler = logging.FileHandler("connection.log")
conn_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
conn_logger.addHandler(conn_handler)
conn_logger.setLevel(logging.INFO)


class _TimeoutError(Exception):
    """Raised when a pipeline step exceeds its timeout."""
    pass


@contextmanager
def _timeout(seconds: int, label: str = "operation"):
    """Thread-safe timeout context manager."""
    result = [None]
    finished = _threading.Event()

    def _target(fn_and_args):
        fn, args, kwargs = fn_and_args
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as exc:
            result[0] = exc
        finally:
            finished.set()

    _expired = [False]

    def _alarm():
        if not finished.wait(seconds):
            _expired[0] = True
            finished.set()

    timer = _threading.Thread(target=_alarm, daemon=True)
    timer.start()
    try:
        yield
    finally:
        finished.set()
        if _expired[0]:
            raise _TimeoutError(f"{label} timed out after {seconds}s")


def run_orchestration_loop(
    data_sources: Optional[List[str]] = None,
    slack_channel: str = "C1234_ENGINEERING",
    slack_channel_name: str = "#engineering-deployments",
    skip_layers: Optional[List[int]] = None
):
    """
    Runs the complete 11-layer pipeline.
    """
    run_id = str(uuid.uuid4())[:8]
    stats = PipelineRunStats(run_id=run_id)
    skip_layers = skip_layers or []

    logger.info(f"╔══════════════════════════════════════════════════════════════╗")
    logger.info(f"║  11-LAYER ORGANIZATIONAL INTELLIGENCE PIPELINE (Run: {run_id}) ║")
    logger.info(f"╚══════════════════════════════════════════════════════════════╝")

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if data_sources is None:
        data_sources = [os.path.join(base_dir, "raw_data")]

    all_raw_messages = []
    all_raw_chunks = []
    all_chunks = []
    distilled_chunks = []
    all_deduped_chunks = []
    _sensitive_chunk_ids: set = set()
    safe_chunks = []

    # ─── Init shared components ──────────────────────────────────
    db_path = os.path.join(base_dir, "database")

    # Legacy store (backward compat)
    store = FilesystemStore(db_path=db_path)
    registry = SkillRegistry(db_path=db_path)

    # New hybrid architecture components
    sqlite_store = SQLiteStore(db_path=db_path)
    memory = MemoryManager(sqlite_store)

    memory.flush_working_memory()
    logger.info("  [Memory] Flushed stale working units from previous run")

    expiry_result = memory.run_expiry_sweep()
    if expiry_result["total_cleaned"] > 0:
        logger.info(f"  [Memory] Expiry sweep: {expiry_result['total_cleaned']} stale items cleaned")
    else:
        logger.info("  [Memory] Expiry sweep: nothing to clean")

    router = ProviderRouter()
    # Phase 3: Hash cache
    from providers.hash_cache import HashCache
    cache = HashCache(db_path=db_path)

    scanner = PrivacyScanner()
    validator = SkillValidator(min_units=2, min_coverage=0.4)

    from core.search import VectorStore as _VectorStore
    _vs = _VectorStore(db_path=db_path)

    providers = router.get_available_providers()
    logger.info(f"\n  AI Providers: {', '.join(p['name'] for p in providers if p['available'])}")

    mem_stats = memory.get_stats()
    logger.info(f"  Memory: {mem_stats['atomic_units_canonical']} canonical units, "
                f"{mem_stats['source_refs']} sources, {mem_stats['audit_entries']} audit entries")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 1: INGESTION
    # ═══════════════════════════════════════════════════════════════
    if 1 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 1: INGESTION                     │")
        logger.info("└─────────────────────────────────────────┘")

        # 1a. Slack Ingestion
        logger.info("\n  [Slack] Pulling channel history...")
        slack = SlackConnector()
        try:
            raw_messages = slack.fetch_channel_history(channel_id=slack_channel)
            stats.raw_items_ingested += len(raw_messages)
            all_raw_messages = raw_messages
            conn_logger.info(f"Slack connected and fetched {len(raw_messages)} messages from {slack_channel}")
            for msg in raw_messages[:5]:
                logger.info(f"    → {msg[:80]}{'...' if len(msg) > 80 else ''}")
        except Exception as e:
            conn_logger.error(f"Slack connection failed for {slack_channel}: {e}")
            raw_messages = []

        # 1b. Local File Ingestion
        for source_path in data_sources:
            if os.path.isdir(source_path):
                logger.info(f"\n  [Files] Scanning: {source_path}")
                parser = LocalFileParser(raw_data_dir=source_path)
                file_chunks = parser.ingest_directory()
                all_raw_chunks.extend(file_chunks)
                stats.raw_items_ingested += len(file_chunks)
                conn_logger.info(f"Local files ingested from {source_path}: {len(file_chunks)} chunks")

        # 1c. Connected App Ingestion
        conn_mgr = ConnectorManager(db_path=db_path)
        connected_chunks = []
        try:
            with _timeout(30, "ConnectorManager.ingest_all_connected"):
                connected_chunks = conn_mgr.ingest_all_connected()
                conn_logger.info(f"Connected apps ingestion successful: {len(connected_chunks)} items")
        except _TimeoutError as te:
            logger.warning(f"  ⚠ {te} — skipping connected apps this run")
            conn_logger.warning(f"Connected apps ingestion timed out")
        except Exception as e:
            logger.warning(f"  ⚠ Connected app ingestion failed: {e}")
            conn_logger.error(f"Connected apps ingestion failed: {e}")

        source_summary = {}
        if connected_chunks:
            from core.models import SourceType as _ST, Department as _Dept
            normalized_connected = []
            for item in connected_chunks:
                if hasattr(item, "source_type"):
                    normalized_connected.append(item)
                else:
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
            logger.info(f"\n  [Apps] {len(normalized_connected)} chunks from connected apps")
        else:
            source_summary["connected_apps"] = 0

        connector_paths = conn_mgr.get_connected_data_paths()
        for cp in connector_paths:
            if cp not in data_sources and os.path.isdir(cp):
                logger.info(f"\n  [Apps] Scanning connected path: {cp}")
                parser = LocalFileParser(raw_data_dir=cp)
                extra_chunks = parser.ingest_directory()
                all_raw_chunks.extend(extra_chunks)
                stats.raw_items_ingested += len(extra_chunks)

        source_summary["slack"] = len(raw_messages)
        source_summary["local_files"] = len(all_raw_chunks) - source_summary.get("connected_apps", 0)

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

        logger.info(f"\n  ✓ Layer 1 Complete: {stats.raw_items_ingested} items ingested")
        logger.info(f"  Source breakdown: {source_summary}")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 2: NORMALIZATION
    # ═══════════════════════════════════════════════════════════════
    if 2 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 2: NORMALIZATION                 │")
        logger.info("└─────────────────────────────────────────┘")

        normalizer = TextNormalizer()
        normalized_slack = normalizer.normalize_slack_messages(all_raw_messages)
        for chunk in all_raw_chunks:
            chunk.content = normalizer.normalize_document(chunk.content)

        stats.items_after_normalization = len([l for l in normalized_slack.split("\n") if l.strip()]) + len(all_raw_chunks)
        logger.info(f"  ✓ Layer 2 Complete: {stats.items_after_normalization} items normalized")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 3: CHUNKING
    # ═══════════════════════════════════════════════════════════════
    if 3 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 3: CHUNKING                      │")
        logger.info("└─────────────────────────────────────────┘")

        chunker = SemanticChunker(max_tokens=1000, overlap_tokens=100)
        slack_chunks = chunker.chunk_slack_messages(
            messages=[l.lstrip("- ") for l in normalized_slack.split("\n") if l.strip()],
            source_identifier=slack_channel_name,
            department=Department.ENGINEERING
        )
        file_chunks = []
        for raw_chunk in all_raw_chunks:
            sub_chunks = chunker.chunk_document(
                text=raw_chunk.content,
                source_type=raw_chunk.source_type,
                source_identifier=raw_chunk.source_identifier,
                department=raw_chunk.department
            )
            file_chunks.extend(sub_chunks)

        all_chunks = slack_chunks + file_chunks
        stats.chunks_created = len(all_chunks)
        logger.info(f"  ✓ Layer 3 Complete: {stats.chunks_created} total chunks")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 4: DISTILLATION
    # ═══════════════════════════════════════════════════════════════
    if 4 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 4: DISTILLATION                  │")
        logger.info("└─────────────────────────────────────────┘")

        distiller = KnowledgeDistiller(router=router, cache=cache, db_path=db_path)
        distilled_chunks = distiller.distill_chunks(all_chunks)
        stats.chunks_after_distillation = len(distilled_chunks)
        stats.noise_filtered = max(0, len(all_chunks) - len(distilled_chunks))
        logger.info(f"  ✓ Layer 4 Complete: {stats.chunks_after_distillation} signals")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 5: DEDUPLICATION
    # ═══════════════════════════════════════════════════════════════
    if 5 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 5: DEDUPLICATION                 │")
        logger.info("└─────────────────────────────────────────┘")

        deduplicator = KnowledgeDeduplicator(router=router, cache=cache, db_path=db_path)
        existing_chunks = store.get_all()
        results = deduplicator.evaluate_batch(distilled_chunks, existing_chunks)

        for action, processed_chunk in results:
            if action in ["ADD", "UPDATE"] and processed_chunk:
                store.save_chunk(processed_chunk)
                sqlite_store.save_chunk(processed_chunk)
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
                except: pass

                if action == "ADD": stats.chunks_added += 1
                else: stats.chunks_updated += 1
            else:
                stats.chunks_discarded += 1

        logger.info(f"  ✓ Layer 5 Complete: +{stats.chunks_added} added, ~{stats.chunks_updated} updated")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 6: PRIVACY SCAN
    # ═══════════════════════════════════════════════════════════════
    if 6 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 6: PRIVACY SCAN                  │")
        logger.info("└─────────────────────────────────────────┘")

        all_deduped_chunks = store.get_all()
        _sensitive_chunk_ids: set = set()

        for chunk in all_deduped_chunks:
            scan_result = scanner.scan(chunk.content)
            stats.items_privacy_scanned += 1
            if scan_result.has_pii:
                stats.items_flagged_sensitive += 1
                _sensitive_chunk_ids.add(chunk.id)
                memory.log_decision(action="privacy_scan", target_type="chunk", target_id=chunk.id, details=scan_result.to_dict())

        logger.info(f"  ✓ Layer 6 Complete: {stats.items_flagged_sensitive} sensitive flagged")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 7: ATOMIC SEGMENTATION
    # ═══════════════════════════════════════════════════════════════
    if 7 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 7: ATOMIC SEGMENTATION           │")
        logger.info("└─────────────────────────────────────────┘")

        safe_chunks = [c for c in all_deduped_chunks if c.id not in _sensitive_chunk_ids]
        for chunk in safe_chunks:
            units = _decompose_to_atomic_units(chunk, scanner)
            for unit in units:
                memory.save_working_unit(unit)
                stats.atomic_units_created += 1

        logger.info(f"  ✓ Layer 7 Complete: {stats.atomic_units_created} atomic units")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 8: CONFLICT RESOLUTION
    # ═══════════════════════════════════════════════════════════════
    if 8 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 8: CONFLICT RESOLUTION           │")
        logger.info("└─────────────────────────────────────────┘")

        working_units = memory.get_working_units()
        canonical_units = memory.get_canonical_units()

        for unit in working_units:
            conflict_found = False
            for existing in canonical_units:
                if existing.department == unit.department and existing.knowledge_type == unit.knowledge_type:
                    if (_text_similarity(existing.claim, unit.claim) > 0.7 and
                        _text_similarity(existing.instruction, unit.instruction) < 0.5):
                        unit.conflicts_with.append(existing.id)
                        unit.status = UnitStatus.CONTESTED
                        conflict_found = True
                        stats.conflicts_detected += 1
                        memory.log_decision(action="conflict_detected", target_type="unit", target_id=unit.id, details={"conflicts_with": existing.id})

            if not conflict_found:
                if unit.confidence_score >= 0.7:
                    memory.promote_to_canonical(unit)
                    stats.conflicts_resolved += 1
                else:
                    memory.save_working_unit(unit)
            else:
                memory.save_working_unit(unit)

        logger.info(f"  ✓ Layer 8 Complete: {stats.conflicts_detected} conflicts")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 9: SYNTHESIS
    # ═══════════════════════════════════════════════════════════════
    if 9 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 9: SYNTHESIS                     │")
        logger.info("└─────────────────────────────────────────┘")

        assembler = SkillAssembler()
        provisioner = ToolProvisioner()
        generator = SkillGenerator(base_output_dir=os.path.join(base_dir, "knowledge_base"))
        connector = AgentConnector(db_path=db_path)

        departments_with_data = set(c.department for c in safe_chunks)
        for dept in departments_with_data:
            approved_units = memory.get_canonical_units(department=dept)
            dept_chunks = []
            if approved_units:
                for u in approved_units:
                    dept_chunks.append(KnowledgeChunk(
                        id=u.id, department=u.department, knowledge_type=u.knowledge_type,
                        source_type=SourceType.LOCAL_FILE, source_identifier=f"atomic:{u.id}",
                        title=u.claim[:80], content=f"{u.claim}\n\n{u.instruction}",
                        summary=u.claim[:120], tags=list(getattr(u, "tags", [])),
                        metadata=KnowledgeMetadata(confidence_score=u.confidence_score),
                        processing_layer=ProcessingLayer.DISTILLED,
                        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
                    ))
            else:
                dept_chunks = [c for c in store.get_all_by_department(dept) if c.id not in _sensitive_chunk_ids]

            if not dept_chunks: continue

            skill_name = f"{dept.value}-operational-knowledge"
            assembled_skill = assembler.assemble_skill(skill_name, f"Knowledge for {dept.value}", dept_chunks, dept)

            provisioner = ToolProvisioner(router=router, cache=cache, db_path=db_path)
            provisioned_skill = provisioner.provision_skill(assembled_skill)

            generator.save_skill(provisioned_skill, dept)
            generator.save_context(provisioned_skill, dept)
            registry.register_skill(skill_name, [c.id for c in dept_chunks])

            target_agent = _determine_agent(provisioned_skill)
            connector.bind_skill_to_agent(target_agent, provisioned_skill.name)
            connector.trigger_agent_reload(target_agent)
            stats.skills_recompiled += 1

        logger.info(f"  ✓ Layer 9 Complete: {stats.skills_recompiled} skills compiled")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 10: MEMORY INDEXING
    # ═══════════════════════════════════════════════════════════════
    if 10 not in skip_layers:
        logger.info("\n┌─────────────────────────────────────────┐")
        logger.info("│  LAYER 10: MEMORY INDEXING              │")
        logger.info("└─────────────────────────────────────────┘")
        memory.run_expiry_sweep()
        final_stats = memory.get_stats()
        stats.units_indexed = final_stats["atomic_units_canonical"]
        memory.log_decision(action="pipeline_complete", target_type="run", target_id=run_id, details={"layers": 11})
        logger.info(f"  ✓ Layer 10 Complete: {stats.units_indexed} canonical units")

    # ═══════════════════════════════════════════════════════════════
    # LAYER 11: AGENT ORCHESTRATION (Handled via hot-reload in Layer 9)
    # ═══════════════════════════════════════════════════════════════

    logger.info("\n╔══════════════════════════════════════════════════════════════╗")
    logger.info(f"║  11-LAYER PIPELINE COMPLETE                                ║")
    logger.info(f"╚══════════════════════════════════════════════════════════════╝")

    return stats


def _determine_agent(skill) -> str:
    if skill.department == Department.ENGINEERING:
        return "eng-ops-agent"
    elif skill.department == Department.MARKETING:
        return "content-creator"
    elif skill.department == Department.SALES:
        return "sales-intel-agent"
    return "orchestrator"


def _get_source_privacy(source_type: SourceType) -> PrivacyMode:
    if source_type in (SourceType.LOCAL_FILE, SourceType.MARKDOWN, SourceType.JSON):
        return PrivacyMode.HARD_LOCAL
    return PrivacyMode.ONLINE_ALLOWED


def _decompose_to_atomic_units(chunk: KnowledgeChunk, scanner: PrivacyScanner) -> List[AtomicKnowledgeUnit]:
    units: List[AtomicKnowledgeUnit] = []
    content = chunk.content.strip()
    claims = _split_into_claims(content)
    for claim_text in claims:
        if len(claim_text.strip()) < 10: continue
        scan = scanner.scan(claim_text)
        _claim_key = f"{claim_text.strip().lower()}|{chunk.department.value}"
        _det_id = f"aku-{hashlib.sha256(_claim_key.encode()).hexdigest()[:12]}"
        unit = AtomicKnowledgeUnit(
            id=_det_id, claim=claim_text.strip(), instruction=_to_imperative(claim_text.strip()),
            knowledge_type=chunk.knowledge_type, department=chunk.department,
            source_type=chunk.source_type, source_identifier=chunk.source_identifier,
            source_excerpt_hash=hashlib.sha256(claim_text.encode()).hexdigest()[:16],
            source_reliability=chunk.metadata.source_reliability, confidence_score=chunk.metadata.confidence_score,
            sensitivity_level=scan.suggested_sensitivity,
            online_allowed=scan.suggested_sensitivity not in (SensitivityLevel.RESTRICTED, SensitivityLevel.CONFIDENTIAL),
            tags=list(chunk.tags),
        )
        units.append(unit)
    return units


def _split_into_claims(content: str) -> List[str]:
    claims = []
    lines = content.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('---'): continue
        for prefix in ["- ", "• ", "* ", "→ "]:
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        if len(line) >= 10: claims.append(line)
    if not claims and len(content) >= 10: claims = [content]
    return claims


def _to_imperative(text: str) -> str:
    text = text.strip()
    if text.lower().startswith(("always ", "never ", "do ", "ensure ", "use ", "avoid ")): return text
    return f"Ensure that {text.lower()}" if text else text


def _text_similarity(a: str, b: str) -> float:
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b: return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


@click.command()
@click.option('--sources', multiple=True, help='Data source paths.')
@click.option('--slack-channel', default="C1234_ENGINEERING", help='Slack channel ID.')
@click.option('--slack-name', default="#engineering-deployments", help='Slack channel name.')
@click.option('--skip', multiple=True, type=int, help='Layers to skip.')
@click.option('--verbose', is_flag=True, help='Enable verbose logging.')
def main(sources, slack_channel, slack_name, skip, verbose):
    if verbose:
        logger.setLevel(logging.DEBUG)

    data_sources = list(sources) if sources else None
    run_orchestration_loop(
        data_sources=data_sources,
        slack_channel=slack_channel,
        slack_channel_name=slack_name,
        skip_layers=list(skip)
    )


if __name__ == "__main__":
    main()
