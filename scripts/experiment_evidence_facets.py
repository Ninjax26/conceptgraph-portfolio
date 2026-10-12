"""Development-only comparison experiment; never changes API behavior or calls providers.

Reuses the authored challenge's frozen candidate pool to isolate reranking and
evidence selection. The fixture must have one chunk per page; do not reconstruct
arbitrary production chunks from page-level diagnostics.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.evidence_facets import comparison_facets


def merge_facet_sources(groups, limit=5):
    """Reserve evidence for both parts; never answer from just one part."""
    if not groups or any(not group for group in groups):
        return []
    result, seen = [], set()
    for source in [group[0] for group in groups] + [source for group in groups for source in group]:
        key = source["document_id"], source["page_number"], source["supporting_passage"]
        if key not in seen:
            result.append(dict(source, source_id=f"source-{len(result) + 1}"))
            seen.add(key)
        if len(result) == limit:
            break
    return result


def run(candidate_report, output):
    import torch
    from sentence_transformers import CrossEncoder
    from app.core.config import settings
    from app.services.parser_service import ParserService
    from app.services.rerank_service import RerankService
    from app.services.citation_service import prepare_evidence
    from scripts.evaluate_retrieval_scaling import equipment_pdf

    challenge_path = ROOT / "evaluation/questions-evidence-context.json"
    challenge = json.loads(challenge_path.read_text())
    baseline = json.loads(candidate_report.read_text())
    if baseline["methodology"]["evidence_min_score"] != settings.evidence_min_score:
        raise ValueError("The experiment must use the candidate report's unchanged evidence threshold.")
    if baseline["methodology"]["dataset_sha256"]["evaluation/questions-evidence-context.json"] != hashlib.sha256(challenge_path.read_bytes()).hexdigest():
        raise ValueError("Challenge labels changed since the candidate report.")
    corpus = baseline["corpora"][0]
    rows = {row["id"]: row for row in corpus["profiles"]["identifier_10_verified"]["results"]}
    parser, texts = ParserService(), {}
    course = json.loads((ROOT / "evaluation/course.json").read_text())
    documents = [(doc["filename"], (ROOT / "public/sample" / doc["filename"]).read_bytes())
                 for doc in course["documents"]]
    documents += [equipment_pdf(doc) for doc in json.loads((ROOT / "evaluation/questions-scaling.json").read_text())["documents"]]
    for index in range(corpus["documents"] - len(documents)):
        documents.append(equipment_pdf({"id": f"PMP-{200 + index}", "pressure": str(8 + index % 20),
                                        "interval": 7 + index % 120}))
    for filename, content in documents:
        if hashlib.sha256(content).hexdigest() != corpus["corpus_sha256"][filename]:
            raise ValueError("Authored source bytes changed since the candidate report.")
        for chunk in parser.chunk_pages(parser.extract_pages_from_bytes(content), "experiment", filename, filename):
            key = filename, chunk.metadata["page_number"]
            if key in texts:
                raise ValueError("This page-level experiment requires one chunk per authored fixture page.")
            texts[key] = chunk

    torch.set_num_threads(2)
    reranker = RerankService()
    reranker._model = CrossEncoder(baseline["methodology"]["reranker"], device="cpu", local_files_only=True)
    results = []
    for case in challenge["questions"]:
        if case["split"] != "development":
            continue
        row, chunks = rows[case["id"]], []
        for candidate in row["evidence_diagnostics"]:
            chunk = texts[candidate["document_name"], candidate["page_number"]]
            chunks.append({"id": chunk.id, "text": chunk.text, "metadata": chunk.metadata,
                           "score": candidate["vector_score"]})
        facets = comparison_facets(case["question"])
        if facets:
            sources = merge_facet_sources([prepare_evidence(facet, reranker._rerank_locally(facet, chunks))[0]
                                          for facet in facets])
        else:
            sources, _ = prepare_evidence(case["question"], reranker._rerank_locally(case["question"], chunks))
        expected = {(source["document_name"], source["page_number"]) for source in case.get("expected_sources", [])}
        found = {(source["document_name"], source["page_number"]) for source in sources}
        text = " ".join(source["supporting_passage"] for source in sources).casefold()
        results.append({"id": case["id"], "should_refuse": case["should_refuse"], "facets": facets,
                        "source_complete_before": row["accepted_evidence"]["all_sources_hit"],
                        "source_complete_after": expected <= found if expected else None,
                        "gate_refused_before": row["refused"], "gate_refused_after": not sources,
                        "expected_fragments_present": all(fragment.casefold() in text for fragment in case.get("expected_passage_fragments", [])) if expected else None,
                        "top_sources": [{"document_name": source["document_name"], "page_number": source["page_number"]} for source in sources]})
    report = {"prototype_only": True, "provider_requests": 0, "generation_requests": 0,
              "evidence_min_score": settings.evidence_min_score,
              "candidate_report_sha256": hashlib.sha256(candidate_report.read_bytes()).hexdigest(),
              "question_sha256": hashlib.sha256(challenge_path.read_bytes()).hexdigest(),
              "limitations": ["Development-only; not an independent held-out gain.",
                              "Fixed reconstructed candidate pool, not a fresh vector search or end-to-end API test.",
                              "Narrow explicit Compare/Contrast A with B syntax only; not general semantic decomposition.",
                              "Missing-attribute checks are not solved; scores measure relevance, not answerability.",
                              "Hosted use would require two rerank requests for matched comparisons; API behavior is unchanged."],
              "results": results}
    output.write_text(json.dumps(report, indent=2) + "\n")
    supported = [row for row in results if not row["should_refuse"]]
    unsupported = [row for row in results if row["should_refuse"]]
    print(f"Development source coverage: {sum(row['source_complete_before'] for row in supported)}/{len(supported)} -> {sum(row['source_complete_after'] for row in supported)}/{len(supported)}")
    print(f"Development gate refusals: {sum(row['gate_refused_before'] for row in unsupported)}/{len(unsupported)} -> {sum(row['gate_refused_after'] for row in unsupported)}/{len(unsupported)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-report", type=Path, default=ROOT / "evaluation/evidence-context-challenge-final.json")
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation/evidence-facet-development-experiment.json")
    args = parser.parse_args()
    run(args.candidate_report, args.output)
