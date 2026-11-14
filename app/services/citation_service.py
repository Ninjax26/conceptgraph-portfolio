import math
from typing import Any

from app.core.config import settings
from app.services.evidence_trace import EvidenceTrace, fingerprint
from app.services.retrieval_candidates import identifier_supported, identifier_terms, passage_excerpt
from app.services.evidence_facets import comparison_facets
from app.services.fact_support import FactRequest, requested_facts, supports_fact


def _source_passage(chunk: dict[str, Any]) -> str:
    text = " ".join(str(chunk.get("text", "")).split())
    selected = " ".join(str(chunk.get("rerank_passage") or "").split())
    # Only literal source text may replace the original passage.
    return selected if selected and selected in text else text


def assess_evidence(
    chunks: list[dict[str, Any]],
    *,
    required_identifiers: list[str] | None = None,
    trace: EvidenceTrace | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        text = _source_passage(chunk)
        # Complementary context need not repeat the identifier. Still exclude
        # passages naming only other identifiers and verify every requested ID
        # across the accepted evidence and final citations below.
        if (required_identifiers and identifier_terms(text, named_only=True)
                and not any(identifier_supported(term, text) for term in required_identifiers)):
            if trace is not None:
                trace.record("evidence_gate", "other_identifier", chunk, passage=text)
            continue
        vector_raw = float(chunk.get("score") or 0.0)
        rerank_raw = chunk.get("rerank_score")
        if not math.isfinite(vector_raw) or (rerank_raw is not None and not math.isfinite(float(rerank_raw))):
            if trace is not None:
                trace.record("evidence_gate", "nonfinite_score", chunk, passage=text)
            continue
        vector_score = max(0.0, min(1.0, vector_raw))
        rerank_score = (
            1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, float(rerank_raw)))))
            if rerank_raw is not None
            else vector_score
        )
        evidence_score = 0.7 * rerank_score + 0.3 * vector_score
        if trace is not None:
            trace.record("evidence_gate", "below_threshold" if evidence_score < settings.evidence_min_score else "accepted",
                         chunk, passage=text, vector_raw=vector_raw, rerank_raw=rerank_raw,
                         vector_component=0.3 * vector_score, rerank_component=0.7 * rerank_score,
                         evidence_score=evidence_score, threshold=settings.evidence_min_score)
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
    if trace is not None:
        trace.record("identifier_coverage", "verified" if identifiers_verified else "missing_accepted_identifiers",
                     required_count=len(required_identifiers or []), accepted_count=len(scored))
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


def _select_sources(
    sources: list[dict[str, Any]], limit: int, required: list[str], facets: int = 0,
    facts: tuple[FactRequest, ...] = (),
) -> list[dict[str, Any]]:
    """Reserve citation slots for verified IDs, then fill by original rank.

    At most eight independent ID/fact constraints plus two facets gives 1,024 coverage states. Find the
    smallest covering set (rank breaks ties); greedy choices can waste slots
    when one passage covers several IDs. Never admit rejected evidence.
    """
    # A fact witness already proves its subject identifier in the cited text.
    # Avoid spending a second coverage bit for the same identifier.
    required = [term for term in required if term not in {fact.identifier for fact in facts}]
    if facts and len(required) + len(facts) > 8:
        return []
    if (not required and not facets and not facts) or len(required) > 8 or facets > 2:
        return sources[:limit]
    masks = [sum(1 << bit for bit, term in enumerate(required)
                 if identifier_supported(term, source["supporting_passage"]))
             | sum(1 << (len(required) + bit) for bit, fact in enumerate(facts)
                   if supports_fact(fact, source["supporting_passage"]))
             | sum(1 << (len(required) + len(facts) + facet) for facet in source.get("_evidence_facets", []))
             for source in sources]
    target = (1 << (len(required) + len(facts) + facets)) - 1
    covered = 0
    for mask in masks[:limit]:
        covered |= mask
    if covered == target:
        return sources[:limit]
    covers: dict[int, tuple[int, ...]] = {0: ()}
    for index, coverage in enumerate(masks):
        for mask, selected in tuple(covers.items()):
            combined = mask | coverage
            candidate = (*selected, index)
            if combined == mask or len(candidate) > limit:
                continue
            previous = covers.get(combined)
            if previous is None or (len(candidate), candidate) < (len(previous), previous):
                covers[combined] = candidate
    reserved = covers.get(target, ())
    indices = list(reserved)
    indices.extend(index for index in range(len(sources)) if index not in reserved)
    return [sources[index] for index in sorted(indices[:limit])]


