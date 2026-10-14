"""Bound HTTP input before authentication or JSON parsing (including chunked bodies)."""
import asyncio
import time
from starlette.responses import JSONResponse

MAX_REQUEST_BYTES = 4 * 1024 * 1024

class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes=MAX_REQUEST_BYTES, max_inflight=16):
        self.app=app; self.max_bytes=max_bytes; self._slots=asyncio.Semaphore(max_inflight)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        async def reject(status, reason):
            response=JSONResponse(status_code=status,content={"ok":False,"code":"INVALID_ARGS","error":{"message":reason,"reason":reason,"retryable":False,"needs_human":False}})
            await response(scope,receive,send)
        lengths=[v for k,v in scope.get("headers",[]) if k.lower()==b"content-length"]
        try:
            if any(int(value)<0 for value in lengths) or len(set(lengths))>1:
                return await reject(400,"invalid_content_length")
            if any(int(value)>self.max_bytes for value in lengths):
                return await reject(413,"request_body_too_large")
        except (ValueError,OverflowError):
            return await reject(400,"invalid_content_length")
        async with self._slots:
            body=bytearray(); deadline=time.monotonic()+30
            while True:
                try:
                    message=await asyncio.wait_for(receive(),timeout=max(0.001,deadline-time.monotonic()))
                except asyncio.TimeoutError:
                    return await reject(408,"request_body_timeout")
                if message["type"]=="http.disconnect": return
                chunk=message.get("body",b"")
                if len(body)+len(chunk)>self.max_bytes:
                    return await reject(413,"request_body_too_large")
                body.extend(chunk)
                if not message.get("more_body",False): break
            replayed=False
            async def bounded_receive():
                nonlocal replayed,body
                if not replayed:
                    replayed=True; payload=bytes(body); body=bytearray()
                    return {"type":"http.request","body":payload,"more_body":False}
                return await receive()
            await self.app(scope,bounded_receive,send)
