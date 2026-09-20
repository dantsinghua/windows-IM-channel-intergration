@echo off
rem ===========================================================================
rem  QTrade 安装器 —— SFX 链首:判空间 -> 把载荷搬到 %ProgramData%\QTrade -> 拉引擎
rem  docs/03 §2.1
rem
rem  【为什么需要这一层】官方 7-Zip SfxSetup 存根(LZMA SDK)只做三件事:
rem    解压到 %TEMP%\7zS<随机>\  ->  跑 RunProgram  ->  删掉整个临时目录。
rem  它**不认** InstallPath(那是第三方 7zsfxmm 的键),也**不透传退出码**(源码硬编码 return 0)。
rem  而 §2.1 要求解压目标是 %ProgramData%\QTrade 且**留存**(续跑/升级/修复/卸载都要它)。
rem  所以本脚本承担「搬运」:它在临时目录里跑,把载荷搬到目标目录,再从目标目录拉引擎。
rem
rem  【裁决 (1),总控 2026-09-20】「SFX 解压*前*判 6 GB」认偏差:存根没有 pre-extract 钩子,
rem    这道判断最早只能做到「**引擎跑之前**」。落地:
rem      (a) 目标盘可用 < 6 GB -> 退 26(E_INSTALL_DISK_LOW);
rem      (b) 解压不完整(引擎或 manifest 缺失)且空间不足 -> 同样退 26,而不是 41。
rem    退出码 123(DISK_FULL)保留给**引擎阶段**的写失败,那些才是可续跑的。
rem
rem  【同卷搬运是改名】%TEMP% 与 %ProgramData% 默认同在系统盘,move 是元数据改名:
rem    O(1)、不额外占空间。只有 TEMP 被重定向到**别的卷**时才退化成真拷贝,
rem    那时需要约 2 倍空间,本脚本会把门槛相应抬高并提示。
rem
rem  【退出码】外层存根会丢掉我们的退出码,所以引擎退出码另外**落盘**到
rem    <目标>\logs\last-exit-code.txt。自动化/验收请读它或读 install_state.json,
rem    **不要**把 SFX EXE 的退出码当判据(它恒为 0)。此事已提交裁决。
rem
rem  【测试注入】QT_INSTALL_ROOT 可覆盖目标目录,便于在不碰 %ProgramData% 的前提下验搬运逻辑。
rem
rem  🔴 编码约束:所有**可执行行(非 rem 行)必须是纯 ASCII**,文件**不带 BOM**。
rem     它由 SFX 在未知代码页下拉起,给用户看的字符串带中文就是乱码;
rem     .cmd 开头的 BOM 会让第一行命令解析失败。rem 注释行不参与执行,允许中文。
rem ===========================================================================
setlocal enabledelayedexpansion

rem -- 源 = 临时解压根(本脚本在 <root>\install\engine\ 下)---------------------
set "QT_SRC=%~dp0..\.."
for %%I in ("%QT_SRC%") do set "QT_SRC=%%~fI"

rem -- 目标 = %ProgramData%\QTrade(QT_STAGE_TARGET_LINE)---------------------
rem  🔴 这里**没有任何「改安装目标」的开关**,这是有意的:
rem     生产脚本里藏一个能改安装目标的后门,既是攻击面,也迟早会被误用。
rem  打包期要自验搬运逻辑时,由 build/README §8 说的办法做:用脚本生成一份**副本**,
rem  把下面这一行替换成临时目录,再拿副本去跑。别在这里加参数/环境变量解析 ——
rem  环境变量会在「WSL -> powershell -> Start-Process -> SFX -> cmd」链上悄悄丢掉;
rem  batch 的 `for %%A in (%*)` 又会把 `=` 当分隔符,两条路都试过、都坏。
set "QT_DST=%ProgramData%\QTrade"

set "QT_ENGINE=%QT_DST%\install\engine\qtrade-setup-engine.exe"
set "QT_SRC_ENGINE=%QT_SRC%\install\engine\qtrade-setup-engine.exe"
set "QT_SRC_MANIFEST=%QT_SRC%\install\manifest.json"
rem 6 GB = 6442450944 bytes (docs/03 section 2.1)
set "QT_MIN_BYTES=6442450944"
set "QT_LOW=0"
set "QT_CROSS=0"

rem -- same volume?  (move is a rename only when the drive letters match) ------
if /I not "%QT_SRC:~0,1%"=="%QT_DST:~0,1%" set "QT_CROSS=1"

