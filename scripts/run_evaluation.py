#!/usr/bin/env python3
"""Run the small, human-annotated ConceptGraph portfolio evaluation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REFUSAL_TEXT = "I could not find enough reliable course content to answer this confidently."
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTIONS = PROJECT_ROOT / "evaluation" / "questions.json"
ABLATION_MODES = ("vector_only", "one_hop", "two_hop")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _read_local_secret(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            return candidate.strip().strip('"').strip("'") or None
    return None


def load_dataset(path: Path) -> dict[str, Any]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    questions = dataset.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Evaluation JSON must contain a non-empty questions list.")
    for index, case in enumerate(questions, start=1):
        if not isinstance(case, dict) or not str(case.get("question", "")).strip():
            raise ValueError(f"Evaluation question {index} is missing question text.")
        should_refuse = bool(case.get("should_refuse"))
        if not should_refuse and not case.get("expected_sources") and (
            not case.get("expected_document") or not isinstance(case.get("expected_page"), int)
        ):
            raise ValueError(
                f"Supported evaluation question {index} needs expected_document and expected_page."
            )
    return dataset


def query_api(
    *,
    api_base: str,
    course_id: str,
    question: str,
    access_token: str | None,
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, float, str | None]:
    body = json.dumps({"question": question, "course_id": course_id}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = Request(
        f"{api_base.rstrip('/')}/query",
        data=body,
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload, time.perf_counter() - started, None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return None, time.perf_counter() - started, f"HTTP {exc.code}: {detail}"
    except (URLError, TimeoutError) as exc:
        return None, time.perf_counter() - started, str(exc)


async def run_direct_evaluation(
    *,
    course_id: str,
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate the configured stores/providers without bypassing deployed HTTP auth."""

    from types import SimpleNamespace

    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal, close_database_connections
    from app.services.citation_service import assess_evidence, build_sources
    from app.services.rag_service import RetrievalService
    from app.services.rerank_service import RerankService
    from app.services.synthesis_service import SynthesisService

    async with AsyncSessionLocal() as session:
        records = (
            await session.execute(
                text(
                    "SELECT course_uuid, course_id, upload_id "
                    "FROM document_uploads "
                    "WHERE stage = :stage AND lower(course_id) = lower(:course_id)"
                ),
                {"stage": "READY", "course_id": course_id},
            )
        ).mappings().all()
    if not records:
        await close_database_connections()
        raise RuntimeError(f"No READY documents were found for course {course_id!r}.")

    document_ids = [str(record["upload_id"]) for record in records]
    graph_course_ids = sorted(
        {
            str(value)
            for record in records
            for value in (record["course_uuid"], record["course_id"])
            if value
        }
    )
    context = SimpleNamespace(
        document_ids=document_ids,
        graph_course_ids=graph_course_ids,
        graph_status="GRAPH_READY",
    )
    retrieval_service = RetrievalService()
    rerank_service = RerankService()
    synthesis_service = SynthesisService()
    results: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(questions, start=1):
            started = time.perf_counter()
            response: dict[str, Any] | None = None
            error: str | None = None
            try:
                retrieval = await retrieval_service.retrieve(
                    question=case["question"],
                    context=context,
                )
                ranked = await rerank_service.rerank(
                    case["question"],
                    retrieval["chunks"],
                )
                evidence, confidence = assess_evidence(ranked)
                sources = build_sources(evidence)
                answer = REFUSAL_TEXT
                response = {
                    "answer": answer,
                    "sources": sources,
                    "confidence": confidence,
                }
                if sources:
                    try:
                        answer = await synthesis_service.synthesize(
                            question=case["question"],
                            graph_context=retrieval["graph_context"],
                            sources=sources,
                        )
                        response["answer"] = answer
                    except Exception as exc:
                        error = f"{type(exc).__name__}: answer provider request failed"
            except Exception as exc:
                error = f"{type(exc).__name__}: retrieval pipeline request failed"
            latency = time.perf_counter() - started
            results.append(score_response(case, response, latency, error))
            status = "error" if error else "done"
            print(f"[{index:02d}/{len(questions):02d}] {status} {latency:.2f}s")
    finally:
        await close_database_connections()
    return results


