"""Synthetic overload isolation; no listeners or production services."""
import asyncio
import json
import pytest
from qtrade_agent.api.body_limit import RequestBodyLimitMiddleware

def request_scope(path="/api/v1/work", method="GET"):
    return {"type": "http", "path": path, "method": method, "headers": []}

async def request(middleware, path="/api/v1/work", chunks=None):
    pending = list(chunks or [{"type": "http.request", "body": b"", "more_body": False}])
    out = []
    async def receive():
        if pending:
            return pending.pop(0)
        await asyncio.Future()
    async def send(message):
        out.append(message)
    await middleware(request_scope(path), receive, send)
    start = next(m for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return start["status"], json.loads(body or b"{}")

async def test_health_has_independent_capacity_and_overload_fails_promptly():
    entered = asyncio.Event()
    release = asyncio.Event()
    async def app(scope, receive, send):
        await receive()
        if scope["path"] == "/api/v1/work":
            entered.set()
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    middleware = RequestBodyLimitMiddleware(app, max_inflight=1)
    first = asyncio.create_task(request(middleware))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert (await asyncio.wait_for(request(middleware, "/health"), .3))[0] == 200
        status, body = await asyncio.wait_for(request(middleware), .3)
        assert status == 503
        assert body["code"] == "RESOURCE_EXHAUSTED"
        assert body["error"]["reason"] == "request_capacity_exhausted"
    finally:
        release.set()
        await first

async def test_reserved_health_capacity_is_also_bounded():
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0
    async def app(scope, receive, send):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        await send({"type":"http.response.start","status":200,"headers":[]})
        await send({"type":"http.response.body","body":b"{}"})
    middleware = RequestBodyLimitMiddleware(app)
    tasks = [asyncio.create_task(request(middleware, "/health")) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        status, body = await asyncio.wait_for(request(middleware, "/health"), .3)
        assert status == 503 and body["error"]["reason"] == "request_capacity_exhausted"
    finally:
        release.set()
        await asyncio.gather(*tasks)

async def test_health_body_cannot_bypass_capacity_byte_limit():
    calls = []
    async def app(*args):
        calls.append(True)
    middleware = RequestBodyLimitMiddleware(app)
    status, body = await request(middleware, "/health", [{"type":"http.request", "body":b"x" * 8192}])
    assert status == 413 and not calls

async def test_cancelled_request_releases_capacity():
    entered = asyncio.Event()
    async def app(scope, receive, send):
        if not entered.is_set():
            entered.set()
            await asyncio.Future()
        await send({"type":"http.response.start","status":200,"headers":[]})
        await send({"type":"http.response.body","body":b"{}"})
    middleware = RequestBodyLimitMiddleware(app, max_inflight=1)
    task = asyncio.create_task(request(middleware))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await request(middleware))[0] == 200
