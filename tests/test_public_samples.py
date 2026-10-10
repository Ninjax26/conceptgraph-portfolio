"""Public sample provenance and generated PDF contracts (no live services)."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from scripts.build_sample_course import build_course
from scripts.verify_public_samples import normalize, validate_references

ROOT = Path(__file__).resolve().parents[1]


class PublicSampleTests(unittest.TestCase):
    def setUp(self):
        self.course = json.loads((ROOT / "evaluation/technical-course.json").read_text())

    def test_both_static_manifests_match_authored_sources(self):
        for source, manifest in (("course.json", "course.json"), ("technical-course.json", "course-technical.json")):
            authored = json.loads((ROOT / "evaluation" / source).read_text())
            self.assertEqual(authored, json.loads((ROOT / "public/sample" / manifest).read_text()))
            validate_references(authored)

    def test_all_native_pages_contain_reference_text_and_scan_has_no_hidden_text(self):
        for document in self.course["documents"]:
            with fitz.open(ROOT / "public/sample" / document["filename"]) as pdf:
                self.assertEqual(len(pdf), len(document["pages"]))
                for index, expected in enumerate(document["pages"]):
                    if expected.get("extraction_method") == "ocr":
                        self.assertEqual(pdf[index].get_text().strip(), "")
                        self.assertTrue(pdf[index].get_images())
                    else:
                        self.assertIn(normalize(expected["text"]), normalize(pdf[index].get_text()))

    def test_supported_technical_examples_require_multiple_documents(self):
        supported = [case for case in self.course["saved_examples"] if not case.get("should_refuse")]
        self.assertEqual(len(supported), 3)
        for case in supported:
            self.assertGreaterEqual(len({source[0] for source in case["sources"]}), 2)
            self.assertTrue(case["nodes"])
            self.assertTrue(case["relationships"])
            self.assertTrue(all(len(source) == 3 for source in case["sources"]))

    def test_ocr_example_and_refusal_cite_image_only_record(self):
        for index, paragraph in ((1, 1), (3, 2)):
            self.assertIn(["equipment-inspection.pdf", 1, paragraph],
                          self.course["saved_examples"][index]["sources"])

    def test_rejects_invalid_source_page_and_paragraph(self):
        for reference in (("missing.pdf", 1), ("equipment-specification.pdf", 0),
                          ("equipment-specification.pdf", True), ("equipment-specification.pdf", 1, 99)):
            course = copy.deepcopy(self.course)
            course["saved_examples"][0]["sources"][0] = reference
            with self.assertRaises(ValueError):
                validate_references(course)

    def test_rejects_invalid_relationships_and_duplicate_nodes(self):
        for field, value in (("target", "missing-node"), ("type", "INVENTED_TYPE")):
            course = copy.deepcopy(self.course)
            course["saved_examples"][0]["relationships"][0][field] = value
            with self.assertRaises(ValueError):
                validate_references(course)
        course = copy.deepcopy(self.course)
        course["saved_examples"][0]["nodes"].append(course["saved_examples"][0]["nodes"][0])
        with self.assertRaises(ValueError):
            validate_references(course)

    def test_refusal_does_not_draw_an_invented_graph(self):
        refused = self.course["saved_examples"][-1]
        self.assertTrue(refused["should_refuse"])
        self.assertEqual(refused["nodes"], [])
        self.assertEqual(refused["relationships"], [])
        self.assertIn("not a live test", refused["answer"])
        course = copy.deepcopy(self.course)
        course["saved_examples"][-1]["nodes"] = course["saved_examples"][0]["nodes"]
        with self.assertRaises(ValueError):
            validate_references(course)

    def test_builder_regenerates_technical_fixture_in_isolated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build_course(ROOT / "evaluation/technical-course.json", output, "course-technical.json")
            self.assertEqual(json.loads((output / "course-technical.json").read_text()), self.course)
            with fitz.open(output / "equipment-inspection.pdf") as pdf:
                self.assertEqual(pdf[0].get_text().strip(), "")
                self.assertIn("Interpreting the Record", pdf[1].get_text())

    def test_recorded_extraction_report_matches_committed_pdf_bytes(self):
        report = json.loads((ROOT / "evaluation/public-samples-verification.json").read_text())
        self.assertEqual(report["pages_passed"], report["page_count"])
        self.assertEqual(report["page_count"], 24)
        for row in report["results"]:
            content = (ROOT / "public/sample" / row["filename"]).read_bytes()
            self.assertEqual(row["pdf_sha256"], hashlib.sha256(content).hexdigest())
            self.assertTrue(row["passed"])
        scanned = [row for row in report["results"] if row["expected_method"] == "ocr"]
        self.assertEqual(len(scanned), 1)
        self.assertTrue(scanned[0]["scan_is_image_only"])
        self.assertEqual(scanned[0]["actual_method"], "ocr")
