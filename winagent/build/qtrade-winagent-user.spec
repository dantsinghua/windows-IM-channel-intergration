# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— ``qtrade-winagent-user.exe``(用户会话代理;计划任务「用户登录时」拉起,**不提权**)。

02 §2.4 / C-02:它要**交互桌面**(微信 UI 自动化)与用户自己的 WSL 登记,所以是独立执行体;
**不监听任何端口**(04 §2.6.3 R-14:防火墙规则里绝不出现它),只作命名管道客户端。

与服务 spec 的两处差别:
1. 多带 UI 自动化与图像栈(pywinauto / pyweixin / Pillow),那是微信模块要用的;
2. 不带 uvicorn / fastapi —— 它不起 HTTP。
"""
import os

block_cipher = None
SRC = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "..", "src")     # noqa: F821

a = Analysis(
    [os.path.join(SRC, "qtrade_winagent", "main_user.py")],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=[
        "win32api", "win32con", "win32file", "win32gui", "win32pipe", "win32process",
        "win32security", "win32ts", "pywintypes", "psutil", "winreg",
        "pywinauto", "pywinauto.application", "pyweixin", "PIL", "PIL.ImageGrab",
        "qtrade_winagent.win.pipes", "qtrade_winagent.win.power", "qtrade_winagent.win.proc",
        "qtrade_winagent.win.wsl", "qtrade_winagent.win.wechat", "qtrade_winagent.win.sysinfo",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "fastapi", "uvicorn", "starlette", "pytest", "matplotlib"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)                        # noqa: F821
exe = EXE(                                                                   # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="qtrade-winagent-user",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                   # 会话代理跟着用户登录常驻,不该弹黑窗
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=os.path.join(os.path.dirname(os.path.abspath(SPEC)), "version_user.txt"),  # noqa: F821
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,  # noqa: F821
               name="qtrade-winagent-user")
