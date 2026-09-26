"""PyInstaller 入口 —— ``qtrade-winagent-user.exe``(会话代理,``console=False`` 窗口程序)。

为什么是包外薄壳:见 ``entry_svc.py`` 文件头(包内 ``main_user.py`` 当 ``__main__`` 跑会因相对导入启动即崩)。

``--selfcheck``(只供 build.ps1 冒烟门用):导入运行期会用到的全部模块后 exit 0,任一失败 exit 1。
本 exe 是窗口程序、``sys.stdout`` 为 ``None``,故 ``--help`` 不可用作冒烟(argparse 写 None 会抛异常弹框挂住);
selfcheck 自己吞掉异常以退出码报告,**绝不弹框**。不连管道、不碰系统状态。
"""
import importlib
import sys

SELFCHECK_MODULES = (
    "qtrade_winagent.main_user", "qtrade_winagent.user", "qtrade_winagent.fakes",
    "qtrade_winagent.win.pipes", "qtrade_winagent.win.power", "qtrade_winagent.win.proc",
    "qtrade_winagent.win.wsl", "qtrade_winagent.win.wechat", "qtrade_winagent.win.sysinfo",
    "pywinauto", "PIL.ImageGrab",
    "win32api", "win32security", "win32ts", "pywintypes", "psutil",
)


def selfcheck() -> int:
    bad = []
    for name in SELFCHECK_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:                      # noqa: BLE001 —— 冒烟门要把所有失败都列出来
            bad.append(f"{name}: {type(e).__name__}: {e}")
    out = sys.stdout or sys.stderr
    if out is not None:
        out.write(("SELFCHECK FAIL\n" + "\n".join(bad) + "\n") if bad
                  else f"SELFCHECK OK qtrade-winagent-user ({len(SELFCHECK_MODULES)} modules)\n")
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        sys.exit(selfcheck())
    from qtrade_winagent.main_user import main
    sys.exit(main())
