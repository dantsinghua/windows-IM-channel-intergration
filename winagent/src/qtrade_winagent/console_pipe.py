"""SID-authenticated local console credentials and elevated install bootstrap."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable


class ConsoleTokenPipe:
    def __init__(self, backend: Any, *, pipe_name: str, install_user_sid: str,
                 tokens: Any, endpoint_provider: Callable,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._backend = backend
        self._name = pipe_name
        self._sid = install_user_sid
        self._tokens = tokens
        self._endpoint = endpoint_provider
        self._clock = clock
        self._task = None
        self._clients: set[asyncio.Task] = set()

    async def handle(self, conn: Any) -> None:
        try:
            if not self._sid or conn.peer_sid != self._sid:
                await conn.send_json({"error": "FORBIDDEN"})
                return
            request = await asyncio.wait_for(conn.recv_json(), timeout=3)
            if not isinstance(request, dict):
                await conn.send_json({"error": "INVALID_ARGS"})
                return
            if request.get("op") == "issue_bootstrap":
                if not getattr(conn, "peer_elevated", False):
                    await conn.send_json({"error": "FORBIDDEN"})
                    return
                if not self._tokens.console or not self._tokens.agent:
                    await conn.send_json({"error": "NOT_READY"})
                    return
                await conn.send_json({"console_token": self._tokens.console,
                                      "winagent_token": self._tokens.agent})
                return
            if request.get("op") != "issue_console_token":
                await conn.send_json({"error": "INVALID_ARGS"})
                return
            if not self._tokens.console:
                await conn.send_json({"error": "NOT_READY"})
                return
            try:
                endpoint = await asyncio.wait_for(self._endpoint(), timeout=5)
            except Exception:
                endpoint = None
            await conn.send_json({"agent_token": self._tokens.console if endpoint else None,
                                  "winagent_token": self._tokens.console,
                                  "expires_at": self._clock() + 3600000,
                                  "agent_base_url": endpoint or None})
        except (ValueError, TimeoutError):
            await conn.send_json({"error": "INVALID_ARGS"})
        finally:
            await conn.close()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="console-pipe")

    async def _run(self) -> None:
        while True:
            if len(self._clients) >= 16:
                await asyncio.wait(self._clients, return_when=asyncio.FIRST_COMPLETED)
            conn = await self._backend.serve(self._name, allow_sid=self._sid)
            task = asyncio.create_task(self._handle_safely(conn))
            self._clients.add(task)
            task.add_done_callback(self._clients.discard)

    async def _handle_safely(self, conn) -> None:
        try:
            await self.handle(conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No request, exception text, or response credentials in logs.
            await conn.close()

    async def stop(self) -> None:
        tasks = [t for t in [self._task, *self._clients] if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
