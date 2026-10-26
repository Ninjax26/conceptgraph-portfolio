"""Offline transaction/cancellation contracts, not a real Neo4j integration test."""

import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from app.core.blocking import run_async_safely
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.ingestion_service import GraphProjectionMarker, IngestionService


def graph(count=2):
    return GraphExtractionResponse(
        nodes=[ConceptNode(id=str(i), name=f"Concept {i}", type="concept",
            upload_id="upload", document_name="sample.pdf", source_chunk_id=f"chunk-{i}",
            page_number=1) for i in range(count)],
        relationships=[ConceptRelationship(source_node_id=str(i), target_node_id=str(i+1),
            relation_type="PREREQUISITE_OF") for i in range(count-1)],
    )


class ManagedSession:
    """Authored managed-transaction double; no Cypher/server correctness claim."""

    def __init__(self, *, fail_on=None, release=None, marker_present=True):
        self.committed = ["old projection"]
        self.calls = []
        self.fail_on = fail_on
        self.release = release
        self.marker_row = {"id": "intent"} if marker_present else None
        self.started = asyncio.Event()
        self.closed = False
        self.transaction_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def execute_write(self, callback):
        self.transaction_count += 1
        staged = []

        async def run(query, **parameters):
            self.calls.append((query, parameters))
            if self.fail_on == len(self.calls):
                raise RuntimeError("injected write failure")
            staged.append((query, copy.deepcopy(parameters)))
            row = {"deleted": 1} if "RETURN count(*) AS deleted" in query else self.marker_row
            return SimpleNamespace(consume=AsyncMock(), single=AsyncMock(return_value=row))

        value = await callback(SimpleNamespace(run=run))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        self.committed = staged
        return value


