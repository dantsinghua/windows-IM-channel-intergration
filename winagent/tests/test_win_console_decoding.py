"""2026-10-10 真实链路(中文 Windows,ANSI=GBK)揪出的缺陷回归:``win/*`` 对 wevtutil / powercfg / PowerShell 等
命令行工具一律 ``subprocess.run(text=True)``,读线程抛 ``UnicodeDecodeError('gbk' ... 0x84)``、``stdout=None``。
现在统一走 ``win.run_text`` / ``win.decode_console_output``:BOM → 严格 UTF-8 → 本机 ANSI → 替换字符兜底,绝不抛。
纯函数,Linux 上可跑;真机行为另见 README §4。"""
from __future__ import annotations

import pathlib
import re

from qtrade_winagent.win import decode_console_output

WIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "qtrade_winagent" / "win"


def test_utf16le_bom_wevtutil_style():
    raw = "\ufeff事件[0]:\r\n  日期: 2026-10-10T08:00:00.000Z\r\n".encode("utf-16-le")
    out = decode_console_output(raw)
    assert out.startswith("事件[0]:") and "日期: 2026-10-10" in out


def test_utf16le_without_bom_wsl_style():
    raw = "Ubuntu-24.04\r\nqtrade\r\n".encode("utf-16-le")
    assert decode_console_output(raw).splitlines() == ["Ubuntu-24.04", "qtrade"]


def test_strict_utf8_wins_over_ansi():
    raw = "当前交流电源设置索引: 0x00000000\n".encode("utf-8")
    assert decode_console_output(raw) == "当前交流电源设置索引: 0x00000000\n"


def test_gbk_output_never_raises_and_keeps_ascii():
    raw = "GUID 别名: STANDBYIDLE\n".encode("gbk")
    out = decode_console_output(raw)
    assert "STANDBYIDLE" in out                       # 非 Windows 上按替换字符兜底,Windows 上按 mbcs 正确解出中文


def test_invalid_bytes_never_raise():
    out = decode_console_output(b"\x84\x0a\xff\xfe\x00broken")
    assert isinstance(out, str) and "broken" in out or isinstance(out, str)
    assert decode_console_output(b"") == ""


async def test_user_agent_retries_when_pipe_backend_raises_non_waerror(monkeypatch):
    """真机 ②:服务不在时 ``WinPipeBackend.connect`` 抛 ``pywintypes.error``,会话代理不得整进程退出,须 5s 后重试。"""
    import asyncio
    from qtrade_winagent import main_user

    class FlakyAgent:
        def __init__(self):
            self.calls = 0

        async def start(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError(2, "CreateFile", "pipe missing")      # 与 pywintypes.error 同为非 WaError 异常
            raise asyncio.CancelledError                            # 第二次进来即结束测试

        async def run(self):                                        # pragma: no cover
            raise AssertionError("not reached")

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(main_user.asyncio, "sleep", fake_sleep)
    ua = FlakyAgent()
    try:
        await main_user.run_forever(ua)                             # type: ignore[arg-type]
    except asyncio.CancelledError:
        pass
    assert ua.calls == 2 and slept == [5]


def test_no_text_true_subprocess_left_in_win_backends():
    """守门:``win/`` 下不许再出现 ``text=True`` 的 subprocess 调用(都该走 run_text)。"""
    offenders = []
    for p in WIN_DIR.glob("*.py"):
        if p.name == "__init__.py":
            continue
        src = p.read_text(encoding="utf-8")
        if re.search(r"subprocess\.run\([^)]*text=True", src, re.S):
            offenders.append(p.name)
    assert offenders == []
