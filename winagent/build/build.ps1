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

function Invoke-Native {
    <#
    .SYNOPSIS
        跑一条原生命令(python / pip / PyInstaller),**退出码非零就 throw,并把输出吐出来**。

    .DESCRIPTION
        🔴 为什么非要包这一层:上面那句 `$ErrorActionPreference = "Stop"` 只管 PowerShell **cmdlet**
        产生的错误记录,**管不到原生 exe 的非零退出码** —— 原生命令失败既不抛异常也不停脚本,
        执行流照样往下走(PS 7.3+ 才有 `$PSNativeCommandUseErrorActionPreference` 能改这个默认,
        而本脚本要能在 Windows 自带的 PowerShell 5.1 上跑,不能指望它)。
        再叠加当初那几个 `| Out-Null` 把 stdout 一起吞掉,pip 的失败原因就彻底看不见了。

        实测(2026-09-21 首次在真 Windows 上跑本脚本):第 2 步装依赖其实**已经失败**,却一路静默,
        直到第 3 步才以 `No module named pytest` 的面目出现 —— 报错点离病灶隔了一整步,很难查。

        故本函数的契约:**成功照旧安静**(等价于原来的 `| Out-Null`,不刷屏),
        **失败必须把完整输出打出来再 throw**,让报错点就落在出问题的那条命令上。

        ⚠️ 里面临时把 `$ErrorActionPreference` 降成 `Continue` 是必须的:`2>&1` 会把原生命令写到
        stderr 的内容转成 ErrorRecord,而在 `Stop` 之下这会**误判成终止错误** —— pip 光是
        「WARNING: You are using pip version ...」就够把一次成功的安装炸成失败。
        成败一律以 `$LASTEXITCODE` 为准,这也是语义上唯一正确的判据。
    #>
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][scriptblock]$Cmd
    )
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $Cmd 2>&1 } finally { $ErrorActionPreference = $prevEap }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "---- $What 的完整输出(exit=$LASTEXITCODE)----" -ForegroundColor Yellow
        $out | ForEach-Object { Write-Host $_ }
        throw "$What 失败(exit=$LASTEXITCODE)"
    }
}

if ($Clean) {
    Write-Host "[1/6] 清理 build/ dist/" -ForegroundColor Cyan
    Remove-Item -Recurse -Force "$root\build\build", "$root\dist" -ErrorAction SilentlyContinue
}

