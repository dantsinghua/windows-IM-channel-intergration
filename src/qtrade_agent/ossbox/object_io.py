"""对象存储线程的窄生命周期守卫；取消等待不等于底层 IO 已经退出。"""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class ObjectIO:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def check_idle(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("oss_io_in_progress")

    async def run(self, operation: Callable[..., Any], *args: Any) -> Any:
        self.check_idle()
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        self._task = task
        # 被取消的调用方可能不再读取结果；仍取走异常，避免未读取Task的回溯泄露。
        task.add_done_callback(self._observe)
        return await asyncio.shield(task)

    @staticmethod
    def _observe(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
