@echo off
chcp 65001 >nul
rem ============================================================
rem  QTrade 测试虚拟机实验室 —— 只读体检的双击入口
rem  这个批处理【只读】:不启用功能、不建虚拟机、不重启。
rem  双击即可运行,跑完停在窗口里等你按键,方便看结果。
rem ============================================================
setlocal
cd /d "%~dp0"

echo.
echo   正在运行只读体检(不会改动这台机器)...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp000-只读体检.ps1"

echo.
echo   体检结束。想拿到最准的功能状态,请以管理员身份重跑一次。
echo.
pause
endlocal
