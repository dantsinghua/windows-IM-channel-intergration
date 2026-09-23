@echo off
rem ===========================================================================
rem  QTrade installer -- SFX chain entry: free-space check, stage payload,
rem  then launch engine. Target: %ProgramData%\QTrade
rem  docs/03 section 2.1
rem
rem  Why this layer exists: official 7-Zip SfxSetup stub (LZMA SDK) only:
rem    extract to %TEMP%\7zS<rand>\ , run RunProgram, delete the temp dir.
rem  It does not honor InstallPath (that is a 7zsfxmm key) and does not
rem  pass through exit codes (source hard-codes return 0).
rem  Section 2.1 requires extract target %ProgramData%\QTrade and keep it
rem  (resume / upgrade / repair / uninstall all need it).
rem  So this script does the "stage": it runs in the temp dir, moves the
rem  payload to the target, then launches the engine from the target.
rem
rem  Ruling (1), 2026-09-20: "check 6 GB before SFX extract" is a known
rem    deviation: the stub has no pre-extract hook. Earliest check is
rem    "before the engine runs". Mapped as:
rem      (a) target drive free less than 6 GB -> exit 26 (E_INSTALL_DISK_LOW);
rem      (b) extract incomplete (engine or manifest missing) AND disk short
rem          -> also exit 26, not 41.
rem    Exit 123 (DISK_FULL) is for engine-phase write failures only
rem    (those are resumable).
rem
rem  Same-volume move is a rename: %TEMP% and %ProgramData% default to the
rem    system drive, so move is O(1) metadata rename with no extra space.
rem    Only when TEMP is redirected to another volume does it become a real
rem    copy; then this script raises the threshold and shows a message.
rem
rem  Exit codes: the outer stub discards ours, so the engine exit code is
rem    also written to <target>\logs\last-exit-code.txt. Automation should
rem    read that file or install_state.json; do NOT treat the SFX EXE exit
rem    code as the verdict (it is always 0). This is already ruled.
rem
rem  Test injection: QT_INSTALL_ROOT may override the target dir so staging
rem    can be verified without touching %ProgramData%.
rem
rem  Encoding: the ENTIRE file must be pure ASCII (including rem lines).
rem    No UTF-8 BOM. Newlines must be CRLF. SFX launches .cmd under an
rem    unknown code page; non-ASCII in rem lines can eat newlines when
rem    cmd.exe misreads UTF-8 as GBK. BOM breaks the first line.
rem ===========================================================================
setlocal enabledelayedexpansion

rem -- source = temp extract root (this script is under <root>\install\engine\)
set "QT_SRC=%~dp0..\.."
for %%I in ("%QT_SRC%") do set "QT_SRC=%%~fI"

rem -- target = %ProgramData%\QTrade (QT_STAGE_TARGET_LINE) --------------------
rem  Intentionally no switch to change the install target here:
rem     a production backdoor that can retarget install is an attack surface
rem     and will be misused. For packaging self-tests of staging, see
rem     build/README section 8: generate a COPY of this script, replace the
rem     line below with a temp dir, then run the copy. Do not add param or
rem     env parsing here -- env vars get dropped on the chain
rem     WSL -> powershell -> Start-Process -> SFX -> cmd; and batch
rem     for %%A in (%*) splits on '='. Both paths were tried and failed.
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
rem  SELF-HOSTING TRAP: this script lives in <src>\install\engine\.
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
