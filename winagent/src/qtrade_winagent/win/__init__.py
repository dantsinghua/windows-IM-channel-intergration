"""``Win*`` 真实现 —— **只在 Windows 真机跑**。

约定(全包统一):
- **所有 Windows 专有导入都写在函数/方法体内**(pywin32、ctypes.wintypes、psutil、wmi),
  这样在 Linux/CI 上 ``import qtrade_winagent.win.*`` 不会报错,测试注入 ``Fake*`` 即可全量跑。
- 每个类都实现 ``backends.py`` 里对应的协议;协议是唯一的对接面,业务模块不认具体实现。
- 凡会**改变系统状态**的调用(netsh/powercfg/注册表/hosts/wsl)都集中在这里,便于审计与真机验证清单对照
  (见 ``winagent/README.md`` 的「须在 Windows 真机验证清单」)。
"""
from __future__ import annotations

import subprocess
import sys
from typing import Sequence


def is_windows() -> bool:
    return sys.platform.startswith("win")


def decode_console_output(raw: bytes) -> str:
    """把 Windows 命令行工具的原始输出解成 str,**绝不抛解码异常**。

    2026-10-10 真实链路(中文 Windows,ANSI=GBK)实测:``subprocess.run(..., text=True)`` 按 locale 的 GBK 解
    ``wevtutil`` / ``powercfg`` / PowerShell 的输出,读线程里抛 ``UnicodeDecodeError('gbk' ... 0x84)``,返回值
    ``stdout=None`` ⇒ 调用方 ``.strip()`` 再炸。这些工具重定向到管道时有的出 UTF-16LE(带 BOM)、有的出
    UTF-8、有的出 OEM 代码页,一把 ``text=True`` 兜不住。判定顺序:BOM → 严格 UTF-8 → 本机 ANSI(``mbcs``,
    仅 Windows)→ UTF-8 替换字符兜底。与 Agent 侧 ``win/wsl.py`` 对 ``wsl.exe`` 输出的处理同一思路。
    """
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    # 无 BOM 的 UTF-16LE(wsl.exe 风格):偶数位全是 NUL 的 ASCII 文本
    if len(raw) >= 4 and len(raw) % 2 == 0 and raw[1::2][: min(64, len(raw) // 2)].count(0) >= min(64, len(raw) // 2) * 0.9:
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if is_windows():
        try:
            return raw.decode("mbcs")
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("utf-8", errors="replace")


def run_text(args: Sequence[str], *, timeout: float, check: bool = False) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True)`` 的文本版:自己解码(见 ``decode_console_output``),不交给 ``text=True``。

    返回的 ``CompletedProcess.stdout/stderr`` 恒为 ``str``(可能为空串,绝不为 ``None``)。
    """
    p = subprocess.run(list(args), capture_output=True, timeout=timeout, check=check)
    return subprocess.CompletedProcess(p.args, p.returncode,
                                       decode_console_output(p.stdout or b""), decode_console_output(p.stderr or b""))


def require_windows(what: str) -> None:
    if not is_windows():
        raise RuntimeError(f"{what} 只能在 Windows 上运行;开发/CI 环境请注入 fakes.py 里的假实现")
