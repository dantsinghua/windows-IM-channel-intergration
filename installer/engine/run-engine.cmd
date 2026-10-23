@echo off
rem QTrade custom SFX entry. The archive was extracted to an exclusive stage.
rem All text is ASCII; retain CRLF for cmd.exe on every system code page.
setlocal
set "QT_STAGE=%~dp0..\.."
for %%I in ("%QT_STAGE%") do set "QT_STAGE=%%~fI"
set "QT_ENGINE=%QT_STAGE%\install\engine\qtrade-setup-engine.exe"
if not exist "%QT_ENGINE%" exit /b 41
rem The engine derives StageRoot from its own executable path.
"%QT_ENGINE%" /QT_FROM_SFX=1 %*
set "QT_CODE=%ERRORLEVEL%"
if not exist "%QT_STAGE%\logs" mkdir "%QT_STAGE%\logs" 2>nul
> "%QT_STAGE%\logs\last-exit-code.txt" echo %QT_CODE%
endlocal & exit /b %QT_CODE%
