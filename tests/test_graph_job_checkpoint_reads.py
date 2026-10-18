"""Source/configuration-qualified reads; no provider or database requests."""

from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import Settings
from app.schemas.extraction import ConceptNode, GraphExtractionResponse
from app.services.graph_continuation_service import GraphContinuationService, validated_job_checkpoints
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


class QualifiedCheckpointReads(unittest.TestCase):
    def setUp(self):
        cfg = Settings(_env_file=None, LLM_PROVIDER="groq", GROQ_API_KEY="fixture-key",
            GROQ_MODEL="fixture-model", GEMINI_API_KEY="", CEREBRAS_API_KEY="",
            RERANK_PROVIDER="local", GRAPH_BATCH_SIZE=3, GRAPH_GLOBAL_LINKING_ENABLED=True)
        self.enterContext(patch("app.services.ingestion_service.settings", cfg))
        self.enterContext(patch("app.services.graph_continuation_service.settings", cfg))
        self.chunks = [DocumentChunk(id=f"chunk-{i}", text=f"DNS evidence {i}", metadata={
            "upload_id":"fixture", "document_name":"fixture.pdf", "page_number":1,
            "section_heading":"DNS"}) for i in range(2)]
        self.batch = IngestionService._graph_section_batches(self.chunks)[0]
        self.graph = GraphExtractionResponse(nodes=[ConceptNode(id="dns", name="DNS", type="concept", source_chunk_id="chunk-0"),
            ConceptNode(id="ip", name="IP address", type="concept", source_chunk_id="chunk-1")])
        self.payloads = {self.batch.batch_key:self.graph.model_dump(mode="json")}

    def test_rebinds_current_sources_without_mutating_saved_json(self):
        result = validated_job_checkpoints(self.chunks, self.payloads)
        self.assertEqual(result[self.batch.batch_key].nodes[0].upload_id, "fixture")
        self.assertEqual(result[self.batch.batch_key].nodes[0].document_name, "fixture.pdf")
        self.assertEqual(self.payloads[self.batch.batch_key]["nodes"][0]["upload_id"], "")

    def test_reuses_explicit_empty_recovery_not_unversioned_rows(self):
        recovery = IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:self.graph})
        self.payloads[recovery.batch_key] = GraphExtractionResponse(empty_extraction=True).model_dump(mode="json")
        self.payloads["legacy"] = self.graph.model_dump(mode="json")
        result = validated_job_checkpoints(self.chunks, self.payloads)
        self.assertEqual(set(result), {self.batch.batch_key, recovery.batch_key})
        self.assertTrue(result[recovery.batch_key].empty_extraction)

    def test_changed_concepts_invalidate_old_recovery(self):
        recovery = IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:self.graph})
        self.payloads[recovery.batch_key] = GraphExtractionResponse(empty_extraction=True).model_dump(mode="json")
        self.payloads[self.batch.batch_key]["nodes"][0]["name"] = "Domain Name System"
        self.assertNotIn(recovery.batch_key, validated_job_checkpoints(self.chunks, self.payloads))

    def test_corrupted_or_foreign_or_limited_sections_are_not_reused(self):
        for payload in (None, {"nodes":[]}, {**self.graph.model_dump(mode="json"), "provider_limited":True}):
            with self.subTest(payload=payload):
                self.assertEqual(validated_job_checkpoints(self.chunks, {self.batch.batch_key:payload}), {})
        self.payloads[self.batch.batch_key]["nodes"][0]["source_chunk_id"] = "foreign"
        self.assertEqual(validated_job_checkpoints(self.chunks, self.payloads), {})

    def test_changed_model_route_does_not_reuse_older_section(self):
        with patch("app.services.ingestion_service.settings.groq_model", "different-model"):
            self.assertEqual(validated_job_checkpoints(self.chunks, self.payloads), {})

    def test_unconfigured_provider_model_is_not_an_extraction_identity(self):
        with patch("app.services.ingestion_service.settings.groq_api_key", ""):
            batch = IngestionService._graph_section_batches(self.chunks)[0]
            payloads = {batch.batch_key:self.graph.model_dump(mode="json")}
            with patch("app.services.ingestion_service.settings.groq_model", "unused-model"):
                self.assertIn(batch.batch_key, validated_job_checkpoints(self.chunks, payloads))


class ReadTransactionBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_replay_lost_claim_never_restores_candidate(self):
        service = GraphContinuationService()
        with patch.object(service, "_locked_claim", AsyncMock(return_value=None)), \
             patch("app.services.graph_continuation_service.restore_graph_publication") as restore:
            self.assertIsNone(await service.restore_publication(SimpleNamespace(), SimpleNamespace(), "untrusted"))
        restore.assert_not_called()

    async def test_replay_uses_persisted_sources_and_current_identity_without_finishing_transaction(self):
        service = GraphContinuationService()
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        record = SimpleNamespace(upload_id="current", course_uuid="course", task_id="task", original_filename="source.pdf")
        job = SimpleNamespace(snapshot_json={}, snapshot_sha256="digest", contract_sha256="contract")
        with patch.object(service, "_locked_claim", AsyncMock(return_value=(record, job, None))), \
             patch("app.services.graph_continuation_service.snapshot_chunks", return_value=("persisted",)), \
             patch("app.services.graph_continuation_service.snapshot_digest", return_value="digest"), \
             patch("app.services.graph_continuation_service.extraction_contract", return_value="contract"), \
             patch("app.services.graph_continuation_service.restore_graph_publication", return_value="graph") as restore:
            self.assertEqual(await service.restore_publication(session, SimpleNamespace(chunks=("foreign",)), "candidate"), "graph")
        self.assertEqual(restore.call_args.args, ("candidate", ("persisted",)))
        self.assertEqual(restore.call_args.kwargs, dict(upload_id="current", course_id="course",
            document_task_id="task", document_name="source.pdf"))
        session.commit.assert_not_awaited()
        session.rollback.assert_not_awaited()

    async def test_replay_changed_snapshot_or_contract_never_restores_candidate(self):
        for digest, contract in (("stale", "contract"), ("digest", "stale")):
            service = GraphContinuationService()
            session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
            job = SimpleNamespace(snapshot_json={}, snapshot_sha256=digest, contract_sha256=contract)
            with self.subTest(digest=digest, contract=contract), \
                 patch.object(service, "_locked_claim", AsyncMock(return_value=(SimpleNamespace(), job, None))), \
                 patch("app.services.graph_continuation_service.snapshot_chunks", return_value=()), \
                 patch("app.services.graph_continuation_service.snapshot_digest", return_value="digest"), \
                 patch("app.services.graph_continuation_service.extraction_contract", return_value="contract"), \
                 patch("app.services.graph_continuation_service.restore_graph_publication") as restore:
                with self.assertRaisesRegex(ValueError, "changed"):
                    await service.restore_publication(session, SimpleNamespace(), "candidate")
            restore.assert_not_called()
            session.commit.assert_not_awaited()
            session.rollback.assert_not_awaited()

    async def test_publication_lost_claim_never_builds_candidate(self):
        service = GraphContinuationService()
        session = SimpleNamespace()
        with patch.object(service, "_locked_claim", AsyncMock(return_value=None)), \
             patch("app.services.graph_continuation_service.prepare_graph_publication") as prepare:
            self.assertIsNone(await service.prepare_publication(session, SimpleNamespace(), GraphExtractionResponse()))
        prepare.assert_not_called()

    async def test_publication_uses_current_identity_and_keeps_transaction(self):
        service = GraphContinuationService()
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        record = SimpleNamespace(upload_id="current", course_uuid="course", task_id="task", original_filename="source.pdf")
        job = SimpleNamespace(snapshot_json={}, snapshot_sha256="digest", contract_sha256="contract")
        with patch.object(service, "_locked_claim", AsyncMock(return_value=(record, job, None))), \
             patch("app.services.graph_continuation_service.snapshot_chunks", return_value=("persisted",)), \
             patch("app.services.graph_continuation_service.snapshot_digest", return_value="digest"), \
             patch("app.services.graph_continuation_service.extraction_contract", return_value="contract"), \
             patch("app.services.graph_continuation_service.prepare_graph_publication", return_value="candidate") as prepare:
            self.assertEqual(await service.prepare_publication(session, SimpleNamespace(chunks=()), GraphExtractionResponse()), "candidate")
        self.assertEqual(prepare.call_args.args[1], ("persisted",))
        self.assertEqual(prepare.call_args.kwargs["upload_id"], "current")
        session.commit.assert_not_awaited()
        session.rollback.assert_not_awaited()

    async def test_changed_snapshot_or_contract_is_not_returned_to_processor(self):
        for digest, contract in (("stale", "contract"), ("digest", "stale")):
            with self.subTest(digest=digest, contract=contract):
                service = GraphContinuationService()
                session = SimpleNamespace(rollback=AsyncMock(), execute=AsyncMock())
                job = SimpleNamespace(snapshot_json={}, snapshot_sha256=digest, contract_sha256=contract)
                with patch.object(service, "_locked_claim", AsyncMock(return_value=(SimpleNamespace(), job, None))), \
                     patch("app.services.graph_continuation_service.snapshot_chunks", return_value=()), \
                     patch("app.services.graph_continuation_service.snapshot_digest", return_value="digest"), \
                     patch("app.services.graph_continuation_service.extraction_contract", return_value="contract"):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        await service.load_input(session, SimpleNamespace())
                session.execute.assert_not_awaited()
                session.rollback.assert_awaited_once()

    async def test_cancelled_input_read_releases_transaction(self):
        service = GraphContinuationService()
        session = SimpleNamespace(rollback=AsyncMock())
        with patch.object(service, "_locked_claim", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await service.load_input(session, SimpleNamespace())
        session.rollback.assert_awaited_once()

    async def test_processor_input_uses_persisted_chunks_and_releases_locks(self):
        service = GraphContinuationService()
        chunks = (DocumentChunk(id="persisted", text="DNS", metadata={}),)
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = []
        session = SimpleNamespace(rollback=AsyncMock(), execute=AsyncMock(return_value=rows))
        record = SimpleNamespace(upload_id="fixture")
        job = SimpleNamespace(snapshot_json={}, snapshot_sha256="digest", contract_sha256="contract")
        with patch.object(service, "_locked_claim", AsyncMock(return_value=(record, job, None))), \
             patch("app.services.graph_continuation_service.snapshot_chunks", return_value=chunks), \
             patch("app.services.graph_continuation_service.snapshot_digest", return_value="digest"), \
             patch("app.services.graph_continuation_service.extraction_contract", return_value="contract"), \
             patch("app.services.graph_continuation_service.validated_job_checkpoints", return_value={}):
            inputs = await service.load_input(session, SimpleNamespace(chunks=("untrusted",)))
        self.assertEqual(inputs.chunks, chunks)
        self.assertEqual(inputs.checkpoints, {})
        session.rollback.assert_awaited_once()

    async def test_lost_claim_returns_none_without_reading_checkpoints(self):
        service = GraphContinuationService()
        session = SimpleNamespace(rollback=AsyncMock(), execute=AsyncMock())
        with patch.object(service, "_locked_claim", AsyncMock(return_value=None)):
            self.assertIsNone(await service.load_checkpoints(session, SimpleNamespace()))
        session.execute.assert_not_awaited()
        session.rollback.assert_awaited()

    async def test_snapshot_failure_releases_read_locks(self):
        service = GraphContinuationService()
        session = SimpleNamespace(rollback=AsyncMock(), execute=AsyncMock())
        with patch.object(service, "_locked_claim", AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(snapshot_json={}), None))), \
             patch("app.services.graph_continuation_service.snapshot_chunks", side_effect=ValueError("bad snapshot")):
            with self.assertRaises(ValueError):
                await service.load_checkpoints(session, SimpleNamespace())
        session.execute.assert_not_awaited()
        session.rollback.assert_awaited_once()
