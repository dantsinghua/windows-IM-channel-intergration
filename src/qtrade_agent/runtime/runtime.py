"""runtime(02 §2.2.4):docker 编排 + 端口按序号推导(00 §3,不查表)+ 卷目录 + ``_purge_ephemeral`` + 企点 ``ensure_root``(06 §2.9.5)。

- 端口推导唯一算法(C-07):``seq = int(account_id[2:])``;qidian ``adb=16000+seq stream=16500+seq frida=16600+seq``;qq ``ws=16100+seq http=16200+seq webui=16300+seq``;
  wechat 无端口;16000 = WSL 内 adb server、16099 = 安装自检临时 redroid ⇒ seq ∈ 1..98。``account_runtime`` 冗余存推导值,真值永远是推导。
- 容器参数必带 ``ulimits core=0``(§2.8.8 L1);``restart=no``(H04 由 Agent 退避重拉)。
- 并发:**启动串行**(A.5 峰值内存翻倍)—— 全局一把 ``start_lock``;停/删不串行。每账号 inspect 缓存 ≤ 5 s。
- ``_purge_ephemeral(acct)``:清可再生临时数据(①容器内 /data/local/tmp/* ②宿主 accounts/<id>/tmp/ ③media/tmp 里属于该账号的 ④frida 转发 ⑤/data/local/tmp/*.js);
  **绝不动** /data 卷、accounts/<id>/data/、agent.db、已入库 media、device_profiles、Vault;幂等;容器不在时跳过容器内那步;记审计 ``runtime.purge_ephemeral``。
- ``ensure_root(acct)``(唯一出处 06 §2.9.5,三步逐字):``health.mark_rooting`` → ``adb root`` → 容器内 ``stop adbd; start adbd`` → 宿主只对本账号 ``disconnect/connect``
  → 判据 ``whoami == root``。**绝不 kill-server**(AdbBackend 协议里根本没有这个方法)。失败只发 warn ``QIDIAN_NOT_ROOT``、state 仍 running(R6-32)。
"""
from __future__ import annotations

import asyncio
import contextlib
import base64
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from ..alerts import QIDIAN_NOT_ROOT
from ..config import H05_BOOT_POLL_S, AgentConfig
from .backends import AdbBackend, ContainerBackend, ContainerInfo, ContainerSpec
from . import apk
from .network import probe_qidian_network

log = logging.getLogger("qtrade.runtime")

PORT_BASE = {"adb": 16000, "stream": 16500, "frida": 16600, "ws": 16100, "http": 16200, "webui": 16300}
ADB_SERVER_PORT = 16000                 # 04 §2.7.4:WSL 内 adb server(Agent 拥有;不用默认 5037)
SEQ_MIN, SEQ_MAX = 1, 98                # 16099 = 安装自检临时 redroid(03)
INSPECT_CACHE_MS = 5000

AccountRow = dict[str, Any]


def port_plan(channel: str, seq: int) -> dict[str, Any]:
    """02 §2.2.4 唯一算法。返回含冗余 ``adb_serial``(``127.0.0.1:160NN``,04-P7)。"""
    if not (SEQ_MIN <= int(seq) <= SEQ_MAX):
        raise ValueError(f"seq 须在 {SEQ_MIN}..{SEQ_MAX}(16000=adb server,16099=安装自检),得到 {seq}")
    seq = int(seq)
    if channel == "qidian":
        return {"adb": PORT_BASE["adb"] + seq, "stream": PORT_BASE["stream"] + seq, "frida": PORT_BASE["frida"] + seq,
                "adb_serial": f"127.0.0.1:{PORT_BASE['adb'] + seq}"}
    if channel == "qq":
        return {"ws": PORT_BASE["ws"] + seq, "http": PORT_BASE["http"] + seq, "webui": PORT_BASE["webui"] + seq}
    if channel == "wechat":
        return {}                        # 无端口(chatlog 固定 5030,由 WinAgent 会话代理管)
    raise ValueError(f"unknown channel {channel!r}")


def seq_of(account_id: str) -> int:
    return int(account_id[2:])


def container_name(account_id: str) -> str:
    return f"qtrade-{account_id}"


