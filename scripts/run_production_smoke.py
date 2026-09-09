#!/usr/bin/env python3
"""Exercise one deployed query and export a secret-free provider telemetry report."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def _request(
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, dict[str, Any], float]:
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload, time.perf_counter() - started
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"detail": "Non-JSON error response"}
        return exc.code, payload, time.perf_counter() - started


def _choose_course(courses: list[dict[str, Any]], preferred: str | None) -> dict[str, Any]:
    ready = [course for course in courses if int(course.get("ready_documents") or 0) > 0]
    if preferred:
        for course in ready:
            if preferred.casefold() in {
                str(course.get("course_id") or "").casefold(),
                str(course.get("course_name") or "").casefold(),
            }:
                return course
        raise RuntimeError(f"No READY course matched {preferred!r}.")
    if not ready:
        raise RuntimeError("The deployment has no READY reviewer course for a query smoke test.")
    return max(ready, key=lambda course: int(course.get("processed_chunk_count") or 0))


def _metric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    fields = (
        "provider_attempts",
        "successful_attempts",
        "failed_attempts",
        "failover_attempts",
        "rate_limit_errors",
        "evidence_fallbacks",
        "estimated_input_tokens",
        "estimated_output_tokens",
    )
    return {
        field: int(after.get(field) or 0) - int(before.get(field) or 0)
        for field in fields
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base",
        default="https://conceptgraph-api.onrender.com/api/v1",
    )
    parser.add_argument("--course", help="Optional course name or UUID")
    parser.add_argument(
        "--question",
        default="Summarize the most important concepts in this course.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    token = _read_local_secret("DEMO_ACCESS_TOKEN")
    if not token:
        raise SystemExit("DEMO_ACCESS_TOKEN is required locally; it is never written to the report.")

    api_base = args.api_base.rstrip("/")
    try:
        health_status, health, health_latency = _request(f"{api_base}/health")
        ready_status, readiness, ready_latency = _request(f"{api_base}/ready", timeout=180)
        status_code, provider_before, _ = _request(
            f"{api_base}/providers/status", token=token
        )
        courses_code, courses, _ = _request(f"{api_base}/ingest/courses", token=token)
    except (URLError, TimeoutError) as exc:
        raise SystemExit(f"Production endpoint could not be reached: {type(exc).__name__}") from exc

    if status_code != 200 or courses_code != 200 or not isinstance(courses, list):
        raise SystemExit("Reviewer authentication or course discovery failed.")

    course = _choose_course(courses, args.course)
    before_totals = provider_before["metrics"]["totals"]
    query_status, query, query_latency = _request(
        f"{api_base}/query",
        token=token,
        body={
            "question": args.question,
            "course_id": course["course_id"],
            "retrieval_mode": "vector_only",
        },
        timeout=180,
    )
    after_code, provider_after, _ = _request(
        f"{api_base}/providers/status", token=token
    )
    if after_code != 200:
        raise SystemExit("Provider metrics could not be read after the query.")

    after_totals = provider_after["metrics"]["totals"]
    generation = query.get("generation_metadata") if query_status == 200 else {}
    confidence = query.get("confidence") if query_status == 200 else {}
    graph_metadata = query.get("graph_metadata") if query_status == 200 else {}
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "api_base": api_base,
        "checks": {
            "health": {
                "passed": health_status == 200 and health.get("status") == "healthy",
                "latency_ms": round(health_latency * 1_000),
            },
            "readiness": {
                "passed": ready_status == 200 and readiness.get("status") == "ready",
                "latency_ms": round(ready_latency * 1_000),
                "degraded_services": readiness.get("degraded_services", []),
            },
            "authenticated_provider_status": {"passed": status_code == 200},
            "authenticated_course_discovery": {"passed": courses_code == 200},
            "grounded_query": {
                "passed": query_status == 200,
                "latency_ms": round(query_latency * 1_000),
                "source_count": len(query.get("sources") or []),
                "confidence": confidence.get("level"),
                "provider_used": generation.get("provider_used"),
                "failover_used": bool(generation.get("failover_used")),
                "requested_mode": graph_metadata.get("requested_mode"),
                "actual_mode": graph_metadata.get("retrieval_mode"),
            },
            "telemetry_incremented": {
                "passed": int(after_totals.get("provider_attempts") or 0)
                > int(before_totals.get("provider_attempts") or 0)
            },
        },
        "provider_configuration": [
            {
                "name": provider["name"],
                "configured": provider["configured"],
                "available": provider["available"],
                "last_outcome": provider["last_outcome"],
            }
            for provider in provider_after["providers"]
        ],
        "observed_query_delta": _metric_delta(before_totals, after_totals),
        "process_totals_after_test": after_totals,
        "privacy": "No access code, prompt, answer, document ID, filename, or provider key is included.",
        "limitations": provider_after["metrics"]["accuracy_note"],
    }
    report["passed"] = all(
        check["passed"] for check in report["checks"].values()
    )

    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
