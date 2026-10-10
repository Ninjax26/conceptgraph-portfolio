"""Verify authored public fixtures with local native extraction and Tesseract.

No databases, providers, authentication, or production data are accessed.
This checks source text and provenance, not LLM answer or retrieval quality.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.parser_service import ParserService


def normalize(text):
    return " ".join(text.casefold().split())


def validate_references(course):
    documents = {doc["filename"]: doc for doc in course["documents"]}

    def check(reference):
        filename, page, *paragraph = reference
        document = documents.get(filename)
        if document is None or type(page) is not int or not 1 <= page <= len(document["pages"]):
            raise ValueError(f"Unknown source page: {reference}")
        if paragraph and (len(paragraph) != 1 or type(paragraph[0]) is not int or
                          not 1 <= paragraph[0] <= len(document["pages"][page - 1]["text"].split("\n\n"))):
            raise ValueError(f"Unknown source paragraph: {reference}")

    for example in course["saved_examples"]:
        for source in example["sources"]:
            check(source)
        nodes = example.get("nodes", [])
        ids = {node["id"] for node in nodes}
        if len(ids) != len(nodes):
            raise ValueError("Duplicate illustrated node ID")
        for node in nodes:
            check(node["source"])
        for edge in example.get("relationships", []):
            if edge["source"] not in ids or edge["target"] not in ids:
                raise ValueError("Unknown illustrated relationship endpoint")
            if edge["type"] not in {"PREREQUISITE_OF", "PART_OF", "EXPLAINS", "RELATED_TO", "CAUSES", "APPLIES_TO"}:
                raise ValueError("Unsupported illustrated relationship type")
            check(edge["source_reference"])
        if example.get("should_refuse") and (nodes or example.get("relationships") or example["concepts"]):
            raise ValueError("Refusal example must not invent an answer graph")


def verify_course(source, manifest, parser):
    course = json.loads(source.read_text())
    if course != json.loads((ROOT / "public/sample" / manifest).read_text()):
        raise ValueError(f"Regenerate the static manifest: {manifest}")
    validate_references(course)
    results = []
    for document in course["documents"]:
        content = (ROOT / "public/sample" / document["filename"]).read_bytes()
        pages = parser.extract_pages_from_bytes(content)
        if len(pages) != len(document["pages"]):
            raise ValueError(f"Page count mismatch: {document['filename']}")
        with fitz.open(stream=content, filetype="pdf") as pdf:
            for expected, extracted in zip(document["pages"], pages, strict=True):
                paragraphs = expected["text"].split("\n\n")
                recovered = [normalize(paragraph) in normalize(extracted.text) for paragraph in paragraphs]
                method = expected.get("extraction_method", "native_text")
                scan_is_image_only = not pdf[extracted.page_number - 1].get_text().strip() if method == "ocr" else None
                results.append({
                    "course": course["name"], "filename": document["filename"],
                    "pdf_sha256": hashlib.sha256(content).hexdigest(), "page": extracted.page_number,
                    "expected_method": method, "actual_method": extracted.extraction_method,
                    "scan_is_image_only": scan_is_image_only,
                    "reference_paragraphs": len(paragraphs), "paragraphs_recovered": sum(recovered),
                    "ocr_confidence": extracted.ocr_confidence, "ocr_attempts": extracted.ocr_attempts,
                    "warnings": list(extracted.extraction_warnings),
                    "passed": all(recovered) and extracted.extraction_method == method and scan_is_image_only is not False,
                })
    return results


def main():
    engine = subprocess.run(["tesseract", "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    parser = ParserService(ocr_enabled=True, ocr_language="eng", ocr_dpi=200,
                           ocr_min_native_characters=40, ocr_max_pages_per_document=10,
                           ocr_page_timeout_seconds=20, ocr_min_confidence=55)
    results = verify_course(ROOT / "evaluation/course.json", "course.json", parser)
    results += verify_course(ROOT / "evaluation/technical-course.json", "course-technical.json", parser)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "engine": engine,
              "scope": "Authored English public fixtures; exact casefolded, whitespace-normalized paragraph recovery. Not a live retrieval, graph, answer, safety, or real-document accuracy benchmark.",
              "pages_passed": sum(row["passed"] for row in results), "page_count": len(results),
              "results": results}
    output = ROOT / "evaluation/public-samples-verification.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['pages_passed']}/{report['page_count']} pages passed; report: {output}")
    if report["pages_passed"] != report["page_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