def data_dir(accounts_dir: str, account_id: str) -> str:
    return f"{accounts_dir}/{account_id}/data"


@dataclass
class RuntimeInfo:
    exists: bool
    running: bool
    container: str
    container_id: Optional[str] = None
    exit_code: Optional[int] = None
    oom_killed: bool = False
    ports: dict[str, Any] = field(default_factory=dict)
    data_dir: Optional[str] = None
    checked_ms: int = 0


# ---------------------------------------------------------------------- 宿主文件系统(可注入假实现,开发容器里不碰真目录)
class Fs(Protocol):
    def purge_dir_contents(self, path: str) -> int: ...          # 清空目录内容(目录本身保留),返回释放字节;不存在 → 0
    def purge_prefixed(self, dir: str, prefix: str) -> int: ...   # 删 dir 下以 prefix 开头的文件
    def ensure_dir(self, path: str) -> None: ...
    def read_json(self, path: str) -> dict[str, Any]: ...
    def write_json(self, path: str, value: dict[str, Any]) -> None: ...


class RealFs:
    def read_json(self, path: str) -> dict[str, Any]:
        try:
            with open(path, "rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return {}
        if len(raw) > 1024 * 1024:
            raise ValueError("NapCat configuration too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid NapCat configuration")
        return result

    def write_json(self, path: str, value: dict[str, Any]) -> None:
        # 原配置内可能含 NapCat 自有 token；原目录内原子替换，不另留凭据副本。
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".qtrade-config-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def purge_dir_contents(self, path: str) -> int:
        if not os.path.isdir(path):
            return 0
        freed = 0
        for name in os.listdir(path):
            p = os.path.join(path, name)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    for root, _dirs, files in os.walk(p):
                        for f in files:
                            with contextlib.suppress(OSError):
                                freed += os.path.getsize(os.path.join(root, f))
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        freed += os.path.getsize(p)
                    os.remove(p)
            except OSError:
                pass
        return freed

    def purge_prefixed(self, dir: str, prefix: str) -> int:
        if not os.path.isdir(dir):
            return 0
        freed = 0
        for name in os.listdir(dir):
            if name.startswith(prefix):
                p = os.path.join(dir, name)
                with contextlib.suppress(OSError):
                    freed += os.path.getsize(p)
                    os.remove(p)
        return freed

    def ensure_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)


class FakeFs:
    """内存文件系统:``files[path] = size``。"""

    def __init__(self):
        self.files: dict[str, int] = {}
        self.dirs: set[str] = set()
        self.json_files: dict[str, dict[str, Any]] = {}

    def read_json(self, path: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.json_files.get(path, {})))

    def write_json(self, path: str, value: dict[str, Any]) -> None:
        self.json_files[path] = json.loads(json.dumps(value))
        self.files[path] = len(json.dumps(value))

    def put(self, path: str, size: int = 1024) -> None:
        self.files[path] = size

    def purge_dir_contents(self, path: str) -> int:
        freed = 0
        for p in list(self.files):
            if p.startswith(path.rstrip("/") + "/"):
                freed += self.files.pop(p)
        return freed

    def purge_prefixed(self, dir: str, prefix: str) -> int:
        freed = 0
        for p in list(self.files):
            if os.path.dirname(p) == dir.rstrip("/") and os.path.basename(p).startswith(prefix):
                freed += self.files.pop(p)
        return freed

    def ensure_dir(self, path: str) -> None:
        self.dirs.add(path)


