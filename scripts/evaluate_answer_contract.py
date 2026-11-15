"""Offline authored answer-format checks; no inference or semantic scoring."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.synthesis_service import InvalidAnswerError, SynthesisService
from app.services.answer_contract import citation_references


def baseline_service(revision):
    if not re.fullmatch(r"[0-9a-f]{7,40}", revision):
        raise ValueError("Use a trusted local hexadecimal commit ID.")
    source = subprocess.check_output(["git", "show", f"{revision}:app/services/synthesis_service.py"], cwd=ROOT, text=True)
    module = ModuleType("baseline_answer_synthesis")
    exec(compile(source, "baseline:synthesis_service", "exec"), module.__dict__)
    return module.SynthesisService, module.InvalidAnswerError, hashlib.sha256(source.encode()).hexdigest()


def evaluate_cases(service, invalid_error, cases, *, legacy=False):
    results = []
    for case in cases:
        try:
            args = (case["answer"], case["question"]) if legacy else (case["answer"], case["question"], case["sources"])
            service._validate_answer(*args)
            accepted, reason = True, None
        except invalid_error as exc:
            accepted, reason = False, str(exc)
        results.append({"id": case["id"], "expected_valid": case["expected_valid"], "accepted": accepted, "reason": reason})
    valid = [case for case in results if case["expected_valid"]]
    invalid = [case for case in results if not case["expected_valid"]]
    return {"valid_count": len(valid), "valid_accepted": sum(case["accepted"] for case in valid),
            "invalid_count": len(invalid), "invalid_rejected": sum(not case["accepted"] for case in invalid), "cases": results}


def fallback_checks(service):
    sources = [{"document_name": "authored.pdf", "page_number": i,
                "supporting_passage": f"Distinct evidence number {i}."} for i in range(1, 6)]
    answer = service._grounded_evidence_fallback(sources)
    tail = "context " * 104 + "input voltage is 24 V DC."
    tail_answer = service._grounded_evidence_fallback([dict(sources[0], supporting_passage=tail)])
    refs = citation_references(answer, sources)
    return {"accepted_source_count": len(sources),
            "excerpts_retained": sum(source["supporting_passage"] in answer for source in sources),
            "source_indices": [ref["index"] for ref in refs],
            "valid_references": sum(ref["index_valid"] and ref["page_valid"] for ref in refs),
            "late_fact_retained": "input voltage is 24 V DC." in tail_answer,
            "fallback_characters": len(answer), "tail_passage_characters": len(tail)}


def run(args):
    dataset = ROOT / "evaluation/answer-contract-cases-v1.json"
    cases = [case for case in json.loads(dataset.read_text())["cases"] if case["split"] == args.split]
    baseline, invalid_error, before_hash = baseline_service(args.baseline_revision)
    report = {"split": args.split, "baseline_revision": args.baseline_revision,
              "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
              "baseline_synthesis_sha256": before_hash,
              "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                  ["app/services/answer_contract.py", "app/services/synthesis_service.py", "scripts/quality_metrics.py", "scripts/evaluate_answer_contract.py"]},
              "before": evaluate_cases(baseline, invalid_error, cases, legacy=True),
              "after": evaluate_cases(SynthesisService, InvalidAnswerError, cases),
              "fallback_before": fallback_checks(baseline), "fallback_after": fallback_checks(SynthesisService),
              "scope": "Authored outputs and deterministic fallback, not real LLM generations or human semantic review.",
              "semantic_accuracy": None, "human_reviewed_answers": 0,
              "limits": ["Valid labels do not prove claim support or completeness.",
                         "Canonical refusal is structurally allowed; semantic refusal correctness is unmeasured.",
                         "Existing provider backup may be used on newly detected invalid answers; no new retry or judge request is added."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"split": args.split, "before": {k:v for k,v in report["before"].items() if k != "cases"},
                      "after": {k:v for k,v in report["after"].items() if k != "cases"},
                      "fallback_before": report["fallback_before"], "fallback_after": report["fallback_after"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["development", "held_out"], default="development")
    parser.add_argument("--baseline-revision", default="8de7bebaebd1cecef48b2a3789cb98b3dde11f60")
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
