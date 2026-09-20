"""scheduler 模块(02 §2.2.11)—— 周期任务:每任务一个 task;同名任务不重入(上一轮没跑完就跳过本轮并计数);``trigger(name)`` 立即触发一轮。

本期注册的任务由 app.py 装配:企点全量轮 ``qidian_poll_all``(每 ``[adapters.qidian] poll_interval_s``)、群缺口 ``qidian_gaps_all``(每 ``gap_check_interval_s``)。
邮件/备份/保留期清理/资源自校准等任务在各自模块落地时再注册。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger("qtrade.scheduler")

TaskFn = Callable[[], Awaitable[None]]


@dataclass
class Job:
    name: str
    interval_s: float
    fn: TaskFn
    run_immediately: bool = False
    runs: int = 0
    skipped: int = 0            # 上一轮没跑完就到点 ⇒ 跳过并计数(02 §2.2.11:同名任务不重入)
    errors: int = 0
    last_started_ms: Optional[int] = None
    last_finished_ms: Optional[int] = None
    last_error: Optional[str] = None
    _running: bool = field(default=False, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)      # 计时循环
    _runner: Optional[asyncio.Task] = field(default=None, repr=False)    # 正在执行的那一轮
    _kick: Optional[asyncio.Event] = field(default=None, repr=False)


class Scheduler:
    def __init__(self, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.jobs: dict[str, Job] = {}
        self._clock = clock
        self._started = False

    def register(self, name: str, interval_s: float, fn: TaskFn, *, run_immediately: bool = False) -> Job:
        if name in self.jobs:
            raise ValueError(f"scheduler job already registered: {name}")
        job = Job(name=name, interval_s=interval_s, fn=fn, run_immediately=run_immediately)
        self.jobs[name] = job
        if self._started:
            self._spawn(job)
        return job

    async def start(self) -> None:
        self._started = True
        for job in self.jobs.values():
            self._spawn(job)

    async def stop(self) -> None:
        self._started = False
        tasks = [t for j in self.jobs.values() for t in (j._task, j._runner) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for j in self.jobs.values():
            j._task = None
            j._runner = None
            j._running = False

    def trigger(self, name: str) -> None:
        """立即触发一轮(不等间隔);任务正在跑时同样记一次 skipped,不排队重跑。"""
        job = self.jobs[name]
        if job._kick is not None:
            job._kick.set()

    async def run_once(self, name: str) -> bool:
        """同步地跑一轮(测试与 API 手动触发用);正在跑则跳过并返回 False。"""
        return await self._run(self.jobs[name])

    def _spawn(self, job: Job) -> None:
        job._kick = asyncio.Event()
        job._task = asyncio.create_task(self._loop(job), name=f"sched:{job.name}")

    async def _run(self, job: Job) -> bool:
        if job._running:
            job.skipped += 1
            log.warning("scheduler 任务 %s 上一轮未完成,跳过本轮(skipped=%d)", job.name, job.skipped)
            return False
        job._running = True
        job.last_started_ms = self._clock()
        try:
            await job.fn()
            job.runs += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:                      # 单轮异常不杀任务
            job.errors += 1
            job.last_error = repr(e)
            log.exception("scheduler 任务 %s 本轮异常: %s", job.name, e)
        finally:
            job.last_finished_ms = self._clock()
            job._running = False
        return True

    def _launch(self, job: Job) -> None:
        """到点 / 被 trigger:启动一轮;上一轮还在跑就跳过并计数(计时循环不等执行,否则「跳过」永远观察不到)。"""
        if job._running:
            job.skipped += 1
            log.warning("scheduler 任务 %s 上一轮未完成,跳过本轮(skipped=%d)", job.name, job.skipped)
            return
        job._runner = asyncio.create_task(self._run(job), name=f"sched-run:{job.name}")

    async def _loop(self, job: Job) -> None:
        assert job._kick is not None
        if job.run_immediately:
            self._launch(job)
        while True:
            try:
                await asyncio.wait_for(job._kick.wait(), timeout=job.interval_s)
            except asyncio.TimeoutError:
                pass
            job._kick.clear()
            self._launch(job)

    def snapshot(self) -> dict[str, dict]:
        return {n: {"interval_s": j.interval_s, "runs": j.runs, "skipped": j.skipped, "errors": j.errors,
                    "last_started_ms": j.last_started_ms, "last_finished_ms": j.last_finished_ms, "last_error": j.last_error}
                for n, j in self.jobs.items()}
