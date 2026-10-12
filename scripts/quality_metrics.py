"""Offline structural checks plus explicitly human-reviewed semantic metrics."""
import hashlib
import json
import re

from app.schemas.extraction import ALLOWED_RELATIONSHIP_TYPES, normalize_concept_name, relationship_key
from scripts.run_evaluation import REFUSAL_TEXT


def normalized(text):
    return " ".join(str(text).split())


def content_hash(response):
    content = {"answer": response.get("answer", ""), "sources": response.get("sources", [])}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_context_hash(case, response, pages):
    locations = {(source.get("document_name"), source.get("page_number")) for source in response.get("sources", [])}
    locations.update(tuple(source) for source in case.get("sources", []))
    context = {"case": case, "source_pages": [
        {"document_name": document, "page_number": page, "text": pages.get((document, page))}
        for document, page in sorted(locations, key=str)]}
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audit_answer(case, response, pages, review=None):
    answer, sources = str(response.get("answer") or ""), response.get("sources") or []
    refs = []
    for tag in re.findall(r"\[Source[^\]\n]*\]", answer, re.IGNORECASE):
        match = re.fullmatch(r"\[Source\s+(\d+)(?:,\s*p\.\s*(\d+))?\]", tag, re.IGNORECASE)
        index, page = (int(match[1]), int(match[2]) if match[2] else None) if match else (None, None)
        in_range = index is not None and 1 <= index <= len(sources)
        page_ok = in_range and (page is None or page == sources[index - 1].get("page_number"))
        refs.append({"label": tag, "index": index, "index_valid": in_range, "page_valid": page_ok})
    literal = []
    for source in sources:
        page = source.get("page_number")
        text = pages.get((source.get("document_name"), page)) if type(page) is int else None
        passage = normalized(source.get("supporting_passage") or "")
        literal.append(bool(text and passage and passage in normalized(text)))
    result = {"id": case["id"], "answer_present": bool(answer.strip()), "content_sha256": content_hash(response),
              "review_context_sha256": review_context_hash(case, response, pages),
              "expected_refusal": case["expected_refusal"], "canonical_refusal": normalized(answer).casefold() == REFUSAL_TEXT.casefold(),
              "citation_references": refs, "citation_count": len(refs),
              "invalid_citation_count": sum(not ref["index_valid"] for ref in refs),
              "wrong_cited_page_count": sum(ref["index_valid"] and not ref["page_valid"] for ref in refs),
              "source_count": len(sources), "literal_source_count": sum(literal),
              "cited_sources_literal": all(ref["index_valid"] and literal[ref["index"] - 1] for ref in refs) if refs else None,
              "review_status": "pending", "claim_count": None, "supported_claims": None,
              "correctly_cited_claims": None, "groundedness": None, "citation_correctness": None,
              "answer_completeness": None, "refusal_correct": None}
    if review is None:
        return result
    if review.get("all_claims_reviewed") is not True and not str(review.get("reviewer") or "").strip():
        return result
    claims = review.get("claims", [])
    covered = review.get("expected_claims_covered", [])
    known = {claim["id"] for claim in case["expected_claims"]}
    cited = {ref["index"] for ref in refs if ref["index_valid"] and ref["page_valid"]}
    valid = (bool(str(review.get("reviewer") or "").strip()) and review.get("content_sha256") == result["content_sha256"]
             and review.get("review_context_sha256") == result["review_context_sha256"]
             and review.get("all_claims_reviewed") is True and isinstance(claims, list)
             and isinstance(covered, list) and all(isinstance(key, str) and key in known for key in covered)
             and type(review.get("refusal_correct")) is bool)
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            valid = False
            continue
        indices = claim.get("source_indices", [])
        valid = valid and (bool(str(claim.get("text") or "").strip()) and type(claim.get("supported")) is bool
            and type(claim.get("citations_correct")) is bool and isinstance(indices, list)
            and all(type(index) is int and index in cited for index in indices)
            and (not claim["citations_correct"] or (bool(indices) and all(literal[index - 1] for index in indices))))
    if not valid:
        return dict(result, review_status="invalid_or_stale")
    count = len(claims)
    supported = sum(claim["supported"] for claim in claims)
    correct = sum(claim["citations_correct"] for claim in claims)
    return dict(result, review_status="reviewed", claim_count=count, supported_claims=supported,
                correctly_cited_claims=correct, groundedness=supported / count if count else None,
                citation_correctness=correct / count if count else None,
                answer_completeness=len(set(covered)) / len(known) if known else None,
                refusal_correct=review["refusal_correct"] if case["expected_refusal"] else None)


