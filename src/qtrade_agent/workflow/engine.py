"""工作流引擎(02 §2.2.6 / §3.4.3 #38~#47;里程碑 **M5**,R3-29)。

- **职责**:YAML 工作流解析、运行、**每步留痕**;步骤 = 对 ``bus.submit`` 的调用。
- **对外接口**(§2.2.6 逐字):``run(workflow_id, args, trigger) -> run_id``、``cancel(run_id)``、``status(run_id)``。
- **内部状态**:运行中的 ``run_id → task``。
- **并发**:一个 run 一个 task;**步骤严格串行、失败即停**(R-13);跨账号的批量由调用方把每个账号各列一步
  (或用 ``POST /broadcast/commands`` #36),**引擎不做隐式展开 / 跨账号并行**。
- **固定工作流,无 LLM(A-1)**:顺序全由 YAML 决定,不接任何大模型做规划/选步/改参。
- **工作流只存表**,Agent 不读 ``/etc/qtrade`` 下任何工作流文件(G-09)。

留痕:每步一行 ``workflow_steps``(``idx`` = 线性执行序,``trace_id`` 对应 ``commands`` 行,``note`` 只放参数摘要/
失败原因、**不含正文**,基线 §11.2 [NOLOG])。事件:``workflow`` payload
``{run_id, workflow, status: "started|finished|needs_human|failed", step_id?}``(00 §7.5;#43 = ``started``,C-16)。

内置步骤(§3.1 ``workflow_steps.account_id`` 注「内置步骤(webhook/sleep)为 NULL」):
``webhook`` = 把**前面结果** POST 给业务系统(固定 URL,取自 ``settings.callback_url``);``sleep`` = 等 N 秒。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from ..ids import ulid
from ..maintenance import DiskFullError
from ..models import Command, CommandOrigin
from .model import WorkflowDef, WorkflowError, WorkflowParseError, parse_workflow

log = logging.getLogger("qtrade.workflow")

TRIGGERS = ("api", "schedule", "email", "console")      # §3.1 workflow_runs.trigger CHECK
RUN_RUNNING, RUN_PAUSED, RUN_DONE, RUN_FAILED, RUN_CANCELLED, RUN_NEEDS_HUMAN = \
    "running", "paused", "done", "failed", "cancelled", "needs_human"
STEP_RUNNING, STEP_DONE, STEP_FAILED, STEP_NEEDS_HUMAN = "running", "done", "failed", "needs_human"
# 00 §7.5 workflow 事件的 status 枚举(四值;cancelled/paused 不在其中——见 handoff「规格张力」)
EV_STARTED, EV_FINISHED, EV_NEEDS_HUMAN, EV_FAILED = "started", "finished", "needs_human", "failed"

SETTINGS_PREFIX = "settings."


class WorkflowCancelled(Exception):
    """``cancel(run_id)``(#46)触发:当前步骤中止、run 落 ``cancelled``。"""


@contextmanager
def _tx(store) -> Iterator[Any]:
    """薄封装:复用 ``Store`` 自己的事务与写锁(文件所有权约束:不往 ``store/`` 里加方法)。"""
    with store._tx() as c:                       # noqa: SLF001 - 见 docstring
        yield c


class WorkflowEngine:
    """直线流程引擎。``bus`` 需提供 ``async submit(Command) -> CommandResult``;
    ``http`` 需提供 ``webhook.HttpClient`` 协议(内置 ``webhook`` 步骤用,测试注 ``FakeHttp``)。"""

    def __init__(self, store, *, bus=None, events=None, http=None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep_fn: Callable[[float], Any] = asyncio.sleep, loader=None,
                 webhook_timeout_ms: int = 5000):
        self._store = store
        self._bus = bus
        self._events = events
        self._http = http
        self._clock = clock
        self._sleep = sleep_fn
        self._loader = loader
        self.webhook_timeout_ms = webhook_timeout_ms
        self.tasks: dict[str, asyncio.Task] = {}          # §2.2.6 内部状态:运行中的 run_id → task
        self._cancelled: set[str] = set()

    # ------------------------------------------------------------ 定义 CRUD(#38~#42)
    def upsert(self, *, name: str, yaml: str, enabled: bool = True, schedule_cron: Optional[str] = None,
               actor: str = "token:console", now_ms: Optional[int] = None) -> dict[str, Any]:
        """#39 ``POST /workflows`` / #41 ``PUT``:整体替换、``version+1``;解析校验失败抛 :class:`WorkflowParseError`。"""
        wf = parse_workflow(yaml, loader=self._loader)
        if wf.name != name:
            raise WorkflowParseError([WorkflowError(1, f"YAML 里的 name={wf.name!r} 与请求的 {name!r} 不一致")])
        now = now_ms if now_ms is not None else self._clock()
        with _tx(self._store) as c:
            row = c.execute("SELECT id, version FROM workflows WHERE name=?", (name,)).fetchone()
            if row is None:
                wid = ulid(now)
                c.execute("INSERT INTO workflows(id, name, version, yaml, checksum, enabled, schedule_cron, created_ms, updated_ms, updated_by) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (wid, name, 1, yaml, wf.checksum, int(enabled), schedule_cron, now, now, actor))
            else:
                wid = row["id"]
                c.execute("UPDATE workflows SET version=version+1, yaml=?, checksum=?, enabled=?, schedule_cron=?, updated_ms=?, updated_by=? WHERE id=?",
                          (yaml, wf.checksum, int(enabled), schedule_cron, now, actor, wid))
        self._store.insert_audit(kind="api", transport="http", actor=actor, action="workflow.upsert",
                                 detail={"workflow_id": wid, "name": name, "checksum": wf.checksum}, now_ms=now)
        return self.get(wid)                                   # type: ignore[return-value]

    def get(self, workflow_id: str) -> Optional[dict[str, Any]]:
        r = self._store.con.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        return dict(r) if r else None

    def by_name(self, name: str) -> Optional[dict[str, Any]]:
        r = self._store.con.execute("SELECT * FROM workflows WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None

    def list(self) -> list[dict[str, Any]]:
        """#38:列表(调用方去掉 ``yaml`` 全文)。"""
        return [dict(r) for r in self._store.con.execute("SELECT * FROM workflows ORDER BY name")]

    def delete(self, workflow_id: str, *, actor: str = "token:console") -> bool:
        """#42:有运行中(``running``/``paused``)run → ``409``(这里抛 ``RuntimeError``,由 api 层转信封)。

        🔴 编码口径:``workflow_runs.workflow_id`` 是**无 ``ON DELETE`` 的外键**(§3.1),
        不先删历史 run 行就永远删不掉定义(FK 直接拒绝),而 #42 只把「有运行中 run」定为拒绝条件 ⇒
        终态 run 连同其 ``workflow_steps``(该外键是 ``ON DELETE CASCADE``)在**同一事务**里一并删,
        删除条数记进审计。见 handoff「建议裁决」:要么 DDL 补 ``ON DELETE CASCADE``、要么规格写明历史去留。
        """
        row = self._store.con.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE workflow_id=? AND status IN ('running','paused')", (workflow_id,)).fetchone()
        if row and int(row[0]) > 0:
            raise RuntimeError("workflow_has_active_runs")
        with _tx(self._store) as c:
            runs = c.execute("DELETE FROM workflow_runs WHERE workflow_id=?", (workflow_id,)).rowcount
            deleted = c.execute("DELETE FROM workflows WHERE id=?", (workflow_id,)).rowcount > 0
        if deleted:
            self._store.insert_audit(kind="api", transport="http", actor=actor, action="workflow.delete",
                                     detail={"workflow_id": workflow_id, "runs_deleted": runs}, now_ms=self._clock())
        return deleted

    def definition(self, workflow_id: str) -> WorkflowDef:
        row = self.get(workflow_id)
        if row is None:
            raise KeyError(workflow_id)
        return parse_workflow(row["yaml"], loader=self._loader)

    # ------------------------------------------------------------ 运行(#43/#44/#45/#46)
    async def run(self, workflow_id: str, args: Optional[dict[str, Any]] = None, trigger: str = "api",
                  *, actor: str = "token:console", now_ms: Optional[int] = None, wait: bool = False) -> str:
        """#43:建 run 行 + 起一个 task,立即返回 ``run_id``(``202``);``wait=True`` 时等跑完(测试/同步调用)。"""
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}")
        wf = self.definition(workflow_id)
        row = self.get(workflow_id)
        assert row is not None
        now = now_ms if now_ms is not None else self._clock()
        run_id = ulid(now)
        with _tx(self._store) as c:
            c.execute("INSERT INTO workflow_runs(run_id, workflow_id, workflow_version, trigger, actor, args_json, status, started_ms) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (run_id, workflow_id, int(row["version"]), trigger, actor,
                       json.dumps(args or {}, ensure_ascii=False), RUN_RUNNING, now))
        self._emit(run_id, wf.name, EV_STARTED, now_ms=now)                 # C-16:#43 事件 workflow{status:'started'}
        task = asyncio.create_task(self._execute(run_id, wf, actor=actor), name=f"wf:{wf.name}:{run_id}")
        self.tasks[run_id] = task
        if wait:
            await self.wait(run_id)
        return run_id

    async def wait(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if task is None:
            return
        try:
            await task
        except (asyncio.CancelledError, WorkflowCancelled):
            pass

    def cancel(self, run_id: str) -> bool:
        """#46 ``POST /workflows/runs/{run_id}/cancel``:置取消标记并中止在跑的 task。"""
        row = self.status(run_id)["run"]
        if row is None or row["status"] not in (RUN_RUNNING, RUN_PAUSED):
            return False
        self._cancelled.add(run_id)
        task = self.tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        self._finish_run(run_id, RUN_CANCELLED, error="cancelled")
        return True

    def pause(self, run_id: str, reason: str) -> bool:
        """05 §2.4.5 账号切换挂起:run 落 ``paused``,由人 ``POST …/resume`` 续跑(#45/#46)。"""
        with _tx(self._store) as c:
            return c.execute("UPDATE workflow_runs SET status=?, pause_reason=? WHERE run_id=? AND status=?",
                             (RUN_PAUSED, reason, run_id, RUN_RUNNING)).rowcount > 0

    async def resume(self, run_id: str, *, actor: str = "token:console", wait: bool = False) -> bool:
        """#46 ``POST …/resume``:从 ``paused`` 续跑(已留痕的步骤不重跑,从下一步开始)。"""
        run = self._run_row(run_id)
        if run is None or run["status"] != RUN_PAUSED:
            return False
        wf = self.definition(run["workflow_id"])
        with _tx(self._store) as c:
            c.execute("UPDATE workflow_runs SET status=?, pause_reason=NULL WHERE run_id=?", (RUN_RUNNING, run_id))
        task = asyncio.create_task(self._execute(run_id, wf, actor=actor, start_idx=self._done_steps(run_id)),
                                   name=f"wf:{wf.name}:{run_id}:resume")
        self.tasks[run_id] = task
        if wait:
            await self.wait(run_id)
        return True

    def status(self, run_id: str) -> dict[str, Any]:
        """#45:``{run, steps:[…]}`` 每步留痕。"""
        return {"run": self._run_row(run_id), "steps": self.steps(run_id)}

    def runs(self, workflow_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """#44:分页(调用方套 §3.4 通用 cursor)。"""
        return [dict(r) for r in self._store.con.execute(
            "SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY started_ms DESC LIMIT ?", (workflow_id, limit))]

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._store.con.execute(
            "SELECT * FROM workflow_steps WHERE run_id=? ORDER BY idx", (run_id,))]

    # ------------------------------------------------------------ 执行体
    def _run_row(self, run_id: str) -> Optional[dict[str, Any]]:
        r = self._store.con.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def _done_steps(self, run_id: str) -> int:
        r = self._store.con.execute("SELECT COUNT(*) FROM workflow_steps WHERE run_id=? AND status=?",
                                    (run_id, STEP_DONE)).fetchone()
        return int(r[0]) if r else 0

    async def _execute(self, run_id: str, wf: WorkflowDef, *, actor: str, start_idx: int = 0) -> None:
        results: list[dict[str, Any]] = []
        try:
            for step in wf.steps[start_idx:]:
                if run_id in self._cancelled:
                    raise WorkflowCancelled(run_id)
                step_id, started = self._step_start(run_id, step)
                try:
                    ok, code, note, trace_id, needs_human = await self._run_step(run_id, step, results, actor=actor)
                except asyncio.CancelledError:
                    self._step_finish(step_id, STEP_FAILED, code=None, note="cancelled")
                    raise
                except DiskFullError as e:
                    # §2.8.8:步骤里的写失败先判磁盘满 —— 码是 `DISK_FULL` 不是 `INTERNAL`,且 `needs_human=true`
                    # ⇒ 与上面 `not ok and needs_human` 那条同口径落 NEEDS_HUMAN(人清完盘再续,不是让它自己重跑)。
                    self._step_finish(step_id, STEP_NEEDS_HUMAN, code="DISK_FULL", note=e.message[:400])
                    self._finish_run(run_id, RUN_NEEDS_HUMAN, error=f"{step.id}:DISK_FULL")
                    self._emit(run_id, wf.name, EV_NEEDS_HUMAN, step_id=step_id)
                    return
                except Exception as e:                       # 步骤内异常 = 失败即停(不吞)
                    self._step_finish(step_id, STEP_FAILED, code="INTERNAL", note=repr(e)[:400])
                    self._finish_run(run_id, RUN_FAILED, error=repr(e)[:400])
                    self._emit(run_id, wf.name, EV_FAILED, step_id=step_id)
                    return
                self._step_finish(step_id, STEP_DONE if ok else (STEP_NEEDS_HUMAN if needs_human else STEP_FAILED),
                                  code=code, note=note, trace_id=trace_id)
                results.append({"id": step.id, "op": step.op, "account": step.account, "ok": ok,
                                "result_code": code, "trace_id": trace_id})
                if not ok:                                   # R-13:失败即停,后续步不跑
                    status = RUN_NEEDS_HUMAN if needs_human else RUN_FAILED
                    self._finish_run(run_id, status, error=f"{step.id}:{code}")
                    self._emit(run_id, wf.name, EV_NEEDS_HUMAN if needs_human else EV_FAILED, step_id=step_id)
                    return
                log.debug("workflow %s 步 %s 完成(%s,耗时 %d ms)", wf.name, step.id, code, self._clock() - started)
            self._finish_run(run_id, RUN_DONE)
            self._emit(run_id, wf.name, EV_FINISHED)
        except WorkflowCancelled:
            self._finish_run(run_id, RUN_CANCELLED, error="cancelled")
        finally:
            self._cancelled.discard(run_id)

    async def _run_step(self, run_id: str, step, results: list[dict[str, Any]], *, actor: str):
        """跑一步。返回 ``(ok, result_code, note, trace_id, needs_human)``。"""
        if step.op == "sleep":
            await self._sleep(float(step.args["seconds"]))
            return True, "OK", f"sleep {step.args['seconds']}s", None, False
        if step.op == "webhook":
            return await self._step_webhook(run_id, step, results)
        if self._bus is None:
            raise RuntimeError("workflow 引擎未接 bus,无法执行通道步骤")
        cmd = Command(account_id=step.account or "", op=step.op, args=dict(step.args),
                      origin=CommandOrigin(transport="local", actor=actor), trace_id=ulid(self._clock()))
        res = await self._bus.submit(cmd)
        needs_human = bool(res.error and res.error.needs_human)
        note = f"op={step.op}" + (f" reason={res.error.reason}" if res.error and res.error.reason else "")
        return bool(res.ok), res.code, note, res.trace_id, needs_human

    async def _step_webhook(self, run_id: str, step, results: list[dict[str, Any]]):
        """内置步骤:把**前面结果** POST 给业务系统(固定 URL,取自 ``settings.callback_url``)。"""
        ref = str(step.args["url_ref"])
        if not ref.startswith(SETTINGS_PREFIX):
            return False, "INVALID_ARGS", f"url_ref 只支持 settings.* 引用:{ref}", None, False
        url = self._store.settings_get(ref[len(SETTINGS_PREFIX):])
        if not isinstance(url, str) or not url:
            return False, "INVALID_ARGS", f"settings 里没有 {ref}", None, True
        if self._http is None:
            raise RuntimeError("workflow 引擎未接 http 客户端,无法执行内置 webhook 步骤")
        run = self._run_row(run_id) or {}
        body = json.dumps({"run_id": run_id, "workflow_id": run.get("workflow_id"), "step_id": step.id,
                           "results": results}, ensure_ascii=False).encode("utf-8")
        resp = await self._http.post(url, body=body, headers={"Content-Type": "application/json; charset=utf-8"},
                                     timeout_ms=self.webhook_timeout_ms)
        ok = 200 <= resp.status < 300
        return ok, "OK" if ok else "INTERNAL", f"http_{resp.status}", None, False

    # ------------------------------------------------------------ 留痕
    def _step_start(self, run_id: str, step) -> tuple[str, int]:
        now = self._clock()
        step_id = ulid(now)
        with _tx(self._store) as c:
            c.execute("INSERT INTO workflow_steps(step_id, run_id, idx, step_name, op, account_id, status, started_ms) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (step_id, run_id, step.idx, step.id, step.op, step.account, STEP_RUNNING, now))
        return step_id, now

    def _step_finish(self, step_id: str, status: str, *, code: Optional[str] = None, note: Optional[str] = None,
                     trace_id: Optional[str] = None) -> None:
        now = self._clock()
        with _tx(self._store) as c:
            c.execute("UPDATE workflow_steps SET status=?, result_code=?, note=?, trace_id=?, finished_ms=? WHERE step_id=?",
                      (status, code, note, trace_id, now, step_id))
            if trace_id:
                # §3.1 commands.workflow_run_id / workflow_step_id:由工作流发起时回填(bus 只知道自己那层)
                run = c.execute("SELECT run_id FROM workflow_steps WHERE step_id=?", (step_id,)).fetchone()
                c.execute("UPDATE commands SET workflow_run_id=?, workflow_step_id=? WHERE trace_id=?",
                          (run["run_id"] if run else None, step_id, trace_id))

    def _finish_run(self, run_id: str, status: str, *, error: Optional[str] = None) -> None:
        now = self._clock()
        with _tx(self._store) as c:
            c.execute("UPDATE workflow_runs SET status=?, error=?, finished_ms=? WHERE run_id=?",
                      (status, error, now, run_id))

    def _emit(self, run_id: str, workflow: str, status: str, *, step_id: Optional[str] = None,
              now_ms: Optional[int] = None) -> None:
        """00 §7.5:``workflow`` payload ``{run_id, workflow, status, step_id?}``(只发枚举里的四个 status)。"""
        if self._events is None:
            return
        payload: dict[str, Any] = {"run_id": run_id, "workflow": workflow, "status": status}
        if step_id is not None:
            payload["step_id"] = step_id
        self._events.emit("workflow", payload=payload, trace_id=run_id, now_ms=now_ms)
