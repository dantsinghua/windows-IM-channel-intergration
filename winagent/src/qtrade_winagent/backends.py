"""Windows 专有能力的**后端协议**(真实现在 ``win/``,假实现在 ``fakes.py``)。

为什么抽协议:WinAgent 的每一件事都压在 Windows 专有 API 上(DPAPI、命名管道、pywin32 服务壳、WMI、``wsl.exe``、
``netsh``/``powercfg``、注册表、UI 自动化)。把它们收敛成十一个协议后:
- **业务逻辑(vault/monitor/netprobe/power/pipe/wslctl/wechat)与平台无关**,可在 Linux 上全量单测;
- 真实现 ``win/*.py`` 一律**延迟导入**(``import`` 语句在函数体内),非 Windows 上导入本包不报错;
- 测试只注入 ``Fake*``,**绝不在开发机上真的改防火墙 / powercfg / .wslconfig / 注册表 / 装服务 / 碰真微信**。

协议清单(括号内 = 真实现手段 / 规格出处):
- ``CryptoBackend``   DPAPI 机器级 + 附加熵、熵文件 ACL(``crypt32.CryptProtectData`` / 05 §2.2.1 方案 B)
- ``SysBackend``      整机采样、锁屏、待重启、唤醒时刻、w32time(PDH/psutil/WTS/注册表 / 04 §2.2 S1~S15)
- ``NetBackend``      适配器/路由/代理/WSL 子网(``Get-NetAdapter``/注册表 / 04 §2.6)
- ``FirewallBackend`` 17610 入站规则的唯一拥有者(``netsh``/``*-NetFirewallRule`` / 04 §2.6.3)
- ``PowerBackend``    ``SetThreadExecutionState`` + ``powercfg``(04 §2.5.1)
- ``ProbeBackend``    dns→tcp→tls→http 逐级探测(04 §2.8.1)
- ``ProcBackend``     进程起停/枚举/WM_CLOSE(psutil + ``win32gui`` / 05 §2.4.6)
- ``PipeBackend``     命名管道服务端/客户端 + 对端 SID(``win32pipe`` / 02 §2.4.1)
- ``WslBackend``      ``wsl.exe`` 全部调用 + ``.wslconfig`` 读写(**会话代理** / 02 §2.4、04 §2.7)
- ``HostsBackend``    整机 hosts 读写与可写性(04 §2.5.4 B-2)
- ``WeChatBackend``   微信/chatlog/讲述人/取钥/读写(**会话代理** / 05 §2.4)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------- 共用数据形态


@dataclass(frozen=True)
class Adapter:
    """04 §2.6.1/§2.6.5:适配器事实(VPN 识别、WSL 网卡查找、MTU 判据都读它)。"""
    name: str
    description: str
    up: bool
    ipv4: Optional[str] = None
    prefix_length: Optional[int] = None
    mtu: Optional[int] = None
    is_virtual: bool = False
    has_default_route: bool = False
    route_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProxyInfo:
    """04 §2.6.4 四个来源。``wininet_user`` 经 ``WTSQueryUserToken`` 读活动会话 HKU(**只用于读注册表**,C-02)。"""
    winhttp: Optional[str] = None
    wininet_user: dict[str, Any] = field(default_factory=dict)
    pac: Optional[str] = None
    policy_per_user: Optional[int] = None          # ProxySettingsPerUser;0 = 机器策略统一下发


@dataclass(frozen=True)
class FirewallRuleSpec:
    """04 §2.6.3 那张表逐项。``profile='Any'``(vEthernet 常被归 Public,限 Private 会不生效)。"""
    name: str
    program: str
    local_port: int
    interface_alias: str
    remote_address: str
    protocol: str = "TCP"
    direction: str = "Inbound"
    action: str = "Allow"
    profile: str = "Any"


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    rss_mb: float = 0.0
    cpu_pct: float = 0.0
    started_ms: Optional[int] = None


@dataclass(frozen=True)
class ProbeStep:
    """一级探测的结果。``level`` ∈ dns|tcp|tls|http;``ok=False`` 时 ``result`` 是 00 §8.5 九值之一。"""
    level: str
    ok: bool
    latency_ms: int = 0
    result: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class PipeFrame:
    """02 §2.4.1 帧格式:请求 ``{id, method, params, trace_id, deadline_ms}``;响应 ``{id, ok, result|error}``。"""
    id: int
    method: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    deadline_ms: Optional[int] = None
    ok: Optional[bool] = None
    result: Any = None
    error: Optional[dict[str, Any]] = None

    def to_wire(self) -> dict[str, Any]:
        if self.method is not None:
            d: dict[str, Any] = {"id": self.id, "method": self.method, "params": self.params}
            if self.trace_id is not None:
                d["trace_id"] = self.trace_id
            if self.deadline_ms is not None:
                d["deadline_ms"] = self.deadline_ms
            return d
        d = {"id": self.id, "ok": bool(self.ok)}
        if self.ok:
            d["result"] = self.result
        else:
            d["error"] = self.error or {}
        return d

    @staticmethod
    def from_wire(d: dict[str, Any]) -> "PipeFrame":
        return PipeFrame(id=int(d.get("id", 0)), method=d.get("method"), params=dict(d.get("params") or {}),
                         trace_id=d.get("trace_id"), deadline_ms=d.get("deadline_ms"),
                         ok=d.get("ok"), result=d.get("result"), error=d.get("error"))


# ---------------------------------------------------------------- 协议


@runtime_checkable
class CryptoBackend(Protocol):
    """05 §2.2.1 方案 B:``CryptProtectData(CRYPTPROTECT_LOCAL_MACHINE, pOptionalEntropy=E)``。"""
    async def protect(self, plaintext: bytes, entropy: bytes) -> bytes: ...
    async def unprotect(self, blob: bytes, entropy: bytes) -> bytes: ...
    def random_bytes(self, n: int) -> bytes: ...
    def tighten_acl(self, path: str) -> None: ...
    def acl_is_tight(self, path: str) -> bool: ...


@runtime_checkable
class SysBackend(Protocol):
    """04 §2.2 采样项 S1/S2/S3/S4/S5/S6/S13/S14/S15 + §2.9 校时。"""
    def cpu_percent(self) -> float: ...
    def memory_mb(self) -> dict[str, float]: ...
    def disk_free_mb(self, path: str) -> tuple[float, float]: ...
    def net_throughput_kbps(self) -> dict[str, tuple[float, float]]: ...
    def processes(self, names: tuple[str, ...]) -> list[ProcInfo]: ...
    def session_locked(self) -> bool: ...
    def active_session(self) -> Optional[dict[str, Any]]: ...
    def pending_reboot(self) -> bool: ...
    def last_resume_ms(self) -> Optional[int]: ...
    def w32time(self) -> dict[str, Any]: ...
    def tz_offset_min(self) -> int: ...
    def file_version(self, path: str) -> Optional[str]: ...
    def policy_lockscreen(self) -> Optional[int]: ...


@runtime_checkable
class NetBackend(Protocol):
    def adapters(self) -> list[Adapter]: ...
    def wsl_adapter(self) -> Optional[Adapter]: ...
    def proxy(self) -> ProxyInfo: ...
    def has_default_route(self) -> bool: ...


@runtime_checkable
class FirewallBackend(Protocol):
    def get_rule(self, name: str) -> Optional[FirewallRuleSpec]: ...
    def put_rule(self, spec: FirewallRuleSpec) -> str: ...     # created|updated|unchanged|blocked_by_policy
    def delete_rule(self, name: str) -> str: ...               # removed|absent|blocked_by_policy


@runtime_checkable
class PowerBackend(Protocol):
    def set_execution_state(self, *, system_required: bool, display_required: bool) -> None: ...
    def current_requests(self) -> dict[str, bool]: ...
    def active_scheme(self) -> str: ...
    def query_timeouts(self, scheme: str, items: tuple[str, ...]) -> dict[str, int]: ...
    def change_timeout(self, scheme: str, item: str, value: int) -> None: ...


@runtime_checkable
class ProbeBackend(Protocol):
    async def dns(self, host: str, timeout_s: float) -> ProbeStep: ...
    async def tcp(self, host: str, port: int, timeout_s: float) -> ProbeStep: ...
    async def tls(self, host: str, port: int, timeout_s: float) -> ProbeStep: ...
    async def http(self, method: str, url: str, timeout_s: float) -> ProbeStep: ...
    async def connections(self, pid_names: tuple[str, ...], duration_s: int) -> list[dict[str, Any]]: ...


@runtime_checkable
class ProcBackend(Protocol):
    async def start(self, argv: list[str], *, cwd: Optional[str] = None) -> int: ...
    async def stop(self, pid: int, *, force: bool = False) -> None: ...
    def find(self, name: str) -> list[ProcInfo]: ...
    async def close_window(self, pid: int) -> bool: ...


@runtime_checkable
class PipeConn(Protocol):
    """一条已建立的管道连接。帧读写一律在线程里做(02 §2.3.1 ⑤:绝不在事件循环里阻塞)。"""
    async def send(self, frame: PipeFrame) -> None: ...
    async def recv(self) -> Optional[PipeFrame]: ...
    async def close(self) -> None: ...
    def inbound_ready(self) -> bool: ...
    @property
    def peer_sid(self) -> Optional[str]: ...


@runtime_checkable
class PipeBackend(Protocol):
    # 服务端:等一个客户端接入;``allow_sid`` = 追加进管道 ACL 的安装用户 SID(02 §2.4.1)
    async def serve(self, name: str, *, allow_sid: Optional[str] = None) -> PipeConn: ...
    # 客户端(会话代理);``peer_sid`` 只有假后端用得上(真后端对端 SID 由服务侧取)
    async def connect(self, name: str, *, peer_sid: Optional[str] = None) -> PipeConn: ...


@runtime_checkable
class WslBackend(Protocol):
    """02 §2.4:``wsl.exe`` 全部调用归**会话代理**;``.wslconfig`` 在安装用户 ``%USERPROFILE%`` 下(R3-6)。"""
    async def list_distros(self) -> list[dict[str, Any]]: ...
    async def start(self, distro: str) -> None: ...
    async def terminate(self, distro: str) -> None: ...
    async def shutdown(self) -> None: ...                      # 🔴 只在 confirm=true 时才允许被调(00 §11.6)
    async def exec(self, distro: str, argv: list[str]) -> tuple[int, str]: ...
    async def version(self) -> str: ...
    def wslconfig_path(self) -> str: ...
    def read_wslconfig(self) -> str: ...
    def write_wslconfig(self, text: str) -> None: ...


@runtime_checkable
class HostsBackend(Protocol):
    def read(self) -> str: ...
    def write(self, text: str) -> None: ...                    # 只读/EDR/组策略 → 抛 PermissionError
    def writable(self) -> bool: ...
    def resolve(self, domain: str) -> Optional[str]: ...


@runtime_checkable
class WeChatBackend(Protocol):
    """05 §2.4 的全部 UI/进程动作(**会话代理**执行);M3.5 骨架只定接口与状态机,真实现见 ``win/wechat.py``。"""
    def locate(self) -> dict[str, Any]: ...
    async def launch(self) -> int: ...
    async def logout(self, *, mode: str, grace_s: int) -> str: ...
    def main_window(self) -> dict[str, Any]: ...     # {exists, visible, minimized, pid, class_name}
    # class_name = 本次探测到的主窗口类名(R6-58 (at);未识别/窗口不存在为 None);
    #   落 wechat_profiles.main_wnd_class 由服务侧 WeChatStore.record_main_wnd_class 做(本协议只探测,不碰 DB)
    def set_main_wnd_class(self, class_name: str) -> None: ...
    # R6-58 (at) ②:切换本次探测要用的主窗口类名(该 wxid 的行值/配置默认由服务侧 effective_main_wnd_class
    #   算好、经 wechat.login.start 管道参数传入;会话代理只应用,不碰 DB)
    def ui_tree_visible(self) -> bool: ...
    async def narrator_start(self) -> int: ...
    async def narrator_stop(self) -> None: ...
    def narrator_running(self) -> bool: ...
    async def chatlog_start(self, dll: str) -> int: ...
    async def chatlog_stop(self) -> None: ...
    def chatlog_status(self) -> dict[str, Any]: ...
    def key_state(self) -> dict[str, Any]: ...
    def current_wxid(self) -> Optional[str]: ...
    async def read_messages(self, *, talker: Optional[str], since_seq: Optional[int], limit: int) -> list[dict[str, Any]]: ...
    async def send(self, *, session_name: str, text: Optional[str], image_path: Optional[str], confirm_timeout_ms: int) -> dict[str, Any]: ...
    async def list_sessions(self, *, keyword: Optional[str], limit: int) -> list[dict[str, Any]]: ...
    async def media(self, key: str) -> bytes: ...
    async def screenshot(self) -> bytes: ...
    async def reinstall(self, installer: str) -> dict[str, Any]: ...
