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
import ntpath
import os
import sys
from typing import Optional

from . import __version__
from .config import WinAgentConfig, load as load_cfg
from .errors import BUSY, FORBIDDEN, VERSION_MISMATCH, WaError
from .logfmt import get_logger
from .main_svc import DEFAULT_ROOT, contain, expand, read_toml, setup_logging, validate_install_sid
from .pipe import BUSY_RETRY_S
from .user import UserAgent

log = get_logger("user")


def build_user_agent(cfg: WinAgentConfig, *, session_id: str, user_sid: str, root: str = DEFAULT_ROOT,
                     fake: bool = False) -> UserAgent:
    if not fake:
        cfg = cfg.with_wechat(bundled_installer=expand(cfg.wechat.bundled_installer, root))
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
        wx = WinWeChat(exe_path=cfg.wechat.exe_path, chatlog_dir=expand(cfg.wechat.chatlog_dir, root),
                       chatlog_port=cfg.wechat.chatlog_port,
                       chatlog_work_dir=os.path.expandvars(cfg.wechat.chatlog_work_dir),
                       main_wnd_class=cfg.wechat.main_wnd_class,
                       process_close_grace_s=cfg.wechat.process_close_grace_s)
    # `.wslconfig` 备份由会话代理写入;fake(``--dev``)时一律重基到 ``root`` 下,
    # 与服务侧 ``build_real_deps`` 同一条自包含口径(不靠「%ProgramData% 展不开」的巧合)。
    return UserAgent(cfg, pipe=pipe, wsl=wsl, wechat=wx, power=power, session_id=session_id,
                     user_sid=user_sid, version=__version__,
                     backup_dir=(contain if fake else expand)(cfg.wsl.backup_dir, root))


async def run_forever(ua: UserAgent) -> int:
    try:
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
            except Exception as e:                                      # noqa: BLE001
                # 2026-10-10 真机实测:服务不在时 ``WinPipeBackend.connect`` 抛 ``pywintypes.error(2, 'CreateFile', …)``,
                # 不是 ``WaError`` ⇒ 这里原本直接把会话代理整个进程炸掉,服务一回来也没人再连管道(health.user_agent 永远 false)。
                # 任何连接期异常都按「服务暂不可达」处理:记日志、5s 后重试,不退出(退出只留给 FORBIDDEN / VERSION_MISMATCH)。
                log.warning("连不上服务管道,5s 后重试", extra={"op": "pipe.hello", "code": "PIPE_UNAVAILABLE",
                                                             "kv": {"err": type(e).__name__}})
                await asyncio.sleep(5)
                continue
            log.info("会话代理已上线", extra={"op": "pipe.hello", "code": "OK", "kv": {"ipc": welcome.get("ipc_version")}})
            await ua.run()
            log.warning("与服务的管道断开,5s 后重连", extra={"op": "pipe.hello", "code": "DISCONNECTED"})
            await asyncio.sleep(5)
    finally:
        await ua.stop()


def current_user_sid() -> str:
    token = None
    try:
        import win32api
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    except Exception:
        return ""
    finally:
        if token is not None:
            token.Close()


def user_log_dir() -> str:
    """用户代理不写 LocalSystem 服务目录;缺少完整用户路径时明确拒绝启动。"""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not ntpath.isabs(local_app_data) or not ntpath.splitdrive(local_app_data)[0]:
        raise ValueError("LOCALAPPDATA must be an absolute Windows user directory")
    return ntpath.normpath(ntpath.join(local_app_data, "QTrade", "logs", "user"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qtrade-winagent-user")
    ap.add_argument("--root", default=None, help="%%ProgramData%%\\QTrade\\winagent(--dev 下必填)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--session-id", default=os.environ.get("SESSIONNAME", "Console"))
    ap.add_argument("--dev", action="store_true")
    args = ap.parse_args(argv)
    user_sid = current_user_sid()
    if not args.dev and not validate_install_sid(user_sid):
        ap.error("Unable to identify the interactive Windows user")
    if args.dev and not args.root:                                      # 同 main_svc:不许隐式落进生产目录
        ap.error("--dev 必须显式给 --root(否则默认落进生产目录 %ProgramData%\\QTrade\\winagent);"
                 "照 README §3 的写法:--dev --root /tmp/wa-dev")
    root = args.root or DEFAULT_ROOT
    cfg = load_cfg(read_toml(args.config or os.path.join(root, "winagent.toml")))
    try:
        log_dir = None if args.dev else user_log_dir()
    except ValueError as e:
        ap.error(str(e))
    setup_logging(cfg.log.level, log_dir)
    ua = build_user_agent(cfg, session_id=args.session_id, user_sid=user_sid, root=root,
                          fake=args.dev)
    return asyncio.run(run_forever(ua))


if __name__ == "__main__":
    sys.exit(main())
