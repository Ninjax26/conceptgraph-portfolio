#!/usr/bin/env python3
"""Run the small, human-annotated ConceptGraph portfolio evaluation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
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
        if not should_refuse and (
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.direct and not args.api_base:
        raise SystemExit("Provide --api-base or set EVAL_API_BASE_URL.")
    dataset = load_dataset(args.questions)
    course_id = args.course_id or str(dataset.get("course_id") or "")
    if not course_id:
        raise SystemExit("The dataset or command line must provide a course ID.")

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
