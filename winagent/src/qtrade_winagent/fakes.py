"""全部后端的**可编程假实现**(测试唯一使用的一套;Linux 上可全跑)。

设计口径与 Agent 侧 ``qtrade_agent.runtime.FakeContainers/FakeAdb`` 一致:
- 每个 Fake 都有可拨的开关(``blocked_by_policy``/``offline``/``locked``/``fail_next`` …),用来演各条降级分支;
- 记录调用轨迹(``calls``)以便断言「有没有真去动那个东西」;
- **不做任何真实的系统副作用**:不写真 hosts、不动真防火墙、不起真进程、不调 DPAPI。

⚠️ ``FakeCrypto`` 的 ``protect`` **不是加密**,只是可逆编码 + 熵绑定校验,用来验证
「熵不对就解不开」「blob 被换掉能被 ``blob_sha256`` 发现」这两条行为,**绝不可用于生产**。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .backends import Adapter, FirewallRuleSpec, PipeFrame, ProbeStep, ProcInfo, ProxyInfo


# ---------------------------------------------------------------- Crypto(DPAPI)
class FakeCrypto:
    """熵绑定的可逆编码。``entropy_mismatch`` 时 ``unprotect`` 抛 ``ValueError``(= 机器迁移/熵丢失的行为,05 §2.2.5)。"""

    MAGIC = b"FAKEDPAPI1"

    def __init__(self, *, acl_tight: bool = True):
        self._acl: dict[str, bool] = {}
        self._default_acl_tight = acl_tight
        self.calls: list[tuple[str, Any]] = []

    async def protect(self, plaintext: bytes, entropy: bytes) -> bytes:
        self.calls.append(("protect", len(plaintext)))
        tag = hashlib.sha256(entropy).digest()[:8]
        return self.MAGIC + tag + bytes(b ^ 0x5A for b in plaintext)

    async def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        self.calls.append(("unprotect", len(blob)))
        if not blob.startswith(self.MAGIC):
            raise ValueError("blob 格式不对(可能被替换)")
        if blob[len(self.MAGIC):len(self.MAGIC) + 8] != hashlib.sha256(entropy).digest()[:8]:
            raise ValueError("附加熵不匹配:DPAPI 解密失败(等同机器迁移)")
        return bytes(b ^ 0x5A for b in blob[len(self.MAGIC) + 8:])

    def random_bytes(self, n: int) -> bytes:
        return os.urandom(n)

    def tighten_acl(self, path: str) -> None:
        self._acl[path] = True
        self.calls.append(("tighten_acl", path))

    def acl_is_tight(self, path: str) -> bool:
        return self._acl.get(path, self._default_acl_tight)

    def loosen_acl(self, path: str) -> None:
        """测试钩子:模拟熵文件 ACL 被放宽(→ ``VAULT_ENTROPY_MISSING``)。"""
        self._acl[path] = False


# ---------------------------------------------------------------- Sys(采样)
@dataclass
class FakeSys:
    """04 §2.2 采样项的可拨真值。默认造一台 16 GB 台式机、未锁屏、无待重启。"""
    cpu: float = 12.5
    total_mb: float = 16384.0
    available_mb: float = 6100.0
    committed_mb: float = 9000.0
    disks: dict[str, tuple[float, float]] = field(default_factory=lambda: {"C:\\": (40960.0, 244140.0)})
    missing_paths: set[str] = field(default_factory=set)       # 拨进来的路径 ⇒ path_exists=False(演「D: 不存在」)
    throughput: dict[str, tuple[float, float]] = field(default_factory=lambda: {"vEthernet (WSL)": (120.0, 80.0)})
    procs: dict[str, ProcInfo] = field(default_factory=dict)
    locked: bool = False
    session: Optional[dict[str, Any]] = field(default_factory=lambda: {"sid": "S-1-5-21-1-2-3-1001", "user": "anlin"})
    reboot_pending: bool = False
    resume_ms: Optional[int] = None
    w32time_source: str = "time.windows.com"
    w32time_last_sync_ms: Optional[int] = None
    tz_min: int = 480
    versions: dict[str, str] = field(default_factory=dict)
    lockscreen_policy: Optional[int] = None

    def cpu_percent(self) -> float:
        return self.cpu

    def memory_mb(self) -> dict[str, float]:
        return {"total_mb": self.total_mb, "available_mb": self.available_mb, "committed_mb": self.committed_mb}

    def disk_free_mb(self, path: str) -> tuple[float, float]:
        return self.disks.get(path, (40960.0, 244140.0))

    def path_exists(self, path: str) -> bool:
        return path not in self.missing_paths

    def net_throughput_kbps(self) -> dict[str, tuple[float, float]]:
        return dict(self.throughput)

    def processes(self, names: tuple[str, ...]) -> list[ProcInfo]:
        return [p for n, p in self.procs.items() if n in names]

    def session_locked(self) -> bool:
        return self.locked

    def active_session(self) -> Optional[dict[str, Any]]:
        return dict(self.session) if self.session else None

    def pending_reboot(self) -> bool:
        return self.reboot_pending

    def last_resume_ms(self) -> Optional[int]:
        return self.resume_ms

    def w32time(self) -> dict[str, Any]:
        return {"source": self.w32time_source,
                "last_sync_ms": self.w32time_last_sync_ms if self.w32time_last_sync_ms is not None
                else int(time.time() * 1000) - 3_600_000}

    def tz_offset_min(self) -> int:
        return self.tz_min

    def file_version(self, path: str) -> Optional[str]:
        return self.versions.get(path)

    def policy_lockscreen(self) -> Optional[int]:
        return self.lockscreen_policy


# ---------------------------------------------------------------- Net
@dataclass
class FakeNet:
    """默认 = 直连、无 VPN、vEthernet (WSL) 在 172.23.16.x/20(04 §2.6.1 的典型 NAT 形态)。"""
    items: list[Adapter] = field(default_factory=lambda: [
        Adapter("以太网", "Realtek Gaming 2.5GbE", True, "192.168.3.20", 24, 1500, False, True, ("0.0.0.0/0",)),
        Adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", True, "172.23.16.1", 20, 1500, True, False,
                ("172.23.16.0/20",)),
    ])
    proxy_info: ProxyInfo = field(default_factory=ProxyInfo)
    default_route: bool = True

    def adapters(self) -> list[Adapter]:
        return list(self.items)

    def wsl_adapter(self) -> Optional[Adapter]:
        """04 §2.6.3 查找规则:``InterfaceDescription == Hyper-V Virtual Ethernet Adapter`` 且 ``Name`` 以 ``vEthernet (WSL`` 开头;
        找不到则退化为「IPv4 属于 172.16.0.0/12 的 Hyper-V 虚拟网卡」。"""
        for a in self.items:
            if a.up and a.description == "Hyper-V Virtual Ethernet Adapter" and a.name.startswith("vEthernet (WSL"):
                return a
        for a in self.items:
            if a.up and a.is_virtual and a.ipv4 and a.ipv4.startswith(("172.1", "172.2", "172.3")):
                return a
        return None

    def proxy(self) -> ProxyInfo:
        return self.proxy_info

    def has_default_route(self) -> bool:
        return self.default_route

    # ---- 测试钩子
    def set_wsl_ipv4(self, ipv4: Optional[str], prefix: int = 20) -> None:
        """演 WSL 子网变化(H16 / ``WSL_SUBNET_CHANGED``);``None`` = 网卡还没有地址(WSL 未启动)。"""
        self.items = [a for a in self.items if not a.name.startswith("vEthernet (WSL")]
        if ipv4 is not None:
            self.items.append(Adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", True, ipv4, prefix, 1500,
                                      True, False, ()))

    def add_vpn(self, name: str = "Cisco AnyConnect Secure Mobility Client", mtu: int = 1380) -> None:
        self.items.append(Adapter(name, name, True, "10.10.0.5", 16, mtu, True, True, ("10.0.0.0/8",)))


# ---------------------------------------------------------------- Firewall
@dataclass
class FakeFirewall:
    """幂等语义按 04 §2.6.3:不存在则建、逐项比对不同才更新、全同不动;``blocked_by_policy`` 演组策略接管。"""
    rules: dict[str, FirewallRuleSpec] = field(default_factory=dict)
    blocked_by_policy: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get_rule(self, name: str) -> Optional[FirewallRuleSpec]:
        return self.rules.get(name)

    def put_rule(self, spec: FirewallRuleSpec) -> str:
        self.calls.append(("put", spec.name))
        if self.blocked_by_policy:
            return "blocked_by_policy"
        old = self.rules.get(spec.name)
        if old is None:
            self.rules[spec.name] = spec
            return "created"
        if old == spec:
            return "unchanged"
        self.rules[spec.name] = spec
        return "updated"

    def delete_rule(self, name: str) -> str:
        self.calls.append(("delete", name))
        if self.blocked_by_policy:
            return "blocked_by_policy"
        return "removed" if self.rules.pop(name, None) is not None else "absent"


# ---------------------------------------------------------------- Power
@dataclass
class FakePower:
    """``powercfg`` 六项 + ``SetThreadExecutionState``;``policy_locked`` 演组策略锁定电源计划(04 §2.5.1)。"""
    scheme: str = "381b4222-f694-41f0-9685-ff5bb260df2e"
    timeouts: dict[str, int] = field(default_factory=lambda: {
        "standby-timeout-ac": 30, "standby-timeout-dc": 15, "monitor-timeout-ac": 10,
        "monitor-timeout-dc": 5, "hibernate-timeout-ac": 180, "hibernate-timeout-dc": 60})
    system_required: bool = False
    display_required: bool = False
    policy_locked: bool = False
    changes: list[tuple[str, int]] = field(default_factory=list)

    def set_execution_state(self, *, system_required: bool, display_required: bool) -> None:
        self.system_required = system_required
        self.display_required = display_required

    def current_requests(self) -> dict[str, bool]:
        return {"system_required": self.system_required, "display_required": self.display_required}

    def active_scheme(self) -> str:
        return self.scheme

    def query_timeouts(self, scheme: str, items: tuple[str, ...]) -> dict[str, int]:
        return {i: self.timeouts.get(i, 0) for i in items}

    def change_timeout(self, scheme: str, item: str, value: int) -> None:
        if self.policy_locked:
            raise PermissionError("电源计划由公司策略管理")
        self.timeouts[item] = value
        self.changes.append((item, value))


# ---------------------------------------------------------------- Probe
@dataclass
class FakeProbe:
    """按 ``host:port`` 编程每一级的结果。缺省全通;``results['host:port'] = ('tcp','TCP_TIMEOUT')`` 表示卡在 tcp 且判该码。"""
    results: dict[str, tuple[str, str]] = field(default_factory=dict)
    latency_ms: int = 7
    conns: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _fail_at(self, host: str, port: Optional[int]) -> Optional[tuple[str, str]]:
        return self.results.get(f"{host}:{port}") or self.results.get(host)

    async def dns(self, host: str, timeout_s: float) -> ProbeStep:
        self.calls.append(("dns", host))
        f = self._fail_at(host, None)
        if f and f[0] == "dns":
            return ProbeStep("dns", False, self.latency_ms, f[1], "fake")
        return ProbeStep("dns", True, self.latency_ms)

    async def tcp(self, host: str, port: int, timeout_s: float) -> ProbeStep:
        self.calls.append(("tcp", f"{host}:{port}"))
        f = self._fail_at(host, port)
        if f and f[0] in ("dns", "tcp"):
            return ProbeStep("tcp", False, self.latency_ms, f[1], "fake")
        return ProbeStep("tcp", True, self.latency_ms)

    async def tls(self, host: str, port: int, timeout_s: float) -> ProbeStep:
        self.calls.append(("tls", f"{host}:{port}"))
        f = self._fail_at(host, port)
        if f and f[0] == "tls":
            return ProbeStep("tls", False, self.latency_ms, f[1], "fake")
        return ProbeStep("tls", True, self.latency_ms)

    async def http(self, method: str, url: str, timeout_s: float) -> ProbeStep:
        self.calls.append(("http", url))
        f = self.results.get(url)
        if f and f[0] == "http":
            return ProbeStep("http", False, self.latency_ms, f[1], "fake")
        return ProbeStep("http", True, self.latency_ms)

    async def connections(self, pid_names: tuple[str, ...], duration_s: int) -> list[dict[str, Any]]:
        """行形状照 04 §3.4:``{pid_name, ip, port, samples, hostname?, resolved_by}``;缺省补 ``pid_name``。"""
        self.calls.append(("connections", ",".join(pid_names)))
        out = []
        for c in self.conns:
            row = dict(c)
            row.setdefault("pid_name", pid_names[0] if pid_names else None)
            row.setdefault("samples", 1)
            row.setdefault("resolved_by", "cache" if row.get("hostname") else "none")
            out.append(row)
        return out


# ---------------------------------------------------------------- Proc
@dataclass
class FakeProc:
    """进程起停:``start`` 分配递增 pid;``close_window`` 按 ``wm_close_works`` 决定 ``WM_CLOSE`` 是否奏效(05 §2.4.6)。"""
    running: dict[int, ProcInfo] = field(default_factory=dict)
    wm_close_works: bool = True
    _next_pid: int = 4000
    started: list[list[str]] = field(default_factory=list)
    killed: list[tuple[int, bool]] = field(default_factory=list)

    async def start(self, argv: list[str], *, cwd: Optional[str] = None) -> int:
        self._next_pid += 1
        name = os.path.basename(argv[0])
        self.running[self._next_pid] = ProcInfo(self._next_pid, name, 64.0, 1.0, int(time.time() * 1000))
        self.started.append(list(argv))
        return self._next_pid

    async def stop(self, pid: int, *, force: bool = False) -> None:
        self.killed.append((pid, force))
        self.running.pop(pid, None)

    def find(self, name: str) -> list[ProcInfo]:
        return [p for p in self.running.values() if p.name.lower() == name.lower()]

    async def close_window(self, pid: int) -> bool:
        if self.wm_close_works:
            self.running.pop(pid, None)
            return True
        return False


# ---------------------------------------------------------------- Pipe(内存管道对)
class FakePipeConn:
    """一端的内存管道。``peer_sid`` 用来演 R3-12 的对端 SID 校验;``max_frame_kb`` 超限抛 ValueError(§2.4.1 单帧 ≤ 1 MB)。"""

    def __init__(self, inbox: "asyncio.Queue[Optional[PipeFrame]]", outbox: "asyncio.Queue[Optional[PipeFrame]]",
                 *, peer_sid: Optional[str] = None, max_frame_kb: int = 1024):
        self._in = inbox
        self._out = outbox
        self._peer_sid = peer_sid
        self._max = max_frame_kb * 1024
        self.closed = False
        self.sent: list[PipeFrame] = []

    async def send(self, frame: PipeFrame) -> None:
        import json
        raw = json.dumps(frame.to_wire(), ensure_ascii=False).encode("utf-8")
        if len(raw) > self._max:
            raise ValueError(f"单帧超过 {self._max} 字节(02 §2.4.1 max_frame_kb)")
        self.sent.append(frame)
        await self._out.put(frame)

    def inbound_ready(self) -> bool:
        return not self._in.empty()

    async def recv(self) -> Optional[PipeFrame]:
        return await self._in.get()

    async def close(self) -> None:
        self.closed = True
        await self._out.put(None)

    @property
    def peer_sid(self) -> Optional[str]:
        return self._peer_sid


class FakePipeBackend:
    """内存实现的命名管道:``connect`` 造一对互联端点并把服务端那头交给等待中的 ``serve``。"""

    def __init__(self, *, max_frame_kb: int = 1024):
        self._pending: dict[str, asyncio.Queue] = {}
        self._max_frame_kb = max_frame_kb
        self.served_allow_sid: Optional[str] = None

    def _q(self, name: str) -> asyncio.Queue:
        return self._pending.setdefault(name, asyncio.Queue())

    async def serve(self, name: str, *, allow_sid: Optional[str] = None) -> FakePipeConn:
        self.served_allow_sid = allow_sid                  # 记下来便于断言「服务确实把安装用户 SID 交给了 ACL」
        return await self._q(name).get()

    async def connect(self, name: str, *, peer_sid: Optional[str] = None) -> FakePipeConn:
        a: asyncio.Queue = asyncio.Queue()
        b: asyncio.Queue = asyncio.Queue()
        server_side = FakePipeConn(a, b, peer_sid=peer_sid, max_frame_kb=self._max_frame_kb)
        client_side = FakePipeConn(b, a, peer_sid=None, max_frame_kb=self._max_frame_kb)
        await self._q(name).put(server_side)
        return client_side


# ---------------------------------------------------------------- Wsl
@dataclass
class FakeWsl:
    """``wsl.exe`` 与 ``.wslconfig``。🔴 ``shutdown_calls`` 用来在测试里断言「没有人偷偷 --shutdown」(00 §11.6)。"""
    wslconfig_text: str = "[wsl2]\nmemory=8GB\n"
    wslconfig_file: str = "%USERPROFILE%\\.wslconfig"
    distros: list[dict[str, Any]] = field(default_factory=lambda: [
        {"name": "qtrade", "running": False, "version": 2, "default": False, "user": "qtrade"}])
    uname: str = "6.6.87.2-binder+"
    filesystems: str = "nodev\tbinder\nnodev\ttmpfs\n"
    binderfs_mountable: bool = True
    wsl_version: str = "WSL 版本: 2.6.1.0"
    terminate_calls: list[str] = field(default_factory=list)
    shutdown_calls: int = 0
    exec_log: list[list[str]] = field(default_factory=list)
    fail_start: bool = False

    async def list_distros(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self.distros]

    async def start(self, distro: str) -> None:
        if self.fail_start:
            raise RuntimeError("wsl.exe 启动失败(fake)")
        for d in self.distros:
            if d["name"] == distro:
                d["running"] = True

    async def terminate(self, distro: str) -> None:
        self.terminate_calls.append(distro)
        for d in self.distros:
            if d["name"] == distro:
                d["running"] = False

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        for d in self.distros:
            d["running"] = False

    async def exec(self, distro: str, argv: list[str]) -> tuple[int, str]:
        self.exec_log.append(list(argv))
        joined = " ".join(argv)
        if joined.startswith("uname"):
            return 0, self.uname
        if "/proc/filesystems" in joined:
            return 0, self.filesystems
        if "binderfs" in joined or "mount" in joined:
            return (0, "binder\nbinder_ctl\nbinderfs_features\n") if self.binderfs_mountable else (32, "mount: 失败")
        return 0, ""

    async def version(self) -> str:
        return self.wsl_version

    def wslconfig_path(self) -> str:
        return self.wslconfig_file

    def read_wslconfig(self) -> str:
        return self.wslconfig_text

    def write_wslconfig(self, text: str) -> None:
        self.wslconfig_text = text


# ---------------------------------------------------------------- Hosts
@dataclass
class FakeHosts:
    text: str = "# Copyright (c) 1993-2009 Microsoft Corp.\n127.0.0.1 localhost\n"
    can_write: bool = True
    resolved: dict[str, str] = field(default_factory=dict)

    def read(self) -> str:
        return self.text

    def write(self, text: str) -> None:
        if not self.can_write:
            raise PermissionError("hosts 只读 / 被 EDR 或组策略保护")
        self.text = text

    def writable(self) -> bool:
        return self.can_write

    def resolve(self, domain: str) -> Optional[str]:
        for line in self.text.splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and domain in parts[1:]:
                return parts[0]
        return self.resolved.get(domain)


# ---------------------------------------------------------------- WeChat(M3.5 骨架)
@dataclass
class FakeWeChat:
    """05 §2.4 的可编程微信。默认:已装 4.1.12.26、未运行、UI 树不可见(要走讲述人仪式)、两把钥都没取到。"""
    installed: bool = True
    path: str = "D:\\Program Files\\Tencent\\Weixin\\Weixin.exe"
    version: str = "4.1.12.26"
    data_root: str = "D:\\Program Files\\Tencent\\Saved Files"
    running_pid: Optional[int] = None
    logged_in: bool = False
    wxid: Optional[str] = None
    nickname: Optional[str] = None
    window_visible: bool = True
    window_minimized: bool = False
    window_class: Optional[str] = None       # R6-58 (at):main_window() 探测到的主窗口类名,测试可编程
    search_class: Optional[str] = None       # R6-58 (at) ②:set_main_wnd_class() 收到的搜索类名,测试可断言
    ui_visible: bool = False
    narrator_pid: Optional[int] = None
    chatlog_pid: Optional[int] = None
    chatlog_dll: Optional[str] = None
    chatlog_http_ok: bool = True
    data_key: bool = False
    img_key: bool = False
    key_error: Optional[str] = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    media_blobs: dict[str, bytes] = field(default_factory=dict)
    send_ok: bool = True
    calls: list[str] = field(default_factory=list)

    def locate(self) -> dict[str, Any]:
        return {"installed": self.installed, "path": self.path, "version": self.version, "data_root": self.data_root}

    async def launch(self) -> int:
        self.calls.append("launch")
        self.running_pid = self.running_pid or 5101
        return self.running_pid

    async def logout(self, *, mode: str, grace_s: int) -> str:
        self.calls.append(f"logout:{mode}")
        self.running_pid, self.logged_in, self.wxid = None, False, None
        return mode

    def main_window(self) -> dict[str, Any]:
        return {"exists": self.running_pid is not None, "visible": self.window_visible,
                "minimized": self.window_minimized, "pid": self.running_pid,
                "class_name": self.window_class if self.running_pid is not None else None}

    def set_main_wnd_class(self, class_name: str) -> None:
        self.calls.append(f"set_main_wnd_class:{class_name}")
        self.search_class = class_name

    def ui_tree_visible(self) -> bool:
        return self.ui_visible

    async def narrator_start(self) -> int:
        self.calls.append("narrator_start")
        self.narrator_pid = 5202
        return self.narrator_pid

    narrator_unkillable: bool = False     # 2026-10-10 真机:高完整性进程,普通用户 taskkill 拒绝访问 ⇒ 服务侧提权结束

    async def narrator_stop(self) -> bool:
        self.calls.append("narrator_stop")
        if self.narrator_unkillable:
            return False
        self.narrator_pid = None
        return True

    def kill_narrator_elevated(self) -> bool:
        """服务侧(提权)结束讲述人的假实现。"""
        self.calls.append("narrator_kill_elevated")
        self.narrator_pid = None
        return True

    def narrator_running(self) -> bool:
        return self.narrator_pid is not None

    def wechat_running(self) -> bool:
        return self.running_pid is not None

    async def chatlog_start(self, dll: str) -> int:
        self.calls.append(f"chatlog_start:{dll}")
        self.chatlog_pid, self.chatlog_dll = 5303, dll
        return self.chatlog_pid

    async def chatlog_stop(self) -> None:
        self.calls.append("chatlog_stop")
        self.chatlog_pid = None

    serve_fails: bool = False             # R6-91:模拟「取到钥但 chatlog server 起不来」

    async def chatlog_serve(self) -> int:
        self.calls.append("chatlog_serve")
        if self.serve_fails:
            raise RuntimeError("找不到微信消息库目录")
        self.chatlog_pid = 5304
        return self.chatlog_pid

    def chatlog_status(self) -> dict[str, Any]:
        return {"running": self.chatlog_pid is not None, "http_ok": self.chatlog_http_ok, "dll": self.chatlog_dll}

    def key_state(self) -> dict[str, Any]:
        """05 §2.4.4a 落盘判据:``data_key`` 与 ``img_key`` **同时非空**才算这一轮成功。"""
        return {"data_key": self.data_key, "img_key": self.img_key,
                "ok": bool(self.data_key and self.img_key), "dll": self.chatlog_dll, "error": self.key_error}

    def current_wxid(self) -> Optional[str]:
        return self.wxid

    async def read_messages(self, *, talker: Optional[str], since_seq: Optional[int], limit: int) -> list[dict[str, Any]]:
        out = [m for m in self.messages if (talker is None or m.get("talker") == talker)
               and (since_seq is None or int(m.get("seq", 0)) > since_seq)]
        return out[:limit]

    async def send(self, *, session_name: str, text: Optional[str], image_path: Optional[str],
                   confirm_timeout_ms: int) -> dict[str, Any]:
        self.calls.append(f"send:{session_name}")
        if not self.send_ok:
            return {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": confirm_timeout_ms}
        seq = len(self.messages) + 1
        self.messages.append({"talker": session_name, "seq": seq, "text": text, "is_self": True})
        return {"ok": True, "code": "DELIVERED", "ext_msg_id": f"{session_name}:{seq}", "confirm_ms": 1200}

    async def list_sessions(self, *, keyword: Optional[str], limit: int) -> list[dict[str, Any]]:
        out = [s for s in self.sessions if not keyword or keyword in str(s.get("name", ""))]
        return out[:limit]

    async def media(self, key: str) -> bytes:
        if key not in self.media_blobs:
            raise FileNotFoundError(key)
        return self.media_blobs[key]

    async def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nFAKE"

    async def reinstall(self, installer: str) -> dict[str, Any]:
        self.calls.append(f"reinstall:{installer}")
        return {"started": True, "installer": installer}


def free_tcp_port() -> int:
    """给「真的起一个 uvicorn」那类测试用;单元测试一律走 ASGI 传输,不占端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