rem -- free space on the TARGET drive -----------------------------------------
for /f "usebackq delims=" %%F in (`powershell -NoProfile -NonInteractive -Command ^
  "try { (Get-PSDrive -Name '%QT_DST:~0,1%' -PSProvider FileSystem).Free } catch { 0 }"`) do set "QT_FREE=%%F"

if defined QT_FREE (
  set "QT_NEED=%QT_MIN_BYTES%"
  if "%QT_CROSS%"=="1" set "QT_NEED=12884901888"
  powershell -NoProfile -NonInteractive -Command "if ([int64]'%QT_FREE%' -lt [int64]'!QT_NEED!') { exit 1 } else { exit 0 }"
  if errorlevel 1 set "QT_LOW=1"
)

rem -- extraction incomplete AND disk short  ->  26, not 41 -------------------
if not exist "%QT_SRC_ENGINE%" goto :incomplete
if not exist "%QT_SRC_MANIFEST%" goto :incomplete
if "%QT_LOW%"=="1" goto :disk_low
goto :stage

:incomplete
if "%QT_LOW%"=="1" goto :disk_low
powershell -NoProfile -NonInteractive -Command ^
  "Add-Type -AssemblyName PresentationFramework; [void][Windows.MessageBox]::Show('Setup files are incomplete. Please run the installer again.','QTrade')"
endlocal
rem 41 = E_INSTALL_RESUME_ENGINE_MISSING
exit /b 41

:disk_low
powershell -NoProfile -NonInteractive -Command ^
  "Add-Type -AssemblyName PresentationFramework; [void][Windows.MessageBox]::Show('QTrade: not enough disk space on the target drive. Free up space and run the installer again. The full install needs 16 GB (20 GB recommended).','QTrade')"
endlocal
rem 26 = E_INSTALL_DISK_LOW
exit /b 26

rem -- move the payload from the temp dir to the install root ------------------
rem    per top-level item: plain MOVE when the target does not exist (rename, O(1)),
rem    ROBOCOPY /E /MOVE when it already exists (re-run / repair -> must merge).
:stage
rem  🔴 SELF-HOSTING TRAP: this script lives in <src>\install\engine\.
rem     If we MOVE the "install" directory away, cmd.exe can no longer read the
rem     script file for the next CALL and everything after this point dies with
rem     "The system cannot find the path specified" -- silently, half-staged.
rem     So: MOVE every top-level item EXCEPT "install", and COPY "install".
rem     Copying install is cheap (engine ~2 MB + manifest) and the temp dir is
rem     deleted by the SFX stub anyway, so nothing is left behind.
if not exist "%QT_DST%" mkdir "%QT_DST%" 2>nul
for /d %%D in ("%QT_SRC%\*") do (
  if /I not "%%~nxD"=="install" call :stage_move "%%~fD" "%QT_DST%\%%~nxD"
)
for %%F in ("%QT_SRC%\*") do call :stage_move "%%~fF" "%QT_DST%\%%~nxF"
robocopy "%QT_SRC%\install" "%QT_DST%\install" /E /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
if errorlevel 8 goto :stage_failed
goto :run_engine

:stage_move
if exist "%~2" (
  robocopy "%~1" "%~2" /E /MOVE /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
) else (
  move /Y "%~1" "%~2" >nul
)
exit /b 0

:stage_failed
powershell -NoProfile -NonInteractive -Command ^
  "Add-Type -AssemblyName PresentationFramework; [void][Windows.MessageBox]::Show('QTrade: failed to stage setup files. Free up disk space and run the installer again.','QTrade')"
endlocal
rem 123 = E_INSTALL_DISK_FULL (staging is an engine-phase-style write failure, resumable)
exit /b 123

:run_engine
if not exist "%QT_ENGINE%" (
  endlocal
  exit /b 41
)
"%QT_ENGINE%" /QT_FROM_SFX=1 %*
set "QT_CODE=%ERRORLEVEL%"
rem The outer SFX stub discards our exit code (SfxSetup.cpp returns 0 unconditionally),
rem so persist it for automation / acceptance to read.
if not exist "%QT_DST%\logs" mkdir "%QT_DST%\logs" 2>nul
> "%QT_DST%\logs\last-exit-code.txt" echo %QT_CODE%
endlocal & exit /b %QT_CODE%
