<#
.SYNOPSIS
    打包 WinAgent 的两个可执行体(02 §2.4,C-02):qtrade-winagent-svc.exe / qtrade-winagent-user.exe。

.DESCRIPTION
    🔴 **本脚本只能在 Windows 上跑**(PyInstaller 不能跨平台产 exe)。WSL/Linux 侧只做代码与测试。

    产物布局(与 03 的安装器约定一致):
        dist\qtrade-winagent-svc\qtrade-winagent-svc.exe    ← 服务;防火墙规则的 Program 指向它(R-14)
        dist\qtrade-winagent-user\qtrade-winagent-user.exe  ← 会话代理;不开任何入站口

    安装位置由 03 决定:%ProgramData%\QTrade\winagent\。本脚本**不注册服务、不写防火墙、不改注册表** ——
    那些全归安装器(03 §2.8)与 WinAgent 自身(#17 firewall/ensure),这里只产文件。

.PARAMETER Sign
    传入代码签名证书指纹(A-4:OV 起步)后对两个 exe 做 signtool 签名;不传则跳过。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build.ps1
    powershell -ExecutionPolicy Bypass -File build.ps1 -Sign <thumbprint> -Clean
#>
[CmdletBinding()]
param(
    [string]$Python = "py -3.12",
    [string]$Sign = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here            # winagent\
Set-Location $root

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "build.ps1 只能在 Windows 上运行(PyInstaller 不跨平台产 exe)"
}

if ($Clean) {
    Write-Host "[1/5] 清理 build/ dist/" -ForegroundColor Cyan
    Remove-Item -Recurse -Force "$root\build\build", "$root\dist" -ErrorAction SilentlyContinue
}

Write-Host "[2/5] 建虚拟环境并装依赖(含 windows + dev extra)" -ForegroundColor Cyan
$venv = "$root\.venv-build"
if (-not (Test-Path $venv)) { Invoke-Expression "$Python -m venv `"$venv`"" }
$py = "$venv\Scripts\python.exe"
& $py -m pip install --upgrade pip wheel | Out-Null
# 🔴 必须连 `dev` 一起装:第 3 步要跑 `pytest -q`,而 pytest / pytest-asyncio / httpx 都只在
#    `[dev]` extra 里(pyproject `[project.optional-dependencies]`)。只装 `[windows]` 的话,
#    干净机器上第 3 步必炸 `No module named pytest`(2026-09-21 首次在真 Windows 上跑本脚本时踩到)。
& $py -m pip install -e ".[windows,dev]" pyinstaller | Out-Null
# 会话代理侧的 UI 自动化栈(不进 pyproject 的硬依赖:Linux 上装不了)
& $py -m pip install pywinauto pillow | Out-Null
Write-Host "    ⚠️ pyweixin 不在公共源上:按 03 的随包清单从本地 wheel 安装后再打包(缺它则微信发送不可用)" -ForegroundColor Yellow

Write-Host "[3/5] 跑单元测试(全假后端;不碰真系统状态)" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试未通过,停止打包" }

Write-Host "[4/5] PyInstaller 打两个执行体(onedir)" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --distpath "$root\dist" --workpath "$root\build\build" `
    "$here\qtrade-winagent-svc.spec"
& $py -m PyInstaller --noconfirm --clean --distpath "$root\dist" --workpath "$root\build\build" `
    "$here\qtrade-winagent-user.spec"

$svcExe  = "$root\dist\qtrade-winagent-svc\qtrade-winagent-svc.exe"
$userExe = "$root\dist\qtrade-winagent-user\qtrade-winagent-user.exe"
foreach ($e in @($svcExe, $userExe)) {
    if (-not (Test-Path $e)) { throw "产物缺失:$e" }
    Write-Host ("    {0}  {1:N1} MB" -f (Split-Path -Leaf $e), ((Get-Item $e).Length / 1MB))
}

if ($Sign) {
    Write-Host "[5/5] 代码签名(A-4:OV 证书)" -ForegroundColor Cyan
    foreach ($e in @($svcExe, $userExe)) {
        & signtool sign /sha1 $Sign /fd SHA256 /tr $TimestampUrl /td SHA256 $e
        if ($LASTEXITCODE -ne 0) { throw "签名失败:$e" }
    }
} else {
    Write-Host "[5/5] 跳过签名(未传 -Sign);正式分发必须签(A-4)" -ForegroundColor Yellow
}

Write-Host "`n完成。交给 03 的安装器:" -ForegroundColor Green
Write-Host "  - 两个目录整体复制到 %ProgramData%\QTrade\winagent\"
Write-Host "  - 服务注册:sc create QTradeWinAgent binPath= `"<svc exe>`" start= delayed-auto obj= LocalSystem"
Write-Host "  - 失败恢复:sc failure QTradeWinAgent reset= 86400 actions= restart/5000/restart/30000/restart/60000  (04 H02)"
Write-Host "  - 计划任务:用户登录时以登录用户身份拉起 <user exe>(不提权)"
Write-Host "  - 防火墙:装完调一次 POST /wa/v1/firewall/ensure(安装器自己不写规则,99b ②)"
