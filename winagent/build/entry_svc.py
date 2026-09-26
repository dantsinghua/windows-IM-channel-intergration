"""PyInstaller 入口 —— ``qtrade-winagent-svc.exe``。

🔴 为什么要这层薄壳:PyInstaller 把 ``Analysis`` 的第一个脚本当 ``__main__`` 执行。若直接指向包内
``qtrade_winagent/main_svc.py``,它的 ``from . import …`` 会报
``ImportError: attempted relative import with no known parent package``,exe 启动即崩
(2026-09-27 发现:9/21 起的包两个 exe 都是这样,pytest 全绿却从没真跑过)。
故入口放在**包外**,以绝对导入进包;包内模块保持原样。

``--selfcheck``(只供 build.ps1 冒烟门用):导入本 exe 运行期会用到的全部模块(含函数内延迟导入的
``win/`` 后端与 uvicorn 动态加载的协议实现),全部成功 exit 0,任一失败打印原因 exit 1。不起服务、不碰系统状态。
"""
import importlib
import sys

SELFCHECK_MODULES = (
    "qtrade_winagent.main_svc", "qtrade_winagent.svc", "qtrade_winagent.fakes",
    "qtrade_winagent.win.dpapi", "qtrade_winagent.win.firewall", "qtrade_winagent.win.hosts",
    "qtrade_winagent.win.netinfo", "qtrade_winagent.win.pipes", "qtrade_winagent.win.power",
    "qtrade_winagent.win.probe", "qtrade_winagent.win.proc", "qtrade_winagent.win.sysinfo",
    "qtrade_winagent.win.wsl", "qtrade_winagent.win.wechat",
    "uvicorn", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on",
    "win32api", "win32service", "win32serviceutil", "win32security", "pywintypes", "psutil",
)


def selfcheck() -> int:
    bad = []
    for name in SELFCHECK_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:                      # noqa: BLE001 —— 冒烟门要把所有失败都列出来
            bad.append(f"{name}: {type(e).__name__}: {e}")
    import os
    try:
        from qtrade_winagent.db import SCHEMA_PATH          # db.py 启动时按这个路径读 DDL 建库
        if not os.path.isfile(SCHEMA_PATH):
            bad.append(f"schema_winagent.sql 未随包: {SCHEMA_PATH}")
    except Exception as e:                          # noqa: BLE001
        bad.append(f"qtrade_winagent.db: {type(e).__name__}: {e}")
    out = sys.stdout or sys.stderr
    if out is not None:
        if bad:
            out.write("SELFCHECK FAIL\n" + "\n".join(bad) + "\n")
        else:
            from qtrade_winagent import __version__
            out.write(f"SELFCHECK OK qtrade-winagent-svc {__version__} ({len(SELFCHECK_MODULES)} modules)\n")
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        sys.exit(selfcheck())
    from qtrade_winagent.main_svc import main
    sys.exit(main())
