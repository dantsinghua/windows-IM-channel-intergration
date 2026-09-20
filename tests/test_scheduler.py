"""scheduler(02 §2.2.11):每任务一个 task、同名不重入(跳过并计数)、trigger 立即触发、单轮异常不杀任务。"""
import asyncio

import pytest

from qtrade_agent.scheduler import Scheduler


async def test_interval_and_trigger():
    calls = []

    async def job():
        calls.append(1)

    s = Scheduler()
    s.register("j", 0.05, job)
    await s.start()
    await asyncio.sleep(0.18)
    n = len(calls)
    assert n >= 2                                   # 按间隔跑
    s.trigger("j")
    await asyncio.sleep(0.02)
    assert len(calls) >= n + 1                      # trigger 不等间隔
    await s.stop()
    snap = s.snapshot()["j"]
    assert snap["runs"] == len(calls) and snap["errors"] == 0


async def test_no_reentry_skips_and_counts():
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(0.3)

    s = Scheduler()
    s.register("slow", 0.02, slow)
    await s.start()
    await started.wait()
    await asyncio.sleep(0.15)
    for _ in range(3):
        s.trigger("slow")
        await asyncio.sleep(0.01)
    assert s.jobs["slow"].runs == 0 and s.jobs["slow"].skipped >= 1     # 上一轮没跑完:跳过并计数,不排队
    await s.stop()


async def test_exception_does_not_kill_task():
    n = {"i": 0}

    async def flaky():
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")

    s = Scheduler()
    s.register("f", 0.03, flaky, run_immediately=True)
    await s.start()
    await asyncio.sleep(0.12)
    await s.stop()
    snap = s.snapshot()["f"]
    assert snap["errors"] == 1 and snap["runs"] >= 2 and "boom" in snap["last_error"]


async def test_run_once_and_duplicate_name():
    async def job():
        pass
    s = Scheduler()
    s.register("a", 10, job)
    with pytest.raises(ValueError):
        s.register("a", 10, job)
    assert await s.run_once("a") is True and s.jobs["a"].runs == 1
