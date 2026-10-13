"""Reproducible offline checkpoint contracts, not graph semantic accuracy.

No PostgreSQL, Qdrant, Neo4j, storage or hosted provider calls. A fresh Python
subprocess verifies serialization/reuse; it is not an ingestion-worker benchmark.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_graph_checkpoint_compatibility.GraphCheckpointCompatibilityTests")
    names = [test.id().rsplit(".", 1)[-1] for test in suite]
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    unsuccessful = {test.id().rsplit(".", 1)[-1] for test, _ in result.failures + result.errors + result.skipped}
    sources = (
        "app/services/ingestion_service.py", "app/services/document_processing_service.py",
        "app/schemas/extraction.py", "tests/test_graph_checkpoint_compatibility.py",
        "scripts/evaluate_graph_checkpoint_compatibility.py",
    )
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "offline synthetic checkpoint contract checks",
        "passed": result.wasSuccessful(), "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
        "hosted_provider_calls": 0, "production_test_data_mutations": 0,
        "tests": [{"name": name, "passed": name not in unsuccessful} for name in names],
        "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in sources},
        "limits": [
            "Model outputs are authored fixtures; no semantic correctness score or actual quota measurement.",
            "Fresh subprocess checks Python restart/serialization only, not split API/worker operation or hard-kill recovery.",
            "Per-upload checkpoints only; cross-upload caching and automatic graph-only continuation remain pending.",
            "Existing unversioned checkpoint rows are retained but not reused; a requested rebuild may need bounded fresh calls.",
        ],
    }
    directory = ROOT / "evaluation"
    (directory / "graph-checkpoint-compatibility-current.json").write_text(
        json.dumps(report, indent=2) + "\n")
    rows = "\n".join(f"| `{test['name']}` | {'Pass' if test['passed'] else 'Fail/skip'} |" for test in report["tests"])
    limits = "\n".join(f"- {item}" for item in report["limits"])
    (directory / "graph-checkpoint-compatibility-current.md").write_text(
        f"# Graph checkpoint compatibility — offline checks\n\nGenerated: {report['generated_at_utc']}.\n\n"
        f"Command: `.venv/bin/python scripts/evaluate_graph_checkpoint_compatibility.py`.\n\n"
        f"{result.testsRun - len(unsuccessful)}/{result.testsRun} contract checks passed. "
        "Hosted provider calls: **0**. Production test-data mutations: **0**.\n\n"
        f"| Check | Result |\n| --- | --- |\n{rows}\n\n## Limits\n\n{limits}\n\n"
        "Runtime/test SHA-256 hashes are in the adjacent JSON report. These are structural checks, not human-reviewed graph accuracy.\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
