"""Bound HTTP bodies and admission, with an independently bounded health lane."""
from __future__ import annotations

import asyncio
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 4 * 1024 * 1024
HEALTH_REQUEST_BYTES = 4096
HEALTH_PATHS = frozenset({"/health", "/api/v1/system/health"})

class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES, max_inflight: int = 16):
        if max_bytes <= 0 or max_inflight <= 0:
            raise ValueError("Body and concurrency limits must be positive")
        self.app = app
        self.max_bytes = max_bytes
        self._slots = asyncio.Semaphore(max_inflight)
        self._health_slots = asyncio.Semaphore(2)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status: int, reason: str) -> None:
            overloaded = status == 503
            response = JSONResponse(status_code=status, content={
                "ok": False,
                "code": "RESOURCE_EXHAUSTED" if overloaded else "INVALID_ARGS",
                "error": {"message": reason, "reason": reason,
                          "retryable": overloaded, "needs_human": False},
            }, headers={"Retry-After": "1"} if overloaded else None)
            await response(scope, receive, send)

        health = scope.get("method") in {"GET", "HEAD"} and scope.get("path") in HEALTH_PATHS
        budget = min(self.max_bytes, HEALTH_REQUEST_BYTES) if health else self.max_bytes
        lengths = [value for key, value in scope.get("headers", []) if key.lower() == b"content-length"]
        try:
            if any(int(value) < 0 for value in lengths) or len(set(lengths)) > 1:
                await reject(400, "invalid_content_length")
                return
            if any(int(value) > budget for value in lengths):
                await reject(413, "request_body_too_large")
                return
        except (ValueError, OverflowError):
            await reject(400, "invalid_content_length")
            return

        slots = self._health_slots if health else self._slots
        # Semaphore.acquire does not suspend when a permit is available. This
        # check/acquire pair is atomic on the ASGI event loop and queues no waiters.
        if slots.locked():
            await reject(503, "request_capacity_exhausted")
            return
        await slots.acquire()
        try:
            body = bytearray()
            deadline = time.monotonic() + (2 if health else 30)
            while True:
                try:
                    message = await asyncio.wait_for(receive(), max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    await reject(408, "request_body_timeout")
                    return
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > budget:
                    await reject(413, "request_body_too_large")
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            replayed = False

            async def bounded_receive() -> Message:
                nonlocal replayed, body
                if not replayed:
                    replayed = True
                    payload = bytes(body)
                    body.clear()
                    return {"type": "http.request", "body": payload, "more_body": False}
                return await receive()

            # Retain the permit while downstream may still own the buffered body.
            await self.app(scope, bounded_receive, send)
        finally:
            slots.release()
