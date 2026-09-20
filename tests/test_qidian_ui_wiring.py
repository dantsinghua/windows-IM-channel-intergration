"""企点 UI 执行层的**装配**测试:生产进程装得上、开发容器/测试保持未接。

背景(本批要修的生产缺口):`app.py` 此前恒把 `sender` 置成 `_sender_not_wired`、`main.py` 既不传
`sender` 也不传 `login_fn` ⇒ 装好的产品里企点**不能自动登录、不能发消息**。

🔴 本文件只构造对象、只读属性,**不 start、不连 docker/adb**(构造 `AdbCliBackend`/`DockerCliBackend`
本身不执行任何命令)。
"""
from __future__ import annotations

import inspect

from qtrade_agent.adapters.qidian.ui import QidianUi
from qtrade_agent.app import AgentApp, _sender_not_wired
from qtrade_agent.config import AgentConfig, DbConfig
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.vault_client import FakeVault
from tests.conftest import Clock


def _cfg(tmp_path) -> AgentConfig:
    return AgentConfig(db=DbConfig(path=str(tmp_path / "agent.db")))


def test_真机后端下_sender与login_fn都装上(tmp_path):
    """不注入假后端 = 生产路径:`QidianUi.send_text` 装为 sender、`QidianUi.login` 装为 login_fn。"""
    app = AgentApp(_cfg(tmp_path), clock=Clock()).open()
    try:
        assert isinstance(app.qidian_ui, QidianUi)
        assert app.adapters["qidian"]._sender == app.qidian_ui.send_text        # 不再是 _sender_not_wired
        assert app.accounts._login_fn is not None
        assert app.accounts._login_fn.__qualname__.startswith("QidianUi.login_fn")
        assert app.qidian_ui._adb is app.runtime._adb                           # runtime 与 UI 层共用同一条 adb
        assert "default" in app.qidian_ui.profiles                              # 定位表随包发布
    finally:
        app.store.close()


def test_注了假后端时保持未接_不影响既有测试(tmp_path):
    """开发容器/测试一律注假后端 ⇒ 维持 `_sender_not_wired` + `login_fn=None`(既有注入方式不被破坏)。"""
    app = AgentApp(_cfg(tmp_path), clock=Clock(), containers=FakeContainers(), adb=FakeAdb(), vault=FakeVault()).open()
    try:
        assert app.adapters["qidian"]._sender is _sender_not_wired
        assert app.accounts._login_fn.__name__ == "_login_not_wired"
    finally:
        app.store.close()


def test_显式注入的sender与login_fn优先(tmp_path):
    async def my_sender(acct, native_id, text):
        return True

    async def my_login(row, account, secret):
        return "running"

    app = AgentApp(_cfg(tmp_path), clock=Clock(), sender=my_sender, login_fn=my_login).open()
    try:
        assert app.adapters["qidian"]._sender is my_sender
        assert app.accounts._login_fn is my_login
    finally:
        app.store.close()


def test_装上去的两个回调签名与契约一致():
    """`SendFn` = (acct, native_id, text) -> bool;`LoginFn` = (row, account, secret) -> str|None。"""
    assert list(inspect.signature(QidianUi.send_text).parameters) == ["self", "acct", "native_id", "text"]
    fn = QidianUi(adb=FakeAdb()).login_fn()
    assert list(inspect.signature(fn).parameters) == ["row", "account", "secret"]


async def test_非企点通道不走企点UI(tmp_path):
    """同一个 login_fn 挂在 AccountService 上,QQ/微信的 row 进来一律回 None(不去点企点的控件)。"""
    adb = FakeAdb()
    fn = QidianUi(adb=adb).login_fn()
    assert await fn({"id": "qq03", "channel": "qq", "state": "logging_in"}, "a", "b") is None
    assert adb.calls == []
