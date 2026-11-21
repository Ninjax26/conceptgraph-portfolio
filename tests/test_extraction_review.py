from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

from app.services.extraction_review import inspect_page
from app.services.parser_service import ParserService, OCRResult
from scripts.evaluate_reviewed_ocr import evaluate


def column_fixture():
    # Alternate content-stream order deliberately; geometric columns carry order.
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=760)
        page.insert_text((44, 75), "Inspection permissions and responsibilities across the two local teams", fontsize=16)
        for y, left, right in [(150, "Alice reviews intake forms.", "Bill inspects lab stock."),
                               (176, "Alice checks incoming receipts.", "Bill records inventory changes."),
                               (202, "Alice assigns a reference.", "Bill signs the stock record.")]:
            page.insert_text((48, y), left)
            page.insert_text((320, y), right)
        return doc.tobytes()


def table_fixture():
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=760)
        for x in (55, 260, 530):
            page.draw_line((x, 120), (x, 210))
        for y in (120, 150, 180, 210):
            page.draw_line((55, y), (530, y))
        rows = [("Part number", "Bins available"), ("Valve B", "12"), ("Bolt Z", "24")]
        for idx, row in enumerate(rows):
            for x, value in zip((65, 270), row):
                page.insert_text((x, 140 + idx * 30), value)
        return doc.tobytes()


class LayoutTests(unittest.TestCase):
    def test_native_columns_keep_expected_order_and_bounded_chunk_boxes(self):
        parser = ParserService(ocr_enabled=False, chunk_size=7, chunk_overlap=2)
        page = parser.extract_pages_from_bytes(column_fixture())[0]
        lines = page.text.splitlines()
        self.assertEqual([line.split()[0] for line in lines[1:]], ["Alice"] * 3 + ["Bill"] * 3)
        chunks = parser.chunk_pages([page], "course", "up")
        for chunk in chunks:
            boxes = json.loads(chunk.metadata["source_lines_pdf_points"])
            self.assertEqual(chunk.metadata["page_number"], 1)
            self.assertEqual([box[0] for box in boxes], sorted(box[0] for box in boxes))
            for order, left, top, right, bottom in boxes:
                self.assertLess(left, right)
                self.assertLess(top, bottom)
                self.assertIn(page.layout_lines[order - 1].text.split()[0], page.text)
        self.assertLess(len(json.loads(chunks[0].metadata["source_lines_pdf_points"])), len(lines))

    def test_ruled_table_pairs_headers_and_values_in_row_order(self):
        parser = ParserService(ocr_enabled=False)
        page = parser.extract_pages_from_bytes(table_fixture())[0]
        self.assertEqual([line.kind for line in page.layout_lines], ["table_row"] * 3)
        self.assertEqual(page.text.splitlines(), ["Part number | Bins available", "Valve B | 12", "Bolt Z | 24"])
        self.assertIn("Bolt Z | 24", parser.chunk_pages([page], "course", "up")[0].text)

    def test_rejected_scan_visible_for_review_and_excluded_from_evidence(self):
        with fitz.open() as doc:
            doc.new_page(width=300, height=200)
            source = doc.tobytes()
        parser = ParserService()
        with patch.object(parser, "_ocr_page", return_value=OCRResult("Illegible stock ???", 21)):
            review = inspect_page(source, 1, parser)
            rejected = parser.extract_pages_from_bytes(source)
        self.assertEqual(review["extracted_text"], "")
        self.assertEqual(review["uncertain_text"], "Illegible stock ???")
        self.assertTrue(review["image"].startswith("data:image/png;base64,"))
        self.assertEqual(review["source_sha256"], sha256(source).hexdigest())
        self.assertEqual(parser.chunk_pages(rejected, "course", "up"), [])

    def test_correction_preserves_neighbors_and_never_reuses_old_boxes(self):
        with fitz.open(stream=column_fixture(), filetype="pdf") as doc:
            extra = doc.new_page()
            extra.insert_text((60, 100), "The exact stock count was eleven.")
            source = doc.tobytes()
        parser = ParserService(ocr_enabled=False)
        old = parser.chunk_pages(parser.extract_pages_from_bytes(source), "course", "up")
        new = parser.chunk_pages(parser.extract_pages_from_bytes(source, corrections={2: "The exact stock count was twelve."}), "course", "up")
        self.assertEqual(old[:-1], new[:-1])
        self.assertEqual(old[-1].id, new[-1].id)
        self.assertEqual(new[-1].text, "The exact stock count was twelve.")
        self.assertEqual(new[-1].metadata["extraction_method"], "reviewer_corrected")
        self.assertNotIn("source_lines_pdf_points", new[-1].metadata)

    @unittest.skipUnless(shutil.which("tesseract"), "requires local Tesseract")
    def test_real_scan_word_boxes_align_with_original_source(self):
        with fitz.open() as source, fitz.open() as scanned:
            page = source.new_page(width=400, height=200)
            page.insert_text((40, 90), "Inspect the intake valve daily.", fontsize=16)
            scan = scanned.new_page(width=400, height=200)
            scan.insert_image(scan.rect, stream=page.get_pixmap(dpi=190).tobytes("png"))
            scan.set_rotation(0)
            parser = ParserService(ocr_enabled=True)
            data = inspect_page(scanned.tobytes(), 1, parser)
        self.assertEqual(data["extracted_text"].strip(), "Inspect the intake valve daily.")
        self.assertTrue(data["blocks"])
        for block in data["blocks"]:
            left, top, right, bottom = block["bbox"]
            self.assertGreaterEqual(left, 0)
            self.assertLessEqual(right, 400)
            self.assertTrue(65 < top < bottom < 100)

    def test_empty_human_reference_manifest_remains_unmeasured(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "references.json"
            manifest.write_text("[]")
            self.assertEqual(evaluate(manifest), {
                "reviewed_pages": 0, "character_error_rate": None, "word_error_rate": None, "pages": []})
