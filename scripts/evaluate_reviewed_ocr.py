"""Measure local OCR on source-pinned pages with supplied human transcriptions.

Pass a JSON list of objects containing pdf (relative to manifest), sha256,
page (one based), reference_text, reviewer and reviewed_on (ISO date).
Only include pages the named reviewer actually checked against the scan.
"""

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.parser_service import ParserService
from scripts.evaluate_ocr import edit_distance, normalize


def evaluate(manifest: Path):
    entries = json.loads(manifest.read_text())
    if not isinstance(entries, list):
        raise ValueError("Expected a JSON list of reviewed pages.")
    rows, seen = [], set()
    characters = words = character_errors = word_errors = 0
    parser = ParserService(ocr_enabled=True)
    for entry in entries:
        reference = normalize(entry["reference_text"])
        if not entry["reviewer"].strip() or not reference or not date.fromisoformat(entry["reviewed_on"]):
            raise ValueError("Each reference needs a reviewer, date, and checked transcription.")
        source = (manifest.parent / entry["pdf"]).read_bytes()
        page = entry["page"]
        digest = sha256(source).hexdigest()
        if digest != entry["sha256"] or (digest, page) in seen:
            raise ValueError("Source hash mismatch or duplicate review page.")
        seen.add((digest, page))
        with fitz.open(stream=source, filetype="pdf") as doc, fitz.open() as single:
            if type(page) is not int or not 1 <= page <= len(doc):
                raise ValueError("Review page is outside this PDF.")
            single.insert_pdf(doc, from_page=page - 1, to_page=page - 1)
            extracted = parser.extract_pages_from_bytes(single.tobytes())
        actual = normalize(extracted[0].text if extracted else "")
        ce = edit_distance(reference, actual)
        we = edit_distance(reference.split(), actual.split())
        character_errors += ce
        word_errors += we
        characters += len(reference)
        words += len(reference.split())
        rows.append({"sha256": digest, "page": page, "reviewer": entry["reviewer"], "reviewed_on": entry["reviewed_on"],
                     "method": extracted[0].extraction_method if extracted else "unreadable",
                     "character_error_rate": ce / len(reference), "word_error_rate": we / len(reference.split())})
    return {"reviewed_pages": len(rows), "character_error_rate": character_errors / characters if characters else None,
            "word_error_rate": word_errors / words if words else None, "pages": rows}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("manifest", type=Path)
    print(json.dumps(evaluate(cli.parse_args().manifest), indent=2))
