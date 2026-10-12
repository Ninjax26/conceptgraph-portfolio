"""Local retrieval scale check with actual PDF parsing, embeddings, Qdrant and reranking.

No LLM generation, remote stores or production uploads. Qdrant local mode uses
brute-force search, so measured latency does not describe hosted ANN performance.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
import sys
import time
from types import SimpleNamespace, ModuleType
import warnings

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_evaluation import load_dataset, score_retrieval_result, summarize_retrieval


def equipment_pdf(record):
    identifier = record["id"]
    page_texts = [
        ("OPERATING SPECIFICATION",
         f"Equipment identifier {identifier}. The approved maximum operating pressure is {record['pressure']} bar. "
         "Record pressure in bar. Stop operation if the measured pressure exceeds this limit. "
         "Use the specification for this exact equipment identifier; limits for other equipment do not apply."),
        ("INSPECTION PROCEDURE",
         f"Equipment identifier {identifier}. The required inspection interval is {record['interval']} days. "
         "Inspect the pump housing, seals and connections. Record the date, observations and equipment identifier. "
         "Use the inspection interval for this exact equipment; another identifier can have a different interval."),
    ]
    document = fitz.open()
    for heading, text in page_texts:
        page = document.new_page()
        page.insert_text((48, 60), heading, fontsize=16)
        if page.insert_textbox(fitz.Rect(48, 95, 550, 750), text, fontsize=12) < 0:
            raise ValueError("Fixture text did not fit in its PDF page.")
    # Suppress random PDF trailer IDs so baseline/current corpus hashes match.
    content = document.tobytes(no_new_id=True)
    document.close()
    return f"equipment-{identifier}.pdf", content


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else 0


def diagnose_source_losses(expected, candidates, ranked, accepted, sources):
    """Locate actual pipeline loss, separately from the top-five label metric."""
    def key(source):
        return source.get("document_name"), source.get("page_number")

    final = {key(source) for source in sources}
    stages = [(stage, {key(chunk.get("metadata") or {}) for chunk in chunks})
              for stage, chunks in (("search", candidates), ("reranking", ranked),
                                    ("evidence_gate", accepted))]
    return [dict(source, stage=next((stage for stage, present in stages
                                    if key(source) not in present), "citation_selection"))
            for source in expected if key(source) not in final]


async def run(args):
    import torch
    from sentence_transformers import SentenceTransformer, CrossEncoder
    from qdrant_client import QdrantClient
    from app.core.config import settings
    from app.core.database import close_database_connections
    from app.services.parser_service import ParserService
    from app.services.ingestion_service import IngestionService
    from app.services.rag_service import RetrievalService
    from app.services.rerank_service import RerankService
    from app.services.citation_service import assess_evidence, prepare_evidence
    from app.services.retrieval_candidates import identifier_terms, passage_excerpt
    from app.services.evidence_facets import comparison_facets

    comparison_sources = {}
    if args.baseline_revision:
        if not re.fullmatch(r"[0-9a-f]{7,40}", args.baseline_revision):
            raise ValueError("The baseline must be a local hexadecimal Git commit ID.")
        # Load only the two trusted, versioned local modules under comparison.
        # No checkout, environment changes, model downloads or remote Git access.
        modules = {}
        for name in ("rerank_service", "citation_service"):
            source = subprocess.check_output([
                "git", "show", f"{args.baseline_revision}:app/services/{name}.py"
            ], cwd=ROOT, text=True)
            comparison_sources[f"app/services/{name}.py"] = hashlib.sha256(source.encode()).hexdigest()
            module = ModuleType(f"baseline_{name}")
            exec(compile(source, f"baseline:{name}", "exec"), module.__dict__)
            modules[name] = module
        RerankService = modules["rerank_service"].RerankService
        prepare_evidence = modules["citation_service"].prepare_evidence
        assess_evidence = modules["citation_service"].assess_evidence

    torch.set_num_threads(2)
    settings.embedding_provider, settings.rerank_provider = "local", "local"
    settings.embedding_model_name = "sentence-transformers/all-MiniLM-L6-v2"
    settings.rerank_model_name = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    settings.embedding_dimension = 384
    comparison_sources = comparison_sources or {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("app/services/citation_service.py", "app/services/rerank_service.py")}
    definitions = json.loads((ROOT / "evaluation/questions-scaling.json").read_text())
    dataset_files = {name: ROOT / "evaluation" / name
                     for name in ("questions-scaling.json", definitions["development_questions"], definitions["held_out_questions"])}
    if args.questions:
        challenge_path = ROOT / args.questions
        challenge = load_dataset(challenge_path)
        questions = challenge["questions"]
        if any(case.get("split") not in ("development", "validation", "held_out") for case in questions):
            raise ValueError("Challenge questions need an explicit development, validation or held_out split.")
        dataset_name = challenge["name"]
        dataset_files = {"questions-scaling.json": ROOT / "evaluation/questions-scaling.json",
                         args.questions: challenge_path}
    else:
        questions = [dict(case, split="development") for case in load_dataset(ROOT / "evaluation" / definitions["development_questions"])["questions"]]
        questions += [dict(case, split="validation") for case in definitions["questions"]]
        questions += [dict(case, split="held_out") for case in load_dataset(ROOT / "evaluation" / definitions["held_out_questions"])["questions"]]
        dataset_name = definitions["name"]
    if args.splits:
        questions = [case for case in questions if case["split"] in args.splits]
    base = [(document["filename"], (ROOT / "public/sample" / document["filename"]).read_bytes())
            for document in json.loads((ROOT / "evaluation/course.json").read_text())["documents"]]
    base += [equipment_pdf(record) for record in definitions["documents"]]
    if min(args.documents) < len(base):
        raise ValueError(f"Each corpus must contain at least {len(base)} PDFs.")
    print("Loading cached models; no downloads or provider requests.", flush=True)
    embedding = SentenceTransformer(settings.embedding_model_name, device="cpu", local_files_only=True)
    reranker = RerankService()
    reranker._model = CrossEncoder(settings.rerank_model_name, device="cpu", local_files_only=True)
    embedding.encode(["Warmup"])
    reranker.model.predict([("Warmup", "Warmup passage")])
    profiles = [("dense_10", 10, False, False), ("dense_20", 20, False, False),
                ("identifier_20", 20, True, False), ("identifier_10_verified", 10, True, True)]
    if args.profiles:
        profiles = [profile for profile in profiles if profile[0] in args.profiles]
    corpora = []
    warnings.filterwarnings("ignore", message="Payload indexes have no effect.*")
    try:
        for count in args.documents:
            documents = list(base)
            for index in range(count - len(base)):
                documents.append(equipment_pdf({"id": f"PMP-{200 + index}", "pressure": str(8 + index % 20), "interval": 7 + index % 120}))
            vectors = QdrantClient(":memory:")
            ingestion = IngestionService(SimpleNamespace(), vectors)
            retrieval = RetrievalService(SimpleNamespace(), vectors)
            ingestion._embedding_model = retrieval._embedding_model = embedding
            uploads, chunks, hashes = [], [], {}
            index_started = time.perf_counter()
            try:
                for index, (filename, content) in enumerate(documents):
                    upload = f"scale-{count}-{index}"
                    uploads.append(upload)
                    parsed = ParserService().extract_pages_from_bytes(content)
                    chunks += ParserService().chunk_pages(parsed, "scale-fixture", upload, filename)
                    hashes[filename] = hashlib.sha256(content).hexdigest()
                ingestion.upsert_chunks_to_qdrant(chunks)
                indexed_seconds = time.perf_counter() - index_started
                context = SimpleNamespace(document_ids=uploads, graph_status="READY_WITHOUT_GRAPH")
                runs = {name: [] for name, _, _, _ in profiles}
                print(f"Indexed {count} PDFs / {len(chunks)} chunks in {indexed_seconds:.2f}s", flush=True)
                for repeat in range(args.repeats):
                    for index, case in enumerate(questions):
                        offset = (repeat + index) % len(profiles)
                        for name, limit, identifier_lookup, verify_identifiers in profiles[offset:] + profiles[:offset]:
                            settings.retrieval_identifier_lookup = identifier_lookup
                            settings.retrieval_verify_identifiers = verify_identifiers
                            timings = {}
                            candidates, ranked, sources, confidence, metadata = [], [], [], {}, {}
                            error = None
                            started = time.perf_counter()
                            try:
                                found = await retrieval.retrieve(case["question"], context, top_k=limit, retrieval_mode="vector_only")
                                candidates, metadata = found["chunks"], found["graph_metadata"]
                                timings.update(metadata["timings"])
                                rerank_started = time.perf_counter()
                                ranked = await reranker.rerank(case["question"], candidates)
                                timings["rerank_ms"] = (time.perf_counter() - rerank_started) * 1000
                                evidence_started = time.perf_counter()
                                sources, confidence = prepare_evidence(case["question"], ranked)
                                timings["evidence_ms"] = (time.perf_counter() - evidence_started) * 1000
                            except Exception as exc:
                                error = type(exc).__name__
                            row = score_retrieval_result(case, sources=sources, confidence=confidence, graph_metadata=metadata,
                                candidates=candidates, ranked_chunks=ranked, latency_seconds=time.perf_counter() - started,
                                stage_timings=timings, error=error)
                            row.update({"id": case["id"], "split": case["split"], "repeat": repeat + 1})
                            parts = comparison_facets(case["question"]) if not args.baseline_revision else []
                            row["rerank_window_count"] = (len(candidates) if args.baseline_revision else
                                sum(len(reranker._windows(part, candidates)) for part in (parts or [case["question"]])))
                            row["rerank_query_count"] = len(parts) or 1
                            # Local fixture diagnostics only; no prompts/documents are logged by the API.
                            required = identifier_terms(case["question"], limit=9, named_only=True) if verify_identifiers else []
                            accepted = []
                            if parts:
                                for index, part in enumerate(parts):
                                    group = [chunk for chunk in ranked if chunk.get("evidence_facet") == index]
                                    accepted += assess_evidence(group, required_identifiers=(
                                        identifier_terms(part, limit=8, named_only=True) if verify_identifiers else []))[0]
                            else:
                                accepted = assess_evidence(ranked, required_identifiers=required)[0]
                            accepted_keys = {(chunk["id"], chunk.get("evidence_facet"), chunk.get("rerank_passage")) for chunk in accepted}
                            row["evidence_selection_reason"] = confidence.get("reason")
                            row["source_loss_diagnostics"] = diagnose_source_losses(
                                row["expected_sources"], candidates, ranked, accepted, sources)
                            row["citation_count"] = len(sources)
                            row["citations_matching_scored_text"] = sum(any(
                                source["document_id"] == chunk["metadata"].get("upload_id")
                                and source["page_number"] == chunk["metadata"].get("page_number")
                                and source["supporting_passage"] == chunk.get("rerank_passage", chunk["text"])
                                for chunk in ranked) for source in sources)
                            row["evidence_diagnostics"] = [{
                                "document_name": chunk["metadata"].get("document_name"),
                                "page_number": chunk["metadata"].get("page_number"),
                                "vector_score": chunk.get("score"),
                                "rerank_score": chunk.get("rerank_score"),
                                "passed_gate": (chunk["id"], chunk.get("evidence_facet"), chunk.get("rerank_passage")) in accepted_keys,
                                "evidence_facet": chunk.get("evidence_facet"),
                                "excerpt": passage_excerpt(str(chunk.get("rerank_passage") or chunk["text"]), case["question"])
                                    if not row["should_refuse"] and not row["accepted_evidence"]["all_sources_hit"] else None,
                            } for chunk in ranked]
                            fragments = case.get("expected_passage_fragments")
                            joined = " ".join(source["supporting_passage"] for source in sources).casefold()
                            row["expected_fragments_present"] = all(fragment.casefold() in joined for fragment in fragments) if fragments else None
                            runs[name].append(row)
                profiles_report = {}
                for name, rows in runs.items():
                    summary = summarize_retrieval(rows)
                    summary["p50_retrieval_seconds"] = round(percentile([row["latency_seconds"] for row in rows], .5), 3)
                    profiles_report[name] = {"summary": summary,
                        "by_split": {split: summarize_retrieval([row for row in rows if row["split"] == split])
                                     for split in ("development", "validation", "held_out")}, "results": rows}
                    print(f"{count} PDFs {name}: accepted sources {summary['stage_metrics']['accepted_evidence']['all_required_sources']}/{summary['supported_question_count']}, refusals {summary['unsupported_questions_refused']}/{summary['unsupported_question_count']}", flush=True)
                corpora.append({"documents": count, "chunks": len(chunks), "index_seconds": round(indexed_seconds, 3),
                                "vector_bytes_estimate": len(chunks) * 384 * 4, "corpus_sha256": hashes, "profiles": profiles_report})
            finally:
                vectors.close()
    finally:
        await close_database_connections()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "dataset": dataset_name,
              "repeats": args.repeats, "questions": len(questions), "corpora": corpora,
              "methodology": {"embedding": settings.embedding_model_name, "reranker": settings.rerank_model_name,
                "baseline_revision": args.baseline_revision,
                "splits": args.splits or ["development", "validation", "held_out"],
                "comparison_code_sha256": comparison_sources,
                "retrieval_candidates_sha256": hashlib.sha256((ROOT / "app/services/retrieval_candidates.py").read_bytes()).hexdigest(),
                "evidence_facets_sha256": hashlib.sha256((ROOT / "app/services/evidence_facets.py").read_bytes()).hexdigest(),
                "evidence_min_score": settings.evidence_min_score,
                "profiles": {name: {"candidate_limit": count, "identifier_lookup": lookup, "identifier_verification": verify}
                             for name, count, lookup, verify in profiles},
                "dataset_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for name, path in dataset_files.items()},
                "scope": "Authored PDFs with synthetic distractors; warm local models and in-memory Qdrant brute-force search.",
                "limitations": ["No hosted ANN, HTTP, concurrency, PostgreSQL, or LLM generation benchmark.",
                                "Page-label precision is not human-verified passage relevance or generated-answer accuracy.",
                                "Source fragments are checked by exact text presence, not semantic correctness.",
                                "Validation cases were used to diagnose identifier failures; fresh held-out authored cases are separate and not independently annotated.",
                                "One warm sequential run per case; not a concurrency, uptime, or production-scale guarantee."]}}
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Retrieval scaling check", "", report["methodology"]["scope"], "",
             "| PDFs | Chunks | Profile | Reranked all sources @5 | Accepted all sources @5 | Refusals | Reranked MRR@5 | p50 seconds | p95 seconds |",
             "| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for corpus in corpora:
        for name, profile in corpus["profiles"].items():
            s = profile["summary"]
            rank, accepted = s["stage_metrics"]["reranked_retrieval"], s["stage_metrics"]["accepted_evidence"]
            lines.append(f"| {corpus['documents']} | {corpus['chunks']} | {name} | {rank['all_required_sources']}/{s['supported_question_count']} | {accepted['all_required_sources']}/{s['supported_question_count']} | {s['unsupported_questions_refused']}/{s['unsupported_question_count']} | {rank['mrr_at_5']} | {s['p50_retrieval_seconds']} | {s['p95_retrieval_seconds']} |")
    lines += ["", "## Separate dataset splits", "",
              "| PDFs | Profile | Split | Accepted all required sources | Unsupported refusals |",
              "| ---: | --- | --- | ---: | ---: |"]
    for corpus in corpora:
        for name, profile in corpus["profiles"].items():
            for split, summary in profile["by_split"].items():
                if summary["supported_question_count"] or summary["unsupported_question_count"]:
                    lines.append(f"| {corpus['documents']} | {name} | {split} | {summary['stage_metrics']['accepted_evidence']['all_required_sources']}/{summary['supported_question_count']} | {summary['unsupported_questions_refused']}/{summary['unsupported_question_count']} |")
    lines += ["", "## Limits", ""] + [f"- {limit}" for limit in report["methodology"]["limitations"]]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, nargs="+", default=[10, 50, 100])
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--splits", nargs="+", choices=("development", "validation", "held_out"))
    parser.add_argument("--profiles", nargs="+", choices=("dense_10", "dense_20", "identifier_20", "identifier_10_verified"))
    parser.add_argument("--baseline-revision", help="Compare trusted local rerank/evidence code from a Git commit")
    parser.add_argument("--questions", help="Use a separate local question JSON with explicit dataset splits")
    parser.add_argument("--output", default="evaluation/retrieval-scaling-current")
    asyncio.run(run(parser.parse_args()))
