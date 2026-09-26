# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— ``qtrade-winagent-svc.exe``(服务 ``QTradeWinAgent``,LocalSystem,delayed-auto)。

02 §2.4:Python 3.12 嵌入式 + pywin32 服务壳 + PyInstaller **单目录**(onedir,不是 onefile):
- onedir 启动快、崩溃转储可读、服务壳能被 SCM 正常托管;onefile 每次启动解压到 %TEMP%,服务场景不合适。
- **HTTP 只在本执行体里**:uvicorn(``ws="websockets"``)监听 ``127.0.0.1:17610`` 与 vEthernet (WSL) 地址。
- 防火墙规则的 ``Program`` 就指向本 exe(04 §2.6.3 / R-14);会话代理 exe 不开任何入站口。

产物:``dist/qtrade-winagent-svc/qtrade-winagent-svc.exe`` + 依赖目录。
"""
import os

block_cipher = None
HERE = os.path.dirname(os.path.abspath(SPEC))                               # noqa: F821
SRC = os.path.join(HERE, "..", "src")

a = Analysis(
    # 🔴 入口必须是**包外**薄壳(entry_svc.py 文件头有原因);指向包内 main_svc.py 会因相对导入启动即崩
    [os.path.join(HERE, "entry_svc.py")],
    pathex=[SRC],
    binaries=[],
    # winagent.db 的 DDL 逐字抽自 docs/02 §3.2,必须随包(db.py 启动时读它建库)
    datas=[(os.path.join(SRC, "qtrade_winagent", "schema_winagent.sql"), "qtrade_winagent")],
    hiddenimports=[
        # uvicorn / fastapi 的动态导入
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on", "websockets",
        # Windows 专有(win/ 里是**函数内延迟导入**,PyInstaller 静态扫不到,必须在这里点名)
        "win32api", "win32con", "win32file", "win32gui", "win32pipe", "win32process",
        "win32security", "win32service", "win32serviceutil", "win32ts", "winerror", "pywintypes",
        "psutil", "winreg",
        # 本包里被 main_svc 按需导入的模块
        "qtrade_winagent.win.dpapi", "qtrade_winagent.win.firewall", "qtrade_winagent.win.hosts",
        "qtrade_winagent.win.netinfo", "qtrade_winagent.win.pipes", "qtrade_winagent.win.power",
        "qtrade_winagent.win.probe", "qtrade_winagent.win.proc", "qtrade_winagent.win.sysinfo",
        "qtrade_winagent.win.wsl", "qtrade_winagent.win.wechat",
        "qtrade_winagent.main_svc", "qtrade_winagent.fakes",
    ],
    hookspath=[],
    runtime_hooks=[],
    # 服务里不需要 UI 自动化 / 图像栈(那些只在会话代理 exe 里用)
    excludes=["tkinter", "pywinauto", "pyweixin", "PIL", "pytest", "matplotlib", "numpy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)                        # noqa: F821
exe = EXE(                                                                   # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="qtrade-winagent-svc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # 🔴 不压:UPX 常被 EDR 判为可疑,安装在企业机上会被拦
    console=True,                    # 服务进程无窗口交互;console=True 便于 `sc start` 排障时抓 stderr
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=os.path.join(os.path.dirname(os.path.abspath(SPEC)), "version_svc.txt"),   # noqa: F821
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,  # noqa: F821
               name="qtrade-winagent-svc")
