"""工作流引擎(02 §2.2.6 / §3.4.3 #38~#47):步骤严格串行、失败即停、每步留痕、``workflow`` 事件四态、
内置 ``webhook``/``sleep``、取消与挂起续跑、有运行中 run 不许删。"""
from __future__ import annotations

import json

import pytest

from qtrade_agent.events import Events
from qtrade_agent.models import CommandError, CommandResult
from qtrade_agent.webhook import FakeHttp, HttpResponse
from qtrade_agent.workflow import WorkflowEngine
from qtrade_agent.workflow.model import WorkflowParseError

YAML_TWO = """name: two_steps
version: 1
steps:
  - id: s1
    op: send_text
    account: qd01
    args: { session: "qd01:10001", text: "一" }
  - id: s2
    op: send_text
    account: qq03
    args: { session: "qq03:10002", text: "二" }
"""


class FakeBus:
    """按 op+account 编排结果;同时落一行 ``commands``(让 ``workflow_run_id`` 回填可被断言)。"""

    def __init__(self, store, clock, *, results=None):
        self._store = store
        self._clock = clock
        self.results = results or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def submit(self, cmd):
        self.calls.append((cmd.account_id, cmd.op, dict(cmd.args)))
        self._store.insert_command(trace_id=cmd.trace_id, account_id=cmd.account_id, op=cmd.op,
                                   args_json=json.dumps(cmd.args, ensure_ascii=False), idempotency_key=None,
                                   confirm=True, timeout_ms=cmd.timeout_ms, transport=cmd.origin.transport,
                                   actor=cmd.origin.actor, ip=None, now_ms=self._clock())
        spec = self.results.get(f"{cmd.account_id}:{cmd.op}", ("OK", True, False))
        code, ok, needs_human = spec
        err = None if ok else CommandError("失败了", reason="boom", needs_human=needs_human)
        return CommandResult(ok=ok, code=code, trace_id=cmd.trace_id, error=err)


def _engine(store, clock, *, bus=None, events=None, http=None, sleeps=None):
    async def sleep_fn(sec):
        (sleeps if sleeps is not None else []).append(sec)

    return WorkflowEngine(store, bus=bus, events=events, http=http, clock=clock, sleep_fn=sleep_fn)


def _wf_events(store):
    return [e["payload"] for e in store.list_events(event="workflow")]


# ---------------------------------------------------------------- 定义 CRUD(#38~#42)
def test_upsert_creates_then_bumps_version_and_records_checksum(store, clock):
    eng = _engine(store, clock)
    row = eng.upsert(name="two_steps", yaml=YAML_TWO)
    assert row["version"] == 1 and len(row["checksum"]) == 64 and row["enabled"] == 1
    row2 = eng.upsert(name="two_steps", yaml=YAML_TWO.replace("一", "壹"))
    assert row2["version"] == 2 and row2["id"] == row["id"] and row2["checksum"] != row["checksum"]
    assert [a["action"] for a in store.list_audit("workflow.upsert")] == ["workflow.upsert"] * 2


def test_upsert_rejects_invalid_yaml_with_line_numbers(store, clock):
    eng = _engine(store, clock)
    with pytest.raises(WorkflowParseError) as e:
        eng.upsert(name="bad", yaml="name: bad\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n")
    assert e.value.as_list()[0]["line"] >= 1


def test_upsert_rejects_name_mismatch(store, clock):
    with pytest.raises(WorkflowParseError):
        _engine(store, clock).upsert(name="other", yaml=YAML_TWO)