def _comparison_evidence(
    parts: list[str], chunks: list[dict[str, Any]], required: list[str],
    *, trace: EvidenceTrace | None = None, facts: tuple[FactRequest, ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    confidences = []
    missing = {"level": "insufficient", "score": 0.0, "evidence_count": 0,
               "reason": "Reliable cited evidence is missing for one side of this comparison."}
    for index, part in enumerate(parts):
        group = [chunk for chunk in chunks if type(chunk.get("evidence_facet")) is int
                 and chunk["evidence_facet"] == index and chunk.get("evidence_facet_count") == len(parts)]
        identifiers = identifier_terms(part, limit=8, named_only=True) if settings.retrieval_verify_identifiers else []
        accepted, confidence = assess_evidence(group, required_identifiers=identifiers, trace=trace)
        sources = build_sources(accepted, limit=len(accepted), question=part, required_identifiers=identifiers, trace=trace, facts=requested_facts(part, fragment=True))
        if not _facts_verified(requested_facts(part, fragment=True), sources, trace):
            return [], missing
        if not sources or not all(any(identifier_supported(term, source["supporting_passage"])
                                     for source in sources) for term in identifiers):
            if trace is not None:
                trace.record("comparison_coverage", "missing_comparison_side", facet=index)
            return [], missing
        confidences.append(confidence)
        for source in sources:
            key = source["document_id"], source["page_number"], source["supporting_passage"]
            existing = candidates.get(key)
            memberships = (existing or {}).get("_evidence_facets", set()) | {index}
            if existing is None or source["metadata"]["evidence_score"] > existing["metadata"]["evidence_score"]:
                candidates[key] = source
            candidates[key]["_evidence_facets"] = memberships
    ranked = sorted(candidates.values(), key=lambda source: source["metadata"]["evidence_score"], reverse=True)
    sources = _select_sources(ranked, 5, required, len(parts), facts)
    if not _facts_verified(facts, sources, trace):
        return [], missing
    if trace is not None:
        selected_ids = {id(source) for source in sources}
        for source in ranked:
            trace.record("comparison_selection", "selected" if id(source) in selected_ids else "citation_limit",
                         document_key=fingerprint(source["document_id"]), page=source["page_number"],
                         passage_sha256=fingerprint(source["supporting_passage"]),
                         facets=sorted(source["_evidence_facets"]))
    if not all(any(index in source["_evidence_facets"] for source in sources) for index in range(len(parts))):
        if trace is not None:
            trace.record("comparison_coverage", "citation_limit_missing_side")
        return [], missing
    if not all(any(identifier_supported(term, source["supporting_passage"]) for source in sources) for term in required):
        if trace is not None:
            trace.record("comparison_coverage", "missing_cited_identifiers")
        return [], missing
    for index, source in enumerate(sources, 1):
        source.pop("_evidence_facets")
        source["source_id"] = f"source-{index}"
    confidence = min(confidences, key=lambda value: value["score"])
    return sources, dict(confidence, evidence_count=len(sources),
                         reason="Reliable cited passages cover both sides of this comparison.")


def build_sources(
    chunks: list[dict[str, Any]], limit: int = 5, *, question: str | None = None,
    required_identifiers: list[str] | None = None,
    trace: EvidenceTrace | None = None,
    facts: tuple[FactRequest, ...] = (),
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, str]] = set()
    source_chunks = []
    for position, chunk in enumerate(chunks):
        metadata = chunk.get("metadata") or {}
        passage = " ".join(str(chunk.get("text", "")).split())
        page = metadata.get("page_number") if isinstance(metadata.get("page_number"), int) else None
        document_id = str(metadata.get("upload_id", ""))
        excerpt = passage_excerpt(_source_passage(chunk), question)
        key = (document_id, page, excerpt)
        if not passage or key in seen:
            if trace is not None:
                trace.record("citation_selection", "empty_text" if not passage else "duplicate", chunk, passage=excerpt)
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
        if trace is not None:
            source_chunks.append(chunk)
        if len(sources) >= limit and not required_identifiers and not facts:
            if trace is not None:
                for remaining in chunks[position + 1:]:
                    trace.record("citation_selection", "citation_limit", remaining)
            break
    selected = _select_sources(sources, limit, required_identifiers or [], facts=facts)
    if trace is not None:
        selected_ids = {id(source) for source in selected}
        for chunk, source in zip(source_chunks, sources, strict=True):
            trace.record("citation_selection", "selected" if id(source) in selected_ids else "citation_limit",
                         chunk, passage=source["supporting_passage"])
    for index, source in enumerate(selected, 1):
        source["source_id"] = f"source-{index}"
    return selected


def _facts_verified(facts: tuple[FactRequest, ...], sources: list[dict[str, Any]], trace: EvidenceTrace | None) -> bool:
    verified = True
    for fact in facts:
        present = any(supports_fact(fact, source["supporting_passage"]) for source in sources)
        if trace is not None:
            trace.record("fact_coverage", "verified" if present else "missing_requested_fact",
                         attribute=fact.attribute, subject_key=fingerprint(fact.identifier), qualifier=fact.qualifier)
        verified = verified and present
    return verified


def _prepare_primary_evidence(question: str, chunks: list[dict[str, Any]], *, trace: EvidenceTrace | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Use the same evidence selection for HTTP queries and evaluation runners."""
    def finish(sources, confidence, reason):
        return sources, confidence, reason

    required = identifier_terms(question, limit=9, named_only=True) if settings.retrieval_verify_identifiers else []
    if len(required) > 8:
        return finish([], {"level": "insufficient", "score": 0.0, "evidence_count": 0,
                    "reason": "This question names too many identifiers to verify in one answer. Ask about a smaller set."}, "identifier_limit")
    facts = requested_facts(question)
    remaining_ids = set(required) - {fact.identifier for fact in facts}
    if facts and len(facts) + len(remaining_ids) > 8:
        return finish([], {"level": "insufficient", "score": 0.0, "evidence_count": 0,
                           "reason": "This question requests too many explicit facts. Ask about a smaller set."}, "fact_limit")
    parts = comparison_facets(question)
    if parts and any("evidence_facet" in chunk for chunk in chunks):
        sources, confidence = _comparison_evidence(parts, chunks, required, trace=trace, facts=facts)
        return finish(sources, confidence, "sources_selected" if sources else "comparison_refused")
    evidence, confidence = assess_evidence(chunks, required_identifiers=required, trace=trace)
    sources = build_sources(evidence, question=question, required_identifiers=required, trace=trace, facts=facts)
    if not _facts_verified(facts, sources, trace):
        return finish([], {"level": "insufficient", "score": 0.0, "evidence_count": 0,
                           "reason": "The requested equipment fact is not explicitly supported by the cited passages."}, "requested_fact_missing")
    if required and not all(any(identifier_supported(term, source["supporting_passage"]) for source in sources) for term in required):
        return finish([], {"level": "insufficient", "score": 0.0, "evidence_count": 0,
                    "reason": "The requested identifiers could not be verified in the cited passages."}, "missing_cited_identifiers")
    return finish(sources, confidence, "sources_selected" if sources else "no_accepted_evidence")


def prepare_evidence(question: str, chunks: list[dict[str, Any]], *, trace: EvidenceTrace | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retain primary evidence; optional context cannot unlock a refused answer."""
    primary = [chunk for chunk in chunks if chunk.get("supporting_evidence") is not True]
    supporting = [chunk for chunk in chunks if chunk.get("supporting_evidence") is True]
    sources, confidence, reason = _prepare_primary_evidence(question, primary, trace=trace)
    if sources and supporting and len(sources) < 5:
        required = identifier_terms(question, limit=9, named_only=True) if settings.retrieval_verify_identifiers else []
        # Primary citations have already verified every requested identifier.
        # Reuse the same individual score/wrong-identifier guards on the combined
        # set, so complementary context need not repeat all identifiers itself.
        accepted, _ = assess_evidence([*primary, *supporting], required_identifiers=required, trace=trace)
        accepted_support = [chunk for chunk in accepted if chunk.get("supporting_evidence") is True]
        extra = build_sources(accepted_support, limit=len(accepted_support),
                              question=question, required_identifiers=required, trace=trace, facts=requested_facts(question))
        pages = {(source["document_id"], source["page_number"]) for source in sources}
        extra.sort(key=lambda source: (source["document_id"], source["page_number"]) in pages)
        primary_count = len(sources)
        for source in extra:
            # Avoid adding a narrower repeat of an already cited literal passage.
            decision = "selected"
            if len(sources) == 5:
                decision = "citation_limit"
            elif any(source["document_id"] == previous["document_id"]
                     and source["page_number"] == previous["page_number"]
                     and source["supporting_passage"] in previous["supporting_passage"] for previous in sources):
                decision = "already_cited"
            if trace is not None:
                trace.record("supporting_selection", decision, document_key=fingerprint(source["document_id"]),
                             page=source["page_number"], passage_sha256=fingerprint(source["supporting_passage"]), role="supporting")
            if decision != "selected":
                continue
            source["source_id"] = f"source-{len(sources) + 1}"
            sources.append(source)
        if len(sources) > primary_count:
            confidence = dict(confidence, evidence_count=len(sources),
                              reason=confidence["reason"] + " Additional cited context passed the same evidence guards.")
    return trace.finish(sources, confidence, reason) if trace is not None else (sources, confidence)
