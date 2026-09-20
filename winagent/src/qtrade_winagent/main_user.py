"""``qtrade-winagent-user.exe`` 入口 —— 用户会话代理(计划任务「用户登录时」拉起,**不提权**;02 §2.4,C-02)。

它**不监听任何端口**(R-14:防火墙规则里绝不出现会话代理 exe),只作为 ``\\\\.\\pipe\\qtrade-winagent-user``
的客户端连服务。被服务拒绝时按 ``HELLO`` 的错误码处理:

- ``FORBIDDEN``(非安装用户)⇒ 退出,不重试(R3-12:这台机上它本就不该控 WSL)。
- ``BUSY``      ⇒ 每 ``retry_after_s``(30s)重试,等持有者断开后接管。
- ``VERSION_MISMATCH`` ⇒ 退出并提示整包升级(两者随同一安装包升级)。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from . import __version__
from .config import WinAgentConfig, load as load_cfg
from .errors import BUSY, FORBIDDEN, VERSION_MISMATCH, WaError
from .logfmt import get_logger
from .main_svc import DEFAULT_ROOT, expand, read_toml, setup_logging
from .pipe import BUSY_RETRY_S
from .user import UserAgent

log = get_logger("user")


def build_user_agent(cfg: WinAgentConfig, *, session_id: str, user_sid: str, root: str = DEFAULT_ROOT,
                     fake: bool = False) -> UserAgent:
    if fake:
        from .fakes import FakePipeBackend, FakePower, FakeWeChat, FakeWsl
        pipe, wsl, wx, power = FakePipeBackend(), FakeWsl(), FakeWeChat(), FakePower()
    else:
        from .win.pipes import WinPipeBackend
        from .win.power import WinPower
        from .win.wechat import WinWeChat
        from .win.wsl import WinWsl
        pipe = WinPipeBackend(max_frame_kb=cfg.ipc.max_frame_kb)
        wsl = WinWsl(distro=cfg.wsl.distro)
        power = WinPower()
        wx = WinWeChat(exe_path=cfg.wechat.exe_path, chatlog_dir=os.path.expandvars(cfg.wechat.chatlog_dir),
                       chatlog_port=cfg.wechat.chatlog_port, main_wnd_class=cfg.wechat.main_wnd_class,
                       process_close_grace_s=cfg.wechat.process_close_grace_s)
    return UserAgent(cfg, pipe=pipe, wsl=wsl, wechat=wx, power=power, session_id=session_id,
                     user_sid=user_sid, version=__version__,
                     backup_dir=expand(cfg.wsl.backup_dir, root))


async def run_forever(ua: UserAgent) -> int:
    while True:
        try:
            welcome = await ua.start()
        except WaError as e:
            if e.code == BUSY:
                log.info("WSL 控制权被别的会话持有,30s 后重试", extra={"op": "pipe.hello", "code": BUSY})
                await asyncio.sleep(int(e.extra.get("retry_after_s") or BUSY_RETRY_S))
                continue
            if e.code == FORBIDDEN:
                log.error("非安装用户会话:本代理不控 WSL,退出", extra={"op": "pipe.hello", "code": FORBIDDEN})
                return 3
            if e.code == VERSION_MISMATCH:
                log.error("与服务主版本不一致,请整包升级", extra={"op": "pipe.hello", "code": VERSION_MISMATCH})
                return 4
            log.warning("握手失败,5s 后重试", extra={"op": "pipe.hello", "code": e.code})
            await asyncio.sleep(5)
            continue
        log.info("会话代理已上线", extra={"op": "pipe.hello", "code": "OK", "kv": {"ipc": welcome.get("ipc_version")}})
        await ua.run()
        log.warning("与服务的管道断开,5s 后重连", extra={"op": "pipe.hello", "code": "DISCONNECTED"})
        await asyncio.sleep(5)


def current_user_sid() -> str:
    try:
        import win32api
        import win32security
        sid, _ = win32security.LookupAccountName(None, win32api.GetUserName())
        return win32security.ConvertSidToStringSid(sid)
    except Exception:
        return os.environ.get("QT_USER_SID", "")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qtrade-winagent-user")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--config", default=None)
    ap.add_argument("--session-id", default=os.environ.get("SESSIONNAME", "Console"))
    ap.add_argument("--dev", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_cfg(read_toml(args.config or os.path.join(args.root, "winagent.toml")))
    sid = os.path.join(args.root, "logs")
    setup_logging(cfg.log.level, None if args.dev else sid)
    ua = build_user_agent(cfg, session_id=args.session_id, user_sid=current_user_sid(), root=args.root,
                          fake=args.dev)
    return asyncio.run(run_forever(ua))


if __name__ == "__main__":
    sys.exit(main())
