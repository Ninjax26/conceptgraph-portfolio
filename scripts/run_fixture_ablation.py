"""Measure real retrieval on authored PDFs using isolated vectors and a temporary Neo4j course.

Uses local cached embedding/rerank models and the configured LLM/Neo4j connection.
Does not require PostgreSQL or change uploaded courses. Graph extraction is saved
for inspection/replay. Only this run's UUID-scoped graph is removed on completion.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_evaluation import (
    ABLATION_MODES, ablation_markdown_table, build_ablation_comparison,
    load_dataset, score_retrieval_result, summarize_retrieval,
)


async def run(args):
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer, CrossEncoder
    import torch
    from app.core.config import settings
    from app.core.database import neo4j_driver, close_database_connections
    from app.services.parser_service import ParserService
    from app.services.ingestion_service import IngestionService
    from app.services.rag_service import RetrievalService
    from app.services.rerank_service import RerankService
    from app.services.citation_service import assess_evidence, build_sources
    from app.schemas.extraction import GraphExtractionResponse, ConceptNode, ConceptRelationship

    torch.set_num_threads(2)
    settings.embedding_provider = "local"
    settings.rerank_provider = "local"
    settings.embedding_model_name = "sentence-transformers/all-MiniLM-L6-v2"
    settings.rerank_model_name = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    dataset = load_dataset(ROOT / "evaluation/questions-multidoc.json")
    course = json.loads((ROOT / "evaluation/course.json").read_text())
    course_id = f"ablation-{uuid4()}"
    vectors = QdrantClient(":memory:")
    ingestion = IngestionService(graph_driver=neo4j_driver, vector_client=vectors)
    retrieval = RetrievalService(graph_driver=neo4j_driver, vector_client=vectors)
    reranker = RerankService()
    uploads = []
    hashes = {}
    graph_records = {}
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path = ROOT / "evaluation/fixture-graphs.json"
    saved = json.loads(snapshot_path.read_text()) if args.reuse_graphs else {}
    results = {m: [] for m in ABLATION_MODES}
    try:
        try:
            await asyncio.wait_for(neo4j_driver.verify_connectivity(), timeout=15)
        except Exception as exc:
            # A blocked local network must produce a useful, auditable artifact
            # instead of looking like a missing benchmark run.
            blocked = {
                "status": "blocked",
                "dataset": dataset["name"],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "blocking_dependency": "neo4j",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "methodology": {
                    "scope": "Authored multi-document fixture; benchmark did not start",
                    "reason": "The isolated graph store could not be reached, so no retrieval score is reported.",
                    "next_step": "Run with network access and the configured NEO4J_URI, or use the committed reference report for the last successful run.",
                    "working_tree_changes": True,
                },
            }
            output.write_text(json.dumps(blocked, indent=2) + "\n")
            output.with_suffix(".md").write_text(
                "# Ablation blocked\n\n"
                f"Neo4j connectivity failed with `{type(exc).__name__}`. No retrieval scores were collected.\n"
                "The committed `evaluation/ablation-reference.md` remains the last successful isolated run.\n"
            )
            print(f"Ablation blocked by Neo4j: {type(exc).__name__}", flush=True)
            return 2
        print("Loading cached CPU models; warmup excluded from measured latency.", flush=True)
        model = SentenceTransformer(settings.embedding_model_name, device="cpu", local_files_only=True)
        reranker._model = CrossEncoder(settings.rerank_model_name, device="cpu", local_files_only=True)
        ingestion._embedding_model = retrieval._embedding_model = model
        model.encode(["Warmup"])
        reranker.model.predict([("Warmup", "Warmup passage")])
        for document in course["documents"]:
            filename = document["filename"]
            content = (ROOT / "public/sample" / filename).read_bytes()
            hashes[filename] = hashlib.sha256(content).hexdigest()
            upload = f"{course_id}-{len(uploads)}"
            uploads.append(upload)
            parser = ParserService()
            chunks = parser.chunk_pages(parser.extract_pages_from_bytes(content), course_id, upload, filename)
            ingestion.upsert_chunks_to_qdrant(chunks)
            print(f"Indexed {filename}: {len(chunks)} chunks", flush=True)
            if args.reference_graph:
                # An explicit controlled reference graph isolates retrieval depth
                # from extraction quality. Never describe this as an LLM graph.
                extraction = GraphExtractionResponse(
                    nodes=[ConceptNode(id=f"p{i+1}", name=page["title"], type="CONCEPT",
                        description=page["text"], document_name=filename, page_number=i+1,
                        section_heading=page["title"], upload_id=upload,
                        source_chunk_id=chunks[i].id) for i, page in enumerate(document["pages"])],
                    relationships=[ConceptRelationship(source_node_id=f"p{i}", target_node_id=f"p{i+1}",
                        relation_type="PREREQUISITE_OF") for i in (1, 2)],
                )
            elif args.reuse_graphs:
                record = saved[filename]
                if record["pdf_sha256"] != hashes[filename]:
                    raise ValueError("Graph snapshot does not match PDF bytes; regenerate the snapshot.")
                extraction = GraphExtractionResponse.model_validate(record["extraction"])
                # Rebind only fixture provenance to this isolated run.
                for node in extraction.nodes:
                    old_upload = node.upload_id
                    node.upload_id = upload
                    if old_upload:
                        node.source_chunk_id = node.source_chunk_id.replace(old_upload, upload)
                        node.source_chunk_ids = [s.replace(old_upload, upload) for s in node.source_chunk_ids]
            else:
                extraction = await ingestion.extract_graph_from_chunks(chunks)
            graph_records[filename] = {"pdf_sha256": hashes[filename], "extraction": extraction.model_dump()}
            await ingestion.store_graph_extraction(extraction, course_id, upload_id=upload, document_name=filename, course_name="Temporary ablation fixture")
            print(f"Graph: {len(extraction.nodes)} nodes, {len(extraction.relationships)} edges", flush=True)
        if not args.reuse_graphs and not args.reference_graph:
            snapshot_path.write_text(json.dumps(graph_records, indent=2) + "\n")
        context = SimpleNamespace(document_ids=uploads, graph_course_ids=[course_id], graph_status="GRAPH_PARTIAL")
        # Corpus is immutable during measurement. Scope is explicitly these fixture uploads.
        for repeat in range(args.repeats):
            for index, case in enumerate(dataset["questions"]):
                offset = (repeat + index) % len(ABLATION_MODES)
                order = (*ABLATION_MODES[offset:], *ABLATION_MODES[:offset])
                for mode in order:
                    started = time.perf_counter()
                    candidates, ranked, sources, confidence, metadata = [], [], [], {}, {}
                    error = None
                    try:
                        found = await retrieval.retrieve(case["question"], context, retrieval_mode=mode)
                        metadata = found["graph_metadata"]
                        candidates = found["chunks"]
                        if not candidates:
                            raise RuntimeError("Fixture has no indexed candidates")
                        ranked = await reranker.rerank(case["question"], candidates)
                        evidence, confidence = assess_evidence(ranked)
                        sources = build_sources(evidence)
                    except Exception as exc:
                        error = type(exc).__name__
                    scored = score_retrieval_result(case, sources=sources, confidence=confidence,
                        graph_metadata=metadata, latency_seconds=time.perf_counter()-started,
                        error=error, ranked_chunks=ranked, candidates=candidates)
                    scored.update({"repeat": repeat+1, "id": case["id"], "requested_mode": mode})
                    results[mode].append(scored)
                    print(f"repeat {repeat+1} {case['id']} {mode}: {scored['latency_seconds']}s {error or 'ok'}", flush=True)
        runs = {mode: {"summary": summarize_retrieval(items), "results": items,
            "by_category": {category: summarize_retrieval([r for r in items if r['category']==category])
                for category in sorted({r['category'] for r in items})}} for mode, items in results.items()}
        report = {
            "dataset": dataset["name"], "generated_at": datetime.now(timezone.utc).isoformat(),
            "methodology": {
                "scope": "Authored synthetic fixture; actual PDF parser, MiniLM, local Qdrant, remote Neo4j, cross-encoder and evidence gate",
                "graph_source": ("Authored reference graph: first three lessons in each PDF form an explicitly documented prerequisite chain. Controls extraction quality; does not measure LLM extraction." if args.reference_graph else "Configured LLM extraction through IngestionService; inspect fixture-graphs.json"),
                "graph_snapshot_reused": args.reuse_graphs, "repeats": args.repeats,
                "latency": "Warm models, rotated mode order; includes Neo4j network round trips; excludes PDF indexing, extraction and synthesis",
                "embedding": settings.embedding_model_name, "reranker": settings.rerank_model_name,
                "evidence_min_score": settings.evidence_min_score, "candidate_count": 10, "scored_top_k": 5,
                "platform": platform.platform(), "python": platform.python_version(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "working_tree_changes": True,
                "corpus_sha256": hashes,
                "dataset_sha256": hashlib.sha256((ROOT / "evaluation/questions-multidoc.json").read_bytes()).hexdigest(),
                "limitations": ["Small synthetic corpus", "Labels checked against authored text, not independent human annotation", "No generated-answer quality or citation correctness score", "No PostgreSQL/HTTP latency measured", "No automatic claim that graph retrieval is better"],
            }, "runs": runs, "comparison_to_vector_only": build_ablation_comparison(runs),
        }
        output.write_text(json.dumps(report, indent=2) + "\n")
        output.with_suffix(".md").write_text(ablation_markdown_table(runs)+"\n")
        print(ablation_markdown_table(runs))
        return 1 if any(r["summary"]["request_errors"] or r["summary"]["graph_fallbacks"] for r in runs.values()) else 0
    finally:
        for upload in uploads:
            await ingestion.cleanup_upload(upload, course_id)
        vectors.close()
        await close_database_connections()
        print(f"Removed temporary fixture scope {course_id}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    graph_source = parser.add_mutually_exclusive_group()
    graph_source.add_argument("--reuse-graphs", action="store_true")
    graph_source.add_argument("--reference-graph", action="store_true", help="Use explicitly authored reference graphs to isolate retrieval quality from extraction quality.")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation/ablation-multidoc.json")
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(run(arguments)))