async def run_direct_ablation(
    *,
    course_id: str,
    questions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compare retrieval modes without paying for or varying answer synthesis."""

    from types import SimpleNamespace

    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal, close_database_connections
    from app.services.citation_service import assess_evidence, build_sources
    from app.services.rag_service import RetrievalMode, RetrievalService
    from app.services.rerank_service import RerankService

    async with AsyncSessionLocal() as session:
        records = (
            await session.execute(
                text(
                    "SELECT course_uuid, course_id, upload_id "
                    "FROM document_uploads "
                    "WHERE stage = :stage AND lower(course_id) = lower(:course_id)"
                ),
                {"stage": "READY", "course_id": course_id},
            )
        ).mappings().all()
    if not records:
        await close_database_connections()
        raise RuntimeError(f"No READY documents were found for course {course_id!r}.")

    context = SimpleNamespace(
        document_ids=[str(record["upload_id"]) for record in records],
        graph_course_ids=sorted(
            {
                str(value)
                for record in records
                for value in (record["course_uuid"], record["course_id"])
                if value
            }
        ),
        graph_status="GRAPH_READY",
    )
    retrieval_service = RetrievalService()
    rerank_service = RerankService()
    mode_results: dict[str, list[dict[str, Any]]] = {
        mode: [] for mode in ABLATION_MODES
    }
    try:
        for index, case in enumerate(questions, start=1):
            # Rotate the first mode per question so one variant does not receive
            # all cold-cache requests or all warm-cache requests.
            offset = (index - 1) % len(ABLATION_MODES)
            mode_order = (*ABLATION_MODES[offset:], *ABLATION_MODES[:offset])
            print(f"\n[question {index:02d}/{len(questions):02d}]")
            for mode_name in mode_order:
                mode = RetrievalMode(mode_name)
                started = time.perf_counter()
                sources: list[dict[str, Any]] = []
                confidence: dict[str, Any] = {}
                graph_metadata: dict[str, Any] = {}
                ranked = []
                candidates = []
                error: str | None = None
                try:
                    retrieval = await retrieval_service.retrieve(
                        question=case["question"],
                        context=context,
                        retrieval_mode=mode,
                    )
                    ranked = await rerank_service.rerank(
                        case["question"],
                        retrieval["chunks"],
                    )
                    candidates = retrieval["chunks"]
                    evidence, confidence = assess_evidence(ranked)
                    sources = build_sources(evidence)
                    graph_metadata = retrieval["graph_metadata"]
                except Exception as exc:
                    error = f"{type(exc).__name__}: retrieval pipeline request failed"
                latency = time.perf_counter() - started
                mode_results[mode.value].append(
                    score_retrieval_result(
                        case,
                        sources=sources,
                        confidence=confidence,
                        graph_metadata=graph_metadata,
                        latency_seconds=latency,
                        error=error,
                        ranked_chunks=ranked,
                        candidates=candidates,
                    )
                )
                status = "error" if error else "done"
                print(
                    f"  {mode.value:<11} {status} {latency:.2f}s"
                )
    finally:
        await close_database_connections()
    return {
        mode: {
            "summary": summarize_retrieval(mode_results[mode]),
            "results": mode_results[mode],
        }
        for mode in ABLATION_MODES
    }


def score_response(
    case: dict[str, Any],
    response: dict[str, Any] | None,
    latency_seconds: float,
    error: str | None = None,
) -> dict[str, Any]:
    sources = list((response or {}).get("sources") or [])[:5]
    answer = str((response or {}).get("answer") or "")
    confidence = (response or {}).get("confidence") or {}
    expected_document = case.get("expected_document")
    expected_page = case.get("expected_page")

    matching_sources = [
        source
        for source in sources
        if expected_document
        and str(source.get("document_name", "")).casefold()
        == str(expected_document).casefold()
    ]
    document_correct = bool(matching_sources) if expected_document else None
    page_correct = (
        any(source.get("page_number") == expected_page for source in matching_sources)
        if expected_page is not None
        else None
    )
    citation_present = bool(sources) and bool(
        re.search(r"\[Source\s+\d+", answer, flags=re.IGNORECASE)
    )
    refused = (
        not sources
        and confidence.get("level") == "insufficient"
        and REFUSAL_TEXT.casefold() in answer.casefold()
    )

    return {
        "question": case["question"],
        "should_refuse": bool(case.get("should_refuse")),
        "document_correct": document_correct,
        "page_correct": page_correct,
        "citation_present": citation_present,
        "refused": refused,
        "latency_seconds": round(latency_seconds, 3),
        "top_sources": [
            {
                "document_name": source.get("document_name"),
                "page_number": source.get("page_number"),
            }
            for source in sources
        ],
        "error": error,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    supported = [result for result in results if not result["should_refuse"]]
    unsupported = [result for result in results if result["should_refuse"]]
    latencies = [float(result["latency_seconds"]) for result in results]
    return {
        "correct_document_top_5": sum(result["document_correct"] is True for result in supported),
        "document_question_count": len(supported),
        "correct_source_page_top_5": sum(result["page_correct"] is True for result in supported),
        "page_question_count": len(supported),
        "answers_with_citations": sum(result["citation_present"] is True for result in supported),
        "supported_question_count": len(supported),
        "unsupported_questions_refused": sum(result["refused"] is True for result in unsupported),
        "unsupported_question_count": len(unsupported),
        "average_query_seconds": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "request_errors": sum(bool(result["error"]) for result in results),
    }


def score_retrieval_result(
    case: dict[str, Any],
    *,
    sources: list[dict[str, Any]],
    confidence: dict[str, Any],
    graph_metadata: dict[str, Any],
    latency_seconds: float,
    error: str | None = None,
    ranked_chunks: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score retrieval only, keeping generation quality outside the ablation."""

    top_sources = list(sources)[:5]
    expected_document = case.get("expected_document")
    expected_page = case.get("expected_page")
    matching_sources = [
        source
        for source in top_sources
        if expected_document
        and str(source.get("document_name", "")).casefold()
        == str(expected_document).casefold()
    ]
    graph_expansion = graph_metadata.get("graph_expansion") or {}
    should_refuse = bool(case.get("should_refuse"))
    expected = case.get("expected_sources") or ([{
        "document_name": expected_document, "page_number": expected_page,
    }] if expected_document else [])
    def pairs(items):
        return {(str(item.get("document_name", "")).casefold(), item.get("page_number")) for item in items}
    expected_pairs = pairs(expected)
    def rank_sources(chunks):
        return [{"document_name": (chunk.get("metadata") or {}).get("document_name"),
                 "page_number": (chunk.get("metadata") or {}).get("page_number")} for chunk in chunks[:5]]
    ranked_sources = rank_sources(ranked_chunks or [])
    raw_sources = rank_sources(candidates or [])
    required_docs = {doc for doc, _ in expected_pairs}
    def hits(items):
        found = pairs(items)
        return {
            "document_hit": bool(required_docs & {doc for doc, _ in found}),
            "page_hit": bool(expected_pairs & found),
            "all_sources_hit": bool(expected_pairs) and expected_pairs <= found,
            "source_recall": len(expected_pairs & found) / len(expected_pairs) if expected_pairs else None,
        }
    return {
        "question": case["question"],
        "category": case.get("category", "unlabelled"),
        "expected_sources": expected,
        "raw_retrieval": hits(raw_sources),
        "reranked_retrieval": hits(ranked_sources),
        "accepted_evidence": hits(top_sources),
        "raw_top_5": raw_sources,
        "reranked_top_5": ranked_sources,
        "fallback_reason": graph_metadata.get("fallback_reason"),
        "retrieval_mode": graph_metadata.get("retrieval_mode"),
        "should_refuse": should_refuse,
        "document_correct": bool(matching_sources) if expected_document else None,
        "page_correct": (
            any(source.get("page_number") == expected_page for source in matching_sources)
            if expected_page is not None
            else None
        ),
        "evidence_found": bool(top_sources),
        "refused": (
            not error and not top_sources and confidence.get("level") == "insufficient"
        ),
        "latency_seconds": round(latency_seconds, 3),
        "graph_anchor_match_found": bool(
            graph_expansion.get("anchor_match_found")
        ),
        "one_hop_terms": int(graph_expansion.get("one_hop_count") or 0),
        "two_hop_terms": int(graph_expansion.get("two_hop_count") or 0),
        "top_sources": [
            {
                "document_name": source.get("document_name"),
                "page_number": source.get("page_number"),
            }
            for source in top_sources
        ],
        "error": error,
    }


def summarize_retrieval(results: list[dict[str, Any]]) -> dict[str, Any]:
    supported = [result for result in results if not result["should_refuse"]]
    unsupported = [result for result in results if result["should_refuse"]]
    latencies = sorted(float(result["latency_seconds"]) for result in results)
    p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1) if latencies else 0
    return {
        "stage_metrics": {
            stage: {
                "document_hits": sum(bool(r.get(stage, {}).get("document_hit")) and not r["error"] for r in supported),
                "page_hits": sum(bool(r.get(stage, {}).get("page_hit")) and not r["error"] for r in supported),
                "all_required_sources": sum(bool(r.get(stage, {}).get("all_sources_hit")) and not r["error"] for r in supported),
                "mean_source_recall": round(statistics.fmean(r.get(stage, {}).get("source_recall") or 0 for r in supported), 3) if supported else 0,
            } for stage in ("raw_retrieval", "reranked_retrieval", "accepted_evidence")
        },
        "graph_fallbacks": sum(bool(r.get("fallback_reason")) for r in results),
        "queries_with_one_hop_terms": sum(bool(r.get("one_hop_terms")) for r in results),
        "queries_with_two_hop_terms": sum(bool(r.get("two_hop_terms")) for r in results),
        "correct_document_top_5": sum(
            result["document_correct"] is True for result in supported
        ),
        "document_question_count": len(supported),
        "correct_source_page_top_5": sum(
            result["page_correct"] is True for result in supported
        ),
        "page_question_count": len(supported),
        "supported_questions_with_evidence": sum(
            result["evidence_found"] is True for result in supported
        ),
        "supported_question_count": len(supported),
        "unsupported_questions_refused": sum(
            result["refused"] is True for result in unsupported
        ),
        "unsupported_question_count": len(unsupported),
        "graph_anchor_matches": sum(
            result["graph_anchor_match_found"] is True for result in results
        ),
        "average_retrieval_seconds": (
            round(statistics.fmean(latencies), 3) if latencies else 0.0
        ),
        "p95_retrieval_seconds": round(latencies[p95_index], 3) if latencies else 0.0,
        "request_errors": sum(bool(result["error"]) for result in results),
    }


