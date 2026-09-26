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
import socket
import sys
import time
from typing import Any, Callable, Optional

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
LOOPBACK = "127.0.0.1"


def bind_listener(host: str, port: int, *, windows: bool = sys.platform == "win32",
                  sock_factory: Callable[..., socket.socket] = socket.socket) -> socket.socket:
    """绑一个 TCP 监听 socket(评审 B2)。

    🔴 Windows 上**必须** ``SO_EXCLUSIVEADDRUSE``、**绝不** ``SO_REUSEADDR``:后者在 Windows 上允许别的进程
    对同一 ``地址:端口`` 再绑一次并抢走连接(端口劫持)——而 17610 上跑的是带令牌的控制面。
    非 Windows(仅开发/测试)保持 ``SO_REUSEADDR``:POSIX 语义下它只放过 TIME_WAIT,不允许两个活跃监听共端口。
    绑定失败抛 ``OSError``(地址还不存在 = ``WSAEADDRNOTAVAIL`` / ``EADDRNOTAVAIL``),socket 不泄漏。
    """
    sock = sock_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if windows:
            # Python 在 Windows 上本就导出该常量;回退值 -5 = winsock.h 的 (int)(~SO_REUSEADDR),其中 Windows
            # SO_REUSEADDR=4(不能用本机 socket.SO_REUSEADDR 现算:Linux 上它是 2)
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.setblocking(False)
        sock.listen(2048)
    except OSError:
        sock.close()
        raise
    return sock