def test_delete_blocked_while_a_run_is_active(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    store.con.execute("INSERT INTO workflow_runs(run_id, workflow_id, workflow_version, trigger, actor, status, started_ms) "
                      "VALUES ('r1',?,1,'api','x','running',?)", (wid, clock()))
    with pytest.raises(RuntimeError, match="workflow_has_active_runs"):
        eng.delete(wid)
    store.con.execute("UPDATE workflow_runs SET status='done' WHERE run_id='r1'")
    assert eng.delete(wid) is True                                     # 终态 run 连同 steps 一并删(见 delete 的编码口径)
    assert store.con.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 0
    assert json.loads(store.list_audit("workflow.delete")[0]["detail_json"])["runs_deleted"] == 1


# ---------------------------------------------------------------- 正路(#43/#45)
async def test_run_executes_steps_in_order_and_records_each(store, clock):
    events = Events(store)
    bus = FakeBus(store, clock)
    eng = _engine(store, clock, bus=bus, events=events)
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, {"k": 1}, "api", wait=True)
    st = eng.status(run_id)
    assert st["run"]["status"] == "done" and st["run"]["trigger"] == "api"
    assert json.loads(st["run"]["args_json"]) == {"k": 1} and st["run"]["workflow_version"] == 1
    assert [(s["idx"], s["step_name"], s["status"]) for s in st["steps"]] == [(0, "s1", "done"), (1, "s2", "done")]
    assert [c[0] for c in bus.calls] == ["qd01", "qq03"]              # 严格串行、按 YAML 顺序
    assert [c[2]["text"] for c in bus.calls] == ["一", "二"]
    assert [p["status"] for p in _wf_events(store)] == ["started", "finished"]
    assert _wf_events(store)[0]["workflow"] == "two_steps" and _wf_events(store)[0]["run_id"] == run_id