def build_ablation_comparison(
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    baseline = runs["vector_only"]["summary"]
    comparison: dict[str, Any] = {}
    for mode in ("one_hop", "two_hop"):
        summary = runs[mode]["summary"]
        comparison[mode] = {
            "document_hits_delta": (
                summary["correct_document_top_5"]
                - baseline["correct_document_top_5"]
            ),
            "page_hits_delta": (
                summary["correct_source_page_top_5"]
                - baseline["correct_source_page_top_5"]
            ),
            "supported_evidence_delta": (
                summary["supported_questions_with_evidence"]
                - baseline["supported_questions_with_evidence"]
            ),
            "unsupported_refusals_delta": (
                summary["unsupported_questions_refused"]
                - baseline["unsupported_questions_refused"]
            ),
            "average_latency_delta_seconds": round(
                summary["average_retrieval_seconds"]
                - baseline["average_retrieval_seconds"],
                3,
            ),
        }
    return comparison


def ablation_markdown_table(runs: dict[str, dict[str, Any]]) -> str:
    summaries = {mode: run["summary"] for mode, run in runs.items()}
    rows = [
        ("Correct document in top 5", "correct_document_top_5", "document_question_count"),
        (
            "Primary expected page in top 5 (single-page label)",
            "correct_source_page_top_5",
            "page_question_count",
        ),
        (
            "Supported questions with evidence",
            "supported_questions_with_evidence",
            "supported_question_count",
        ),
        (
            "Unsupported questions refused",
            "unsupported_questions_refused",
            "unsupported_question_count",
        ),
    ]
    lines = [
        "| Metric | Vector only | One hop | Two hop |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, numerator, denominator in rows:
        values = [
            f"{summaries[mode][numerator]}/{summaries[mode][denominator]}"
            for mode in ABLATION_MODES
        ]
        lines.append(f"| {label} | " + " | ".join(values) + " |")
    lines.append(
        "| Average retrieval time | "
        + " | ".join(
            f"{summaries[mode]['average_retrieval_seconds']:.2f}s"
            for mode in ABLATION_MODES
        )
        + " |"
    )
    lines.append(
        "| p95 retrieval time | "
        + " | ".join(
            f"{summaries[mode]['p95_retrieval_seconds']:.2f}s"
            for mode in ABLATION_MODES
        )
        + " |"
    )
    for stage in ("raw_retrieval", "reranked_retrieval", "accepted_evidence"):
        for metric in ("page_hits", "all_required_sources"):
            if all("stage_metrics" in summaries[m] for m in ABLATION_MODES):
                values = [f"{summaries[m]['stage_metrics'][stage][metric]}/{summaries[m]['supported_question_count']}" for m in ABLATION_MODES]
                stage_label = {
                    "raw_retrieval": "raw vector/graph retrieval",
                    "reranked_retrieval": "after cross-encoder reranking",
                    "accepted_evidence": "after evidence gate",
                }[stage]
                metric_label = "page hits" if metric == "page_hits" else "all required sources"
                lines.append(f"| {stage_label} — {metric_label} @5 | " + " | ".join(values) + " |")
    for key in ("request_errors", "graph_fallbacks", "queries_with_two_hop_terms"):
        label = {
            "request_errors": "request errors",
            "graph_fallbacks": "graph→vector fallbacks",
            "queries_with_two_hop_terms": "queries with two-hop terms",
        }[key]
        lines.append(f"| {label} | " + " | ".join(str(summaries[m].get(key, 0)) for m in ABLATION_MODES) + " |")
    return "\n".join(lines)


def markdown_table(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "| Metric | Result |",
            "| --- | ---: |",
            (
                "| Correct document in top 5 | "
                f"{summary['correct_document_top_5']}/{summary['document_question_count']} |"
            ),
            (
                "| Correct source page in top 5 | "
                f"{summary['correct_source_page_top_5']}/{summary['page_question_count']} |"
            ),
            (
                "| Answers with citations | "
                f"{summary['answers_with_citations']}/{summary['supported_question_count']} |"
            ),
            (
                "| Unsupported questions refused | "
                f"{summary['unsupported_questions_refused']}/{summary['unsupported_question_count']} |"
            ),
            f"| Average query time | {summary['average_query_seconds']:.2f}s |",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base",
        default=os.getenv("EVAL_API_BASE_URL"),
        help="API base ending in /api/v1 (or set EVAL_API_BASE_URL).",
    )
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--course-id", help="Override the dataset course_id.")
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run against configured stores/providers instead of the HTTP API.",
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help=(
            "Compare vector-only, one-hop, and two-hop retrieval. "
            "Requires --direct and skips answer synthesis."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ablation and not args.direct:
        raise SystemExit("GraphRAG ablation requires --direct.")
    if not args.direct and not args.api_base:
        raise SystemExit("Provide --api-base or set EVAL_API_BASE_URL.")
    dataset = load_dataset(args.questions)
    course_id = args.course_id or str(dataset.get("course_id") or "")
    if not course_id:
        raise SystemExit("The dataset or command line must provide a course ID.")

    if args.ablation:
        runs = asyncio.run(
            run_direct_ablation(
                course_id=course_id,
                questions=dataset["questions"],
            )
        )
        report = {
            "experiment": "graphrag_retrieval_ablation",
            "dataset": dataset.get("name"),
            "course_id": course_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "methodology": {
                "controlled_variable": "graph expansion depth",
                "modes": list(ABLATION_MODES),
                "shared_components": [
                    "question set",
                    "READY document scope",
                    "Qdrant collection",
                    "top_k",
                    "reranker",
                    "evidence threshold",
                ],
                "answer_synthesis": False,
                "execution_order": (
                    "deterministic round-robin by question to reduce warm-cache bias"
                ),
            },
            "runs": runs,
            "comparison_to_vector_only": build_ablation_comparison(runs),
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("\n" + ablation_markdown_table(runs))
        return 1 if any(
            run["summary"]["request_errors"] for run in runs.values()
        ) else 0
    if args.direct:
        results = asyncio.run(
            run_direct_evaluation(
                course_id=course_id,
                questions=dataset["questions"],
            )
        )
    else:
        access_token = _read_local_secret("DEMO_ACCESS_TOKEN")
        results = []
        for index, case in enumerate(dataset["questions"], start=1):
            response, latency, error = query_api(
                api_base=args.api_base,
                course_id=course_id,
                question=case["question"],
                access_token=access_token,
                timeout_seconds=args.timeout,
            )
            result = score_response(case, response, latency, error)
            results.append(result)
            status = "error" if error else "done"
            print(f"[{index:02d}/{len(dataset['questions']):02d}] {status} {latency:.2f}s")

    summary = summarize(results)
    report = {
        "dataset": dataset.get("name"),
        "course_id": course_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "results": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\n" + markdown_table(summary))
    return 1 if summary["request_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
