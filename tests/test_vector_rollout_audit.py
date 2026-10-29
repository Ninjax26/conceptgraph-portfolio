"""Read-only metadata audit contracts; no hosted or database calls."""
from dataclasses import replace
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.core.config import Settings
from app.services.vector_rollout_audit import VectorReadContract, ReadyVectorIdentity, audit_ready_vectors


def config(**changes):
    return Settings(_env_file=None, QDRANT_URL="http://localhost:6333", QDRANT_COLLECTION_NAME="audit",
        EMBEDDING_DIMENSION=3, GRAPH_PROJECTION_FENCING_ENABLED=True, **changes)


def source(**changes):
    values = dict(upload_id="upload", task_id="current", course_uuid="course", stage="READY",
        processed_chunk_count=1, result_json={})
    return SimpleNamespace(**(values | changes))


def point(**changes):
    return SimpleNamespace(id="legacy-physical-id", payload=dict(upload_id="upload", course_id="course",
        execution_token="current", chunk_id="logical", page_number=1) | changes)


def client(pages):
    return Mock(get_collection=Mock(return_value=SimpleNamespace(config=SimpleNamespace(
        params=SimpleNamespace(vectors=SimpleNamespace(size=3, distance="Cosine"))))),
        scroll=Mock(side_effect=pages))


class VectorRolloutAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.contract = VectorReadContract.from_settings(config())
        self.identity = ReadyVectorIdentity.from_record(source(), known_tasks={"current", "old"})

    async def audit(self, vector, **kwargs):
        return await audit_ready_vectors(vector, self.contract, self.identity, peer_contract=self.contract, **kwargs)

    def test_contract_is_credential_free_and_detects_vector_configuration_drift(self):
        credentialed = config().model_copy(update={"qdrant_url": "http://user:private@LOCALHOST:6333/"})
        self.assertEqual(self.contract, VectorReadContract.from_settings(credentialed))
        for field, value in (("embedding_dimension", 4), ("embedding_model_name", "other"),
            ("embedding_provider", "qdrant_cloud"), ("qdrant_collection_name", "other"),
            ("qdrant_url", "http://other:6333"), ("graph_projection_fencing_enabled", False)):
            self.assertNotEqual(self.contract, VectorReadContract.from_settings(config().model_copy(update={field:value})))
        self.assertNotIn("private", repr(self.contract))
        self.assertNotIn("localhost", repr(self.contract))

    def test_source_rejects_non_ready_deleting_missing_identity_and_invalid_counts(self):
        for changes in ({"stage":"EMBEDDING"}, {"result_json":{"deletion_requested":True}},
            {"task_id":""}, {"course_uuid":None}, {"processed_chunk_count":True}, {"processed_chunk_count":0}):
            with self.assertRaises(ValueError):
                ReadyVectorIdentity.from_record(source(**changes), known_tasks={"current"})
        with self.assertRaises(ValueError):
            ReadyVectorIdentity.from_record(source(), known_tasks={"old"})

    async def test_direct_or_replaced_contracts_reject_invalid_types_and_fingerprints_before_io(self):
        with self.assertRaises(ValueError):
            VectorReadContract("audit", "", "", 3, "false")
        for changes in ({"collection_name":""}, {"collection_name":"a"*256}, {"store_sha256":""},
            {"store_sha256":"g"*64}, {"embedding_sha256":""}, {"dimension":True},
            {"dimension":0}, {"scoped":"false"}, {"scoped":1}):
            with self.assertRaises(ValueError):
                replace(self.contract, **changes)
        vector = client([])
        with self.assertRaises(ValueError):
            await audit_ready_vectors(vector, SimpleNamespace(scoped=True), self.identity,
                peer_contract=SimpleNamespace(scoped=True))
        vector.get_collection.assert_not_called()

    async def test_current_payload_accepts_legacy_physical_id_without_activation(self):
        vector = client([([point()], None)])
        r = await self.audit(vector)
        self.assertTrue(r["read_coverage_ok"])
        self.assertTrue(r["cleanup_payloads_trackable"])
        self.assertFalse(r["activation_allowed"])
        self.assertFalse(r["writer_drain_verified"])
        self.assertEqual(r["counts"]["current_unique_chunks"], 1)
        arguments = vector.scroll.call_args.kwargs
        self.assertFalse(arguments["with_vectors"])
        self.assertEqual(set(arguments["with_payload"]),
            {"upload_id", "course_id", "execution_token", "chunk_id", "page_number"})
        self.assertEqual(arguments["scroll_filter"].must[0].match.value, "upload")

    async def test_known_stale_is_excluded_but_unknown_and_untagged_cannot_claim_cleanup_coverage(self):
        vector = client([([point(execution_token="old"), point(execution_token="unknown"),
            point(execution_token=None), point()], None)])
        r = await self.audit(vector)
        self.assertTrue(r["read_coverage_ok"])
        self.assertFalse(r["cleanup_payloads_trackable"])
        for field in ("known_stale_points", "unknown_task_points", "untagged_points"):
            self.assertEqual(r["counts"][field], 1)
        missing = await self.audit(client([([point(execution_token="old")], None)]))
        self.assertFalse(missing["read_coverage_ok"])

    async def test_duplicates_foreign_scope_malformed_chunk_and_non_integer_page_fail_coverage(self):
        for points in ([point(), point()], [point(course_id="other")], [point(upload_id="other")],
            [point(chunk_id="")], [point(page_number=True)], [point(page_number=1.)]):
            r = await self.audit(client([(points, None)]))
            self.assertFalse(r["read_coverage_ok"])

    async def test_contract_or_collection_mismatch_never_scans(self):
        vector = client([])
        r = await audit_ready_vectors(vector, self.contract, self.identity,
            peer_contract=replace(self.contract, dimension=4))
        self.assertFalse(r["scan_completed"])
        vector.get_collection.assert_not_called()
        vector.get_collection.return_value.config.params.vectors.size = 4
        r = await self.audit(vector)
        self.assertFalse(r["read_coverage_ok"])
        vector.scroll.assert_not_called()

    async def test_actual_pagination_budget_and_zero_cursor_are_not_silently_truncated(self):
        vector = client([([point(execution_token="old")], 0), ([point()], None)])
        r = await self.audit(vector, page_size=1, max_points=2)
        self.assertTrue(r["scan_completed"])
        self.assertTrue(r["read_coverage_ok"])
        self.assertEqual(vector.scroll.call_args_list[1].kwargs["offset"], 0)
        r = await self.audit(client([([point()], "next")]), page_size=1, max_points=1)
        self.assertFalse(r["scan_completed"])
        self.assertFalse(r["read_coverage_ok"])

    async def test_cursor_cycles_empty_pages_and_rpc_errors_fail_closed_without_secret_messages(self):
        for pages in ([([point()], "same"), ([point()], "same")], [([], "next")],
            [RuntimeError("private credentials and source text")]):
            r = await self.audit(client(pages), page_size=1)
            self.assertFalse(r["scan_completed"])
            self.assertFalse(r["read_coverage_ok"])
            self.assertNotIn("private", repr(r))

    async def test_bounds_and_unscoped_contract_are_rejected_before_io(self):
        for arguments in ({"page_size":True}, {"page_size":257}, {"max_points":0}, {"max_points":100001}):
            vector = client([])
            with self.assertRaises(ValueError):
                await self.audit(vector, **arguments)
            vector.get_collection.assert_not_called()
        disabled = replace(self.contract, scoped=False)
        r = await audit_ready_vectors(client([]), disabled, self.identity, peer_contract=disabled)
        self.assertFalse(r["read_coverage_ok"])

    async def test_empty_or_wrong_distinct_chunk_count_and_oversized_pages_never_pass(self):
        for points in ([], [point(), point(chunk_id="extra")]):
            r = await self.audit(client([(points, None)]))
            self.assertFalse(r["read_coverage_ok"])
        r = await self.audit(client([([point(), point(chunk_id="extra")], None)]), page_size=1)
        self.assertFalse(r["scan_completed"])

    async def test_collection_errors_return_only_error_type(self):
        vector = client([])
        vector.get_collection.side_effect = RuntimeError("private endpoint and credentials")
        r = await self.audit(vector)
        self.assertEqual(r["error_type"], "RuntimeError")
        self.assertNotIn("private", repr(r))
        vector.scroll.assert_not_called()

    async def test_cancellation_drains_inflight_read_and_never_returns_partial_approval(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def scroll(**kwargs):
            entered.set()
            release.wait(3)
            finished.set()
            return [point()], None
        vector = client([])
        vector.scroll.side_effect = scroll
        task = asyncio.create_task(self.audit(vector))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
