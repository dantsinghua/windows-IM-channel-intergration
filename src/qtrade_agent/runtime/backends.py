"""容器 / adb 执行后端 —— 协议 + 真实 CLI 实现 + 可编程假实现。

真实后端只包 ``docker`` / ``adb`` 命令行(无 SDK 依赖);容器里跑测试一律注入假后端,**绝不在开发容器里碰真 docker / adb**。
adb 协议**故意没有** ``kill_server``:06 §2.9.5 / 04 §2.7.4 第 3 条 —— 单账号问题一律不 ``kill-server``,本模块从接口上就做不到。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class ContainerSpec:
    name: str
    image: str
    ports: dict[int, int]                        # host_port → container_port,一律绑 127.0.0.1
    volumes: dict[str, str]                      # host_dir → container_dir
    mem_limit_mb: int
    props: dict[str, str] = field(default_factory=dict)     # redroid 启动参数(ro.product.* / ro.serialno …)
    mac: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    privileged: bool = True


@dataclass
class ContainerInfo:
    id: str
    name: str
    running: bool
    exit_code: Optional[int] = None
    oom_killed: bool = False
    started_ms: Optional[int] = None
    account_env: Optional[str] = None


def docker_run_argv(spec: ContainerSpec) -> list[str]:
    """``docker run`` 参数(02 §2.2.4):必带 ``--ulimit core=0``(§2.8.8 L1);端口只绑 127.0.0.1;``restart=no``(H04 由 Agent 自己退避重拉)。"""
    argv = ["docker", "run", "-d", "--name", spec.name, "--restart", "no", "--ulimit", "core=0", "--memory", f"{spec.mem_limit_mb}m"]
    if spec.privileged:
        argv.append("--privileged")
    if spec.mac:
        argv += ["--mac-address", spec.mac]
    for host_port, cport in sorted(spec.ports.items()):
        argv += ["-p", f"127.0.0.1:{host_port}:{cport}"]
    for host_dir, cdir in sorted(spec.volumes.items()):
        argv += ["-v", f"{host_dir}:{cdir}"]
    for k, v in sorted(spec.env.items()):
        argv += ["-e", f"{k}={v}"]
    argv.append(spec.image)
    for k, v in sorted(spec.props.items()):          # redroid 以 androidboot.* 形式接收 ro.* 覆盖
        argv.append(f"{k}={v}")
    return argv


class ContainerBackend(Protocol):
    async def create(self, spec: ContainerSpec) -> str: ...
    async def start(self, name: str) -> None: ...
    async def stop(self, name: str, *, timeout_s: int) -> None: ...
    async def remove(self, name: str, *, volumes: bool) -> None: ...
    async def inspect(self, name: str) -> Optional[ContainerInfo]: ...
    async def exec(self, name: str, cmd: str) -> str: ...
    async def ping(self) -> bool: ...


class AdbBackend(Protocol):
    async def connect(self, serial: str) -> bool: ...
    async def disconnect(self, serial: str) -> None: ...
    async def root(self, serial: str) -> None: ...
    async def shell(self, serial: str, cmd: str) -> str: ...
    async def shell_result(self, serial: str, cmd: str, *, timeout_s: float) -> AdbShellResult: ...
    async def devices(self) -> dict[str, str]: ...             # serial → device|offline|unauthorized
    async def forward_remove(self, serial: str, local: str) -> None: ...
    async def package_installed(self, serial: str, package: str) -> bool: ...
    async def install(self, serial: str, apk_path: str, *, expected_package: str) -> None: ...


@dataclass(frozen=True)
class AdbShellResult:
    returncode: int
    output: str


# ---------------------------------------------------------------------- 真实 CLI 实现(真机用;本仓库测试不执行)
async def _run(argv: list[str], *, timeout_s: float = 30) -> tuple[int, str]:
    def go() -> tuple[int, str]:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    return await asyncio.to_thread(go)


async def _run_cancellable(argv: list[str], *, timeout_s: float = 30) -> tuple[int, str]:
    """取消/超时终止并回收本次 CLI 子进程；不能撤销 Android 已原子完成的安装。"""
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout_s)
        return process.returncode, (stdout + stderr).decode("utf-8", errors="replace")
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(asyncio.shield(output), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            while not output.done():
                try:
                    await asyncio.shield(output)
                except asyncio.CancelledError:
                    continue
        raise


class DockerCliBackend:
    def __init__(self, docker_bin: str = "docker"):
        self._bin = docker_bin

    async def create(self, spec: ContainerSpec) -> str:
        argv = docker_run_argv(spec)
        argv[0] = self._bin
        rc, out = await _run(argv, timeout_s=120)
        if rc != 0:
            raise RuntimeError(f"docker run 失败 rc={rc}: {out.strip()[:500]}")
        return out.strip().splitlines()[-1]

    async def start(self, name: str) -> None:
        rc, out = await _run([self._bin, "start", name], timeout_s=60)
        if rc != 0:
            raise RuntimeError(f"docker start 失败: {out.strip()[:500]}")

    async def stop(self, name: str, *, timeout_s: int) -> None:
        await _run([self._bin, "stop", "-t", str(timeout_s), name], timeout_s=timeout_s + 30)

    async def remove(self, name: str, *, volumes: bool) -> None:
        argv = [self._bin, "rm", "-f"] + (["-v"] if volumes else []) + [name]
        await _run(argv, timeout_s=60)

    async def inspect(self, name: str) -> Optional[ContainerInfo]:
        rc, out = await _run([self._bin, "inspect", "-f", "{{json .}}", name], timeout_s=10)
        if rc != 0:
            return None
        try:
            d = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        st = d.get("State") or {}
        account_env = next((entry.partition("=")[2] for entry in (d.get("Config") or {}).get("Env", []) or []
                            if isinstance(entry, str) and entry.startswith("ACCOUNT=")), None)
        return ContainerInfo(id=d.get("Id", "")[:12], name=str(d.get("Name") or name).lstrip("/"),
                             running=bool(st.get("Running")), exit_code=st.get("ExitCode"),
                             oom_killed=bool(st.get("OOMKilled")), account_env=account_env)

    async def exec(self, name: str, cmd: str) -> str:
        _rc, out = await _run([self._bin, "exec", name, "sh", "-c", cmd], timeout_s=60)
        return out

    async def ping(self) -> bool:
        rc, _ = await _run([self._bin, "info"], timeout_s=5)
        return rc == 0


class AdbCliBackend:
    """宿主 adb server 固定 127.0.0.1:16000(04 §2.7.4);每条命令都 ``-s <serial>``,只动本账号那一条连接。"""

    def __init__(self, adb_bin: str = "adb", server_port: int = 16000):
        self._base = [adb_bin, "-P", str(server_port)]

    async def connect(self, serial: str) -> bool:
        _rc, out = await _run(self._base + ["connect", serial], timeout_s=10)
        return "connected" in out

    async def disconnect(self, serial: str) -> None:
        await _run(self._base + ["disconnect", serial], timeout_s=10)

    async def root(self, serial: str) -> None:
        await _run(self._base + ["-s", serial, "root"], timeout_s=15)

    async def shell(self, serial: str, cmd: str) -> str:
        _rc, out = await _run(self._base + ["-s", serial, "shell", cmd], timeout_s=30)
        return out

    async def shell_result(self, serial: str, cmd: str, *, timeout_s: float) -> AdbShellResult:
        """保留 Android shell 退出码；超时/取消只回收本次有所有权的 adb CLI。"""
        rc, out = await _run_cancellable(self._base + ["-s", serial, "shell", cmd], timeout_s=timeout_s)
        return AdbShellResult(rc, out)

    async def devices(self) -> dict[str, str]:
        _rc, out = await _run(self._base + ["devices", "-l"], timeout_s=10)
        res: dict[str, str] = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                res[parts[0]] = parts[1]
        return res

    async def forward_remove(self, serial: str, local: str) -> None:
        await _run(self._base + ["-s", serial, "forward", "--remove", local], timeout_s=10)

    async def package_installed(self, serial: str, package: str) -> bool:
        if re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package) is None:
            raise ValueError("非法 Android 包名")
        rc, out = await _run_cancellable(self._base + ["-s", serial, "shell", "pm", "list", "packages", package])
        if rc != 0:
            raise RuntimeError(f"查询 Android 包失败 rc={rc}")
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if any(not line.startswith("package:") for line in lines):
            raise RuntimeError("查询 Android 包返回异常")
        return f"package:{package}" in lines

    async def install(self, serial: str, apk_path: str, *, expected_package: str) -> None:
        # 无 -r/-d：只装缺失包，拒绝隐式替换或降级已有应用。
        rc, out = await _run_cancellable(self._base + ["-s", serial, "install", str(apk_path)], timeout_s=300)
        lines = [line.strip() for line in out.splitlines()]
        if rc != 0 or "Success" not in lines or any(line.lower().startswith(("failure", "error", "adb:")) for line in lines):
            raise RuntimeError(f"Android 包安装失败 rc={rc}")
        if not await self.package_installed(serial, expected_package):
            raise RuntimeError("Android 包安装后仍不存在")


# ---------------------------------------------------------------------- 假实现(开发容器 / 验收用)
@dataclass
class _FakeContainer:
    spec: ContainerSpec
    id: str
    running: bool = False
    exit_code: Optional[int] = None
    oom_killed: bool = False
    files: set[str] = field(default_factory=set)      # 容器内文件(模拟 /data/local/tmp 等)


class FakeContainers:
    """可编程容器后端:记录全部调用 ``calls``;``fail_start`` 可让 start 抛错。"""

    def __init__(self):
        self.containers: dict[str, _FakeContainer] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_start: set[str] = set()
        self.dockerd_ok = True
        self._n = 0

    async def create(self, spec: ContainerSpec) -> str:
        self._n += 1
        cid = f"fake{self._n:04d}"
        self.containers[spec.name] = _FakeContainer(spec, cid)
        self.calls.append(("create", spec.name))
        return cid

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))
        if name in self.fail_start:
            raise RuntimeError("docker start 失败(假后端注入)")
        self.containers[name].running = True
        self.containers[name].exit_code = None

    async def stop(self, name: str, *, timeout_s: int) -> None:
        self.calls.append(("stop", name))
        c = self.containers.get(name)
        if c:
            c.running = False
            c.exit_code = 0

    async def remove(self, name: str, *, volumes: bool) -> None:
        self.calls.append(("remove", name))
        self.containers.pop(name, None)

    async def inspect(self, name: str) -> Optional[ContainerInfo]:
        self.calls.append(("inspect", name))
        c = self.containers.get(name)
        return ContainerInfo(c.id, name, c.running, c.exit_code, c.oom_killed,
                             account_env=c.spec.env.get("ACCOUNT")) if c else None

    async def exec(self, name: str, cmd: str) -> str:
        self.calls.append(("exec", f"{name}: {cmd}"))
        return ""

    async def ping(self) -> bool:
        return self.dockerd_ok


class FakeAdb:
    """可编程 adb:``whoami_after_root``(root 后 whoami 回什么)、``boot_completed``(getprop 回什么)、``connected`` 集合;记录 ``calls``。
    没有 kill-server 方法 —— 调不到就是设计。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.whoami_after_root: dict[str, str] = {}      # serial → "root" | "shell"
        self.boot_completed: dict[str, str] = {}         # serial → "1" | "0"
        self.connected: set[str] = set()
        self.state: dict[str, str] = {}                  # serial → device|offline
        self.tmp_files: dict[str, set[str]] = {}         # serial → 容器内 /data/local/tmp 文件
        self._rooted: set[str] = set()
        # 既有夹具明确假设两包已装;全新 Android 测试按 serial 显式放入空集合。
        self.installed_packages_by_serial: dict[str, set[str]] = {}
        self.package_query_calls: list[tuple[str, str]] = []
        self.package_query_results: dict[tuple[str, str], list[Any]] = {}
        self.package_query_errors: dict[tuple[str, str], Exception] = {}
        self.install_calls: list[tuple[str, str, str]] = []
        self.install_errors: dict[tuple[str, str], Exception] = {}
        self.install_missing_packages: set[tuple[str, str]] = set()
        self.install_gates: dict[tuple[str, str], asyncio.Event] = {}
        self.install_started = asyncio.Event()
        self.active_installs: set[tuple[str, str]] = set()
        self.cancelled_installs: list[tuple[str, str]] = []
        # Android 网络探测专用脚本；键为 (serial, "dns"|"tcp")。
        self.shell_result_calls: list[tuple[str, str, float]] = []
        self.shell_result_results: dict[tuple[str, str], list[Any]] = {}
        self.shell_result_gates: dict[tuple[str, str], asyncio.Event] = {}
        self.shell_result_started = asyncio.Event()
        self.active_shell_results: set[tuple[str, str]] = set()
        self.cancelled_shell_results: list[tuple[str, str]] = []

    async def package_installed(self, serial: str, package: str) -> bool:
        self.package_query_calls.append((serial, package))
        if (serial, package) in self.package_query_errors:
            raise self.package_query_errors[(serial, package)]
        results = self.package_query_results.get((serial, package))
        if results:
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return bool(result)
        packages = self.installed_packages_by_serial.setdefault(
            serial, {"com.tencent.qidian", "com.android.adbkeyboard"})
        return package in packages

    async def install(self, serial: str, apk_path: str, *, expected_package: str) -> None:
        key = (serial, expected_package)
        self.install_calls.append((serial, str(apk_path), expected_package))
        self.active_installs.add(key)
        self.install_started.set()
        try:
            gate = self.install_gates.get(key)
            if gate is not None:
                await gate.wait()
            if key in self.install_errors:
                raise self.install_errors[key]
            packages = self.installed_packages_by_serial.setdefault(
                serial, {"com.tencent.qidian", "com.android.adbkeyboard"})
            if key not in self.install_missing_packages:
                packages.add(expected_package)
            if not await self.package_installed(serial, expected_package):
                raise RuntimeError(f"installed package missing: {expected_package}")
        except asyncio.CancelledError:
            self.cancelled_installs.append(key)
            raise
        finally:
            self.active_installs.discard(key)

    async def connect(self, serial: str) -> bool:
        self.calls.append(("connect", serial))
        self.connected.add(serial)
        self.state[serial] = "device"
        return True

    async def disconnect(self, serial: str) -> None:
        self.calls.append(("disconnect", serial))
        self.connected.discard(serial)
        self.state.pop(serial, None)

    async def root(self, serial: str) -> None:
        self.calls.append(("root", serial))
        self._rooted.add(serial)
        self.state[serial] = "offline"          # 提权后设备常卡 offline(06 §2.9.5)

    async def shell(self, serial: str, cmd: str) -> str:
        self.calls.append(("shell", f"{serial}: {cmd}"))
        if cmd.strip() == "whoami":
            if serial in self._rooted:
                return self.whoami_after_root.get(serial, "root") + "\n"
            return "shell\n"
        if cmd.startswith("getprop sys.boot_completed"):
            return self.boot_completed.get(serial, "1") + "\n"
        if cmd.startswith("rm -rf /data/local/tmp"):
            self.tmp_files.pop(serial, None)
            return ""
        if cmd.startswith("stop adbd"):
            self.state[serial] = "offline"      # 容器内重启 adbd ⇒ 短暂 offline
            return ""
        if cmd.startswith("ime list") or cmd.startswith("settings get secure default_input_method"):
            return "com.android.adbkeyboard/.AdbIME\n"
        if cmd.startswith("uiautomator dump"):
            return ('<hierarchy><node resource-id="com.tencent.qidian:id/account" bounds="[0,0][100,40]" />'
                    '<node resource-id="com.tencent.qidian:id/password" bounds="[0,50][100,90]" />'
                    '<node resource-id="com.tencent.qidian:id/login" bounds="[0,100][100,140]" /></hierarchy>')
        return ""

    async def shell_result(self, serial: str, cmd: str, *, timeout_s: float) -> AdbShellResult:
        stage = "dns" if "ping" in shlex.split(cmd) else "tcp"
        key = (serial, stage)
        self.shell_result_calls.append((serial, cmd, timeout_s))
        self.calls.append(("shell_result", f"{serial}: {cmd}"))
        self.active_shell_results.add(key)
        self.shell_result_started.set()
        try:
            gate = self.shell_result_gates.get(key)
            if gate is not None:
                await gate.wait()
            results = self.shell_result_results.get(key)
            if results:
                result = results.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return result
            if stage == "dns":
                host = shlex.split(cmd)[-1]
                return AdbShellResult(1, f"PING {host} (192.0.2.10) 56(84) bytes of data.\n"
                                         "1 packets transmitted, 0 received, 100% packet loss\n")
            return AdbShellResult(0, "")
        except asyncio.CancelledError:
            self.cancelled_shell_results.append(key)
            raise
        finally:
            self.active_shell_results.discard(key)

    async def devices(self) -> dict[str, str]:
        self.calls.append(("devices", ""))
        return dict(self.state)

    async def forward_remove(self, serial: str, local: str) -> None:
        self.calls.append(("forward_remove", f"{serial}: {local}"))

    # 便于断言
    def shell_cmds(self, serial: str) -> list[str]:
        return [c.split(": ", 1)[1] for k, c in self.calls if k == "shell" and c.startswith(serial + ": ")]

    def has_call(self, kind: str) -> bool:
        return any(k == kind for k, _ in self.calls)

    @staticmethod
    def quote(s: str) -> str:
        return shlex.quote(s)
