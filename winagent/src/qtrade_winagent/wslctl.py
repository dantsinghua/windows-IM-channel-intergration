"""``wslctl`` 模块(**会话代理**执行;02 §2.4 / 04 §2.7 / 03 §2.6)。

``wsl.exe`` 的全部调用与 ``.wslconfig`` 的读写都在这里 —— 它们必须在**安装用户会话**里做:
LocalSystem 在 Session 0 解析到的是 ``systemprofile\\.wslconfig``,**不是安装用户的那份**(R3-6)。

``.wslconfig`` 写入规则(04 §2.7.1,**键集与规则的唯一出处 = 04**):

- **R1 先备份**到 ``%ProgramData%\\QTrade\\wsl\\.wslconfig.bak-<yyyyMMdd-HHmmss>``(R6-14 后缀统一;保留最近 10 份)。
- **R2 只写/改我们关心的键**(逐行解析,**不整文件覆盖**,其余键与注释原样保留)。
- **R3 只有 ``memory=`` 一键改用户已有值**(B-5);``maxCrashDumpCount``/``crashDumpFolder`` 按 N-7 =
  **键缺失才写我方值,已有用户值一律保留不改**(仅 ``maxCrashDumpCount>2`` 时告警建议 2);
  其余键(``swap``/``localhostForwarding``/``autoMemoryReclaim``/``sparseVhd``/``guiApplications``)
  **用户已有值一律保留、只提示,不覆盖**,仅缺失时按默认写。
- **幂等**:用户文件里的键值若已等于我方目标值则**不写不备份不提示**。
- 改后需 ``wsl --shutdown`` 生效,**时机由用户确认**(00 §11.6 [NOSHUTDOWN]);本模块**从不自动 shutdown**。

内核三判据(R-15,取代已证伪的 ``ls /dev/binder`` 单一节点):① ``uname -r`` 含我方 binder 内核标识;
② ``/proc/filesystems`` 里有 ``binder``;③ **实际 mount 测试**(挂 binderfs 并读回三设备)。**三段全过**才算可用。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .backends import WslBackend
from .config import WslConfig
from .errors import INVALID_ARGS, INTERNAL, WaError
from .logfmt import get_logger

log = get_logger("wslctl")

# 04 §2.7.1 键集(**十一键**;R-10 由九键补入 crash dump 两键)。
#   section  = 写进 .wslconfig 的段;policy = 写入规则
#     "ours"          我方键,缺失则写、已有且不同则改(只有 memory= 属于这一类,B-5 / R3)
#     "fill_missing"  缺失才写,已有用户值保留不改(crash dump 两键按 N-7;其余键按 R3 的「保留只提示」)
#     "never"         不写、不动(Win11 专有键 + 已证会引发 fse 自旋锁风暴的两个)
WSLCONFIG_KEYS: dict[str, tuple[str, str]] = {
    "kernel": ("wsl2", "fill_missing"),              # 03 按 kernel_state 决定;OTHER_CUSTOM 时只提示不改(B-4)
    "memory": ("wsl2", "ours"),                      # 🔴 唯一「改用户已有值」的键(B-5 / R3)
    "processors": ("wsl2", "fill_missing"),          # 默认 0 = 不写(全部逻辑核)
    "swap": ("wsl2", "fill_missing"),
    "localhostForwarding": ("wsl2", "fill_missing"),
    "autoMemoryReclaim": ("experimental", "fill_missing"),
    "sparseVhd": ("experimental", "fill_missing"),
    "vmIdleTimeout": ("wsl2", "never"),              # Win11 才有
    "guiApplications": ("wsl2", "fill_missing"),
    "maxCrashDumpCount": ("wsl2", "fill_missing"),   # N-7:缺失才写 2;已有保留,>2 只告警
    "crashDumpFolder": ("wsl2", "fill_missing"),
}
# 04 §2.7.1:**不写、不动**;用户文件里已有则原样保留并在 P-ENV 显示为「用户自定义」
NEVER_TOUCH = ("networkingMode", "dnsTunneling", "autoProxy", "firewall", "vmIdleTimeout")
MAX_CRASH_DUMP_SUGGEST = 2
BACKUP_SUFFIX_FMT = "bak-%Y%m%d-%H%M%S"              # R6-14:统一 bak-<yyyyMMdd-HHmmss>,点号式旧后缀作废

KERNEL_MARKER = "binder"                             # 判据①:uname -r 含我方 binder 内核标识
BINDERFS_DEVICES = ("binder", "binder_ctl", "binderfs_features")   # 判据③ 读回三设备


# ---------------------------------------------------------------------- .wslconfig 逐行解析


@dataclass
class WslConfigFile:
    """逐行模型:保留原文顺序、注释与未知键(R2「不整文件覆盖」的实现基础)。"""
    lines: list[str]

    @staticmethod
    def parse(text: str) -> "WslConfigFile":
        return WslConfigFile(text.splitlines())

    def values(self) -> dict[str, tuple[str, str]]:
        """→ ``{key: (section, value)}``;键名大小写按原文,查找时不敏感。"""
        out: dict[str, tuple[str, str]] = {}
        section = ""
        for line in self.lines:
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1].strip()
                continue
            if not s or s.startswith(("#", ";")) or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = (section, v.strip())
        return out

    def get(self, key: str) -> Optional[str]:
        for k, (_, v) in self.values().items():
            if k.lower() == key.lower():
                return v
        return None

    def set(self, key: str, value: str, section: str) -> None:
        """就地改已有键;没有则插到该段末尾;段不存在则追加新段。**其余行一字不动**。"""
        cur_section = ""
        for i, line in enumerate(self.lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                cur_section = s[1:-1].strip()
                continue
            if "=" in s and not s.startswith(("#", ";")):
                k = s.split("=", 1)[0].strip()
                if k.lower() == key.lower() and cur_section.lower() == section.lower():
                    self.lines[i] = f"{k}={value}"
                    return
        idx = None
        cur_section = ""
        for i, line in enumerate(self.lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if cur_section.lower() == section.lower():
                    idx = i
                    break
                cur_section = s[1:-1].strip()
        if cur_section.lower() == section.lower() and idx is None:
            idx = len(self.lines)
        if idx is None:
            self.lines += ([""] if self.lines and self.lines[-1].strip() else []) + [f"[{section}]", f"{key}={value}"]
        else:
            self.lines.insert(idx, f"{key}={value}")

    def text(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines else "")


def esc_path(p: str) -> str:
    """``.wslconfig`` 里的 Windows 路径要**反斜杠转义**(04 §2.7.1 ``kernel``/``crashDumpFolder`` 行)。"""
    return p.replace("\\", "\\\\") if "\\\\" not in p else p


# ---------------------------------------------------------------------- wslctl


@dataclass
class KernelVerdict:
    """#26 ``POST /wa/v1/wsl/kernel/verify`` 的返回:``{ok, uname, binder_fs, binderfs_mount}``(02 §3.6 字段逐字)。"""
    ok: bool
    uname: str
    binder_fs: bool
    binderfs_mount: bool

    def view(self) -> dict[str, Any]:
        return {"ok": self.ok, "uname": self.uname, "binder_fs": self.binder_fs, "binderfs_mount": self.binderfs_mount}


