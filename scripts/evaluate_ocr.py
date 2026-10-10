"""Compare current extraction with a Git revision on authored English PDF fixtures.

Runs local Tesseract only. Reference text is known by construction, not independently
labelled real-world ground truth. No providers, databases, or production data are used.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import types

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.parser_service import ParserService


REFERENCE = "\n".join([
    "EQUIPMENT INSPECTION PROCEDURE",
    "Record the equipment identifier and the inspection date.",
    "Check the maintenance log before starting the inspection.",
    "Inspect the pump housing for visible damage and corrosion.",
    "Record pressure in bar and shaft speed in revolutions per minute.",
    "Compare measured values with the approved specification.",
    "Check connections and record any leaks or loose fittings.",
    "Inspect seals and record evidence of wear or contamination.",
    "Verify that the emergency stop control is accessible.",
    "Record observations and attach a source reference for each finding.",
    "Ask the responsible engineer to review unexpected measurements.",
    "Keep the equipment stopped until the inspection is approved.",
    "This document is an authored software test fixture.",
])


def normalize(text):
    return " ".join(text.casefold().split())


def edit_distance(expected, actual):
    previous = list(range(len(actual) + 1))
    for index, value in enumerate(expected, start=1):
        current = [index]
        for other_index, other in enumerate(actual, start=1):
            current.append(min(current[-1] + 1, previous[other_index] + 1,
                               previous[other_index - 1] + (value != other)))
        previous = current
    return previous[-1]


def make_fixtures():
    source = fitz.open()
    page = source.new_page(width=612, height=792)
    page.insert_text((48, 90), REFERENCE, fontsize=12, lineheight=1.7)
    fixtures = [{"name": "native", "pdf": source.tobytes(), "reference": REFERENCE}]
    for name, dpi, rotation in (
        ("clean_scan", 180, 0), ("rotated_90_scan", 180, 90),
        ("rotated_180_scan", 180, 180), ("low_resolution_scan", 85, 0),
        ("slightly_skewed_scan", 150, 3),
    ):
        pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72).prerotate(rotation), alpha=False)
        document = fitz.open()
        scan = document.new_page(width=pixmap.width * 72 / dpi, height=pixmap.height * 72 / dpi)
        scan.insert_image(scan.rect, stream=pixmap.tobytes("png"))
        fixtures.append({"name": name, "pdf": document.tobytes(), "reference": REFERENCE})
        document.close()
    document = fitz.open()
    mixed = document.new_page(width=612, height=900)
    native = "NATIVE EQUIPMENT GUIDE\nRead the following scanned inspection procedure before operating equipment."
    mixed.insert_text((48, 55), native, fontsize=11)
    mixed.insert_image(fitz.Rect(48, 140, 564, 850), stream=page.get_pixmap(dpi=180).tobytes("png"))
    fixtures.append({"name": "mixed_native_image", "pdf": document.tobytes(), "reference": native + "\n" + REFERENCE})
    document.close()
    document = fitz.open()
    corrupt = document.new_page(width=612, height=792)
    corrupt.insert_image(corrupt.rect, stream=page.get_pixmap(dpi=180).tobytes("png"))
    corrupt.insert_text((48, 45), "x" * 300, fontsize=1, render_mode=3)
    fixtures.append({"name": "corrupted_hidden_text", "pdf": document.tobytes(), "reference": REFERENCE})
    document.close()
    source.close()
    return fixtures


def load_baseline(revision):
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", revision) or revision.startswith("-"):
        raise ValueError("Use a simple Git revision or commit SHA.")
    source = subprocess.run(["git", "show", f"{revision}:app/services/parser_service.py"],
                            cwd=ROOT, check=True, capture_output=True, text=True).stdout
    module = types.ModuleType("conceptgraph_ocr_baseline")
    sys.modules[module.__name__] = module
    exec(compile(source, "<local Git OCR baseline>", "exec"), module.__dict__)
    return module.ParserService


def measure(parser, fixture):
    started = time.perf_counter()
    try:
        pages = parser.extract_pages_from_bytes(fixture["pdf"])
        text = normalize("\n".join(page.text for page in pages))
        error = None
    except Exception as exc:
        pages, text, error = [], "", type(exc).__name__
    elapsed = round(time.perf_counter() - started, 3)
    expected = normalize(fixture["reference"])
    return {
        "cer": round(edit_distance(expected, text) / len(expected), 4),
        "wer": round(edit_distance(expected.split(), text.split()) / len(expected.split()), 4),
        "seconds": elapsed,
        "extraction_methods": [page.extraction_method for page in pages],
        "warnings": [warning for page in pages for warning in getattr(page, "extraction_warnings", ())],
        "error": error,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="HEAD", help="Trusted local Git revision for the baseline parser.")
    parser.add_argument("--output", default="evaluation/ocr-quality-current")
    args = parser.parse_args()
    version = subprocess.run(["tesseract", "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    baseline_class = load_baseline(args.baseline)
    common = dict(ocr_enabled=True, ocr_language="eng", ocr_dpi=200,
                  ocr_min_native_characters=40, ocr_max_pages_per_document=50,
                  ocr_page_timeout_seconds=20)
    results = []
    for fixture in make_fixtures():
        row = {"fixture": fixture["name"], "baseline": measure(baseline_class(**common), fixture),
               "current": measure(ParserService(**common), fixture)}
        results.append(row)
        print(json.dumps(row), flush=True)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "tesseract": version,
              "baseline_revision": args.baseline, "fixture_count": len(results),
              "normalization": "casefold and collapse whitespace; punctuation retained",
              "scope": "Authored English fixtures; not a real-document or retrieval-quality benchmark.",
              "results": results}
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# OCR extraction check", "", report["scope"], "", f"Engine: {version}.", "",
             "CER/WER are edit-distance error rates (lower is better). Text is casefolded and whitespace normalized; punctuation is retained.", "",
             "| Fixture | Baseline CER | Current CER | Baseline WER | Current WER | Baseline seconds | Current seconds |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in results:
        b, c = row["baseline"], row["current"]
        lines.append(f"| {row['fixture']} | {b['cer']:.2%} | {c['cer']:.2%} | {b['wer']:.2%} | {c['wer']:.2%} | {b['seconds']} | {c['seconds']} |")
    lines.extend(["", "These results do not establish OCR accuracy on Japanese documents, handwriting, tables, CAD drawings, or production scans.",
                  "Recognition confidence and the text corruption score are diagnostics, not calibrated accuracy probabilities."])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
