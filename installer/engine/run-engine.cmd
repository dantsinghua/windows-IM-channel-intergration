@echo off
rem ===========================================================================
rem  QTrade 安装器 —— SFX 链首(**自编存根 QTradeSD.sfx** 路径)
rem  docs/03 §2.1
rem
rem  【和 precheck-disk.cmd 的关系】
rem    两条路都保留,用哪条取决于出包时缝进去的是哪个存根:
rem      * 官方 7zSD.sfx  -> precheck-disk.cmd:它要在 %TEMP% 里判空间、
rem        再把载荷搬到 %ProgramData%\QTrade,因为官方存根解到临时目录且跑完即删;
rem      * 自编 QTradeSD.sfx -> 本脚本:存根已经把载荷解到 %ProgramData%\QTrade
rem        并留存了,解压前的空间判据也在存根里做完了(不足直接退 26,压根不会跑到这)。
rem        所以这里只剩两件事:拉引擎、把退出码落盘。
rem
rem  【退出码】自编存根会用 GetExitCodeProcess 把本脚本的退出码原样透传成 EXE 的
rem    退出码,所以验收可以直接读 EXE 退出码。last-exit-code.txt 仍然落,
rem    作为旁证(以及万一有人拿官方存根缝了这份配置时的兜底证据)。
rem
rem  🔴 编码约束:可执行行(非 rem 行)必须纯 ASCII,文件不带 BOM,换行必须 CRLF。
rem     它由 SFX 在未知代码页下拉起;.cmd 带 BOM 会让第一行解析失败,
rem     LF-only 会让 for/if/call/标签解析错乱。rem 行不参与执行,允许中文。
rem ===========================================================================
setlocal

rem -- 本脚本在 <安装根>\install\engine\ 下,上两级就是安装根 --------------
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