class WslCtl:
    def __init__(self, wsl: WslBackend, cfg: WslConfig, *, backup_dir: Optional[str] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._wsl = wsl
        self._cfg = cfg
        self._backup_dir = backup_dir or cfg.backup_dir
        self._clock = clock
        self.pending_restart = False                 # #25 返回 {pending_restart:true} 并推 WSLCONFIG_PENDING_RESTART

    # ---------------------------------------------------------------- #20 status
    async def status(self) -> dict[str, Any]:
        distros = await self._wsl.list_distros()
        d = next((x for x in distros if x.get("name") == self._cfg.distro), None)
        cfgfile = WslConfigFile.parse(self._wsl.read_wslconfig())
        wsl_state = "RUNNING" if (d and d.get("running")) else ("INSTALLED" if d else "MISSING")
        kernel = cfgfile.get("kernel")
        kernel_state = "OURS" if (kernel and "QTrade" in kernel) else ("OTHER_CUSTOM" if kernel else "DEFAULT")
        return {"wsl_state": wsl_state, "kernel_state": kernel_state,
                "distro": {"name": self._cfg.distro, "running": bool(d and d.get("running")),
                           "version": (d or {}).get("version"), "user": (d or {}).get("user")},
                "wslconfig": {"memory": cfgfile.get("memory"), "processors": cfgfile.get("processors"),
                              "autoMemoryReclaim": cfgfile.get("autoMemoryReclaim"), "kernel": kernel,
                              "pending_restart": self.pending_restart}}

    # ---------------------------------------------------------------- #21/#22/#23
    async def start(self) -> dict[str, Any]:
        await self._wsl.start(self._cfg.distro)
        return {"started": self._cfg.distro}

    async def stop(self) -> dict[str, Any]:
        """#22:``wsl --terminate qtrade`` —— **只终止我们的发行版,永不 ``--shutdown``**。"""
        await self._wsl.terminate(self._cfg.distro)
        return {"terminated": self._cfg.distro}

    async def restart(self, *, mode: str = "terminate", confirm: bool = False) -> dict[str, Any]:
        """#23:``shutdown`` **必须 ``confirm=true``**(00 §11.6 [NOSHUTDOWN]),否则 400。"""
        if mode not in ("terminate", "shutdown"):
            raise WaError(INVALID_ARGS, "mode 必须是 terminate|shutdown", reason="bad_mode")
        if mode == "shutdown" and not confirm:
            raise WaError(INVALID_ARGS, "wsl --shutdown 会中断用户全部发行版,必须由用户确认时机(confirm=true)",
                          reason="shutdown_not_confirmed", needs_human=True)
        if mode == "shutdown":
            await self._wsl.shutdown()
        else:
            await self._wsl.terminate(self._cfg.distro)
        await self._wsl.start(self._cfg.distro)
        self.pending_restart = False
        return {"mode": mode, "restarted": self._cfg.distro}

    # ---------------------------------------------------------------- #24 config get
    def config_get(self) -> dict[str, Any]:
        f = WslConfigFile.parse(self._wsl.read_wslconfig())
        vals = {k: v for k, (_s, v) in f.values().items()}
        return {"path": self._wsl.wslconfig_path(), "text": f.text(), "values": vals,
                "backups": self.list_backups(), "pending_restart": self.pending_restart,
                "user_custom": [k for k in NEVER_TOUCH if k in vals]}

    def list_backups(self) -> list[str]:
        try:
            names = sorted(n for n in os.listdir(self._backup_dir) if n.startswith(".wslconfig."))
        except OSError:
            return []
        return names

    def _backup(self, text: str) -> Optional[str]:
        """R1:改前必备份,保留最近 ``backup_keep`` 份。备份目录是**服务**的提权半(``%ProgramData%``)。"""
        try:
            os.makedirs(self._backup_dir, exist_ok=True)
            stamp = time.strftime(BACKUP_SUFFIX_FMT, time.localtime(self._clock() / 1000))
            path = os.path.join(self._backup_dir, f".wslconfig.{stamp}")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            keep = self._cfg.backup_keep
            olds = sorted(n for n in os.listdir(self._backup_dir) if n.startswith(".wslconfig."))
            for n in olds[:-keep] if len(olds) > keep else []:
                os.remove(os.path.join(self._backup_dir, n))
            return path
        except OSError as e:
            raise WaError(INTERNAL, f".wslconfig 备份失败:{e}", reason="backup_failed") from e

    # ---------------------------------------------------------------- #25 config put
    def config_put(self, desired: dict[str, Any], *, allow_keys: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
        """按 R1/R2/R3 写;返回 ``{pending_restart, backup, changed, kept, warnings}``。

        ``allow_keys`` = 控制台白名单(#25 只让改 ``memory/processors/autoMemoryReclaim/swap``);
        ``None`` = 安装器/内核流程的全键写入。
        """
        text = self._wsl.read_wslconfig()
        f = WslConfigFile.parse(text)
        current = {k.lower(): v for k, (_s, v) in f.values().items()}
        changed: dict[str, Any] = {}
        kept: dict[str, Any] = {}
        warnings: list[str] = []
        for key, value in desired.items():
            if key not in WSLCONFIG_KEYS:
                raise WaError(INVALID_ARGS, f"{key} 不在 04 §2.7.1 的十一键集内", reason="unknown_key")
            section, policy = WSLCONFIG_KEYS[key]
            if policy == "never":
                warnings.append(f"{key}:按 04 §2.7.1 不写不动,已忽略")
                continue
            if allow_keys is not None and key not in allow_keys:
                raise WaError(INVALID_ARGS, f"{key} 不在本端点的白名单 {allow_keys} 内", reason="key_not_allowed")
            val = str(value)
            cur = current.get(key.lower())
            if cur is not None and cur == val:                 # 幂等:已等于目标值 ⇒ 不写不备份不提示
                continue
            if cur is not None and policy == "fill_missing":   # R3:已有用户值一律保留、只提示
                kept[key] = cur
                if key == "maxCrashDumpCount":
                    try:
                        if int(cur) > MAX_CRASH_DUMP_SUGGEST:
                            warnings.append(f"maxCrashDumpCount={cur} 偏大(单个转储实测 16 GB),建议值 {MAX_CRASH_DUMP_SUGGEST}")
                    except ValueError:
                        pass
                if key == "crashDumpFolder" and cur.upper().startswith("C:"):
                    warnings.append("crashDumpFolder 指向系统盘,建议挪到数据盘")
                continue
            f.set(key, val, section)
            changed[key] = {"from": cur, "to": val}
        if not changed:
            return {"pending_restart": self.pending_restart, "backup": None, "changed": {}, "kept": kept,
                    "warnings": warnings}
        backup = self._backup(text)
        self._wsl.write_wslconfig(f.text())
        reread = WslConfigFile.parse(self._wsl.read_wslconfig())      # 写后重读校验(04 §2.7.2)
        for key, ch in changed.items():
            if reread.get(key) != ch["to"]:
                raise WaError(INTERNAL, f".wslconfig 写后重读校验失败:{key}", reason="verify_failed")
        self.pending_restart = True                                   # **不自动 shutdown**(00 §11.6)
        return {"pending_restart": True, "backup": backup, "changed": changed, "kept": kept, "warnings": warnings}

    # ---------------------------------------------------------------- #26 kernel verify(三判据)
    async def kernel_verify(self) -> KernelVerdict:
        rc, uname = await self._wsl.exec(self._cfg.distro, ["uname", "-r"])
        uname = uname.strip()
        ok1 = rc == 0 and KERNEL_MARKER in uname
        rc2, fs = await self._wsl.exec(self._cfg.distro, ["cat", "/proc/filesystems"])
        ok2 = rc2 == 0 and any(line.split()[-1] == "binder" for line in fs.splitlines() if line.strip())
        rc3, devs = await self._wsl.exec(self._cfg.distro, ["sh", "-c",
                                                            "mkdir -p /dev/binderfs && mount -t binder binder /dev/binderfs && ls /dev/binderfs"])
        ok3 = rc3 == 0 and all(d in devs for d in BINDERFS_DEVICES)
        return KernelVerdict(ok=bool(ok1 and ok2 and ok3), uname=uname, binder_fs=ok2, binderfs_mount=ok3)

    # ---------------------------------------------------------------- 内核 apply / rollback 的**管道半**
    async def kernel_write_and_restart(self, *, kernel_path: Optional[str], confirm_shutdown: bool) -> dict[str, Any]:
        """#46/#27 的**会话代理半**:写 ``.wslconfig kernel=`` → ``wsl --shutdown`` → 拉起 → 三判据校验。

        🔴 ``confirm_shutdown`` 必须为 true 才允许 shutdown(00 §11.6);服务半(内核文件落盘/ACL/``install_history``)
        在 ``installer_ops``,两半的时序与失败合并见 02 §2.4.1 R3-15。
        """
        if not confirm_shutdown:
            raise WaError(INVALID_ARGS, "重新应用/回滚内核需要 wsl --shutdown,必须带 confirm_shutdown=true",
                          reason="shutdown_not_confirmed", needs_human=True, stage="user")
        done: list[str] = []
        if kernel_path is None:
            f = WslConfigFile.parse(self._wsl.read_wslconfig())
            text = self._wsl.read_wslconfig()
            if f.get("kernel") is not None:
                self._backup(text)
                f.lines = [l for l in f.lines if not l.strip().lower().startswith("kernel=")]
                self._wsl.write_wslconfig(f.text())
            done.append("wslconfig.kernel.cleared")
        else:
            out = self.config_put({"kernel": esc_path(kernel_path)})
            done.append("wslconfig.kernel.written")
            if out["changed"]:
                done.append(f"backup:{out['backup']}")
        await self._wsl.shutdown()
        done.append("wsl.shutdown")
        await self._wsl.start(self._cfg.distro)
        done.append("wsl.start")
        verdict = await self.kernel_verify()
        self.pending_restart = False
        return {"verify": verdict.view(), "partial": done}

    # ---------------------------------------------------------------- #47 distro repair
    async def distro_repair(self, *, confirm: bool, rootfs: str, backup_tar: str) -> dict[str, Any]:
        """03 §2.7 重导入:``--terminate`` → 导出 ``/var/lib/qtrade`` → ``--unregister`` → ``--import`` 随包 rootfs → 还原 → 拉起。

        账号卷、``agent.db``、``winagent.token`` 全保留;``confirm=true`` 必带。**不做 ``--shutdown``**。
        """
        if not confirm:
            raise WaError(INVALID_ARGS, "重导入发行版会先注销 qtrade,必须 confirm=true", reason="not_confirmed",
                          needs_human=True, stage="user")
        steps: list[str] = []
        await self._wsl.terminate(self._cfg.distro)
        steps.append("terminate")
        rc, _ = await self._wsl.exec(self._cfg.distro, ["sh", "-c", f"tar -cf {backup_tar} /var/lib/qtrade"])
        steps.append(f"export:{backup_tar}")
        await self._wsl.exec(self._cfg.distro, ["--unregister"])
        steps.append("unregister")
        await self._wsl.exec(self._cfg.distro, ["--import", self._cfg.distro, rootfs])
        steps.append("import")
        await self._wsl.start(self._cfg.distro)
        steps.append("start")
        await self._wsl.exec(self._cfg.distro, ["sh", "-c", f"tar -xf {backup_tar} -C /"])
        steps.append("restore")
        return {"accepted": True, "partial": steps}
