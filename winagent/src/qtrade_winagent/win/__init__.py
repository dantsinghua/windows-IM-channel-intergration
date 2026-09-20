"""``Win*`` 真实现 —— **只在 Windows 真机跑**。

约定(全包统一):
- **所有 Windows 专有导入都写在函数/方法体内**(pywin32、ctypes.wintypes、psutil、wmi),
  这样在 Linux/CI 上 ``import qtrade_winagent.win.*`` 不会报错,测试注入 ``Fake*`` 即可全量跑。
- 每个类都实现 ``backends.py`` 里对应的协议;协议是唯一的对接面,业务模块不认具体实现。
- 凡会**改变系统状态**的调用(netsh/powercfg/注册表/hosts/wsl)都集中在这里,便于审计与真机验证清单对照
  (见 ``winagent/README.md`` 的「须在 Windows 真机验证清单」)。
"""
from __future__ import annotations

import sys


def is_windows() -> bool:
    return sys.platform.startswith("win")


def require_windows(what: str) -> None:
    if not is_windows():
        raise RuntimeError(f"{what} 只能在 Windows 上运行;开发/CI 环境请注入 fakes.py 里的假实现")