Write-Host "[2/6] 建虚拟环境并装依赖(含 windows + dev extra)" -ForegroundColor Cyan
$venv = "$root\.venv-build"
if (-not (Test-Path $venv)) { Invoke-Native "建虚拟环境" { Invoke-Expression "$Python -m venv `"$venv`"" } }
$py = "$venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "虚拟环境里没有 python.exe:$py" }
Invoke-Native "升级 pip/wheel" { & $py -m pip install --upgrade pip wheel }
# 🔴 必须连 `dev` 一起装:第 3 步要跑 `pytest -q`,而 pytest / pytest-asyncio / httpx 都只在
#    `[dev]` extra 里(pyproject `[project.optional-dependencies]`)。只装 `[windows]` 的话,
#    干净机器上第 3 步必炸 `No module named pytest`(2026-09-21 首次在真 Windows 上跑本脚本时踩到)。
Invoke-Native "装 windows+dev 依赖与 pyinstaller" { & $py -m pip install -e ".[windows,dev]" pyinstaller }
# 会话代理侧的 UI 自动化栈(不进 pyproject 的硬依赖:Linux 上装不了)
Invoke-Native "装 pywinauto/pillow" { & $py -m pip install pywinauto pillow }
# 就地自检:第 3 步要用的东西现在就确认装到了**这个** venv 里,别等跑到第 3 步才发现缺件。
Invoke-Native "自检 pytest/pyinstaller 可导入" { & $py -c "import pytest, PyInstaller" }
Write-Host "    ⚠️ pyweixin 不在公共源上:按 03 的随包清单从本地 wheel 安装后再打包(缺它则微信发送不可用)" -ForegroundColor Yellow

Write-Host "[3/6] 跑单元测试(全假后端;不碰真系统状态)" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试未通过,停止打包" }

Write-Host "[4/6] PyInstaller 打两个执行体(onedir)" -ForegroundColor Cyan
# 这两条同样是原生命令:不查 $LASTEXITCODE 的话,PyInstaller 失败会被静默吞掉,
# 一路滑到下面的 Test-Path 才以「产物缺失」的面目出现 —— 那时真正的报错早已刷过去了。
Invoke-Native "PyInstaller 打 svc" {
    & $py -m PyInstaller --noconfirm --clean --distpath "$root\dist" --workpath "$root\build\build" `
        "$here\qtrade-winagent-svc.spec"
}
Invoke-Native "PyInstaller 打 user" {
    & $py -m PyInstaller --noconfirm --clean --distpath "$root\dist" --workpath "$root\build\build" `
        "$here\qtrade-winagent-user.spec"
}

$svcExe  = "$root\dist\qtrade-winagent-svc\qtrade-winagent-svc.exe"
$userExe = "$root\dist\qtrade-winagent-user\qtrade-winagent-user.exe"
foreach ($e in @($svcExe, $userExe)) {
    if (-not (Test-Path $e)) { throw "产物缺失:$e" }
    Write-Host ("    {0}  {1:N1} MB" -f (Split-Path -Leaf $e), ((Get-Item $e).Length / 1MB))
}

function Invoke-ExeSmoke {
    <#
    .SYNOPSIS
        冒烟门:真跑一次打出来的 exe,退出码非 0 或超时(视为挂住)就 throw。

    .DESCRIPTION
        🔴 为什么要这道门:pytest 全绿只证明源码在解释器里能跑,**证明不了 exe 能跑**。9/21 起的包里
        两个 exe 启动即崩(spec 把包内 main_*.py 当入口,相对导入失败;user 是窗口程序,崩了弹错误框挂住),
        直到 9/27 才被发现。故打完必须真跑一次,挂住按超时处理并按 PID 结束**本函数自己起的**进程。
        输出落到 build\build\smoke-*.txt(gitignored),失败时整段吐出来。
    #>
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$ArgList,
        [int]$TimeoutS = 20
    )
    $tag = "{0}{1}" -f [IO.Path]::GetFileNameWithoutExtension($Exe), ($ArgList -join "")
    $outF = "$root\build\build\smoke-$tag.out.txt"
    $errF = "$root\build\build\smoke-$tag.err.txt"
    New-Item -ItemType Directory -Force -Path "$root\build\build" | Out-Null
    $p = Start-Process -FilePath $Exe -ArgumentList $ArgList -NoNewWindow -PassThru `
        -RedirectStandardOutput $outF -RedirectStandardError $errF
    $null = $p.Handle                     # 先取句柄:否则 PS 5.1 下进程退出后 ExitCode 读不到(为 $null)
    $label = "$(Split-Path -Leaf $Exe) $($ArgList -join ' ')"
    if (-not $p.WaitForExit($TimeoutS * 1000)) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        throw "冒烟失败:$label 超过 ${TimeoutS}s 未返回(挂住),已结束 PID $($p.Id)"
    }
    $p.WaitForExit()
    $text = @(Get-Content -LiteralPath $outF, $errF -ErrorAction SilentlyContinue)
    if ($p.ExitCode -ne 0) {
        Write-Host "---- $label 的输出(exit=$($p.ExitCode))----" -ForegroundColor Yellow
        $text | ForEach-Object { Write-Host $_ }
        throw "冒烟失败:$label exit=$($p.ExitCode)"
    }
    Write-Host "    OK  $label" -ForegroundColor Green
    $text | Select-Object -First 3 | ForEach-Object { Write-Host "        $_" }
}

Write-Host "[5/6] exe 冒烟门(--help / --selfcheck,超时 20s 视为挂住)" -ForegroundColor Cyan
Invoke-ExeSmoke $svcExe  @("--help")
Invoke-ExeSmoke $svcExe  @("--selfcheck")
# user 是 console=False 的窗口程序:stdout 为 None,--help 会让 argparse 抛异常弹框挂住,只能用 --selfcheck
Invoke-ExeSmoke $userExe @("--selfcheck")

if ($Sign) {
    Write-Host "[6/6] 代码签名(A-4:OV 证书)" -ForegroundColor Cyan
    foreach ($e in @($svcExe, $userExe)) {
        & signtool sign /sha1 $Sign /fd SHA256 /tr $TimestampUrl /td SHA256 $e
        if ($LASTEXITCODE -ne 0) { throw "签名失败:$e" }
    }
} else {
    Write-Host "[6/6] 跳过签名(未传 -Sign);正式分发必须签(A-4)" -ForegroundColor Yellow
}

Write-Host "`n完成。交给 03 的安装器:" -ForegroundColor Green
Write-Host "  - 两个目录整体复制到 %ProgramData%\QTrade\winagent\"
Write-Host "  - 服务注册:sc create QTradeWinAgent binPath= `"<svc exe>`" start= delayed-auto obj= LocalSystem"
Write-Host "  - 失败恢复:sc failure QTradeWinAgent reset= 86400 actions= restart/5000/restart/30000/restart/60000  (04 H02)"
Write-Host "  - 计划任务:用户登录时以登录用户身份拉起 <user exe>(不提权)"
Write-Host "  - 防火墙:装完调一次 POST /wa/v1/firewall/ensure(安装器自己不写规则,99b ②)"