# ---------------------------------------------------------------------- Runtime
class Runtime:
    def __init__(self, *, containers: ContainerBackend, adb: AdbBackend, cfg: AgentConfig, health, alerts, store,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000), fs: Optional[Fs] = None, boot_poll_s: float = H05_BOOT_POLL_S,
                 adbkeyboard_apk: Optional[str] = None):
        self._containers = containers
        self._adb = adb
        self.cfg = cfg
        self._health = health
        self._alerts = alerts
        self._store = store
        self._clock = clock
        self._fs: Fs = fs or RealFs()
        self._boot_poll_s = boot_poll_s
        self._adbkeyboard_apk = adbkeyboard_apk
        self.start_lock = asyncio.Lock()                       # A.5:启动全局串行
        self._inspect_cache: dict[str, tuple[int, RuntimeInfo]] = {}
        self.qidian_root_fail_streak: dict[str, int] = {}      # 04 H06 (b):内存态、按 account_id 一个整数、不落库
        self.h06_fail_streak: dict[str, int] = {}              # 04 H06 (a)
        self.start_count = 0
        self.max_concurrent_starts = 0
        self._starting_now = 0

    # ---- 推导
    @staticmethod
    def port_plan(channel: str, seq: int) -> dict[str, Any]:
        return port_plan(channel, seq)

    def data_dir(self, account_id: str) -> str:
        return data_dir(self.cfg.runtime.accounts_dir, account_id)

    def _serial(self, acct: AccountRow) -> str:
        return f"127.0.0.1:{PORT_BASE['adb'] + int(acct['seq'])}"

    def spec_for(self, acct: AccountRow, *, props: Optional[dict[str, str]] = None, mac: Optional[str] = None) -> ContainerSpec:
        ch, seq, aid = acct["channel"], int(acct["seq"]), acct["id"]
        rt = self.cfg.runtime
        dd = self.data_dir(aid)
        if ch == "qidian":
            w, h = rt.qidian_resolution.split("x")
            base = {"androidboot.redroid_width": w, "androidboot.redroid_height": h, "androidboot.redroid_dpi": str(rt.qidian_dpi),
                    "androidboot.redroid_gpu_mode": rt.gpu_mode, "androidboot.use_memfd": "true",
                    "ro.enable.native.bridge.exec": "1", "ro.dalvik.vm.native.bridge": "libnb.so"}
            base.update(props or {})            # device_profiles → ro.product.* / ro.serialno / fingerprint(05 §2.5.1)
            return ContainerSpec(name=container_name(aid), image=rt.redroid_image, ports={PORT_BASE["adb"] + seq: 5555}, volumes={dd: "/data"},
                                 mem_limit_mb=int(acct.get("mem_limit_mb") or rt.qidian_mem_limit_mb), props=base, mac=mac)
        if ch == "qq":
            identity = json.loads(acct.get("identity_json") or "{}")
            uin = str(identity.get("qq_uin") or acct.get("self_uid") or "")
            if uin and (not uin.isascii() or not uin.isdigit()):
                raise ValueError("Invalid QQ identity")
            return ContainerSpec(name=container_name(aid), image=rt.napcat_image,
                                 ports={PORT_BASE["ws"] + seq: 3001, PORT_BASE["http"] + seq: 3000, PORT_BASE["webui"] + seq: 6099},   # WebUI 始终映射(C-35)
                                 volumes={f"{dd}/qq_data": "/app/.config/QQ", f"{dd}/napcat_config": "/app/napcat/config"},
                                 mem_limit_mb=int(acct.get("mem_limit_mb") or rt.qq_mem_limit_mb), privileged=False,
                                 env={"ACCOUNT": uin} if uin else {})
        raise ValueError(f"{ch} 账号的 runtime 在 WinAgent 侧(host=windows),本模块不编排")

    # ---- inspect(缓存 ≤ 5 s)
    async def inspect(self, acct: AccountRow, *, fresh: bool = False) -> RuntimeInfo:
        aid = acct["id"]
        now = self._clock()
        hit = self._inspect_cache.get(aid)
        if hit and not fresh and now - hit[0] <= INSPECT_CACHE_MS:
            return hit[1]
        ci: Optional[ContainerInfo] = await self._containers.inspect(container_name(aid))
        info = RuntimeInfo(exists=ci is not None, running=bool(ci and ci.running), container=container_name(aid),
                           container_id=ci.id if ci else None, exit_code=ci.exit_code if ci else None, oom_killed=bool(ci and ci.oom_killed),
                           ports=port_plan(acct["channel"], int(acct["seq"])), data_dir=self.data_dir(aid), checked_ms=now)
        self._inspect_cache[aid] = (now, info)
        return info

    # ---- 编排
    async def provision(self, acct: AccountRow, *, props: Optional[dict[str, str]] = None, mac: Optional[str] = None) -> RuntimeInfo:
        """建卷目录 + 容器(不存在才 create,不 start)。"""
        info = await self.inspect(acct, fresh=True)
        self._fs.ensure_dir(self.data_dir(acct["id"]))
        self._fs.ensure_dir(f"{self.cfg.runtime.accounts_dir}/{acct['id']}/tmp")
        if not info.exists:
            spec = self.spec_for(acct, props=props, mac=mac)
            cid = await self._containers.create(spec)
            self._store.upsert_runtime(acct["id"], kind=self._store.RUNTIME_KIND[acct["channel"]], container_id=cid, container_name=spec.name,
                                       image_ref=spec.image, data_dir=self.data_dir(acct["id"]), mem_limit_mb=spec.mem_limit_mb)
            info = await self.inspect(acct, fresh=True)
        return info

    def _napcat_config_path(self, acct: AccountRow, filename: str) -> str:
        seq = int(acct["seq"])
        if acct.get("channel") != "qq" or not 1 <= seq <= 98 or acct["id"] != f"qq{seq:02d}":
            raise ValueError("Invalid QQ account")
        return f"{self.data_dir(acct['id'])}/napcat_config/{filename}"

    def napcat_webui_token(self, acct: AccountRow) -> str:
        value = self._fs.read_json(self._napcat_config_path(acct, "webui.json")).get("token")
        return value if isinstance(value, str) else ""

    def napcat_webui_enabled(self, acct: AccountRow) -> bool:
        config = self._fs.read_json(self._napcat_config_path(acct, "webui.json"))
        return bool(config) and config.get("disableWebUI", False) is False

    async def prepare_napcat(self, acct: AccountRow) -> None:
        """启动前落实官方 4.18.28 配置；不清 QQ 数据，不覆盖其它配置键。"""
        # 官方 Docker entrypoint 在 napcat.json 不存在时复制整套模板；先保留/建立
        # 官方允许的默认配置，避免首次解压覆盖下面已写好的 OneBot/WebUI 配置。
        core_path = self._napcat_config_path(acct, "napcat.json")
        self._fs.write_json(core_path, self._fs.read_json(core_path))
        webui_path = self._napcat_config_path(acct, "webui.json")
        webui = self._fs.read_json(webui_path)
        webui.update({"host": "0.0.0.0", "port": 6099, "disableWebUI": False})
        self._fs.write_json(webui_path, webui)
        identity = json.loads(acct.get("identity_json") or "{}")
        uin = str(identity.get("qq_uin") or acct.get("self_uid") or "")
        if uin and (not uin.isascii() or not uin.isdigit()):
            raise ValueError("Invalid QQ identity")
        filenames = ["onebot11.json"] + ([f"onebot11_{uin}.json"] if uin else [])
        for filename in filenames:
            path = self._napcat_config_path(acct, filename)
            config = self._fs.read_json(path)
            network = config.setdefault("network", {})
            for key, port in (("websocketServers", 3001), ("httpServers", 3000)):
                servers = network.setdefault(key, [])
                server = next((s for s in servers if s.get("port") == port), None)
                if server is None:
                    server = {"name": f"qtrade-{key}", "port": port, "token": ""}
                    servers.append(server)
                server.update({"enable": True, "host": "0.0.0.0", "messagePostFormat": "array", "debug": False})
                if key == "websocketServers":
                    server.update({"reportSelfMessage": True, "heartInterval": 5000})
            self._fs.write_json(path, config)

    def napcat_onebot_token(self, acct: AccountRow) -> Optional[str]:
        identity = json.loads(acct.get("identity_json") or "{}")
        uin = str(identity.get("qq_uin") or acct.get("self_uid") or "")
        filename = f"onebot11_{uin}.json" if uin and uin.isascii() and uin.isdigit() else "onebot11.json"
        config = self._fs.read_json(self._napcat_config_path(acct, filename))
        for server in config.get("network", {}).get("websocketServers", []):
            if server.get("port") == 3001 and server.get("enable"):
                return server.get("token") or None
        return None

    async def read_napcat_qrcode(self, acct: AccountRow) -> bytes:
        self._napcat_config_path(acct, "webui.json")   # 账号路径/容器名校验
        from ..adapters.qq.login import MAX_QR_BYTES, NapCatLoginError
        try:
            # 固定路径、限长；不把 QR 写到 Agent 的文件或日志。
            raw = await asyncio.wait_for(self._containers.exec(container_name(acct["id"]),
                f"head -c {MAX_QR_BYTES + 1} /app/napcat/cache/qrcode.png | base64 -w0"), 5)
            data = base64.b64decode(raw.strip(), validate=True)
            if len(data) > MAX_QR_BYTES:
                raise ValueError
            return data
        except Exception:
            raise NapCatLoginError("qrcode_file_unavailable") from None

    async def set_napcat_webui(self, acct: AccountRow, enable: bool) -> bool:
        path = self._napcat_config_path(acct, "webui.json")
        config = self._fs.read_json(path)
        confirmed_uid = acct.get("qq_confirmed_uid")
        if confirmed_uid is not None and (not isinstance(confirmed_uid, str) or not confirmed_uid.isascii() or not confirmed_uid.isdigit()):
            raise ValueError("Invalid confirmed QQ identity")
        current_attempt = acct.get("qq_login_current", lambda: True)
        async with self._serial_ctx():
            if not current_attempt():
                return False
            observed = await self._containers.inspect(container_name(acct["id"]))
            if not current_attempt():
                return False
            registered = self._store.get_runtime(acct["id"]) or {}
            cid = registered.get("container_id") or ""
            owned = (observed is not None and observed.name == registered.get("container_name") == container_name(acct["id"])
                     and observed.id and (cid == observed.id or
                     (len(observed.id) >= 12 and cid.startswith(observed.id))))
            if not owned:
                raise RuntimeError("QQ container ownership changed")
            if confirmed_uid is not None:
                current = self._store.get_account_full(acct["id"]) or {}
                if current.get("state") != "logging_in" or not observed.running:
                    raise RuntimeError("QQ container or login phase changed")
            # Only the inspected startup ACCOUNT proves what a restart will use.
            # A database UID alone cannot repair a container created without -q.
            rebuild = not enable and confirmed_uid is not None and observed.account_env != confirmed_uid
            if (config.get("disableWebUI", False) == (not enable)
                    and not acct.get("napcat_restart_required") and not rebuild):
                return False
            config["disableWebUI"] = not enable
            self._fs.write_json(path, config)
            if observed.running:
                await self._containers.stop(container_name(acct["id"]), timeout_s=10)
                stopped = await self.inspect(acct, fresh=True)
                if not current_attempt():
                    return False
                if stopped.container_id != observed.id:
                    raise RuntimeError("QQ container identity changed while stopping")
                if stopped.running:
                    raise RuntimeError("QQ container did not stop")
                if rebuild:
                    await self._containers.remove(container_name(acct["id"]), volumes=False)
                    if (await self.inspect(acct, fresh=True)).exists:
                        raise RuntimeError("QQ container was not removed")
                    if not current_attempt():
                        return False
                    spec = self.spec_for(acct | {"self_uid": confirmed_uid})
                    spec.image = registered.get("image_ref") or spec.image
                    new_id = await self._containers.create(spec)
                    self._store.upsert_runtime(acct["id"], kind="napcat", container_id=new_id,
                                               container_name=spec.name, now_ms=self._clock())
                    if not current_attempt():
                        return False
                await self._containers.start(container_name(acct["id"]))
                self._inspect_cache.pop(acct["id"], None)
                if not (await self.inspect(acct, fresh=True)).running:
                    raise RuntimeError("QQ container did not start")
                return True
        return False

    def _serial_ctx(self):
        return self.start_lock if self.cfg.runtime.start_serial else contextlib.nullcontext()

    async def start(self, acct: AccountRow, *, props: Optional[dict[str, str]] = None, mac: Optional[str] = None) -> RuntimeInfo:
        """在全局 ``start_lock`` 里:先 ``_purge_ephemeral``(崩溃残留补清,§2.2.4)→ provision → docker start → (企点)adb connect。"""
        async with self._serial_ctx():
            self._starting_now += 1
            self.max_concurrent_starts = max(self.max_concurrent_starts, self._starting_now)
            try:
                await self._purge_ephemeral(acct)
                if acct["channel"] == "qq":
                    await self.prepare_napcat(acct)
                info = await self.provision(acct, props=props, mac=mac)
                if not info.running:
                    await self._containers.start(info.container)
                if acct["channel"] == "qidian":
                    await self._adb.connect(self._serial(acct))
                self.start_count += 1
                self._store.upsert_runtime(acct["id"], kind=self._store.RUNTIME_KIND[acct["channel"]], last_started_ms=self._clock())
                return await self.inspect(acct, fresh=True)
            finally:
                self._starting_now -= 1

    async def wait_boot(self, acct: AccountRow) -> bool:
        """05 §2.1.1 ⑤ / 04 H05:轮询 ``sys.boot_completed=1``,超时 ``[runtime] boot_timeout_s``(默认 180)→ False(调用方置 error(BOOT_TIMEOUT))。"""
        serial = self._serial(acct)
        deadline = self._clock() + self.cfg.runtime.boot_timeout_s * 1000
        while True:
            v = (await self._adb.shell(serial, "getprop sys.boot_completed")).strip()
            if v == "1":
                self._store.upsert_runtime(acct["id"], kind="redroid", last_boot_completed_ms=self._clock())
                return True
            if self._clock() >= deadline:
                return False
            await asyncio.sleep(self._boot_poll_s)

    async def ensure_root(self, acct: AccountRow) -> bool:
        """06 §2.9.5 唯一出处,三步逐字;判据 = ``whoami == root``。失败:warn ``QIDIAN_NOT_ROOT``、state 不动、永不 crit(R6-32)。"""
        aid, serial = acct["id"], self._serial(acct)
        self._health.mark_rooting(aid, self.cfg.health.adb_root_grace_s)       # 窗口起点 = ensure_root 开始(R6-32/R6-35)
        await self._adb.root(serial)                                           # adb -s 127.0.0.1:160NN root
        await self._adb.shell(serial, "stop adbd; start adbd")                 # ① 容器内重启 adbd
        await self._adb.disconnect(serial)                                     # ② 宿主只断该账号这一条连接
        await self._adb.connect(serial)                                        # ③ 宿主只重连该账号这一条连接
        whoami = (await self._adb.shell(serial, "whoami")).strip()
        subject = f"account:{aid}"
        if whoami == "root":
            self.qidian_root_fail_streak[aid] = 0
            self._alerts.resolve(QIDIAN_NOT_ROOT, subject=subject, account_id=aid)
            return True
        n = self.qidian_root_fail_streak[aid] = self.qidian_root_fail_streak.get(aid, 0) + 1
        self._alerts.firing(QIDIAN_NOT_ROOT, subject=subject, account_id=aid, hint_actions=["open_env"],
                            evidence={"whoami": whoami, "ensure_root_attempts": n, "db_visible": False})
        log.warning("ensure_root 失败 account=%s whoami=%r attempts=%d(state 保持 running,读取降级到控件树)", aid, whoami, n)
        return False

    async def check_qidian_network(self, acct: AccountRow) -> dict[str, Any]:
        """boot/root 后的一次启动前置；与周期宿主探测开关独立，不访问凭据。"""
        self._store.insert_audit(
            kind="system", transport="system", actor="system:runtime", action="runtime.network_probe.start",
            account_id=acct["id"], result_code="OK", detail={"phase": "android_dns_tcp"}, now_ms=self._clock(),
        )
        report = await probe_qidian_network(self._adb, self._serial(acct), self.cfg.probe.qidian_hosts)
        self._store.insert_audit(
            kind="system", transport="system", actor="system:runtime", action="runtime.network_probe",
            account_id=acct["id"], result_code="OK" if report["ok"] else "NOT_READY",
            detail=report, now_ms=self._clock(),
        )
        return report

    async def prepare_qidian(self, acct: AccountRow) -> None:
        """05 §2.1.1 ⑥：先查包，只给缺包的实例准备 APK 并安装，绝不清登录数据。"""
        serial = self._serial(acct)
        missing: list[str] = []
        for package in (apk.ADBKEYBOARD_PACKAGE, apk.QIDIAN_PACKAGE):
            try:
                if not await self._adb.package_installed(serial, package):
                    missing.append(package)
            except Exception as e:
                raise apk.ApkPreparationError("INSTALL_FAILED", f"无法确认 {package} 安装状态") from e
        sources: dict[str, Path] = {}
        for package in missing:
            if package == apk.ADBKEYBOARD_PACKAGE:
                path = Path(self._adbkeyboard_apk or apk.ADBKEYBOARD_APK)
                if not path.is_file():
                    raise apk.ApkPreparationError("APK_UNAVAILABLE", "随包 ADBKeyboard APK 不可用")
            else:
                path = await apk.qidian_apk(self.cfg.runtime, honor_env_proxy=self.cfg.net.honor_env_proxy)
            sources[package] = path
        for package, path in sources.items():
            try:
                await self._adb.install(serial, str(path), expected_package=package)
                if not await self._adb.package_installed(serial, package):
                    raise RuntimeError("安装后仍未找到应用")
            except Exception as e:
                raise apk.ApkPreparationError("INSTALL_FAILED", f"{package} 安装失败或安装后不可见") from e

    async def stop(self, acct: AccountRow, *, graceful: bool = True) -> RuntimeInfo:
        """停容器(不串行)。``_purge_ephemeral`` 由账号状态机在置 ``stopped`` 之后、释放额度之前调(05 §2.5.7 / 02 §2.2.4)。"""
        info = await self.inspect(acct, fresh=True)
        if info.exists and info.running:
            await self._containers.stop(info.container, timeout_s=30 if graceful else 5)
        return await self.inspect(acct, fresh=True)

    async def restart(self, acct: AccountRow) -> RuntimeInfo:
        await self.stop(acct, graceful=True)
        await self._purge_ephemeral(acct)
        return await self.start(acct)

    async def destroy(self, acct: AccountRow, *, keep_data: bool = True) -> None:
        """删容器;``keep_data=True`` 保留卷目录(基线 §11.7 [KEEPVOL],#7 软删默认)。"""
        info = await self.inspect(acct, fresh=True)
        if info.exists:
            if info.running:
                await self._containers.stop(info.container, timeout_s=10)
            await self._containers.remove(info.container, volumes=not keep_data)
        await self._purge_ephemeral(acct)
        self._inspect_cache.pop(acct["id"], None)

    async def dockerd_ok(self) -> bool:
        try:
            return await self._containers.ping()
        except Exception:
            return False

    # ---- 清临时(E-19)
    async def _purge_ephemeral(self, acct: AccountRow) -> dict[str, Any]:
        aid, ch = acct["id"], acct["channel"]
        root = self.cfg.runtime.accounts_dir
        media_tmp = f"{os.path.dirname(root.rstrip('/'))}/media/tmp"
        freed = 0
        targets: list[str] = []
        info = await self.inspect(acct, fresh=True)
        if ch == "qidian" and info.running:
            serial = self._serial(acct)
            # ① 容器内截图 / uiautomator dump / frida gadget 临时;⑤ frida 注入的临时脚本 *.js 也在这条 rm 里
            await self._adb.shell(serial, "rm -rf /data/local/tmp/* /data/local/tmp/.study* 2>/dev/null")
            targets.append("container:/data/local/tmp")
            # ④ frida 转发
            await self._adb.forward_remove(serial, f"tcp:{PORT_BASE['frida'] + int(acct['seq'])}")
            targets.append(f"adb_forward:tcp:{PORT_BASE['frida'] + int(acct['seq'])}")
        elif ch == "qidian":
            targets.append("container:skipped(not running)")
        # ② 宿主 accounts/<id>/tmp/ 整目录清空
        freed += self._fs.purge_dir_contents(f"{root}/{aid}/tmp")
        targets.append(f"host:{root}/{aid}/tmp")
        # ③ media/tmp 里属于该账号的中间文件(按 account_id 前缀筛,不误删别账号在途下载)
        freed += self._fs.purge_prefixed(media_tmp, f"{aid}-")
        targets.append(f"host:{media_tmp}/{aid}-*")
        freed_mb = round(freed / 1048576, 3)
        self._store.insert_audit(kind="system", transport="system", actor="system:runtime", action="runtime.purge_ephemeral", account_id=aid,
                                 result_code="OK", detail={"account_id": aid, "freed_mb": freed_mb, "targets": targets}, now_ms=self._clock())
        return {"freed_mb": freed_mb, "targets": targets}
