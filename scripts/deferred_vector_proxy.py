"""Loopback-only fault fixture: delay one validated synthetic vector PUT."""
import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx


class DeferredVectorProxy:
    MAX_BODY = 1024 * 1024

    def __init__(self, target, collection, upload_id, task_id):
        parsed = urlsplit(target)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or not re.fullmatch(r"deletion_[0-9a-f]{32}", collection)
            or not all(isinstance(value, str) and value.strip() for value in (upload_id, task_id))):
            raise ValueError("Only a private disposable loopback vector collection is allowed.")
        self.target, self.collection = target, collection
        self.upload_id, self.task_id = upload_id, task_id
        self._handlers = set()
        self._closed = False
        self.rejections = []

    @property
    def pending_handlers(self):
        return len(self._handlers)

    async def __aenter__(self):
        self.captured = asyncio.get_running_loop().create_future()
        self.delivered = asyncio.get_running_loop().create_future()
        self._release = asyncio.Event()
        self._client = httpx.AsyncClient(base_url=self.target, trust_env=False, timeout=10)
        try:
            self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=16384)
        except BaseException:
            await self._client.aclose()
            raise
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        self._closed = True
        self._server.close()
        await asyncio.sleep(0)
        while self._handlers:
            tasks = list(self._handlers)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._server.wait_closed()
        await self._client.aclose()
        for future in (self.captured, self.delivered):
            if not future.done():
                future.cancel()

    async def release(self):
        if not self.captured.done() or self.captured.cancelled():
            raise RuntimeError("No complete vector request has been captured.")
        self._release.set()
        return await asyncio.wait_for(asyncio.shield(self.delivered), 20)

    async def _read(self, reader):
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = headers.decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
        parsed = urlsplit(target)
        root = f"/collections/{self.collection}"
        allowed = {("GET", "/collections"), ("GET", root), ("PUT", root),
            ("GET", root+"/exists"), ("POST", root+"/points/delete"),
            ("PUT", root+"/points"), ("PUT", root+"/index")}
        if (version != "HTTP/1.1"
            or parsed.scheme or parsed.netloc or parsed.fragment
            or (method, parsed.path) not in allowed):
            raise ValueError("Request is outside the private collection.")
        fields = {}
        for line in lines[1:]:
            if not line:
                continue
            name, value = line.split(":", 1)
            name = name.lower()
            if name in fields:
                raise ValueError("Duplicate fixture headers are not supported.")
            fields[name] = value.strip()
        length = int(fields.get("content-length", "0"))
        if length < 0 or length > self.MAX_BODY or "transfer-encoding" in fields:
            raise ValueError("Unsupported or oversized fixture body.")
        body = await asyncio.wait_for(reader.readexactly(length), 10)
        return method, target, parsed.path, body

    def _validate_points(self, body):
        data = json.loads(body)
        points = data.get("points") if isinstance(data, dict) else None
        if not isinstance(points, list) or not points:
            raise ValueError("Expected a complete point-list request.")
        for point in points:
            payload = point.get("payload") if isinstance(point, dict) else None
            if (not isinstance(payload, dict) or payload.get("upload_id") != self.upload_id
                or payload.get("execution_token") != self.task_id
                or point.get("vector") != [1., 0., 0.]):
                raise ValueError("Only the expected synthetic source execution is allowed.")
        return {"point_count": len(points), "body_sha256": hashlib.sha256(body).hexdigest()}

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            if self._closed:
                return
            method, target, path, body = await self._read(reader)
            held = method == "PUT" and path == f"/collections/{self.collection}/points"
            if held:
                captured = self._validate_points(body)
                if self.captured.done():
                    raise ValueError("Only one delayed PUT is allowed per proxy.")
                self.captured.set_result(captured)
                await self._release.wait()
            response = await self._client.request(method, target, content=body,
                headers={"Content-Type": "application/json"})
            if held:
                self.delivered.set_result({**captured, "http_status": response.status_code,
                    "completed": response.json().get("result", {}).get("status") == "completed"})
            response_body, status = response.content, response.status_code
            await self._respond(writer, status, response_body)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
            self.rejections.append(str(error)[:160])
            await self._respond(writer, 400, b'{"error":"invalid private fixture request"}')
        except httpx.HTTPError as error:
            if self.captured.done() and not self.delivered.done():
                self.delivered.set_result({"http_status": 502, "completed": False, "error": type(error).__name__})
            await self._respond(writer, 502, b'{"error":"private fixture upstream failed"}')
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            finally:
                self._handlers.discard(task)

    @staticmethod
    async def _respond(writer, status, body):
        try:
            writer.write(f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode()+body)
            await writer.drain()
        except ConnectionError:
            # The accepted upstream request must survive its worker disconnecting.
            pass
