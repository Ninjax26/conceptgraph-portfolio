import hashlib
import unittest
from unittest.mock import patch

import pymupdf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.endpoints.revisions import router
from app.core.database import get_postgres_session
from app.models.document_upload import Base, Course, DocumentUpload
from app.models.revision_pair import RevisionPair
from app.services.revision_service import ComparisonUnavailable, Section, compare_sections, extract_sections


def pdf(pages):
    with pymupdf.open() as doc:
        for title, lines in pages:
            page = doc.new_page()
            page.insert_text((48, 64), title, fontname="hebo", fontsize=18)
            for index, line in enumerate(lines):
                page.insert_text((48, 100 + 18 * index), line, fontsize=11)
        return doc.tobytes()


class RevisionDiffTest(unittest.TestCase):
    def test_renamed_and_repaginated_with_table_change(self):
        old = [Section("1.1", 1, "Grading", "Lab | 10%\nExam | 90%"),
               Section("2.1", 2, "Books", "Bring the first edition.")]
        new = [Section("1.1", 1, "Reading", "Bring the first edition."),
               Section("4.1", 4, "Grading", "Lab | 20%\nExam | 80%")]
        result = compare_sections(old, new, {})
        self.assertEqual(result["counts"], {"modified": 1, "renamed": 1})
        self.assertEqual(result["changes"][0]["later"]["page"], 4)
        self.assertIn("-Lab | 10%", result["changes"][0]["diff"])
        self.assertIn("+Exam | 80%", result["changes"][0]["diff"])
        self.assertEqual(result, compare_sections(old, new, {}))

    def test_missing_and_ambiguous_headings_wait_for_review(self):
        old = [Section("1.1", 1, "Policy", "Notify the chair."),
               Section("2.1", 2, "Policy", "Bring old notes.")]
        new = [Section("3.1", 3, "Policy", "Notify the dean."),
               Section("4.1", 4, "New module", "Review poems.")]
        result = compare_sections(old, new, {})
        self.assertEqual(len(result["pending"]), 2)
        self.assertFalse(result["counts"])
        result = compare_sections(old, new, {"1.1": "3.1", "2.1": None})
        self.assertEqual(result["counts"], {"modified": 1, "removed": 1, "added": 1})
        self.assertEqual(result["changes"][0]["basis"], "reviewed")
        with self.assertRaises(ComparisonUnavailable):
            compare_sections(old, new, {"1.1": "3.1", "2.1": "3.1"})

    def test_real_pdf_table_diff_preserves_cell_association(self):
        def table(days):
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((48, 60), "Loan terms", fontsize=18, fontname="hebo")
                for y in (80, 110, 140):
                    page.draw_line((48, y), (350, y))
                for x in (48, 200, 350):
                    page.draw_line((x, 80), (x, 140))
                for x, y, value in [(55, 100, "Item"), (207, 100, "Duration"),
                                    (55, 130, "Camera"), (207, 130, days)]:
                    page.insert_text((x, y), value)
                return doc.tobytes()
        old, _ = extract_sections(table("30 days"), None)
        new, _ = extract_sections(table("7 days"), None)
        diff = compare_sections(old, new, {})["changes"][0]["diff"]
        self.assertIn("-Camera | 30 days", diff)
        self.assertIn("+Camera | 7 days", diff)

    def test_blank_page_is_flagged_and_hash_is_checked(self):
        contents = pdf([("Rules", ["Attendance is mandatory."]), ("", [])])
        sections, flagged = extract_sections(contents, hashlib.sha256(contents).hexdigest())
        self.assertTrue(any("Attendance" in item.text for item in sections))
        self.assertIn(2, flagged)
        with self.assertRaises(ComparisonUnavailable):
            extract_sections(contents, "1" * 64)


class AsyncTestSession:
    """Run the route's real SQL against an isolated in-memory schema."""
    def __init__(self, session):
        self.session = session

    async def execute(self, stmt):
        return self.session.execute(stmt)

    async def rollback(self):
        self.session.rollback()

    async def commit(self):
        self.session.commit()


class RevisionApiTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine, tables=[Course.__table__, DocumentUpload.__table__, RevisionPair.__table__])
        self.addCleanup(engine.dispose)
        self.session = Session(engine)
        self.addCleanup(self.session.close)
        self.sources = {
            "a": pdf([("Assessment rules", ["The exam is open book."]),
                      ("Office hours", ["Come Wednesday to the library."])]),
            "b": pdf([("Assessment rules", ["The exam is closed book."]),
                      ("Weekly meeting", ["Visit the library on Friday."])]),
        }
        self.session.add_all([Course(id=k, normalized_name=k, display_name=k) for k in ["alpha", "beta"]])
        self.session.flush()
        for key, course, stage in [("a", "alpha", "READY"), ("b", "alpha", "READY"),
                                   ("other", "beta", "READY"), ("busy", "alpha", "UPLOADED"),
                                   ("deleted", "alpha", "READY")]:
            content = self.sources.get(key)
            self.session.add(DocumentUpload(upload_id=key, task_id=f"task-{key}", course_uuid=course,
                course_id=course, storage_key=key, original_filename="syllabus.pdf", week_number=1,
                content_hash=hashlib.sha256(content).hexdigest() if content else None,
                status="ready" if stage == "READY" else "active", stage=stage, processed_chunk_count=2,
                result_json={"deletion_requested": True} if key == "deleted" else {}))
        self.session.commit()
        app = FastAPI()
        app.include_router(router)
        async def get_session():
            try:
                yield AsyncTestSession(self.session)
            finally:
                self.session.rollback()
        app.dependency_overrides[get_postgres_session] = get_session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        mock = patch("app.api.endpoints.revisions.storage_service.get_bytes", side_effect=self.sources.__getitem__)
        self.reader = mock.start()
        self.addCleanup(mock.stop)
        self.url = "/api/v1/courses/alpha/revisions"

    def compare(self, new="b", review=None):
        return self.client.post(self.url + "/compare", json={"earlier_id": "a", "later_id": new, "review": review})

    def test_invalid_uploads_never_read_storage(self):
        self.assertEqual({d["id"] for d in self.client.get(self.url).json()["documents"]}, {"a", "b"})
        for key in ["other", "busy", "deleted", "absent"]:
            self.assertEqual(self.compare(key).status_code, 404)
        self.assertEqual(self.compare("a").status_code, 422)
        self.reader.assert_not_called()

    def test_pair_metadata_review_and_concurrency(self):
        response = self.compare()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["counts"], {"modified": 1})
        self.assertEqual(data["changes"][0]["earlier"]["page"], 1)
        self.assertEqual(data["changes"][0]["later"]["page"], 1)
        self.assertIn("+The exam is closed book.", data["changes"][0]["diff"])
        self.assertEqual(self.client.get(self.url).json()["pairs"], [{"earlier_id": "a", "later_id": "b"}])
        review = dict(earlier_section=data["pending"][0]["earlier"]["id"], later_section=data["candidates"][0]["id"],
                      version=data["version"], evidence_digest=data["evidence_digest"])
        response = self.compare(review=review)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"], {"modified": 2})
        self.assertEqual(self.compare(review=review).status_code, 409)
        self.assertFalse(self.compare().json()["pending"])
        self.session.delete(self.session.get(DocumentUpload, "a"))
        self.session.commit()
        self.assertEqual(self.session.scalar(select(func.count()).select_from(RevisionPair)), 0)

    def test_failed_source_leaves_no_metadata(self):
        self.sources["b"] = self.sources["a"]
        self.assertEqual(self.compare().status_code, 422)
        self.assertEqual(self.client.get(self.url).json()["pairs"], [])