class GraphProjectionTests(unittest.IsolatedAsyncioTestCase):
    def service(self, session):
        driver = SimpleNamespace(session=Mock(return_value=session))
        return IngestionService(graph_driver=driver, vector_client=SimpleNamespace())

    async def replace(self, service, extraction=None, **overrides):
        args = dict(upload_id="upload", document_name="sample.pdf",
                    course_name="Course", execution_token="original-ready-task")
        args.update(overrides)
        await service.replace_graph_extraction(extraction if extraction is not None else graph(), "course", **args)

    async def test_replacement_uses_one_transaction_and_both_deletion_scope_fields(self):
        session = ManagedSession()
        await self.replace(self.service(session))
        self.assertEqual(session.transaction_count, 1)
        deletion, parameters = session.calls[0]
        self.assertIn("course_id: $course_id, upload_id: $upload_id", deletion)
        self.assertEqual(parameters, {"course_id": "course", "upload_id": "upload"})
        self.assertIn("WHERE NOT (course)-[:CONTAINS]->(:Concept)", session.calls[-1][0])
        self.assertEqual(session.committed, session.calls)
        self.assertTrue(session.closed)

    async def test_existing_writer_query_parameters_are_preserved(self):
        auto = ManagedSession()
        auto.run = AsyncMock()
        await self.service(auto).store_graph_extraction(graph(), "course", upload_id="upload",
            document_name="sample.pdf", course_name="Course", execution_token="original-ready-task")
        managed = ManagedSession()
        await self.replace(self.service(managed))
        original = [(call.args[0], call.kwargs) for call in auto.run.await_args_list]
        self.assertEqual(managed.calls[1:-1], original)
        for _, parameters in original:
            if "execution_token" in parameters:
                self.assertEqual(parameters["execution_token"], "original-ready-task")
                self.assertEqual(parameters["upload_id"], "upload")
        relationship = original[-1][1]
        self.assertEqual(relationship["source_node_id"], IngestionService._scoped_concept_id("course", "upload", "0"))
        self.assertEqual(relationship["target_node_id"], IngestionService._scoped_concept_id("course", "upload", "1"))

    async def test_failure_at_each_write_never_commits_partial_replacement(self):
        # Delete, course, two nodes, relation, empty-course cleanup.
        for index in range(1, 7):
            with self.subTest(index=index):
                session = ManagedSession(fail_on=index)
                with self.assertRaises(RuntimeError):
                    await self.replace(self.service(session))
                self.assertEqual(session.committed, ["old projection"])
                self.assertTrue(session.closed)

    async def test_replayed_replacement_has_identical_scoped_writes(self):
        session = ManagedSession()
        service = self.service(session)
        await self.replace(service)
        first = copy.deepcopy(session.committed)
        await self.replace(service)
        self.assertEqual(session.committed, first)
        self.assertEqual(session.transaction_count, 2)

    async def test_merged_graph_is_not_limited_to_one_provider_batch(self):
        session = ManagedSession()
        await self.replace(self.service(session), graph(13))
        self.assertEqual(len(session.calls), 28)  # two deletes + course + 13 nodes + 12 edges

    async def test_optional_marker_is_written_in_the_same_transaction(self):
        session = ManagedSession()
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "original-ready-task", "a" * 64)
        await self.replace(self.service(session), marker=marker)
        self.assertEqual(session.transaction_count, 1)
        self.assertIn("GraphPublication", session.calls[-1][0])
        self.assertEqual(session.calls[-1][1], marker.as_parameters())

    async def test_mismatched_marker_never_opens_remote_session(self):
        session = ManagedSession()
        marker = GraphProjectionMarker("intent", "job", "foreign", "course", "original-ready-task", "a" * 64)
        with self.assertRaises(ValueError):
            await self.replace(self.service(session), marker=marker)
        self.assertFalse(session.calls)

    async def test_marker_write_failure_does_not_commit_replacement(self):
        session = ManagedSession(fail_on=7)
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "original-ready-task", "a" * 64)
        with self.assertRaises(RuntimeError):
            await self.replace(self.service(session), marker=marker)
        self.assertEqual(session.committed, ["old projection"])

    async def test_existing_marker_identity_conflict_rolls_back(self):
        session = ManagedSession(marker_present=False)
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "original-ready-task", "a" * 64)
        with self.assertRaises(ValueError):
            await self.replace(self.service(session), marker=marker)
        self.assertEqual(session.committed, ["old projection"])

    async def test_marker_reader_uses_exact_intent_id(self):
        session = ManagedSession()
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "original-ready-task", "a" * 64)
        session.run = AsyncMock(return_value=SimpleNamespace(single=AsyncMock(return_value=marker.as_parameters())))
        self.assertEqual(await self.service(session).read_graph_publication_marker("intent"), marker)
        self.assertEqual(session.run.await_args.kwargs, {"id": "intent"})

    async def test_obsolete_marker_cleanup_is_exact_and_uses_one_transaction(self):
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "original-ready-task", "a" * 64)
        session = ManagedSession()
        session.marker_row = marker.as_parameters()
        self.assertTrue(await self.service(session).cleanup_graph_publication(marker))
        self.assertEqual(session.transaction_count, 1)
        self.assertEqual(len(session.calls), 4)
        self.assertIn("GraphPublication", session.calls[0][0])
        self.assertIn("DETACH DELETE marker", session.calls[1][0])
        self.assertIn("execution_token: $document_task_id", session.calls[2][0])
        self.assertIn("DETACH DELETE concept", session.calls[2][0])
        self.assertEqual(session.calls[2][1], marker.as_parameters())
        self.assertTrue(session.closed)

    async def test_obsolete_marker_cleanup_rejects_absent_conflicting_or_failed_write(self):
        marker = GraphProjectionMarker("intent", "job", "upload", "course", "original-ready-task", "a" * 64)
        missing = ManagedSession(marker_present=False)
        self.assertFalse(await self.service(missing).cleanup_graph_publication(marker))
        self.assertEqual(len(missing.calls), 1)
        conflicting = ManagedSession()
        conflicting.marker_row = {**marker.as_parameters(), "job_id": "other"}
        with self.assertRaises(ValueError):
            await self.service(conflicting).cleanup_graph_publication(marker)
        self.assertEqual(conflicting.committed, ["old projection"])
        failed = ManagedSession(fail_on=2)
        failed.marker_row = marker.as_parameters()
        with self.assertRaises(RuntimeError):
            await self.service(failed).cleanup_graph_publication(marker)
        self.assertEqual(failed.committed, ["old projection"])

    async def test_obsolete_marker_cleanup_rejects_invalid_identity_before_io(self):
        session = ManagedSession()
        service = self.service(session)
        with self.assertRaises(ValueError):
            await service.cleanup_graph_publication(
                GraphProjectionMarker("intent", "job", "upload", "course", "task", "invalid"))
        service.graph_driver.session.assert_not_called()

    async def test_empty_replacement_deletes_only_upload_then_cleans_empty_course(self):
        session = ManagedSession()
        await self.replace(self.service(session), GraphExtractionResponse(empty_extraction=True))
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0][1]["upload_id"], "upload")
        self.assertIn("WHERE NOT", session.calls[-1][0])

    async def test_invalid_provenance_or_identity_never_opens_session(self):
        for field, value in (("upload_id", "foreign"), ("document_name", "foreign.pdf"),
                             ("source_chunk_id", ""), ("page_number", None)):
            with self.subTest(field=field):
                extraction = graph()
                setattr(extraction.nodes[0], field, value)
                session = ManagedSession()
                service = self.service(session)
                with self.assertRaises(ValueError):
                    await self.replace(service, extraction)
                service.graph_driver.session.assert_not_called()
        session = ManagedSession()
        service = self.service(session)
        with self.assertRaises(ValueError):
            await self.replace(service, execution_token="")
        service.graph_driver.session.assert_not_called()

    async def test_mutated_invalid_graph_never_opens_session(self):
        for field, value in (("relation_type", "INVENTED"), ("target_node_id", "foreign")):
            with self.subTest(field=field):
                extraction = graph()
                setattr(extraction.relationships[0], field, value)
                service = self.service(ManagedSession())
                with self.assertRaises(ValueError):
                    await self.replace(service, extraction)
                service.graph_driver.session.assert_not_called()
        extraction = graph()
        extraction.nodes[1].name = extraction.nodes[0].name
        service = self.service(ManagedSession())
        with self.assertRaises(ValueError):
            await self.replace(service, extraction)
        service.graph_driver.session.assert_not_called()

    async def test_cancellation_waits_for_commit_and_session_close(self):
        release = asyncio.Event()
        session = ManagedSession(release=release)
        task = asyncio.create_task(self.replace(self.service(session)))
        await session.started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertFalse(session.closed)
        self.assertEqual(session.committed, ["old projection"])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(session.closed)
        self.assertNotEqual(session.committed, ["old projection"])

    async def test_draining_remote_failure_preserves_cancellation(self):
        started, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def write():
            started.set()
            await release.wait()
            settled.set()
            raise RuntimeError("remote failure after cancellation")

        task = asyncio.create_task(run_async_safely(write()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(settled.is_set())
