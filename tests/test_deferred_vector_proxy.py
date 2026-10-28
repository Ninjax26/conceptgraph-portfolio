"""Offline HTTP delay-fixture contracts, with a disposable loopback upstream."""
import asyncio
import json
import unittest
from urllib.parse import urlsplit

import httpx
from qdrant_client import QdrantClient, models

from scripts.deferred_vector_proxy import DeferredVectorProxy


class DeferredVectorProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        async def upstream(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:")), 0)
            self.received.append(await reader.readexactly(length))
            body = b'{"result":{"status":"completed"},"status":"ok","time":0.001}'
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                         +str(len(body)).encode()+b"\r\n\r\n"+body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        self.server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        self.target = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.collection = "deletion_"+"a"*32
        self.payload = json.dumps({"points": [{"id": 1, "vector": [1., 0., 0.],
            "payload": {"upload_id": "upload", "execution_token": "task"}}]}).encode()

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    def proxy(self):
        return DeferredVectorProxy(self.target, self.collection, "upload", "task")

    async def test_ingestion_catalog_read_is_allowed_but_catalog_mutation_is_rejected(self):
        async with self.proxy() as proxy:
            async with httpx.AsyncClient(trust_env=False) as client:
                self.assertEqual((await client.get(proxy.url+"/collections")).status_code, 200)
                self.assertEqual((await client.put(proxy.url+"/collections", content=b"{}")).status_code, 400)
            self.assertEqual(self.received, [b""])

    async def test_installed_qdrant_sdk_write_can_be_held_and_forwarded(self):
        async with self.proxy() as proxy:
            client = QdrantClient(url=proxy.url, timeout=3, check_compatibility=False, cloud_inference=False)
            self.addCleanup(client.close)
            write = asyncio.create_task(asyncio.to_thread(client.upsert, self.collection, points=[
                models.PointStruct(id=1, vector=[1., 0., 0.], payload={"upload_id": "upload", "execution_token": "task"})]))
            try:
                done, _ = await asyncio.wait({write, proxy.captured}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
                self.assertIn(proxy.captured, done, proxy.rejections)
                await proxy.release()
                self.assertEqual((await write).status, "completed")
            finally:
                proxy._release.set()
                await asyncio.gather(write, return_exceptions=True)

    async def test_holds_complete_request_and_delivers_identical_body_after_client_disconnect(self):
        async with self.proxy() as proxy:
            async with httpx.AsyncClient(trust_env=False) as client:
                request = asyncio.create_task(client.put(proxy.url+f"/collections/{self.collection}/points?wait=true",
                    content=self.payload, headers={"Content-Type": "application/json"}))
                captured = await asyncio.wait_for(proxy.captured, 3)
                self.assertEqual(captured["point_count"], 1)
                self.assertEqual(self.received, [])
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                delivery = await proxy.release()
                self.assertEqual(delivery["http_status"], 200)
                self.assertTrue(delivery["completed"])
                self.assertEqual(self.received, [self.payload])
                self.assertEqual(delivery["body_sha256"], captured["body_sha256"])

    async def test_rejects_external_targets_and_non_private_collections(self):
        for target in ("https://example.com", "http://localhost:6333", "http://user:pass@127.0.0.1:6333",
                       "http://127.0.0.1:6333/path", "http://127.0.0.1:6333?other=true"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                DeferredVectorProxy(target, self.collection, "upload", "task")
        with self.assertRaises(ValueError):
            DeferredVectorProxy(self.target, "production", "upload", "task")

    async def test_foreign_collection_or_task_is_not_forwarded(self):
        async with self.proxy() as proxy:
            async with httpx.AsyncClient(trust_env=False) as client:
                wrong_collection = await client.put(proxy.url+"/collections/foreign/points", content=self.payload)
                wrong_body = self.payload.replace(b'"task"', b'"foreign-task"')
                wrong_task = await client.put(proxy.url+f"/collections/{self.collection}/points", content=wrong_body)
            self.assertEqual(wrong_collection.status_code, 400)
            self.assertEqual(wrong_task.status_code, 400)
            self.assertFalse(proxy.captured.done())
            self.assertEqual(self.received, [])

    async def test_raw_traversal_and_encoded_paths_cannot_escape_collection(self):
        async with self.proxy() as proxy:
            for suffix in ("/../../collections/foreign/points", "/%2e%2e/%2e%2e/collections/foreign/points",
                           "/points%2fdelete"):
                reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
                try:
                    writer.write(f"POST /collections/{self.collection}{suffix} HTTP/1.1\r\nContent-Length: 2\r\n\r\n{{}}".encode())
                    await writer.drain()
                    self.assertIn(b"400", await asyncio.wait_for(reader.readline(), 3))
                finally:
                    writer.close()
                    await writer.wait_closed()
            self.assertEqual(self.received, [])

    async def test_shutdown_discards_held_request_and_closes_handler(self):
        proxy = self.proxy()
        async with httpx.AsyncClient(trust_env=False) as client:
            async with proxy:
                request = asyncio.create_task(client.put(proxy.url+f"/collections/{self.collection}/points", content=self.payload))
                await asyncio.wait_for(proxy.captured, 3)
            with self.assertRaises(httpx.RemoteProtocolError):
                await asyncio.wait_for(request, 3)
        self.assertEqual(self.received, [])
        self.assertEqual(proxy.pending_handlers, 0)

    async def test_malformed_or_oversized_body_is_rejected(self):
        async with self.proxy() as proxy:
            async with httpx.AsyncClient(trust_env=False) as client:
                for body in (b"not-json", b'{}', b'{"points":[]}'):
                    response = await client.put(proxy.url+f"/collections/{self.collection}/points", content=body)
                    self.assertEqual(response.status_code, 400)
            reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
            writer.write(f"PUT /collections/{self.collection}/points HTTP/1.1\r\nContent-Length: {proxy.MAX_BODY+1}\r\n\r\n".encode())
            await writer.drain()
            self.assertIn(b"400", await asyncio.wait_for(reader.readline(), 3))
            writer.close()
            await writer.wait_closed()
            self.assertFalse(proxy.captured.done())
            self.assertEqual(self.received, [])
