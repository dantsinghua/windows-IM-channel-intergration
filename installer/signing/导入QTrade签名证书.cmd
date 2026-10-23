@echo off
rem QTrade —— 双击导入代码签名证书(给目标机同事用)
rem 规格:docs/03 §2.2.3(AppLocker 环境下由 IT 把发布者加白)
rem
rem 它做什么:自动请求管理员权限 -> 调 Import-QtCodeSigningCert.ps1
rem           (由 ps1 自己在本目录找 .cer、核对指纹、导入两个存储)。
rem 撤销:同目录 Remove-QtCodeSigningCert.ps1 -Thumbprint <指纹>(管理员 PowerShell)。
rem
rem 🔴 本文件必须 CRLF、无 BOM、可执行行纯 ASCII(与 engine\*.cmd 同一套规矩:
rem    LF-only 会让 cmd.exe 的 for/if/call/标签解析错乱,BOM 会让第一行解析失败)。
rem 🔴 所有中文提示一律由 PowerShell 脚本打印 —— 本文件的可执行行不出现任何非 ASCII 字符,
rem    因为 cmd.exe 在目标机上的代码页未知,内联中文会乱码甚至改变解析。

setlocal
chcp 65001 >nul 2>&1

rem ---- 提权自检:未提权就用 UAC 重新拉起自己(带 --elevated 防止无限递归)----
if /i "%~1"=="--elevated" goto :elevated
net session >nul 2>&1
if not errorlevel 1 goto :elevated
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
exit /b %errorlevel%

:elevated
rem 必须 -ExecutionPolicy Bypass:证书还没导入,ps1 在 AllSigned 机器上本来就跑不了 —— 鸡生蛋。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Import-QtCodeSigningCert.ps1"
set QT_RC=%errorlevel%
echo.
pause
exit /b %QT_RC%