async def test_step_trace_id_backfills_commands_workflow_columns(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    step = eng.steps(run_id)[0]
    cmd = store.get_command(step["trace_id"])
    assert cmd["workflow_run_id"] == run_id and cmd["workflow_step_id"] == step["step_id"]


async def test_runs_listing_and_unknown_workflow(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    await eng.run(wid, wait=True)
    await eng.run(wid, wait=True)
    assert len(eng.runs(wid)) == 2
    with pytest.raises(KeyError):
        await eng.run("nope")


async def test_unknown_trigger_rejected(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    with pytest.raises(ValueError):
        await eng.run(wid, trigger="message")


# ---------------------------------------------------------------- 失败即停(R-13)
async def test_failure_stops_the_run_and_later_steps_never_run(store, clock):
    events = Events(store)
    bus = FakeBus(store, clock, results={"qd01:send_text": ("SEND_FAILED", False, False)})
    eng = _engine(store, clock, bus=bus, events=events)
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    st = eng.status(run_id)
    assert st["run"]["status"] == "failed" and st["run"]["error"] == "s1:SEND_FAILED"
    assert len(st["steps"]) == 1 and st["steps"][0]["status"] == "failed"
    assert st["steps"][0]["result_code"] == "SEND_FAILED" and "reason=boom" in st["steps"][0]["note"]
    assert len(bus.calls) == 1
    last = _wf_events(store)[-1]
    assert last["status"] == "failed" and last["step_id"] == st["steps"][0]["step_id"]


async def test_needs_human_result_marks_run_needs_human(store, clock):
    events = Events(store)
    bus = FakeBus(store, clock, results={"qd01:send_text": ("LOGIN_REQUIRED", False, True)})
    eng = _engine(store, clock, bus=bus, events=events)
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.status(run_id)["run"]["status"] == "needs_human"
    assert eng.steps(run_id)[0]["status"] == "needs_human"
    assert _wf_events(store)[-1]["status"] == "needs_human"


async def test_step_exception_fails_the_run_without_swallowing(store, clock):
    class Boom(FakeBus):
        async def submit(self, cmd):
            raise RuntimeError("适配器炸了")

    eng = _engine(store, clock, bus=Boom(store, clock), events=Events(store))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    st = eng.status(run_id)
    assert st["run"]["status"] == "failed" and st["steps"][0]["result_code"] == "INTERNAL"
    assert "适配器炸了" in st["steps"][0]["note"]


# ---------------------------------------------------------------- 内置步骤
async def test_builtin_webhook_posts_previous_results_to_settings_url(store, clock):
    store.settings_set("callback_url", "https://biz.example/wf")
    http = FakeHttp()
    eng = _engine(store, clock, bus=FakeBus(store, clock), events=Events(store), http=http)
    y = YAML_TWO + '  - id: s3\n    op: webhook\n    args: { url_ref: "settings.callback_url" }\n'
    wid = eng.upsert(name="two_steps", yaml=y)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.status(run_id)["run"]["status"] == "done"
    body = json.loads(http.requests[0]["body"])
    assert http.requests[0]["url"] == "https://biz.example/wf"
    assert body["run_id"] == run_id and [r["id"] for r in body["results"]] == ["s1", "s2"]
    assert eng.steps(run_id)[2]["account_id"] is None                 # 内置步骤不挂账号(§3.1)


async def test_builtin_webhook_without_settings_value_needs_human(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock), events=Events(store), http=FakeHttp())
    y = 'name: only_hook\nversion: 1\nsteps:\n  - id: s1\n    op: webhook\n    args: { url_ref: "settings.callback_url" }\n'
    wid = eng.upsert(name="only_hook", yaml=y)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.status(run_id)["run"]["status"] == "needs_human"


async def test_builtin_webhook_non_2xx_fails_the_run(store, clock):
    store.settings_set("callback_url", "https://biz.example/wf")
    http = FakeHttp(default=HttpResponse(500))
    eng = _engine(store, clock, bus=FakeBus(store, clock), events=Events(store), http=http)
    y = 'name: only_hook\nversion: 1\nsteps:\n  - id: s1\n    op: webhook\n    args: { url_ref: "settings.callback_url" }\n'
    wid = eng.upsert(name="only_hook", yaml=y)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.status(run_id)["run"]["status"] == "failed"
    assert eng.steps(run_id)[0]["note"] == "http_500"


async def test_builtin_sleep_uses_injected_sleep(store, clock):
    sleeps: list[float] = []
    eng = _engine(store, clock, bus=FakeBus(store, clock), events=Events(store), sleeps=sleeps)
    y = "name: nap\nversion: 1\nsteps:\n  - id: s1\n    op: sleep\n    args: { seconds: 3 }\n"
    wid = eng.upsert(name="nap", yaml=y)["id"]
    run_id = await eng.run(wid, wait=True)
    assert sleeps == [3.0] and eng.status(run_id)["run"]["status"] == "done"


# ---------------------------------------------------------------- 取消 / 挂起续跑(#46)
async def test_cancel_marks_run_cancelled(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock), events=Events(store))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.cancel(run_id) is False                                # 已终态,取消无效
    store.con.execute("UPDATE workflow_runs SET status='running' WHERE run_id=?", (run_id,))
    assert eng.cancel(run_id) is True
    assert eng.status(run_id)["run"]["status"] == "cancelled"


async def test_pause_then_resume_continues_from_next_step(store, clock):
    bus = FakeBus(store, clock, results={"qq03:send_text": ("SEND_FAILED", False, False)})
    eng = _engine(store, clock, bus=bus, events=Events(store))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.status(run_id)["run"]["status"] == "failed"             # s1 done、s2 failed
    store.con.execute("UPDATE workflow_runs SET status='paused', pause_reason='wechat_switch' WHERE run_id=?", (run_id,))
    bus.results = {}                                                   # 人处理完,s2 这次能过
    assert await eng.resume(run_id, wait=True) is True
    st = eng.status(run_id)
    assert st["run"]["status"] == "done" and st["run"]["pause_reason"] is None
    assert [s["step_name"] for s in st["steps"]] == ["s1", "s2", "s2"]  # s1 不重跑,s2 再留一行痕
    assert len(bus.calls) == 3


async def test_pause_only_applies_to_running_and_resume_only_to_paused(store, clock):
    eng = _engine(store, clock, bus=FakeBus(store, clock), events=Events(store))
    wid = eng.upsert(name="two_steps", yaml=YAML_TWO)["id"]
    run_id = await eng.run(wid, wait=True)
    assert eng.pause(run_id, "x") is False
    assert await eng.resume(run_id) is False
