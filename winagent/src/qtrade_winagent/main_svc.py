"""``qtrade-winagent-svc.exe`` 入口 —— 服务 ``QTradeWinAgent``(LocalSystem,``delayed-auto``;02 §2.4,C-02)。

装配顺序:配置 → 日志 → ``winagent.db`` → 后端(全 ``Win*``)→ 模块 → 管道枢纽 → FastAPI → uvicorn。

- **监听集合**由 ``netprobe.desired_listen()`` 算(``{127.0.0.1} ∪ vEthernet (WSL) IPv4``,**不绑 0.0.0.0**);
  子网变化时 H16 自愈:关旧 listener → 在新地址 listen → 刷防火墙 → 写 ``host.json`` → ``WSL_SUBNET_CHANGED``。
- ``ws="websockets"``(与 Agent 侧同,02 §2.4「同样 ws=websockets」),**不许 auto**。
- 后台周期任务:10s 采样 / 60s 慢采样 + net_state / 整分整点降采样 / 10min H14·H20·H21 / **每日 03:00 保留期清理**(R3-20)。
- ``--dev`` 用假后端起一份本机实例(**仅开发**;不碰真防火墙/电源/注册表/微信)。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any, Optional

from . import __version__
from .alerts import AlertBuffer
from .audit import Audit
from .config import WinAgentConfig, load as load_cfg
from .db import Db
from .installer_ops import InstallerOps
from .logfmt import WaFormatter, get_logger
from .monitor import DiskTarget, Monitor
from .netprobe import NetProbe
from .pipe import PipeHub
from .power import Power
from .svc import SvcDeps, Tokens, build_app
from .vault import Vault
from .wechat import WeChatHostsBlock, WeChatStore

log = get_logger("svc")
DEFAULT_ROOT = os.path.expandvars(r"%ProgramData%\QTrade\winagent")


def setup_logging(level: str, log_dir: Optional[str]) -> None:
    """**禁止任何模块自己 basicConfig**(02 §2.9);全进程只在这里配一次,按天滚动留 30 天。"""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    h: logging.Handler = logging.StreamHandler(sys.stderr)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        from logging.handlers import TimedRotatingFileHandler
        h = TimedRotatingFileHandler(os.path.join(log_dir, "winagent.log"), when="midnight", backupCount=30,
                                     encoding="utf-8")
    h.setFormatter(WaFormatter())
    root.addHandler(h)


def expand(path: str, root: str) -> str:
    """展开 ``%ProgramData%`` 一类环境变量;**在非 Windows 上展不开就落到 ``--root`` 下**。

    ``os.path.expandvars`` 只认当前平台的语法,Linux 上 ``%ProgramData%\QTrade\winagent\vault`` 会原样留下,
    于是被当成**相对当前目录**的路径创建出来(``--dev`` 冒烟时实测在仓库里拉了一坨 ``%ProgramData%\...`` 目录)。
    这里统一收口:展不开(仍含 ``%``)就取它的末段拼到 ``root`` 下,``--dev`` 因此是自包含的。
    """
    p = os.path.expandvars(path)
    if "%" not in p:
        return p
    tail = p.replace("\\", "/").split("QTrade/", 1)[-1] if "QTrade" in p.replace("\\", "/") else os.path.basename(p)
    return os.path.join(root, *[seg for seg in tail.split("/") if seg])


def read_toml(path: str) -> dict[str, Any]:
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def build_real_deps(cfg: WinAgentConfig, *, root: str, install_user_sid: str, fake: bool = False) -> SvcDeps:
    """真机用 ``Win*``;``fake=True``(``--dev`` 与测试)用 ``fakes.py``。两条路径**装配代码完全相同**。"""
    db = Db(os.path.join(root, "winagent.db")).open()
    audit = Audit(db)
    alerts = AlertBuffer(cfg.alert)
    if fake:
        from .fakes import (FakeCrypto, FakeFirewall, FakeHosts, FakeNet, FakePipeBackend, FakePower, FakeProbe,
                            FakeSys, FakeWsl)
        crypto, sysb, netb, fwb, pwrb, probeb, pipeb = (FakeCrypto(), FakeSys(), FakeNet(), FakeFirewall(),
                                                        FakePower(), FakeProbe(), FakePipeBackend())
        hostsb: Any = FakeHosts()
    else:
        from .win.dpapi import WinCrypto
        from .win.firewall import WinFirewall
        from .win.hosts import WinHosts
        from .win.netinfo import WinNet
        from .win.pipes import WinPipeBackend
        from .win.power import WinPower
        from .win.probe import WinProbe
        from .win.sysinfo import WinSys
        crypto = WinCrypto(scope=cfg.vault.dpapi_scope)
        sysb, netb, fwb = WinSys(), WinNet(), WinFirewall()
        pwrb, probeb = WinPower(), WinProbe()
        pipeb = WinPipeBackend(max_frame_kb=cfg.ipc.max_frame_kb)
        hostsb = WinHosts(backup_dir=expand(cfg.wechat.hosts_backup_dir, root),
                          backup_keep=cfg.wechat.hosts_backup_keep)
    vault = Vault(db, crypto, cfg.vault, root=expand(cfg.vault.dir, root), audit=audit)
    disks = [DiskTarget("programdata", expand(r"%ProgramData%\QTrade", root)),
             DiskTarget("vhdx", expand(r"%LOCALAPPDATA%", root)),
             DiskTarget("wechat_data", "D:\\", product_level=False)]
    monitor = Monitor(db, sysb, alerts, cfg.monitor, probe=probeb, disks=disks)
    svc_exe = os.path.join(root, "qtrade-winagent-svc.exe")            # R-14:防火墙规则 Program 限**服务 exe**
    netprobe = NetProbe(db, netb, fwb, probeb, cfg.net, cfg.probe, alerts, svc_exe=svc_exe, port=cfg.api.port)
    hub = PipeHub(pipeb, cfg.ipc, pipe_name=cfg.api.pipe_user, install_user_sid=install_user_sid,
                  version=__version__, alerts=alerts, audit=audit)
    installer = InstallerOps(db, kernel_dir=expand(cfg.wsl.kernel_dir, root),
                             wsl_backup_dir=expand(cfg.wsl.backup_dir, root), package_version=__version__,
                             crypto=crypto, audit=audit, hub=hub)
    return SvcDeps(cfg=cfg, db=db, audit=audit, vault=vault, monitor=monitor, netprobe=netprobe,
                   power=Power(db, pwrb, sysb, cfg.wechat, audit=audit), hub=hub,
                   wechat_store=WeChatStore(db), hosts_block=WeChatHostsBlock(hostsb, cfg.wechat, cfg.probe),
                   installer=installer, alerts=alerts, tokens=Tokens(), started_ms=int(time.time() * 1000))


async def load_tokens(d: SvcDeps) -> None:
    """两把令牌都存 Vault(C-04);首装缺失则生成并写入(``agent_token`` 的明文落盘由 03 安装器做,不在这里)。"""
    for attr, ref in (("agent", d.cfg.api.agent_token_ref), ("console", d.cfg.api.console_token_ref)):
        val = await d.vault.read(ref, trace_id="svc-boot", owner="system")
        if val is None:
            import secrets
            val = secrets.token_urlsafe(32)
            await d.vault.put(ref, val, scope="winagent", owner="system", trace_id="svc-boot")
        setattr(d.tokens, attr, val)


async def periodic(d: SvcDeps) -> None:
    """后台周期任务(04 §2.2 周期表 + R3-20 每日 03:00 清理)。任一轮抛异常只记日志,不让循环停。"""
    cfg = d.cfg
    next_slow = next_sweep = 0
    while True:
        now = int(time.time() * 1000)
        try:
            d.monitor.sample_fast()
            d.monitor.check_session_locked()
            listen = d.netprobe.desired_listen()
            if set(listen) != set(d.listen):
                d.netprobe.reconcile_listen(actual=d.listen)           # H16 自愈:刷防火墙 + 推 WSL_SUBNET_CHANGED
                d.listen = listen
            if now >= next_slow:
                d.monitor.sample_slow()
                d.monitor.check_disks()
                d.monitor.check_pending_reboot()
                d.monitor.aggregate()
                await d.netprobe.refresh()
                d.power.recheck()
                next_slow = now + cfg.monitor.slow_interval_s * 1000
            if next_sweep == 0:
                next_sweep = Db.next_sweep_ms(now)
            elif now >= next_sweep:
                deleted = d.db.retention_sweep(monitor=cfg.monitor, probe=cfg.probe, retention=cfg.retention)
                log.info("保留期清理完成", extra={"op": "db.retention", "code": "OK", "kv": {"deleted": deleted}})
                next_sweep = Db.next_sweep_ms(now)
        except Exception:
            log.exception("周期任务一轮失败")
        await asyncio.sleep(cfg.monitor.sample_interval_s)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qtrade-winagent-svc")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="%%ProgramData%%\\QTrade\\winagent")
    ap.add_argument("--config", default=None, help="winagent.toml 路径(默认 <root>\\winagent.toml)")
    ap.add_argument("--install-user-sid", default=os.environ.get("QT_INSTALL_USER_SID", ""),
                    help="安装用户 SID(R3-12 仲裁前的安全校验基准)")
    ap.add_argument("--dev", action="store_true", help="用假后端起一份本机实例(仅开发;不碰任何真系统状态)")
    args = ap.parse_args(argv)
    cfg = load_cfg(read_toml(args.config or os.path.join(args.root, "winagent.toml")))
    setup_logging(cfg.log.level, None if args.dev else os.path.join(args.root, "logs"))
    d = build_real_deps(cfg, root=args.root, install_user_sid=args.install_user_sid, fake=args.dev)

    async def boot() -> None:
        await load_tokens(d)
        alert = d.vault.startup_selfcheck()                            # 熵缺失 / ACL 放宽 ⇒ VAULT_ENTROPY_MISSING
        if alert:
            d.alerts.firing(alert["code"], subject=alert["subject"], severity=alert["severity"],
                            evidence=alert["evidence"])
        await d.hub.start()
        await d.netprobe.refresh()
        d.listen = d.netprobe.desired_listen()
        d.netprobe.firewall_ensure()
        asyncio.create_task(periodic(d), name="wa-periodic")

    app = build_app(d)

    @app.on_event("startup")
    async def _startup() -> None:                                       # noqa: D401
        await boot()

    import uvicorn
    host = d.netprobe.desired_listen()[0] if not args.dev else "127.0.0.1"
    log.info("WinAgent 服务启动", extra={"op": "svc.start", "code": "OK",
                                        "kv": {"listen": ",".join(d.listen), "port": cfg.api.port}})
    uvicorn.run(app, host=host, port=cfg.api.port, ws="websockets", log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
