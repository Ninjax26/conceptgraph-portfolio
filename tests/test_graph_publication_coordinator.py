"""Offline coordinator gates; real cross-store checks live in the local runner."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.schemas.extraction import GraphExtractionResponse
from app.services.graph_continuation_service import GraphContinuationService
from app.services.graph_publication_candidate import GraphPublicationCandidate
from app.services.ingestion_service import GraphProjectionMarker


class PublicationCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, missing_claim=False, bad_generation=None):
        commits = []
        record = SimpleNamespace(upload_id='upload', task_id='task', course_uuid='course',
            course_id='COURSE', original_filename='fixture.pdf', attempt_count=1, result_json={'chunks_indexed':3})
        job = SimpleNamespace(id='job')
        intent = SimpleNamespace(id='intent', job_id='job', upload_id='upload', document_task_id='task',
            course_id='course', payload_sha256='a'*64, source_generation=bad_generation or 1)
        staged_intent = SimpleNamespace(**vars(intent))
        staged_intent.source_generation = 1
        result = Mock()
        result.scalar_one_or_none.return_value = intent

        async def commit():
            commits.append('commit')
        @asynccontextmanager
        async def sessions():
            yield SimpleNamespace(commit=commit, execute=AsyncMock(return_value=result))

        expected = GraphProjectionMarker('intent', 'job', 'upload', 'course', 'task', 'a'*64, 1)
        remote = SimpleNamespace(read_graph_publication_marker=AsyncMock(return_value=expected),
            replace_graph_extraction=AsyncMock())
        service = GraphContinuationService()
        with (
            patch.object(service, 'stage_publication_intent', AsyncMock(return_value=None if missing_claim else staged_intent)),
            patch.object(service, 'load_publication_draft', AsyncMock(return_value=GraphPublicationCandidate('{}', 'a'*64))),
            patch.object(service, 'restore_publication', AsyncMock(return_value=GraphExtractionResponse())),
            patch.object(service, '_locked_claim', AsyncMock(return_value=(record, job, object()))),
            patch.object(service, 'complete_publication', AsyncMock(return_value=True)),
        ):
            if bad_generation is not None:
                with self.assertRaisesRegex(ValueError, 'intent no longer matches'):
                    await service.publish_publication(sessions, SimpleNamespace(), remote)
                answer = None
            else:
                answer = await service.publish_publication(sessions, SimpleNamespace(), remote)
        return answer, commits, remote

    async def test_missing_claim_makes_no_commit_or_remote_request(self):
        answer, commits, remote = await self.exercise(missing_claim=True)
        self.assertFalse(answer)
        self.assertEqual(commits, [])
        remote.read_graph_publication_marker.assert_not_awaited()
        remote.replace_graph_extraction.assert_not_awaited()

    async def test_intent_is_revalidated_after_commit_before_any_remote_request(self):
        _, commits, remote = await self.exercise(bad_generation=2)
        self.assertEqual(commits, ['commit'])
        remote.read_graph_publication_marker.assert_not_awaited()
        remote.replace_graph_extraction.assert_not_awaited()

    async def test_exact_marker_completes_without_replacing_the_remote_graph(self):
        answer, commits, remote = await self.exercise()
        self.assertTrue(answer)
        self.assertEqual(commits, ['commit', 'commit'])
        remote.replace_graph_extraction.assert_not_awaited()
