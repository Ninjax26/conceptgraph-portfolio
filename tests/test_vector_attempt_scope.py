"""Opt-in vector identities and READY execution scopes, with offline vectors."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from qdrant_client import QdrantClient, models

from app.core.config import settings
from app.services.citation_service import build_sources
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk
from app.services.rag_service import RetrievalService


class VectorAttemptScopeTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("graph_projection_fencing_enabled", True),
                            ("embedding_provider", "local"), ("embedding_dimension", 2),
                            ("retrieval_identifier_lookup", True)):
            self.enterContext(patch.object(settings, name, value))
        self.client = QdrantClient(":memory:")
        self.addCleanup(self.client.close)
        self.ingestion = IngestionService(SimpleNamespace(), self.client)
        self.retrieval = RetrievalService(SimpleNamespace(), self.client)
        self.client.create_collection(self.ingestion.collection_name,
            vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        self.ingestion._embedding_model = SimpleNamespace(encode=Mock(side_effect=
            lambda texts, **kw: np.array([[1., 0.] for _ in texts])))
        self.retrieval._embedding_model = SimpleNamespace(encode=Mock(
            return_value=SimpleNamespace(tolist=lambda: [1., 0.])))

    @staticmethod
    def chunk(task="current", upload="upload", text="PMP-104 requires monthly inspection."):
        chunk_id = f"{upload}:1:0"
        return DocumentChunk(id=chunk_id, text=text, metadata={"upload_id": upload,
            "execution_token": task, "chunk_id": chunk_id, "document_id": "course",
            "document_name": "synthetic.pdf", "page_number": 1})

    def search(self, tasks=None, top_k=2):
        tasks = {"upload": "current"} if tasks is None else tasks
        return self.retrieval.search_qdrant("Inspect PMP-104", [], [], list(tasks), top_k,
            document_tasks=tasks)

    def mixed_points(self):
        points = []
        for index, (upload, task, vector) in enumerate((
            ("upload", "old", [1., 0.]), ("upload", "current", [.6, .8]),
            ("other", "current", [1., 0.]), ("other", "other-current", [.5, .9]),
            ("private", "current", [1., 0.]), ("upload", None, [1., 0.]),
        ), 1):
            payload = dict(self.chunk(task=task, upload=upload).metadata,
                text="PMP-104 requires monthly inspection.", identifier_terms=["pmp104"])
            points.append(models.PointStruct(id=index, vector=vector, payload=payload))
        self.client.upsert(self.ingestion.collection_name, points=points, wait=True)

    def test_late_old_vector_replay_cannot_overwrite_new_execution(self):
        with patch.object(self.client, "upsert", wraps=self.client.upsert) as write:
            self.ingestion.upsert_chunks_to_qdrant([self.chunk(task="old", text="Old evidence.")])
            old_points = [point.model_copy(deep=True) for point in write.call_args.kwargs["points"]]
        self.ingestion.upsert_chunks_to_qdrant([self.chunk()])
        self.client.upsert(self.ingestion.collection_name, points=old_points, wait=True)
        points, _ = self.client.scroll(self.ingestion.collection_name, with_payload=True)
        self.assertEqual({point.payload["execution_token"] for point in points}, {"old", "current"})
        result = self.search()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metadata"]["execution_token"], "current")

    def test_default_mode_keeps_legacy_point_uuid_and_upload_only_search(self):
        with patch.object(settings, "graph_projection_fencing_enabled", False):
            chunk = self.chunk()
            self.ingestion.upsert_chunks_to_qdrant([chunk])
            result = self.retrieval.search_qdrant("Inspect PMP-104", [], [], ["upload"], 2)
        self.assertEqual(result[0]["id"], str(uuid5(NAMESPACE_URL, chunk.id)))

    def test_scoped_point_identity_is_stable_and_unambiguous(self):
        point_id = self.ingestion._qdrant_point_id
        first = point_id("a:b", execution=("c", "d"))
        self.assertEqual(first, point_id("a:b", execution=("c", "d")))
        self.assertNotEqual(first, point_id("a", execution=("b:c", "d")))
        self.assertNotEqual(first, point_id("a:b", execution=("c", "next")))
        self.assertNotEqual(first, point_id("a:b"))

    def test_opt_in_write_with_both_scope_fields_absent_cannot_fall_back_to_legacy(self):
        chunk = self.chunk()
        chunk.metadata.pop("upload_id")
        chunk.metadata.pop("execution_token")
        with self.assertRaises(ValueError):
            self.ingestion.upsert_chunks_to_qdrant([chunk])
        self.ingestion._embedding_model.encode.assert_not_called()

    def test_opt_in_write_rejects_missing_identity_before_embedding_or_store_io(self):
        for field, value in (("upload_id", ""), ("execution_token", None)):
            chunk = self.chunk()
            chunk.metadata[field] = value
            with self.subTest(field=field), patch.object(self.client, "collection_exists") as lookup:
                with self.assertRaises(ValueError):
                    self.ingestion.upsert_chunks_to_qdrant([chunk])
                lookup.assert_not_called()
        self.ingestion._embedding_model.encode.assert_not_called()

    def test_entire_batch_is_validated_before_any_write(self):
        invalid = self.chunk(upload="other")
        invalid.metadata["execution_token"] = ""
        with patch.object(self.client, "upsert") as write:
            with self.assertRaises(ValueError):
                self.ingestion.upsert_chunks_to_qdrant([self.chunk(), invalid])
            write.assert_not_called()
        self.ingestion._embedding_model.encode.assert_not_called()

    def test_opt_in_indexes_execution_token_without_changing_default_indexes(self):
        vector = SimpleNamespace(get_collection=Mock(return_value=SimpleNamespace(payload_schema={})),
            create_payload_index=Mock())
        service = IngestionService(SimpleNamespace(), vector)
        for enabled in (False, True):
            vector.create_payload_index.reset_mock()
            with patch.object(settings, "graph_projection_fencing_enabled", enabled):
                service._ensure_qdrant_payload_indexes()
            self.assertEqual({call.kwargs["field_name"] for call in vector.create_payload_index.call_args_list},
                {"upload_id", "identifier_terms", *(["execution_token"] if enabled else [])})

    def test_dense_and_identifier_search_apply_exact_document_task_pairs_before_limits(self):
        self.mixed_points()
        with patch.object(self.client, "query_points", wraps=self.client.query_points) as dense:
            with patch.object(self.client, "scroll", wraps=self.client.scroll) as lexical:
                result = self.search({"upload": "current", "other": "other-current"})
        self.assertEqual({(chunk["metadata"]["upload_id"], chunk["metadata"]["execution_token"])
            for chunk in result}, {("upload", "current"), ("other", "other-current")})
        self.assertEqual(len(result), 2)
        self.assertTrue(dense.called and lexical.called)

    def test_older_client_dense_fallback_retains_the_same_scope(self):
        self.mixed_points()
        original_query = self.client.query_points
        def old_search(**kwargs):
            kwargs["query"] = kwargs.pop("query_vector")
            return original_query(**kwargs).points
        self.retrieval.vector_client = SimpleNamespace(collection_exists=self.client.collection_exists,
            query_points=Mock(side_effect=AttributeError("old SDK")), search=Mock(side_effect=old_search),
            scroll=self.client.scroll)
        self.assertEqual([chunk["metadata"]["execution_token"] for chunk in self.search()], ["current"])

    def test_legacy_physical_uuid_with_current_payload_remains_readable_and_citable(self):
        chunk = self.chunk()
        with patch.object(settings, "graph_projection_fencing_enabled", False):
            self.ingestion.upsert_chunks_to_qdrant([chunk])
        result = self.search()
        self.assertEqual(result[0]["metadata"]["chunk_id"], chunk.id)
        source = build_sources(result)[0]
        self.assertEqual(source["document_id"], "upload")
        self.assertEqual(source["preview_url"], "/ingest/uploads/upload/preview#page=1")
        self.assertEqual(source["supporting_passage"], chunk.text)

    def test_missing_or_mismatched_scope_fails_before_embedding_or_store_io(self):
        for tasks in (None, {}, {"upload": ""}, {"upload": None}, {"private": "current"}):
            with self.subTest(tasks=tasks), patch.object(self.client, "collection_exists") as lookup:
                with self.assertRaises(ValueError):
                    self.retrieval.search_qdrant("Question", [], [], ["upload"], document_tasks=tasks)
                lookup.assert_not_called()
        self.retrieval._embedding_model.encode.assert_not_called()

    def test_retrieve_derives_scope_from_ready_records(self):
        self.mixed_points()
        context = SimpleNamespace(document_ids=["upload"], graph_status="GRAPH_READY",
            documents=(SimpleNamespace(upload_id="upload", task_id="current"),))
        result = asyncio.run(self.retrieval.retrieve("Inspect PMP-104", context, retrieval_mode="vector_only"))
        self.assertEqual([chunk["metadata"]["execution_token"] for chunk in result["chunks"]], ["current"])

    def test_invalid_ready_scope_does_not_reserve_hosted_embedding_work(self):
        context = SimpleNamespace(document_ids=["upload"], graph_status="GRAPH_READY",
            documents=(SimpleNamespace(upload_id="upload", task_id=None),))
        with patch.object(settings, "embedding_provider", "qdrant_cloud"):
            with patch("app.services.rag_service.reserve_provider_request", new_callable=AsyncMock) as reserve:
                with self.assertRaises(ValueError):
                    asyncio.run(self.retrieval.retrieve("Question", context, retrieval_mode="vector_only"))
                reserve.assert_not_awaited()
