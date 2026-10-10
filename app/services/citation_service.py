import math
from typing import Any

from app.core.config import settings
from app.services.retrieval_candidates import identifier_supported, identifier_terms, passage_excerpt


def assess_evidence(
    chunks: list[dict[str, Any]],
    *,
    required_identifiers: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        if required_identifiers and not any(identifier_supported(term, str(chunk.get("text", ""))) for term in required_identifiers):
            continue
        vector_raw = float(chunk.get("score") or 0.0)
        rerank_raw = chunk.get("rerank_score")
        if not math.isfinite(vector_raw) or (rerank_raw is not None and not math.isfinite(float(rerank_raw))):
            continue
        vector_score = max(0.0, min(1.0, vector_raw))
        rerank_score = (
            1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, float(rerank_raw)))))
            if rerank_raw is not None
            else vector_score
        )
        evidence_score = 0.7 * rerank_score + 0.3 * vector_score
        if evidence_score < settings.evidence_min_score:
            continue
        enriched = dict(chunk)
        enriched["evidence_score"] = evidence_score
        scored.append(enriched)

    scored.sort(key=lambda item: float(item["evidence_score"]), reverse=True)
    identifiers_verified = not required_identifiers or all(
        any(identifier_supported(term, str(chunk.get("text", ""))) for chunk in scored)
        for term in required_identifiers
    )
    if not scored or not identifiers_verified:
        return [], {
            "level": "insufficient",
            "score": 0.0,
            "evidence_count": 0,
            "reason": ("The requested identifiers could not be verified in the evidence."
                       if required_identifiers and not identifiers_verified else
                       "No retrieved passage met the minimum evidence threshold."),
        }

    top_scores = [float(chunk["evidence_score"]) for chunk in scored[:4]]
    confidence_score = sum(top_scores) / len(top_scores)
    if confidence_score >= settings.evidence_high_score and len(top_scores) >= 2:
        level = "high"
    elif confidence_score >= settings.evidence_medium_score:
        level = "medium"
    else:
        level = "low"
    return scored, {
        "level": level,
        "score": round(confidence_score, 3),
        "evidence_count": len(scored),
        "reason": f"{len(scored)} passage(s) met the configured evidence threshold.",
    }


def build_sources(chunks: list[dict[str, Any]], limit: int = 5, *, question: str | None = None) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, str]] = set()
    for chunk in chunks:
        metadata = chunk.get("metadata") or {}
        passage = " ".join(str(chunk.get("text", "")).split())
        page = metadata.get("page_number") if isinstance(metadata.get("page_number"), int) else None
        document_id = str(metadata.get("upload_id", ""))
        excerpt = passage_excerpt(passage, question)
        key = (document_id, page, excerpt)
        if not passage or key in seen:
            continue
        seen.add(key)
        page_suffix = f"#page={page}" if page is not None else ""
        sources.append(
            {
                "source_id": f"source-{len(sources) + 1}",
                "document_id": document_id,
                "document_name": str(metadata.get("document_name") or "Course PDF"),
                "page_number": page,
                "section_heading": str(metadata.get("section_heading") or "") or None,
                "supporting_passage": excerpt,
                "source_type": "pdf",
                "preview_url": (
                    f"/ingest/uploads/{document_id}/preview{page_suffix}"
                    if document_id
                    else None
                ),
                "metadata": {
                    "retrieval_score": chunk.get("rerank_score", chunk.get("score")),
                    "evidence_score": chunk.get("evidence_score"),
                    "upload_id": document_id,
                    "page_number": page,
                },
            }
        )
        if len(sources) >= limit:
            break
    return sources


def prepare_evidence(question: str, chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use the same evidence selection for HTTP queries and evaluation runners."""
    required = identifier_terms(question, limit=9, named_only=True) if settings.retrieval_verify_identifiers else []
    if len(required) > 8:
        return [], {"level": "insufficient", "score": 0.0, "evidence_count": 0,
                    "reason": "This question names too many identifiers to verify in one answer. Ask about a smaller set."}
    evidence, confidence = assess_evidence(chunks, required_identifiers=required)
    sources = build_sources(evidence, question=question)
    if required and not all(any(identifier_supported(term, source["supporting_passage"]) for source in sources) for term in required):
        return [], {"level": "insufficient", "score": 0.0, "evidence_count": 0,
                    "reason": "The requested identifiers could not be verified in the cited passages."}
    return sources, confidence
