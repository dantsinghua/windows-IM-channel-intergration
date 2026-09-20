"""第五批独立验收 —— **确认的实现缺陷**留在这里(主文件 `test_spec_infra.py` 不留红)。

按规格原文写,当前实现跑不过;**不 skip / 不 xfail** —— 修好实现后整条挪回主文件即可。
"""
from __future__ import annotations

import json

from test_spec_infra import H, P, QD, TOK_W, add_hmac, hmac_get          # 同目录主文件的常量与工具
from test_spec_infra import c5, disk, http, rig5                          # noqa: F401 — 夹具需重新导出


def test_PD01_store_write_failure_through_bus_returns_disk_full_507(rig5, c5, monkeypatch):
    """02 §2.8.8「写入报错先判磁盘满(诊断顺序固定,HTTP=507 R-02)」逐字:

    「`store`/`mail`/`media` 任何写失败(SQLite `SQLITE_FULL` / `OSError ENOSPC` / `disk I/O error` / 文件写异常),
    **第一诊断项是磁盘满** —— 统一返回结果码 **`DISK_FULL`**(不是 `INTERNAL`;HTTP **`507 Insufficient Storage`**,
    基线 §8.3/§10,R-02)…`retryable=false`(盘满自动重试只会加剧)、`needs_human=true`;
    **非磁盘类写失败才回落 `INTERNAL`**。」

    实际行为:经 `bus` 的写路径(#26 `POST /accounts/{id}/send`,C-21「发送前先落库」⇒ `store.ingest` 的
    `write_guard` 已把 `ENOSPC` 转成 `DiskFullError`)被 `bus` 消费者的 `except Exception` 兜住,
    回 **`500` + `code='INTERNAL'` + `retryable=true` + `needs_human=false`**,只有 `error.message` 是磁盘满文案。
    三项都与规格相反 —— 且 `retryable=true` 会让调用方自动重试,正是 §2.8.8 明文禁止的那件事。

    建议修法:`bus` 消费者在 `except Exception` 之前先 `except DiskFullError`,产出
    `CommandResult(ok=False, code='DISK_FULL', error=CommandError(msg, retryable=False, needs_human=True))`;
    `api` 的 `HTTP_BY_CODE` 已有 `DISK_FULL → 507`,无需再改。"""
    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rig5.agent.store, "_ingest_one", boom)
    r = c5.post(f"{P}/accounts/{QD}/send",
                json={"session": f"{QD}:415011447", "text": "盘满了", "idempotency_key": "k-507"},
                headers=H(TOK_W))
    body = r.json()
    assert r.status_code == 507, body
    assert body["code"] == "DISK_FULL"
    assert body["error"]["retryable"] is False and body["error"]["needs_human"] is True
