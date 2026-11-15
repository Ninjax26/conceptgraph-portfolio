"""Prepare prompt-bound oracle-context requests offline; never executes inference."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.answer_contract import REFUSAL_TEXT
from app.services.synthesis_service import SynthesisService
from scripts.evaluate_quality import fixture_pages, keyed, load_quality_dataset
from scripts.quality_metrics import normalized

MODEL = "openai/gpt-oss-20b"
OUTPUT_CAP = 1200


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_cases(cases, pages, *, baseline=None):
    """Keep known contexts identical; construct fresh contexts from authored pages."""
    prior = keyed(baseline) if baseline is not None else None
    if prior is not None and set(prior) != set(keyed(cases)):
        raise ValueError("Baseline IDs must exactly match the known cases.")
    result = []
    for case in cases:
        if len(case["sources"]) > 5:
            raise ValueError("More than five sources requires a different evaluation design.")
        if prior is not None:
            if prior[case["id"]].get("question") != case["question"]:
                raise ValueError("Baseline question text no longer matches the case.")
            sources = prior[case["id"]]["response"]["sources"]
            locations = [[s.get("document_name"), s.get("page_number")] for s in sources]
            if locations != case["sources"]:
                raise ValueError("Baseline source order/pages no longer match the case.")
        else:
            sources = [{"source_id": f"source-{index}", "document_id": f"quality-{name}",
                        "document_name": name, "page_number": page,
                        "supporting_passage": pages.get((name, page), "")}
                       for index, (name, page) in enumerate(case["sources"], 1)]
        for source in sources:
            page = pages.get((source["document_name"], source["page_number"]))
            passage = source.get("supporting_passage")
            if (not isinstance(page, str) or not isinstance(passage, str) or not passage.strip()
                    or len(passage) > 900 or normalized(passage) not in normalized(page)):
                raise ValueError("Missing, oversized or nonliteral authored source passage.")
        row = {"id": case["id"], "question": case["question"], "sources": sources}
        if not sources:
            if not case["expected_refusal"]:
                raise ValueError("A supported case requires evidence.")
            row.update(action="local_refusal_no_request", local_answer=REFUSAL_TEXT)
        else:
            request = {"model": MODEL, "max_completion_tokens": OUTPUT_CAP,
                       "messages": [{"role": "system", "content": SynthesisService._system_prompt()},
                                    {"role": "user", "content": SynthesisService._user_prompt(case["question"], [], sources)}]}
            row.update(action="proposed_hosted_request", request=request, request_sha256=digest(request))
        result.append(row)
    return result


def summarize(rows):
    requests = [row["request"] for row in rows if "request" in row]
    characters = sum(len(message["content"]) for request in requests for message in request["messages"])
    return {"cases": len(rows), "maximum_hosted_requests": len(requests),
            "maximum_completion_tokens_per_request": OUTPUT_CAP,
            "maximum_completion_tokens_total": len(requests) * OUTPUT_CAP,
            "prompt_characters": characters, "rough_input_tokens_characters_divided_by_four": (characters+3)//4,
            "retries": 0, "failover": False, "stop_on_quota_or_provider_failure": True,
            "execution_status": "proposed_only_not_enforced_by_this_offline_tool"}


def write_new(path, value):
    # Exclusive creation preserves prior manifests and captures even on reruns.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def prepare():
    known_path = ROOT / "evaluation/quality-cases.json"
    heldout_path = ROOT / "evaluation/quality-source-fidelity-heldout-v1.json"
    captured_path = ROOT / "evaluation/answer-generation-current.responses.json"
    original_path = ROOT / "evaluation/answer-generation-preflight.json"
    known = load_quality_dataset(known_path)
    heldout = load_quality_dataset(heldout_path)
    pages, corpus_hashes = fixture_pages()
    captures = json.loads(captured_path.read_text())["captures"]
    originals = keyed(json.loads(original_path.read_text())["cases"])
    if set(originals) != set(keyed(captures)):
        raise ValueError("Original request and captured IDs must exactly match.")
    captures = [dict(row, question=originals[row["id"]]["question"]) for row in captures]
    groups = {"known_comparison": build_cases(known["cases"], pages, baseline=captures),
              "fresh_candidate_only": build_cases(heldout["cases"], pages)}
    paths = [known_path, heldout_path, captured_path, original_path, Path(__file__),
             ROOT / "app/services/synthesis_service.py", ROOT / "app/services/answer_contract.py",
             ROOT / "scripts/evaluate_quality.py"]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return {"status": "offline_prepared_awaiting_new_explicit_quota_approval",
            "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "provider": "groq", "source_sha256": hashes, "corpus_sha256": corpus_hashes,
            "groups": groups, "budgets": {key: summarize(rows) for key, rows in groups.items()},
            "combined_budget": summarize([row for rows in groups.values() for row in rows]),
            "provider_calls_made": 0, "generated_answers": 0, "human_reviews": 0,
            "limitations": ["Oracle sources isolate synthesis; this does not test retrieval.",
                            "Known baseline is one historical stochastic output, not a controlled same-time model trial.",
                            "Fresh cases have no baseline outputs and assistant-authored checklists, not human gold.",
                            "Checklists and baseline answers are excluded from request messages.",
                            "No candidate semantic improvement, token usage, cost or latency measured.",
                            "Requires a separately approved runner with durable attempt journal, max_retries=0, no backup and these caps.",
                            "Validate all source and request hashes immediately before any future execution."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare()
    write_new(args.output, manifest)
    print(json.dumps(manifest["combined_budget"], indent=2))
