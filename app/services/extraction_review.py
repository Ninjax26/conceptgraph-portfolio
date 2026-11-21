"""Page-level review against the original raster, with bounded local rendering."""

from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
import math

import pymupdf as fitz

from app.services.parser_service import ParserService


@dataclass(frozen=True)
class Correction:
    page_number: int
    source_sha256: str
    task_id: str
    revision: int
    text: str


class CorrectionConflict(ValueError):
    pass


def inspect_page(source: bytes, page_number: int, parser: ParserService):
    with fitz.open(stream=source, filetype="pdf") as doc:
        if not 1 <= page_number <= len(doc):
            raise IndexError("Page number is outside this PDF.")
        page = doc[page_number - 1]
        scale = min(1.6, 1500 / max(page.rect.width, page.rect.height))
        raster = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = b64encode(raster.tobytes("png")).decode("ascii")
        with fitz.open() as single:
            single.insert_pdf(doc, from_page=page_number - 1, to_page=page_number - 1)
            try:
                extracted = next(iter(parser.extract_pages_from_bytes(single.tobytes())), None)
                warnings = list(extracted.extraction_warnings) if extracted else ["no_extracted_text"]
            except (RuntimeError, ValueError):
                # The original page is still reviewable when OCR is unavailable.
                extracted, warnings = None, ["extraction_unavailable"]
        blocks = []
        if extracted:
            for line in (*extracted.layout_lines, *extracted.ocr_lines):
                rect = fitz.Rect(line.bbox) * page.rotation_matrix
                if all(math.isfinite(value) for value in rect):
                    blocks.append({"order": len(blocks) + 1, "text": line.text, "kind": line.kind,
                                   "bbox": list(rect)})
        return {"page_number": page_number, "page_count": len(doc), "image": f"data:image/png;base64,{image}",
                "width": page.rect.width, "height": page.rect.height, "source_sha256": sha256(source).hexdigest(),
                "extracted_text": extracted.text if extracted else "", "uncertain_text": extracted.review_text if extracted else "",
                "warnings": warnings, "blocks": blocks}
