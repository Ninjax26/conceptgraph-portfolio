"""Offline Phase 2: citation/graph audits and hash-bound human-review worksheets.

No provider calls, database access, ingestion, or production data mutation.
--fallback captures the actual deterministic fallback, NOT LLM answers.
--responses scores separately captured output; semantic scores require reviews.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.quality_metrics import audit_answer, audit_graph, audit_graph_reviews, content_hash, normalized, review_context_hash


def fixture_pages():
    from scripts.evaluate_retrieval_scaling import equipment_pdf
    import pymupdf
    course = json.loads((ROOT / "evaluation/course.json").read_text())
    pages, hashes = {}, {}
    for document in course["documents"]:
        filename = document["filename"]
        content = (ROOT / "public/sample" / filename).read_bytes()
        hashes[filename] = hashlib.sha256(content).hexdigest()
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            if len(pdf) != len(document["pages"]):
                raise ValueError("Authored corpus page counts no longer match their PDFs.")
            for number, page in enumerate(document["pages"], 1):
                if normalized(page["text"]) not in normalized(pdf[number - 1].get_text()):
                    raise ValueError("Authored corpus text no longer matches the PDF source.")
                pages[filename, number] = page["text"]
    for record in json.loads((ROOT / "evaluation/questions-scaling.json").read_text())["documents"]:
        filename, content = equipment_pdf(record)
        hashes[filename] = hashlib.sha256(content).hexdigest()
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            pages.update({(filename, number): normalized(page.get_text()) for number, page in enumerate(pdf, 1)})
    return pages, hashes


def keyed(records):
    result = {}
    for record in records:
        if record["id"] in result:
            raise ValueError("Duplicate quality-case IDs are not valid evaluation records.")
        result[record["id"]] = record
    return result


def run(args):
    pages, hashes = fixture_pages()
    dataset_path = ROOT / "evaluation/quality-cases.json"
    reference_path = ROOT / "evaluation/quality-graph-reference.json"
    dataset = json.loads(dataset_path.read_text())
    cases = keyed(dataset["cases"])
    references = json.loads(reference_path.read_text())["edges"]
    for case in cases.values():
        for filename, page in case["sources"]:
            if (filename, page) not in pages:
                raise ValueError("A quality case names a missing authored source page.")
    for edge in references:
        if normalized(edge["quote"]).casefold() not in normalized(pages[edge["document_name"], edge["page_number"]]).casefold():
            raise ValueError("A graph reference quote does not exist in its labelled source.")
    captures, output_kind = {}, "not_captured"
    if args.fallback:
        from app.services.synthesis_service import SynthesisService
        captures = {case["id"]: {"id": case["id"], "response": {"sources": [
            {"source_id": f"source-{index}", "document_name": filename, "page_number": page,
             "document_id": f"quality-{filename}", "supporting_passage": pages[filename, page]}
            for index, (filename, page) in enumerate(case["sources"], 1)]}}
            for case in cases.values()}
        for capture in captures.values():
            capture["response"]["answer"] = SynthesisService._grounded_evidence_fallback(capture["response"]["sources"])
        output_kind = "deterministic_evidence_fallback_not_llm"
    elif args.responses:
        supplied = json.loads(args.responses.read_text())
        captures = keyed(supplied["captures"])
        output_kind = supplied.get("output_kind", "supplied_capture_origin_unverified")
    if set(captures) - set(cases):
        raise ValueError("Response captures contain unknown case IDs.")
    reviews = json.loads(args.reviews.read_text()) if args.reviews else {}
    answer_reviews = keyed(reviews.get("answers", []))
    if set(answer_reviews) - set(cases):
        raise ValueError("Reviews contain unknown case IDs.")
    answers, worksheet = [], []
    for case in cases.values():
        if case["id"] not in captures:
            continue
        response = captures[case["id"]]["response"]
        result = audit_answer(case, response, pages, answer_reviews.get(case["id"]))
        answers.append(result)
        worksheet.append({"id": case["id"], "question": case["question"], "response": response,
                          "expected_claims": case["expected_claims"], "expected_refusal": case["expected_refusal"],
                          "review": {"id": case["id"], "content_sha256": content_hash(response), "reviewer": "",
                                     "review_context_sha256": review_context_hash(case, response, pages),
                                     "all_claims_reviewed": False, "claims": [], "expected_claims_covered": [],
                                     "refusal_correct": None}})
    snapshot = json.loads(args.graph_snapshot.read_text())
    graphs, edge_queue = [], []
    for filename, record in snapshot.items():
        if record["pdf_sha256"] != hashes.get(filename):
            graphs.append({"document_name": filename, "status": "source_bytes_mismatch_not_scored"})
            continue
        extraction = record["extraction"]
        result = audit_graph(filename, extraction, references, pages)
        result.update(audit_graph_reviews(filename, extraction, pages, reviews.get("graph_edges", [])))
        result["snapshot_sha256"] = hashlib.sha256(json.dumps(extraction, sort_keys=True).encode()).hexdigest()
        graphs.append(result)
        nodes = {node["id"]: node for node in extraction["nodes"]}
        for index, edge in enumerate(extraction["relationships"]):
            if edge["source_node_id"] not in nodes or edge["target_node_id"] not in nodes:
                continue
            locations = {(filename, page) for identifier in (edge["source_node_id"], edge["target_node_id"])
                         for page in ([nodes[identifier].get("page_number")] + nodes[identifier].get("page_numbers", []))
                         if (filename, page) in pages}
            edge_queue.append({"document_name": filename, "edge_index": index, "snapshot_sha256": result["snapshot_sha256"],
                               "source": nodes[edge["source_node_id"]]["name"], "target": nodes[edge["target_node_id"]]["name"],
                               "relation_type": edge["relation_type"], "candidate_pages": [
                                   {"page_number": page, "text": pages[document, page]} for document, page in sorted(locations)],
                               "supported": None, "direction_correct": None, "supporting_evidence": None,
                               "reviewer": "", "note": "Candidate node pages are not proof of relationship support. Human review pending."})
    reviewed = [answer for answer in answers if answer["review_status"] == "reviewed"]
    valid_graphs = [graph for graph in graphs if "edges" in graph]
    summary = {"captured_answers": len(answers), "expected_cases": len(cases), "reviewed_answers": len(reviewed),
               "citation_references": sum(answer["citation_count"] for answer in answers),
               "invalid_citation_references": sum(answer["invalid_citation_count"] for answer in answers),
               "wrong_cited_pages": sum(answer["wrong_cited_page_count"] for answer in answers),
               "source_passages": sum(answer["source_count"] for answer in answers),
               "literal_source_passages": sum(answer["literal_source_count"] for answer in answers),
               "human_reviewed_claims": sum(answer["claim_count"] for answer in reviewed),
               "human_supported_claims": sum(answer["supported_claims"] for answer in reviewed),
               "human_correctly_cited_claims": sum(answer["correctly_cited_claims"] for answer in reviewed),
               "graph_nodes": sum(graph["nodes"] for graph in valid_graphs),
               "graph_edges": sum(graph["edges"] for graph in valid_graphs),
               "known_reference_edges": sum(graph["reference_edges"] for graph in valid_graphs),
               "known_reference_edges_found": sum(graph["reference_edges_found"] for graph in valid_graphs),
               "unreviewed_graph_edges": sum(graph["edges"] - graph["human_reviewed_edges"] for graph in valid_graphs),
               "human_reviewed_graph_edges": sum(graph["human_reviewed_edges"] for graph in valid_graphs),
               "human_correct_graph_edges": sum(graph["human_correct_edges"] for graph in valid_graphs)}
    claim_count = summary["human_reviewed_claims"]
    complete = [answer["answer_completeness"] for answer in reviewed if answer["answer_completeness"] is not None]
    refusals = [answer["refusal_correct"] for answer in reviewed if answer["refusal_correct"] is not None]
    summary.update(groundedness=summary["human_supported_claims"] / claim_count if claim_count else None,
                   citation_correctness=summary["human_correctly_cited_claims"] / claim_count if claim_count else None,
                   mean_answer_completeness=sum(complete) / len(complete) if complete else None,
                   reviewed_supported_answers=len(complete), reviewed_unsupported_answers=len(refusals),
                   correct_reviewed_refusals=sum(refusals) if refusals else None)
    fully_reviewed = (len(reviewed) == len(cases) and len(answers) == len(cases)
                      and bool(valid_graphs) and len(valid_graphs) == len(snapshot)
                      and all(graph["human_reviewed_edges"] == graph["edges"] for graph in valid_graphs))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "status": "reviewed_capture_not_production_accuracy" if fully_reviewed else "partial_pending_semantic_review",
              "dataset": dataset["name"], "output_kind": output_kind, "provider_calls": 0,
              "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
              "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
              "graph_snapshot_sha256": hashlib.sha256(args.graph_snapshot.read_bytes()).hexdigest(),
              "evaluator_sha256": {name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
                                   for name in ("quality_metrics.py", "evaluate_quality.py")},
              "corpus_sha256": hashes, "summary": summary, "answers": answers, "graphs": graphs,
              "limitations": ["No live provider generation, production traffic or data-store access.",
                              "Fallback output is not LLM answer quality; context is oracle-selected, not retrieved.",
                              "Citation format and literal excerpts do not prove claim support.",
                              "Semantic answer metrics require explicit hash-bound human reviews; missing reviews are not zero scores.",
                              "Graph snapshot is an older saved provider extraction, not a new run of the current extractor.",
                              "Partial gold reference measures six known links, not total graph semantic precision/recall."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    review_path = args.output.with_suffix(".review.json")
    # Review files may contain user judgements. Never replace them on rerun.
    if not review_path.exists():
        review_path.write_text(json.dumps({"instructions": "Read every answer/claim and its cited source. Fill answers and graph_edges; do not set all_claims_reviewed until every factual claim is assessed. Rerun with --reviews pointing to this file. Pending or stale reviews do not receive semantic scores.",
            "answers": [item["review"] for item in worksheet], "answer_context": worksheet,
            "graph_edges": edge_queue}, indent=2) + "\n")
    if args.fallback:
        args.output.with_suffix(".responses.json").write_text(json.dumps({"output_kind": output_kind, "captures": list(captures.values())}, indent=2) + "\n")
    def metric(value):
        return "Not measured (review pending)" if value is None else str(round(value, 3))
    lines = ["# Phase 2 offline quality checks", "", f"Output kind: `{output_kind}`. Semantic metrics use reviewed records only.", "",
             "| Check | Actual result |", "| --- | ---: |",
             f"| Captured responses | {len(answers)}/{len(cases)} |",
             f"| Citation labels with invalid indices | {summary['invalid_citation_references']}/{summary['citation_references']} |",
             f"| Labels citing the wrong page | {summary['wrong_cited_pages']}/{summary['citation_references']} |",
             f"| Literal source excerpts | {summary['literal_source_passages']}/{summary['source_passages']} |",
             f"| Hash-bound human answer reviews | {len(reviewed)}/{len(answers)} |",
             f"| Known prerequisite links found | {summary['known_reference_edges_found']}/{summary['known_reference_edges']} |",
             f"| Graph edges pending semantic review | {summary['unreviewed_graph_edges']}/{summary['graph_edges']} |",
             f"| Groundedness (reviewed claims only) | {metric(summary['groundedness'])} |",
             f"| Citation correctness (reviewed claims only) | {metric(summary['citation_correctness'])} |",
             f"| Mean completeness ({len(complete)} reviewed supported answers) | {metric(summary['mean_answer_completeness'])} |",
             f"| Refusals ({len(refusals)} reviewed unsupported answers) | {summary['correct_reviewed_refusals']}/{len(refusals)} |" if refusals else "| Refusal correctness | Not measured (review pending) |",
             "", "Missing reviews receive **no semantic score**, not zero or assumed success. Partial reviewed metrics are not whole-dataset accuracy.",
             "", "## Limits", ""] + [f"- {limit}" for limit in report["limitations"]]
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fallback", action="store_true", help="Capture deterministic fallback only, without LLM calls")
    mode.add_argument("--responses", type=Path, help="Previously captured answer/source records")
    parser.add_argument("--reviews", type=Path, help="Explicit hash-bound human answer reviews")
    parser.add_argument("--graph-snapshot", type=Path, default=ROOT / "evaluation/fixture-graphs.json")
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation/quality-offline-current")
    run(parser.parse_args())