class Listeners:
    """WinAgent HTTP 监听集合 ``{127.0.0.1} ∪ vEthernet (WSL) IPv4``(评审 B2)。

    - 启动期:回环**必须**绑上(绑不上服务无意义,照常抛);vEthernet 地址此刻可能还不存在(WSL 未起)——
      **跳过 + 记 warn**,不让服务起不来。
    - 运行期(``sync``,由周期任务在期望集合与实际不一致时调):补绑缺的地址、挂到正在跑的 uvicorn 上;
      子网变了则关掉旧地址的监听(回环永不关)。uvicorn 还没完成 startup 时本轮不补绑,下一轮再试。
    - 挂载用的是 uvicorn ``Server.startup`` 里同一套协议工厂(``config.http_protocol_class`` + ``server_state``
      + ``lifespan.state``;uvicorn 0.53 实测),新 asyncio server 追加进 ``server.servers``,关机时由 uvicorn 一并关。
    """

    def __init__(self, port: int, *, bind: Callable[[str, int], socket.socket] = bind_listener):
        self.port = port
        self._bind = bind
        self.sockets: dict[str, socket.socket] = {}
        self.server: Any = None                                    # uvicorn.Server;main() 里挂上

    def actual(self) -> tuple[str, ...]:
        return tuple(self.sockets)

    def _try_bind(self, host: str, *, required: bool = False) -> Optional[socket.socket]:
        try:
            s = self._bind(host, self.port)
        except OSError as e:
            if required:
                raise
            log.warning("监听地址绑定失败,先跳过、由周期任务补绑", extra={
                "op": "svc.bind", "code": "BIND_FAILED", "kv": {"host": host, "port": self.port, "err": str(e)}})
            return None
        self.sockets[host] = s
        return s

    def open(self, hosts: tuple[str, ...]) -> tuple[str, ...]:
        for h in hosts:
            self._try_bind(h, required=(h == LOOPBACK))
        return self.actual()

    def _running(self) -> bool:
        return self.server is not None and bool(getattr(self.server, "started", False))

    async def _attach(self, sock: socket.socket) -> None:
        srv = self.server
        cfg = srv.config

        def create_protocol(_loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.Protocol:
            return cfg.http_protocol_class(config=cfg, server_state=srv.server_state,
                                           app_state=srv.lifespan.state, _loop=_loop)
        aserver = await asyncio.get_running_loop().create_server(create_protocol, sock=sock, backlog=cfg.backlog)
        srv.servers.append(aserver)

    def _detach(self, host: str) -> None:
        sock = self.sockets.pop(host)
        name = sock.getsockname()
        for a in list(getattr(self.server, "servers", None) or []):
            if any(s.getsockname() == name for s in (a.sockets or ())):
                a.close()                                          # 连同底层 socket 一起关
                self.server.servers.remove(a)
                break
        else:
            sock.close()
        log.info("关闭已失效的监听地址", extra={"op": "svc.bind", "code": "UNBOUND", "kv": {"host": host}})

    async def sync(self, want: tuple[str, ...]) -> tuple[str, ...]:
        """把实际监听对齐到 ``want``;返回对齐后的实际集合(可能仍缺地址——绑不上就等下一轮)。"""
        if not self._running():                                    # uvicorn 还没把启动期 socket 接过去:本轮不动
            return self.actual()
        for h in [h for h in self.sockets if h not in want and h != LOOPBACK]:
            self._detach(h)
        for h in want:
            if h in self.sockets:
                continue
            s = self._try_bind(h)
            if s is None:
                continue
            try:
                await self._attach(s)
            except Exception:
                log.exception("新监听挂载到 uvicorn 失败", extra={"op": "svc.bind", "code": "ATTACH_FAILED",
                                                                "kv": {"host": h}})
                self.sockets.pop(h, None)
                s.close()
            else:
                log.info("补绑监听地址", extra={"op": "svc.bind", "code": "OK", "kv": {"host": h, "port": self.port}})
        return self.actual()


async def align_listen(d: SvcDeps, listeners: Optional[Listeners],
                       reported: Optional[frozenset[str]]) -> Optional[frozenset[str]]:
    """周期任务里的 H16 一段:期望监听集合 vs 实际。返回「已上报过的期望集合」(下一轮传回来)。

    - 同一个期望集合只走一次 ``reconcile_listen`` 的告警/刷防火墙/``WSL_SUBNET_CHANGED``,绑不上就每轮静默重试;
    - 对齐之后再调一次 ``reconcile_listen``(此时一致)⇒ 解除 H16;
    - ``listeners=None``(``--dev``)保持旧语义:只告警,把期望集合当实际集合记下。
    """
    want = await asyncio.to_thread(d.netprobe.desired_listen)
    if set(want) == set(d.listen):
        return reported
    if listeners is None:
        await asyncio.to_thread(d.netprobe.reconcile_listen, actual=d.listen)
        d.listen = want
        return reported
    if frozenset(want) != reported:
        await asyncio.to_thread(d.netprobe.reconcile_listen, actual=d.listen)     # H16 + 刷防火墙 + WSL_SUBNET_CHANGED
        reported = frozenset(want)
    d.listen = await listeners.sync(want)
    if set(d.listen) == set(want):
        await asyncio.to_thread(d.netprobe.reconcile_listen, actual=d.listen)     # 一致 ⇒ 解除 H16
        reported = None
    return reported


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


def contain(path: str, root: str) -> str:
    """**自包含重基**:把配置里的一条**写入**路径钉进 ``root``,不看平台、不看变量展不展得开。

    ``--dev``/假后端装配走这条(README §3:「只起 HTTP + 假后端,**不动任何真系统状态**」)。
    🔴 它**不能**靠 ``expand()`` 的「展不开才兜底」——那只是 Linux 上的巧合:真 Windows 上
    ``%ProgramData%`` 展得开,vault / 内核落盘 / ``.wslconfig`` 备份会一起逃出 ``--root``
    落进生产目录 ``C:\\ProgramData\\QTrade\\``(实测:跑一次 pytest 就在生产熵文件的位置上
    留下一把 ``FakeCrypto`` 造的假熵)。所以这里是**显式开关**,与能否展开无关。

    取法:先 ``expandvars``,取 ``QTrade`` 段之后的相对段(没有 ``QTrade`` 段则取末段);
    仍带 ``%`` 的段与 ``.``/``..`` 一律丢弃,保证结果**绝对**且**无未展开变量**。
    """
    segs = [s for s in os.path.expandvars(path).replace("\\", "/").split("/") if s]
    segs = segs[segs.index("QTrade") + 1:] if "QTrade" in segs else segs[-1:]
    return os.path.abspath(os.path.join(root, *[s for s in segs if "%" not in s and s not in (".", "..")]))


def expand(path: str, root: str) -> str:
    """展开 ``%ProgramData%`` 一类环境变量(**生产语义**);**在非 Windows 上展不开才落到 ``--root`` 下**。

    Windows 上 ``%ProgramData%\\QTrade\\winagent\\vault`` 就该解析成真机那个真实目录——02 §7.2 的
    ``[vault] dir`` 默认值指的正是它。``os.path.expandvars`` 只认当前平台的语法,Linux 上该串会原样留下,
    于是被当成**相对当前目录**的路径创建出来(``--dev`` 冒烟时实测在仓库里拉了一坨 ``%ProgramData%\\...`` 目录),
    故展不开(仍含 ``%``)时兜回 ``root`` 下。

    🔴 **自包含不归这个函数管**:``--dev`` 的落点收口一律走 ``contain()``。
    """
    p = os.path.expandvars(path)
    if "%" not in p:
        return p
    return contain(p, root)


def read_toml(path: str) -> dict[str, Any]:
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def build_real_deps(cfg: WinAgentConfig, *, root: str, install_user_sid: str, fake: bool = False) -> SvcDeps:
    """真机用 ``Win*``;``fake=True``(``--dev`` 与测试)用 ``fakes.py``。两条路径**装配代码完全相同**。

    🔴 ``fake=True`` 时**所有写入落点一律重基到 ``root`` 下**(``place = contain``):``winagent.db`` /
    vault(``entropy.bin``+``blobs``)/ 内核落盘目录 / ``.wslconfig`` 备份目录。README §3 说 ``--dev``
    「不动任何真系统状态」,那就不能只靠「Linux 上 ``%ProgramData%`` 展不开」的巧合——真 Windows 上它展得开,
    这三个目录会直接写进生产的 ``C:\\ProgramData\\QTrade\\``。``monitor`` 的 ``disks`` 是**只读**受检目标,
    按定义不算落点,仍走生产语义的 ``expand``。
    """
    place = contain if fake else expand                                 # 自包含开关:显式,不依赖变量展不展得开
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
    vault = Vault(db, crypto, cfg.vault, root=place(cfg.vault.dir, root), audit=audit)
    disks = [DiskTarget("programdata", expand(r"%ProgramData%\QTrade", root)),
             DiskTarget("vhdx", expand(r"%LOCALAPPDATA%", root)),
             DiskTarget("wechat_data", "D:\\", product_level=False)]
    monitor = Monitor(db, sysb, alerts, cfg.monitor, probe=probeb, disks=disks)
    svc_exe = os.path.join(root, "qtrade-winagent-svc.exe")            # R-14:防火墙规则 Program 限**服务 exe**
    netprobe = NetProbe(db, netb, fwb, probeb, cfg.net, cfg.probe, alerts, svc_exe=svc_exe, port=cfg.api.port)
    hub = PipeHub(pipeb, cfg.ipc, pipe_name=cfg.api.pipe_user, install_user_sid=install_user_sid,
                  version=__version__, alerts=alerts, audit=audit)
    installer = InstallerOps(db, kernel_dir=place(cfg.wsl.kernel_dir, root),
                             wsl_backup_dir=place(cfg.wsl.backup_dir, root), package_version=__version__,
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


async def periodic(d: SvcDeps, listeners: Optional[Listeners] = None) -> None:
    """后台周期任务(04 §2.2 周期表 + R3-20 每日 03:00 清理)。任一轮抛异常只记日志,不让循环停。"""
    cfg = d.cfg
    next_slow = next_sweep = 0
    reported: Optional[frozenset[str]] = None
    while True:
        now = int(time.time() * 1000)
        try:
            await asyncio.to_thread(d.monitor.sample_fast)
            await asyncio.to_thread(d.monitor.check_session_locked)
            reported = await align_listen(d, listeners, reported)       # H16 自愈:真的补绑/解绑(评审 B2)
            if now >= next_slow:
                await asyncio.to_thread(d.monitor.sample_slow)
                await asyncio.to_thread(d.monitor.check_disks)
                await asyncio.to_thread(d.monitor.check_pending_reboot)
                await asyncio.to_thread(d.monitor.aggregate)
                await d.netprobe.refresh()
                await asyncio.to_thread(d.power.recheck)
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
    ap.add_argument("--root", default=None, help="%%ProgramData%%\\QTrade\\winagent(--dev 下必填)")
    ap.add_argument("--config", default=None, help="winagent.toml 路径(默认 <root>\\winagent.toml)")
    ap.add_argument("--install-user-sid", default=os.environ.get("QT_INSTALL_USER_SID", ""),
                    help="安装用户 SID(R3-12 仲裁前的安全校验基准)")
    ap.add_argument("--dev", action="store_true", help="用假后端起一份本机实例(仅开发;不碰任何真系统状态)")
    args = ap.parse_args(argv)
    if args.dev and not args.root:
        # 不给 --root 时默认 root **就是生产目录**,--dev 的假后端会把 winagent.db/vault/内核/备份写进真机生产位置。
        # 这里选「拒绝启动」而不是「默认到临时目录」:临时目录是隐式的,假熵会悄悄堆在 %TEMP% 里且仍可能被当真;
        # 显式给 --root 零歧义、零写入,也正是 README §3 示例的写法。
        ap.error("--dev 必须显式给 --root(否则默认落进生产目录 %ProgramData%\\QTrade\\winagent);"
                 "照 README §3 的写法:--dev --root /tmp/wa-dev")
    root = args.root or DEFAULT_ROOT
    cfg = load_cfg(read_toml(args.config or os.path.join(root, "winagent.toml")))
    setup_logging(cfg.log.level, None if args.dev else os.path.join(root, "logs"))
    d = build_real_deps(cfg, root=root, install_user_sid=args.install_user_sid, fake=args.dev)
    listeners = Listeners(cfg.api.port)
    tasks: set[asyncio.Task] = set()                                   # 留住后台任务引用,防被 GC

    async def boot() -> None:
        await load_tokens(d)
        alert = d.vault.startup_selfcheck()                            # 熵缺失 / ACL 放宽 ⇒ VAULT_ENTROPY_MISSING
        if alert:
            d.alerts.firing(alert["code"], subject=alert["subject"], severity=alert["severity"],
                            evidence=alert["evidence"])
        await d.hub.start()
        await d.netprobe.refresh()
        # 真机:d.listen = **真正绑上的**集合(vEthernet 没绑上时与期望不一致 ⇒ 周期任务报 H16 并补绑)
        d.listen = d.netprobe.desired_listen() if args.dev else listeners.actual()
        d.netprobe.firewall_ensure()
        tasks.add(asyncio.create_task(periodic(d, None if args.dev else listeners), name="wa-periodic"))

    app = build_app(d)

    @app.on_event("startup")
    async def _startup() -> None:                                       # noqa: D401
        await boot()

    import uvicorn
    # 规格要求同时听 127.0.0.1 和 vEthernet (WSL)。uvicorn 单 host 只会绑 desired_listen()[0],
    # WSL 里的 Agent 打到网关地址会连不上。这里把期望集合里的每个地址都绑上;
    # vEthernet 此刻不存在就跳过(Listeners.open 记 warn),由周期任务补绑。
    listeners.open((LOOPBACK,) if args.dev else d.netprobe.desired_listen())
    log.info("WinAgent 服务启动", extra={"op": "svc.start", "code": "OK",
                                        "kv": {"listen": ",".join(listeners.actual()), "port": cfg.api.port}})
    server = uvicorn.Server(uvicorn.Config(app, ws="websockets", log_config=None))
    listeners.server = server
    server.run(sockets=list(listeners.sockets.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
