"""Extraction quality and resource limits, using synthetic PDFs and mocked OCR."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymupdf as fitz

from app.core.processing import FailureCategory, classify_failure
from app.services.document_processing_service import DocumentProcessingService
from app.services.parser_service import ExtractedPage, LayoutLine, OCRResult, ParserService


class OCRQualityTests(unittest.TestCase):
    @staticmethod
    def pdf(text="", image=False):
        document = fitz.open()
        page = document.new_page()
        if text:
            page.insert_text((72, 80), text, fontsize=12)
        if image:
            source = fitz.open()
            scan = source.new_page(width=400, height=300)
            scan.insert_text((30, 60), "SCANNED PROCEDURE\nInspect the pump before restarting.")
            page.insert_image(fitz.Rect(72, 200, 472, 500), stream=scan.get_pixmap().tobytes("png"))
            source.close()
        data = document.tobytes()
        document.close()
        return data

    def test_corrupted_native_text_triggers_ocr_despite_large_character_count(self):
        parser = ParserService()
        line = LayoutLine("\ufffd" * 80 + " broken " * 40, 11, False, (72, 80, 300, 120), 792)
        with patch.object(parser, "_layout_lines_from_dict", return_value=[line]), \
             patch.object(parser, "_ocr_page", return_value=OCRResult(
                 "Inspection requires checking the pump before restarting equipment.", 90)) as ocr:
            pages = parser.extract_pages_from_bytes(self.pdf("native placeholder"))
        ocr.assert_called_once()
        self.assertEqual(pages[0].extraction_method, "ocr")

    def test_longer_garbled_ocr_does_not_replace_short_readable_native_text(self):
        parser = ParserService(ocr_min_native_characters=100)
        native = "Inspect the pump."
        with patch.object(parser, "_ocr_page", return_value=OCRResult("x" * 500, 15)):
            pages = parser.extract_pages_from_bytes(self.pdf(native))
        self.assertEqual(pages[0].text, native)
        self.assertEqual(pages[0].extraction_method, "native_text")
        self.assertIn("ocr_did_not_improve_text", pages[0].extraction_warnings)

    def test_mixed_page_retains_native_text_and_indexes_image_text(self):
        native = "EQUIPMENT GUIDE\nNative instructions explain the equipment and its safe use."
        parser = ParserService()
        with patch.object(parser, "_ocr_page", return_value=OCRResult(
            "SCANNED PROCEDURE\nInspect the pump before restarting.", 95)) as ocr:
            pages = parser.extract_pages_from_bytes(self.pdf(native, image=True))
        ocr.assert_called_once()
        self.assertEqual(pages[0].extraction_method, "native_text+ocr")
        self.assertTrue(pages[0].layout_lines)
        chunks = parser.chunk_pages(pages, "course", "upload", "mixed.pdf")
        combined = " ".join(chunk.text for chunk in chunks)
        self.assertIn("Native instructions", combined)
        self.assertIn("Inspect the pump", combined)

    def test_mixed_ocr_does_not_duplicate_native_lines(self):
        native = "Native instructions explain the equipment and its safe use."
        parser = ParserService()
        with patch.object(parser, "_ocr_page", return_value=OCRResult(
            native + "\nInspect the pump before restarting.", 95)):
            pages = parser.extract_pages_from_bytes(self.pdf(native, image=True))
        self.assertEqual(pages[0].text.count(native), 1)

    def test_mixed_page_keeps_native_text_when_ocr_engine_is_missing(self):
        native = "Native instructions explain the equipment and its safe use."
        parser = ParserService()
        with patch.object(parser, "_ocr_page", side_effect=RuntimeError("The OCR engine is unavailable.")):
            pages = parser.extract_pages_from_bytes(self.pdf(native, image=True))
        self.assertEqual(pages[0].text, native)
        self.assertIn("ocr_unavailable", pages[0].extraction_warnings)

    def test_scanned_page_still_fails_clearly_when_engine_is_missing(self):
        parser = ParserService()
        with patch.object(parser, "_ocr_page", side_effect=RuntimeError("The OCR engine is unavailable.")):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                parser.extract_pages_from_bytes(self.pdf())

    def test_ocr_disabled_preserves_native_text_and_skips_image_regions(self):
        parser = ParserService(ocr_enabled=False)
        with patch.object(parser, "_ocr_page") as ocr:
            pages = parser.extract_pages_from_bytes(self.pdf("Readable native content.", image=True))
        ocr.assert_not_called()
        self.assertEqual(pages[0].extraction_method, "native_text")

    def test_tsv_rejects_non_finite_and_out_of_range_confidences(self):
        header = "block_num\tpar_num\tline_num\tconf\ttext\n"
        result = ParserService._parse_tesseract_tsv(header +
            "1\t1\t1\tnan\tNoise\n1\t1\t1\tinf\tNoise\n"
            "1\t1\t1\t101\tNoise\n1\t1\t1\t90\tEvidence\n")
        self.assertEqual(result.text, "Evidence")
        self.assertEqual(result.confidence, 90)

    def test_page_summary_reports_hybrid_pages_and_review_warnings(self):
        pages = [ExtractedPage(1, "native"), ExtractedPage(2, "scan", extraction_method="ocr", ocr_confidence=90),
                 ExtractedPage(3, "mixed", extraction_method="native_text+ocr", ocr_confidence=80,
                               extraction_warnings=("ocr_did_not_improve_text",), ocr_attempts=2)]
        summary = DocumentProcessingService._page_extraction_summary(pages)
        self.assertEqual(summary["hybrid_text_ocr_pages"], 1)
        self.assertEqual(summary["extraction_warning_pages"], 1)
        self.assertEqual(summary["ocr_average_confidence"], 85)

    def test_timeout_is_retryable_and_not_reported_as_a_missing_engine(self):
        category, retryable, message = classify_failure(RuntimeError("The OCR engine timed out while processing a page."))
        self.assertEqual(category, FailureCategory.TIMEOUT_ERROR)
        self.assertTrue(retryable)
        self.assertIn("OCR", message)

    def test_ocr_retry_is_bounded_and_does_not_replace_a_better_first_result(self):
        parser = ParserService(ocr_page_timeout_seconds=10)
        document = fitz.open()
        page = document.new_page()
        run = Mock(side_effect=[OCRResult("Readable inspection procedure.", 50), OCRResult("x" * 200, 10)])
        with patch.object(parser, "_run_tesseract", run):
            result = parser._ocr_page(page)
        document.close()
        self.assertEqual(result.text, "Readable inspection procedure.")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.attempts, 2)

    def test_rendering_large_pages_respects_pixel_budget(self):
        parser = ParserService(ocr_max_render_pixels=1_000_000)
        document = fitz.open()
        page = document.new_page(width=2000, height=3000)
        pixmap, dpi = parser._render_ocr_page(page, dpi=300)
        self.assertLessEqual(pixmap.width * pixmap.height, 1_000_000)
        self.assertLess(dpi, 300)
        document.close()

    def test_reliable_first_ocr_result_skips_retry(self):
        parser = ParserService()
        document = fitz.open()
        page = document.new_page()
        run = Mock(return_value=OCRResult("Inspect the pump before restarting.", 95))
        with patch.object(parser, "_run_tesseract", run):
            result = parser._ocr_page(page)
        document.close()
        run.assert_called_once()
        self.assertEqual(result.attempts, 1)

    def test_disabling_retry_allows_only_one_ocr_call(self):
        parser = ParserService(ocr_retry_enabled=False)
        document = fitz.open()
        page = document.new_page()
        run = Mock(return_value=OCRResult("x" * 100, 10))
        with patch.object(parser, "_run_tesseract", run):
            result = parser._ocr_page(page)
        document.close()
        run.assert_called_once()
        self.assertIn("ocr_confidence_low", result.warnings)

    def test_rejected_scan_is_reported_but_never_indexed(self):
        parser = ParserService()
        with patch.object(parser, "_ocr_page", return_value=OCRResult("x" * 100, 10)):
            pages = parser.extract_pages_from_bytes(self.pdf())
        self.assertEqual(pages[0].extraction_method, "unreadable")
        self.assertIn("ocr_no_reliable_text", pages[0].extraction_warnings)
        self.assertEqual(parser.chunk_pages(pages, "course", "upload"), [])

    def test_quality_signal_accepts_numbers_and_non_latin_text(self):
        for text in ("Limit 12.5 bar at 300 rpm, clearance 0.15 mm.", "日本語の文書には技術情報が含まれています。", "A multilingual résumé uses naïve, café, and μm."):
            with self.subTest(text=text):
                self.assertGreaterEqual(ParserService._text_quality(text).score, ParserService.MIN_TEXT_QUALITY)

    def test_native_text_covering_an_image_skips_duplicate_ocr(self):
        parser = ParserService()
        page = SimpleNamespace(rect=fitz.Rect(0, 0, 100, 100), rotation_matrix=fitz.Matrix(1, 1),
            get_image_info=lambda: [{"bbox": (0, 0, 100, 100)}])
        lines = [LayoutLine("Readable native text already covers this scanned image.", 12, False, (0, 0, 100, 40), 100)]
        self.assertIsNone(parser._image_ocr_region(page, lines))

    def test_small_logos_do_not_trigger_ocr(self):
        parser = ParserService()
        page = SimpleNamespace(rect=fitz.Rect(0, 0, 100, 100), rotation_matrix=fitz.Matrix(1, 1),
            get_image_info=lambda: [{"bbox": (0, 0, 10, 10)}])
        self.assertIsNone(parser._image_ocr_region(page, []))

    def test_mixed_page_timeout_preserves_the_native_layer(self):
        native = "Native instructions explain the equipment and its safe use."
        parser = ParserService()
        with patch.object(parser, "_ocr_page", side_effect=RuntimeError("The OCR engine timed out.")):
            pages = parser.extract_pages_from_bytes(self.pdf(native, image=True))
        self.assertEqual(pages[0].text, native)
        self.assertIn("ocr_timeout", pages[0].extraction_warnings)

    def test_retry_uses_remaining_page_deadline(self):
        parser = ParserService(ocr_page_timeout_seconds=10)
        document = fitz.open()
        page = document.new_page()
        run = Mock(return_value=OCRResult("x" * 100, 10))
        with patch.object(parser, "_run_tesseract", run), \
             patch("app.services.parser_service.time.monotonic", side_effect=[0, 1, 2, 4]):
            result = parser._ocr_page(page)
        document.close()
        self.assertEqual(result.attempts, 2)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 9)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 6)

    def test_sparse_readable_native_text_survives_missing_ocr(self):
        parser = ParserService()
        with patch.object(parser, "_ocr_page", side_effect=RuntimeError("The OCR engine is unavailable.")):
            pages = parser.extract_pages_from_bytes(self.pdf("Appendix"))
        self.assertEqual(pages[0].text, "Appendix")
        self.assertIn("ocr_unavailable", pages[0].extraction_warnings)

    def test_optional_mixed_page_ocr_respects_page_budget_without_losing_native_text(self):
        document = fitz.open(stream=self.pdf("Native instructions explain safe equipment use.", image=True), filetype="pdf")
        original = document.tobytes()
        with fitz.open(stream=original, filetype="pdf") as source:
            document.insert_pdf(source)
        content = document.tobytes()
        document.close()
        parser = ParserService(ocr_max_pages_per_document=1)
        with patch.object(parser, "_ocr_page", return_value=OCRResult("Recovered inspection procedure.", 95)) as ocr:
            pages = parser.extract_pages_from_bytes(content)
        self.assertEqual(len(pages), 2)
        ocr.assert_called_once()
        self.assertIn("ocr_page_budget_reached", pages[1].extraction_warnings)
        self.assertIn("Native instructions", pages[1].text)


if __name__ == "__main__":
    unittest.main()
