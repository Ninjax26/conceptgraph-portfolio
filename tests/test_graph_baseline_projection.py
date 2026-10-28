"""Call/transaction contracts; the separate Neo4j runner checks Cypher behavior."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.services.ingestion_service import GraphProjectionMarker, IngestionService
from tests.test_graph_projection_replacement import ManagedSession, graph


class BaselineProjectionTests(unittest.IsolatedAsyncioTestCase):
    def service(self, session):
        return IngestionService(graph_driver=SimpleNamespace(session=Mock(return_value=session)),
            vector_client=Mock())

    async def replace(self, service, **extra):
        await service.replace_graph_extraction(graph(), "course", upload_id="upload",
            document_name="sample.pdf", course_name="Course", execution_token="task", **extra)

    async def test_baseline_advances_fence_and_cleans_markers_in_one_transaction(self):
        session = ManagedSession()
        await self.replace(self.service(session), source_generation=2)
        self.assertEqual(session.transaction_count, 1)
        query, parameters = session.calls[0]
        self.assertIn("GraphProjectionFence", query)
        self.assertEqual(parameters["generation"], 2)
        self.assertEqual(parameters["mode"], "baseline")
        self.assertEqual(len(parameters["digest"]), 64)
        self.assertTrue(any("DETACH DELETE marker" in sql for sql, _ in session.calls))
        self.assertFalse(any("MERGE (marker:GraphPublication" in sql for sql, _ in session.calls))
        self.assertEqual(session.committed, session.calls)

    async def test_baseline_rejects_invalid_generations_before_io(self):
        for generation in (0, -1, True, "2"):
            with self.subTest(generation=generation):
                session = ManagedSession()
                service = self.service(session)
                with self.assertRaises(ValueError):
                    await self.replace(service, source_generation=generation)
                service.graph_driver.session.assert_not_called()

    async def test_marker_and_baseline_generation_cannot_be_combined(self):
        session = ManagedSession()
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "task", "a"*64, 2)
        with self.assertRaises(ValueError):
            await self.replace(self.service(session), marker=marker, source_generation=2)
        self.assertFalse(session.calls)

    async def test_baseline_failure_at_every_write_rolls_back(self):
        for position in range(1, 9):
            with self.subTest(position=position):
                session = ManagedSession(fail_on=position)
                with self.assertRaises(RuntimeError):
                    await self.replace(self.service(session), source_generation=2)
                self.assertEqual(session.committed, ["old projection"])
                self.assertTrue(session.closed)

    async def test_attempt_cleanup_closes_its_generation_atomically_without_vector_calls(self):
        session = ManagedSession()
        service = self.service(session)
        method = getattr(service, "cleanup_graph_attempt", None)
        self.assertTrue(callable(method), "Generation-aware attempt cleanup is missing")
        await method("upload", "course", execution_token="task", source_generation=2)
        self.assertEqual(session.transaction_count, 1)
        self.assertEqual(session.calls[0][1]["mode"], "abort")
        self.assertEqual(len(session.calls), 4)
        service.vector_client.delete.assert_not_called()

    async def test_attempt_cleanup_rejects_invalid_identity_before_io(self):
        session = ManagedSession()
        service = self.service(session)
        method = getattr(service, "cleanup_graph_attempt", None)
        self.assertTrue(callable(method), "Generation-aware attempt cleanup is missing")
        for token, generation in (("", 2), ("task", 0), ("task", True)):
            with self.subTest(token=token, generation=generation), self.assertRaises(ValueError):
                await method("upload", "course", execution_token=token, source_generation=generation)
        service.graph_driver.session.assert_not_called()

    async def test_attempt_cleanup_failure_rolls_back_fence_and_deletion(self):
        session = ManagedSession(fail_on=3)
        service = self.service(session)
        method = getattr(service, "cleanup_graph_attempt", None)
        self.assertTrue(callable(method), "Generation-aware attempt cleanup is missing")
        with self.assertRaises(RuntimeError):
            await method("upload", "course", execution_token="task", source_generation=2)
        self.assertEqual(session.committed, ["old projection"])
