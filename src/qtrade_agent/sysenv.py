"""WSL 侧系统只读采集与 docker 代理落地(#74 的 ``agent.wsl_env_reader`` / #85 的 ``agent.docker_proxy`` 执行体)。

两个执行体都把「碰真机」的那几件事收进**可注入的小协议**里,Fake 走同一条码路:

- ``WslEnvReader``:``snapshot() -> dict``。比 ``api/routes_ext.LinuxWslEnvReader`` 多读 ``/etc/os-release``、
  ``uname``(经 ``os.uname``)与 ``df``(经 ``shutil.disk_usage``)——全部**只读**,读不到的项一律 ``None``,**不编造**。
- ``DockerProxyApplier``:``async apply(proxy) -> bool`` / ``async disable() -> bool``。
  🔴 **本批只做到「读当前值 + 生成将写入的内容 + 落盘」**;`systemctl restart docker` 属整机危险动作
  (连带重启全部容器),**不执行** —— 落盘后把 ``restart_required=True`` 记在 ``last_result`` 里,
  由人在 P-ENV 决定何时重启(04 §3.4「零账号或用户确认后才重启 docker」)。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Awaitable, Callable, Optional, Protocol

log = logging.getLogger("qtrade.sysenv")

#: systemd drop-in 的落点(04 §2.7:dockerd 的代理只能经 service 环境变量给)
DOCKER_DROPIN_DIR = "/etc/systemd/system/docker.service.d"
DOCKER_DROPIN_FILE = "proxy.conf"
DROPIN_HEADER = "# 由 QTrade Agent 写入(#85 docker 代理透传);手改会在下次开关时被覆盖\n"

#: 采集 df 的挂载点(02 §7.1 的两个数据根 + 根分区)
DF_PATHS = ("/", "/var/lib/qtrade", "/var/lib/docker")


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_os_release(raw: Optional[str]) -> dict[str, str]:
    """``/etc/os-release`` 的 ``KEY=value`` 解析(值两侧的引号剥掉);读不到回空表。"""
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class LinuxSysReader:
    """``LinuxWslEnvReader`` 读不到的那几项的真实现;三个读法各自可注入(测试给假的)。"""

    def __init__(self, *, etc: str = "/etc", read_text: Optional[Callable[[str], Optional[str]]] = None,
                 uname: Optional[Callable[[], Any]] = None,
                 disk_usage: Optional[Callable[[str], Any]] = None,
                 df_paths: tuple[str, ...] = DF_PATHS):
        self.etc = etc
        self._read_text = read_text or _read_text
        self._uname = uname or os.uname
        self._disk_usage = disk_usage or shutil.disk_usage
        self._df_paths = df_paths

    def os_release(self) -> dict[str, Optional[str]]:
        kv = parse_os_release(self._read_text(os.path.join(self.etc, "os-release")))
        return {"id": kv.get("ID") or None, "version_id": kv.get("VERSION_ID") or None,
                "pretty_name": kv.get("PRETTY_NAME") or None}

    def uname(self) -> dict[str, Optional[str]]:
        try:
            u = self._uname()
        except OSError:
            return {"sysname": None, "release": None, "version": None, "machine": None}
        return {"sysname": u.sysname, "release": u.release, "version": u.version, "machine": u.machine}

    def df(self) -> list[dict[str, Any]]:
        """只列**存在且读得到**的挂载点;读不到的路径整条不出现(不放 0 进去冒充「盘满了」)。"""
        rows: list[dict[str, Any]] = []
        for p in self._df_paths:
            try:
                u = self._disk_usage(p)
            except OSError:
                continue
            rows.append({"path": p, "total_mb": u.total // 1048576, "used_mb": u.used // 1048576,
                         "free_mb": u.free // 1048576})
        return rows


async def _docker_version() -> Optional[str]:
    """只读服务端版本；失败不等于已证明 Docker 离线，健康判据仍由 H03 负责。"""
    from .runtime.backends import _run_cancellable

    try:
        rc, value = await _run_cancellable(
            ["docker", "version", "--format", "{{.Server.Version}}"], timeout_s=5)
    except (OSError, asyncio.TimeoutError):
        return None
    value = value.strip()
    return value if rc == 0 and value and "\n" not in value else None


class WslEnvReader:
    """#74 的 WSL 侧快照:``LinuxWslEnvReader``(网络/docker/KSM/ZRAM/kernel)+ 本模块三项。

    ``base`` 缺省 = ``routes_ext.LinuxWslEnvReader()``;两者都可注入,故整条码路在测试里不碰真 ``/proc``。
    """

    def __init__(self, *, base=None, sys_reader: Optional[LinuxSysReader] = None,
                 docker_version: Optional[Callable[[], Awaitable[Optional[str]]]] = None):
        if base is None:
            from .api.routes_ext import LinuxWslEnvReader          # 局部导入:避免包导入期就拉起 FastAPI 那一串
            base = LinuxWslEnvReader()
        self._base = base
        self._sys = sys_reader or LinuxSysReader()
        self._docker_version = docker_version or _docker_version

    async def versions(self) -> dict[str, Optional[str]]:
        """读取本机已知版本；没有 Windows 权威来源时不推断 WSL/内核归属状态。"""
        out: dict[str, Optional[str]] = {"kernel": None, "docker": None, "distro": None}
        try:
            out["kernel"] = self._sys.uname().get("release")
        except OSError:
            pass
        try:
            out["distro"] = self._sys.os_release().get("pretty_name")
        except OSError:
            pass
        try:
            out["docker"] = await asyncio.wait_for(self._docker_version(), timeout=10)
        except Exception:
            # 版本探测失败不妨碍 API 返回 Agent/schema 等已知元信息。
            pass
        return out

    def snapshot(self) -> dict[str, Any]:
        out = dict(self._base.snapshot())
        out["os_release"] = self._sys.os_release()
        out["uname"] = self._sys.uname()
        out["disks"] = self._sys.df()
        return out


class DropinIo(Protocol):
    """落盘的最小面(让测试用内存实现替掉,不碰 ``/etc``)。"""

    def read(self, path: str) -> Optional[str]: ...
    def write(self, path: str, content: str) -> None: ...
    def remove(self, path: str) -> bool: ...


class OsDropinIo:
    def read(self, path: str) -> Optional[str]:
        return _read_text(path)

    def write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)                       # 原子替换:半截文件会让 dockerd 起不来

    def remove(self, path: str) -> bool:
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False


def render_dropin(proxy: dict[str, Any]) -> str:
    """把 ``GET /wa/v1/net`` 报的机器级代理渲染成 systemd drop-in 正文。

    只认三个键(``http`` / ``https`` / ``no_proxy``,别名 ``http_proxy`` 等同);一个都没有 ⇒ ``ValueError``。
    ``NO_PROXY`` 一定带上 ``localhost,127.0.0.1``(WSL 侧的 dockerd 还要访问本机 registry mirror / Agent)。
    """
    http_p = proxy.get("http") or proxy.get("http_proxy") or ""
    https_p = proxy.get("https") or proxy.get("https_proxy") or http_p
    no_p = proxy.get("no_proxy") or proxy.get("noproxy") or ""
    if not http_p and not https_p:
        raise ValueError("代理配置里没有 http/https 任一项")
    locals_ = ["localhost", "127.0.0.1"]
    parts = [x.strip() for x in str(no_p).split(",") if x.strip()]
    for l in locals_:
        if l not in parts:
            parts.append(l)
    lines = [DROPIN_HEADER, "[Service]\n"]
    if http_p:
        lines.append(f'Environment="HTTP_PROXY={http_p}"\n')
    if https_p:
        lines.append(f'Environment="HTTPS_PROXY={https_p}"\n')
    lines.append(f'Environment="NO_PROXY={",".join(parts)}"\n')
    return "".join(lines)


class DockerProxyApplier:
    """#85 的执行体:写/删 ``docker.service.d/proxy.conf``。

    🔴 **不执行 `systemctl restart docker`**(本批范围):落盘后 ``last_result['restart_required']=True``,
    由人在确认时机后重启。``apply`` / ``disable`` 的返回值 = 「文件内容是否已是目标状态」。
    """

    def __init__(self, *, io: Optional[DropinIo] = None, dropin_dir: str = DOCKER_DROPIN_DIR,
                 dropin_file: str = DOCKER_DROPIN_FILE):
        self._io = io or OsDropinIo()
        self.path = os.path.join(dropin_dir, dropin_file)
        #: 最近一次动作的结果(给 #85b / 日志看:改了什么、要不要重启)
        self.last_result: dict[str, Any] = {}

    def current(self) -> Optional[str]:
        """当前落盘内容(没有 ⇒ ``None``);#85b 与「生成将写入的内容」的对照基准。"""
        return self._io.read(self.path)

    async def apply(self, proxy: Optional[dict[str, Any]]) -> bool:
        if not proxy:
            return await self.disable()
        want = render_dropin(proxy)
        before = self.current()
        changed = before != want
        if changed:
            self._io.write(self.path, want)
        self.last_result = {"action": "apply", "path": self.path, "changed": changed,
                            "restart_required": changed, "content": want}
        if changed:
            log.warning("已写 %s:dockerd 需重启后生效(本执行体不重启,等人确认时机)", self.path)
        return True

    async def disable(self) -> bool:
        removed = self._io.remove(self.path)
        self.last_result = {"action": "disable", "path": self.path, "changed": removed,
                            "restart_required": removed, "content": None}
        if removed:
            log.warning("已删 %s:dockerd 需重启后才回到不走代理(本执行体不重启)", self.path)
        return True
