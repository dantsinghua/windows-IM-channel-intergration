# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— ``qtrade-winagent-user.exe``(用户会话代理;计划任务「用户登录时」拉起,**不提权**)。

02 §2.4 / C-02:它要**交互桌面**(微信 UI 自动化)与用户自己的 WSL 登记,所以是独立执行体;
**不监听任何端口**(04 §2.6.3 R-14:防火墙规则里绝不出现它),只作命名管道客户端。

与服务 spec 的差别:
1. 多带 UI 自动化与图像栈(pywinauto / pyweixin / Pillow),那是微信模块要用的;
2. 不带 uvicorn —— 它不起 HTTP(fastapi 因 main_user→main_svc→svc 的顶层导入链仍会被带上)。
"""
import os

try:
    from PyInstaller.utils.hooks import collect_all, collect_submodules
except ImportError:                       # 仅测试在无 PyInstaller 的环境里执行本文件做静态检查;真打包时一定有
    def collect_all(_pkg):                # type: ignore[no-redef]
        return [], [], []

    def collect_submodules(_pkg):         # type: ignore[no-redef]
        return []

block_cipher = None
HERE = os.path.dirname(os.path.abspath(SPEC))                               # noqa: F821
SRC = os.path.join(HERE, "..", "src")

# R6-92:pyweixin(随包 wheel,必需)在包级 import 时就把 WeChatAuto 的全部依赖拉进来(pyautogui / pycaw /
# sounddevice / soundfile / bs4 / emoji / markdownify …),而 win/ 里是延迟导入 —— PyInstaller 静态扫不到,
# 漏一个 exe 里发送就 ImportError。soundfile / sounddevice 还带原生 DLL(libsndfile / portaudio),必须 collect_all。
_datas, _binaries, _hidden = [], [], []
for _pkg in ("pyweixin", "_soundfile_data", "_sounddevice_data", "soundfile", "sounddevice", "pycaw", "comtypes",
             "pyautogui", "emoji", "markdownify", "bs4"):
    _d, _b, _h = collect_all(_pkg)
    _datas += _d
    _binaries += _b
    _hidden += _h

a = Analysis(
    # 🔴 入口必须是**包外**薄壳(entry_user.py 文件头有原因);指向包内 main_user.py 会因相对导入启动即崩
    [os.path.join(HERE, "entry_user.py")],
    pathex=[SRC],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=[
        "win32api", "win32con", "win32file", "win32gui", "win32pipe", "win32process",
        "win32security", "win32ts", "pywintypes", "psutil", "winreg",
        "pywinauto", "pywinauto.application", "PIL", "PIL.ImageGrab",
        "qtrade_winagent.win.pipes", "qtrade_winagent.win.power", "qtrade_winagent.win.proc",
        "qtrade_winagent.win.wsl", "qtrade_winagent.win.wechat", "qtrade_winagent.win.sysinfo",
        "qtrade_winagent.main_user", "qtrade_winagent.fakes",
    ] + collect_submodules("pyweixin") + collect_submodules("pywinauto") + _hidden,
    hookspath=[],
    runtime_hooks=[],
    # ⚠️ 不能排除 fastapi/starlette:main_user 顶层 import main_svc(取 DEFAULT_ROOT 等),main_svc 顶层 import svc,
    #    svc 顶层 import fastapi —— 排掉它 exe 一启动就 ModuleNotFoundError。uvicorn 在 main_svc.main() 里才导入,可排。
    excludes=["tkinter", "uvicorn", "pytest", "matplotlib"],
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
