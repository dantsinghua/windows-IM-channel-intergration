@echo off
rem ===========================================================================
rem  QTrade 安装器 —— SFX 阶段的磁盘判断 + 引擎拉起(docs/03 §2.1)
rem
rem  【裁决 (1),总控 2026-09-20 初裁】「SFX 解压*前*判 6 GB」认偏差:
rem    7zSD.sfx 的 ExecuteFile / RunProgram 都在**解压完成之后**才执行,模块没有
rem    pre-extract 钩子,所以这道判断最早只能做到「**引擎跑之前**」。落地口径:
rem      (a) 本脚本在引擎之前判一次目标盘可用空间,< 6 GB 直接退 26(E_INSTALL_DISK_LOW);
rem      (b) 解压目录空间不足导致 SFX 自身解压失败时,该失败同样**映射到退出码 26**
rem          —— 表现为解压不完整(引擎或 manifest 缺失)且可用空间不足,本脚本据此判定并退 26,
rem          而不是退 41(RESUME_ENGINE_MISSING,那是「用户把 ProgramData 删了」的场景)。
rem    退出码 123(E_INSTALL_DISK_FULL)保留给**引擎阶段**的写失败:manifest 落盘 /
rem    wsl --import / docker load / 升级期 data-backup —— 那些才是可续跑的。
rem
rem  判完把**同样的命令行参数**透传给引擎,并把引擎退出码原样当作自己的退出码(§2.1)。
rem
rem  🔴 编码约束:**所有可执行行(非 rem 行)必须是纯 ASCII**，且文件**不带 BOM**。
rem     理由:它由 SFX 在**未知代码页**下拉起——给用户看的字符串带中文就是乱码；
rem     .cmd 开头的 BOM 会让第一行命令解析失败。rem 注释行不参与执行，允许中文。
rem ===========================================================================
setlocal enabledelayedexpansion

set "QT_ROOT=%ProgramData%\QTrade"
set "QT_ENGINE=%QT_ROOT%\install\engine\qtrade-setup-engine.exe"
set "QT_MANIFEST=%QT_ROOT%\install\manifest.json"
rem 6 GB = 6442450944 bytes (docs/03 section 2.1)
set "QT_MIN_BYTES=6442450944"
set "QT_LOW=0"

rem -- free space on the ProgramData drive ------------------------------------
for /f "usebackq delims=" %%F in (`powershell -NoProfile -NonInteractive -Command ^
  "(Get-PSDrive -Name ([IO.Path]::GetPathRoot($env:ProgramData).Substring(0,1)) -PSProvider FileSystem).Free"`) do set "QT_FREE=%%F"

if defined QT_FREE (
  powershell -NoProfile -NonInteractive -Command "if ([int64]'%QT_FREE%' -lt [int64]'%QT_MIN_BYTES%') { exit 1 } else { exit 0 }"
  if errorlevel 1 set "QT_LOW=1"
)

rem -- (b) extraction incomplete AND disk short  ->  26, not 41 ---------------
if not exist "%QT_ENGINE%" goto :incomplete
if not exist "%QT_MANIFEST%" goto :incomplete

rem -- (a) plain low-disk gate before the engine ------------------------------
if "%QT_LOW%"=="1" goto :disk_low

goto :run_engine

:incomplete
if "%QT_LOW%"=="1" goto :disk_low
powershell -NoProfile -NonInteractive -Command ^
  "Add-Type -AssemblyName PresentationFramework; [void][Windows.MessageBox]::Show('Setup files are incomplete. Please run the installer again.','QTrade')"
endlocal
rem 41 = E_INSTALL_RESUME_ENGINE_MISSING
exit /b 41

:disk_low
powershell -NoProfile -NonInteractive -Command ^
  "Add-Type -AssemblyName PresentationFramework; [void][Windows.MessageBox]::Show('QTrade: not enough disk space. The system drive needs at least 6 GB to extract, and 16 GB (20 GB recommended) for the full install. Free up space and run the installer again.','QTrade')"
endlocal
rem 26 = E_INSTALL_DISK_LOW
exit /b 26

:run_engine
"%QT_ENGINE%" /QT_FROM_SFX=1 %*
set "QT_CODE=%ERRORLEVEL%"
endlocal & exit /b %QT_CODE%