def audit_graph(filename, extraction, references, pages):
    nodes, edges = extraction.get("nodes", []), extraction.get("relationships", [])
    by_id = {node["id"]: node for node in nodes}
    expected = [edge for edge in references if edge["document_name"] == filename]
    variants = [{relationship_key(normalize_concept_name(source), normalize_concept_name(target), edge["relation_type"])
                 for source in edge["source_names"] for target in edge["target_names"]} for edge in expected]
    seen, found, reversed_edges, unknown, matches = set(), set(), [], [], []
    dangling = unsupported = self_edges = duplicates = 0
    for index, edge in enumerate(edges):
        source, target, kind = edge.get("source_node_id"), edge.get("target_node_id"), edge.get("relation_type")
        unsupported += kind not in ALLOWED_RELATIONSHIP_TYPES
        if source not in by_id or target not in by_id:
            dangling += 1
            continue
        if kind not in ALLOWED_RELATIONSHIP_TYPES:
            continue
        if source == target:
            self_edges += 1
        raw_key = relationship_key(source, target, kind)
        duplicates += raw_key in seen
        seen.add(raw_key)
        name_key = relationship_key(normalize_concept_name(by_id[source]["name"]), normalize_concept_name(by_id[target]["name"]), kind)
        hit = [number for number, keys in enumerate(variants) if name_key in keys]
        found.update(hit)
        if hit:
            matches.extend(hit)
        elif kind != "RELATED_TO" and any((name_key[1], name_key[0], kind) in keys for keys in variants):
            reversed_edges.append(index)
        else:
            unknown.append(index)
    valid_provenance = sum(node.get("document_name") == filename and type(node.get("page_number")) is int
                           and (filename, node["page_number"]) in pages and bool(node.get("upload_id"))
                           and str(node.get("source_chunk_id") or "").startswith(f"{node['upload_id']}:{node['page_number']}:")
                           for node in nodes)
    return {"document_name": filename, "nodes": len(nodes), "edges": len(edges),
            "duplicate_node_ids": len(nodes) - len(by_id), "dangling_edges": dangling,
            "unsupported_types": unsupported, "self_edges": self_edges, "duplicate_edges": duplicates,
            "nodes_with_valid_provenance_metadata": valid_provenance,
            "reference_edges": len(expected), "reference_edges_found": len(found),
            "known_reversed_edge_indices": reversed_edges, "unreviewed_edge_indices": unknown,
            "reference_alias_duplicate_matches": len(matches) - len(found),
            "semantic_precision": None, "note": "Partial gold reference; unlabelled edges require review, not automatic rejection."}


def audit_graph_reviews(filename, extraction, pages, reviews):
    digest = hashlib.sha256(json.dumps(extraction, sort_keys=True).encode()).hexdigest()
    judged, invalid, seen = [], 0, set()
    for review in reviews:
        if review.get("document_name") != filename:
            continue
        index = review.get("edge_index")
        valid = (type(index) is int and 0 <= index < len(extraction["relationships"])
                 and index not in seen and review.get("snapshot_sha256") == digest
                 and bool(str(review.get("reviewer") or "").strip())
                 and type(review.get("supported")) is bool and type(review.get("direction_correct")) is bool)
        evidence = review.get("supporting_evidence", [])
        valid = valid and isinstance(evidence, list)
        for source in evidence if isinstance(evidence, list) else []:
            if not isinstance(source, dict):
                valid = False
                continue
            key = source.get("document_name"), source.get("page_number")
            quote = normalized(source.get("quote") or "")
            valid = valid and key in pages and bool(quote) and quote in normalized(pages[key])
        valid = valid and (review.get("supported") is False or bool(evidence))
        if not valid:
            invalid += 1
            continue
        seen.add(index)
        judged.append(review)
    correct = sum(review["supported"] and review["direction_correct"] for review in judged)
    return {"human_reviewed_edges": len(judged), "human_correct_edges": correct,
            "invalid_or_stale_reviews": invalid,
            "semantic_precision": correct / len(judged) if judged and len(judged) == len(extraction["relationships"]) else None}
