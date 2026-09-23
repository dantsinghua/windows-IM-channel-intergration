@echo off
rem ===========================================================================
rem  QTrade installer -- SFX chain entry (custom stub QTradeSD.sfx path)
rem  docs/03 section 2.1
rem
rem  Relation to precheck-disk.cmd:
rem    Both paths stay; which one runs depends on which stub was sewn in:
rem      * Official 7zSD.sfx  - use precheck-disk.cmd: it checks free space
rem        under %TEMP%, then moves the payload to %ProgramData%\QTrade,
rem        because the official stub extracts to a temp dir and deletes it.
rem      * Custom QTradeSD.sfx - this script: the stub already extracted to
rem        %ProgramData%\QTrade and kept it; pre-extract space check is done
rem        in the stub (exit 26 on short disk, so we never reach here).
rem        So this script only: launch engine, persist exit code to disk.
rem
rem  Exit codes: the custom stub uses GetExitCodeProcess to pass this
rem    script's exit code through as the EXE exit code. Acceptance can read
rem    the EXE exit code directly. last-exit-code.txt is still written as
rem    side evidence (and as fallback if someone sewed the official stub).
rem
rem  Encoding: the ENTIRE file must be pure ASCII (including rem lines).
rem    No UTF-8 BOM. Newlines must be CRLF. SFX launches .cmd under an
rem    unknown code page; non-ASCII bytes in rem lines can eat newlines
rem    when cmd.exe misreads UTF-8 as GBK, and comment fragments then run
rem    as commands. BOM breaks the first line; LF-only breaks for/if/call.
rem ===========================================================================
setlocal

rem -- this script lives under <install root>\install\engine\; two levels up --
set "QT_DST=%~dp0..\.."
for %%I in ("%QT_DST%") do set "QT_DST=%%~fI"

set "QT_ENGINE=%QT_DST%\install\engine\qtrade-setup-engine.exe"

if not exist "%QT_ENGINE%" (
  powershell -NoProfile -NonInteractive -Command ^
    "Add-Type -AssemblyName PresentationFramework; [void][Windows.MessageBox]::Show('Setup files are incomplete. Please run the installer again.','QTrade')"
  endlocal
  rem 41 = E_INSTALL_RESUME_ENGINE_MISSING
  exit /b 41
)

"%QT_ENGINE%" /QT_FROM_SFX=1 %*
set "QT_CODE=%ERRORLEVEL%"

if not exist "%QT_DST%\logs" mkdir "%QT_DST%\logs" 2>nul
> "%QT_DST%\logs\last-exit-code.txt" echo %QT_CODE%

endlocal & exit /b %QT_CODE%
