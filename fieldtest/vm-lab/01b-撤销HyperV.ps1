<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 01 的撤销脚本:关闭 Hyper-V。

.DESCRIPTION
    ⚠️ 本脚本会【改动这台机器】:它会禁用 Hyper-V 功能,把系统恢复到 01 之前的状态。

    与 01 对称的安全约束:
      1. 未以管理员身份运行 → 中文提示后退出。
      2. 执行前打印将要运行的命令,要求输入 YES 才继续。
      3. 带 /NoRestart,【绝不自动重启】。
      4. 写操作日志。

    什么时候用它:
      · 测试做完了,不想让主机长期带着 Hyper-V 监控程序跑;
      · 启用 Hyper-V 后发现别的虚拟化软件(VMware / VirtualBox)不正常;
      · 怀疑 Hyper-V 影响了主机 WSL 的稳定性,想先排除这个变量。

    注意:禁用 Hyper-V 只是关掉功能,【不会】删除已经建好的虚拟机文件
    (D:\HyperV\QTrade-Test 下的 vhdx 与配置仍在盘上)。要回收磁盘请自己删那个目录。
    本脚本【不碰】「虚拟机平台 VirtualMachinePlatform」与「适用于 Linux 的 Windows 子系统」
    —— 那两个是主机 WSL2 的命根子,动它们会让你的 WSL 起不来。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\01b-撤销HyperV.ps1 -WhatIfOnly
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\01b-撤销HyperV.ps1
#>

[CmdletBinding()]
param(
    [string] $LogDir = (Join-Path $env:LOCALAPPDATA 'QTrade-VMLab\logs'),
    [switch] $WhatIfOnly
)

$ErrorActionPreference = 'Stop'
$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = $null

function Initialize-Log {
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
    $script:logFile = Join-Path $LogDir ("disable-hyperv-{0}.log" -f $stamp)
    "QTrade VM-Lab 01b-撤销HyperV 操作日志  开始于 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" |
        Out-File -FilePath $script:logFile -Encoding UTF8
}
function Write-Log {
    param([string] $Message, [string] $Level = 'INFO', [string] $Color = 'Gray')
    Write-Host $Message -ForegroundColor $Color
    if ($script:logFile) {
        ("{0} [{1}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message) |
            Out-File -FilePath $script:logFile -Append -Encoding UTF8
    }
}
function Write-LogOnly([string] $Message) {
    if ($script:logFile) { $Message | Out-File -FilePath $script:logFile -Append -Encoding UTF8 }
}

# 原生命令:PS 5.1 的 $ErrorActionPreference 管不住 exe,必须自己读 $LASTEXITCODE
function Invoke-Dism {
    param([Parameter(Mandatory = $true)][string[]] $Arguments, [int[]] $SuccessCodes = @(0, 3010))
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Default
        $out = & dism.exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $prevEnc
    }
    foreach ($l in $out) { Write-LogOnly ("    | " + $l) }
    return [pscustomobject]@{ ExitCode = $code; Output = $out; Success = ($SuccessCodes -contains $code) }
}

# ---------- 管理员检查 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ''
    Write-Host '  ❌ 本脚本需要管理员权限。' -ForegroundColor Red
    Write-Host '     请以管理员身份打开 PowerShell,再执行:' -ForegroundColor Yellow
    Write-Host ("     powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $PSCommandPath) -ForegroundColor White
    Write-Host ''
    exit 1
}

Initialize-Log
Write-Host ''
Write-Log 'QTrade 测试虚拟机实验室 —— 01b 撤销 Hyper-V' 'INFO' 'White'
Write-Log ("操作日志:{0}" -f $logFile) 'INFO' 'DarkGray'

# ---------- 现状 ----------
Write-Host ''
Write-Log '【检查】Hyper-V 当前状态…' 'INFO' 'Cyan'
$state = 'Unknown'
try {
    $f = Get-WindowsOptionalFeature -Online -FeatureName 'Microsoft-Hyper-V-All' -ErrorAction Stop
    $state = [string] $f.State
    Write-Log ("  Microsoft-Hyper-V-All = {0}" -f $state) 'INFO' 'Gray'
} catch {
    Write-Log '  查询不到 Microsoft-Hyper-V-All(可能从未启用过)。' 'INFO' 'Gray'
}
if ($state -ne 'Enabled') {
    Write-Log '  Hyper-V 并未处于启用状态,无需撤销。' 'INFO' 'Green'
    exit 0
}

# ---------- 现有虚拟机提醒 ----------
Write-Host ''
try {
    $vms = @(Get-VM -ErrorAction Stop)
    if ($vms.Count -gt 0) {
        Write-Log ("  ⚠️ 本机当前有 {0} 台 Hyper-V 虚拟机:" -f $vms.Count) 'WARN' 'Yellow'
        $vms | ForEach-Object { Write-Log ("     - {0}(状态 {1})" -f $_.Name, $_.State) 'WARN' 'Yellow' }
        Write-Log '     禁用 Hyper-V 后这些虚拟机将无法启动,但磁盘文件仍会保留在原处。' 'WARN' 'Yellow'
        $running = @($vms | Where-Object { $_.State -eq 'Running' })
        if ($running.Count -gt 0) {
            Write-Log ("     其中 {0} 台正在运行 —— 请先自行关机,避免非正常掉电损坏来宾系统。" -f $running.Count) 'WARN' 'Red'
        }
    }
} catch {
    Write-Log '  (Hyper-V PowerShell 模块不可用,跳过虚拟机清点)' 'INFO' 'DarkGray'
}

# ---------- 打印计划 ----------
$dismLog = $logFile -replace '\.log$', '.dism.log'
$cmd = ('dism.exe /Online /Disable-Feature /FeatureName:Microsoft-Hyper-V-All /NoRestart /LogPath:"{0}"' -f $dismLog)
Write-Host ''
Write-Host ('─' * 74) -ForegroundColor DarkYellow
Write-Host '  将要执行的命令:' -ForegroundColor Yellow
Write-Host ("    {0}" -f $cmd) -ForegroundColor White
Write-Host ('─' * 74) -ForegroundColor DarkYellow
Write-LogOnly ("PLAN $cmd")
Write-Host ''
Write-Host '  会改动的东西:' -ForegroundColor Yellow
Write-Host '    · 禁用 Hyper-V 功能,重启后 Windows 不再运行在 Hyper-V 监控程序之上' -ForegroundColor Gray
Write-Host '    · Default Switch 与 vm* 系统服务随之失效' -ForegroundColor Gray
Write-Host '  不会改动的东西:' -ForegroundColor Green
Write-Host '    · 已建好的虚拟机文件(D:\HyperV\… 下的 vhdx 与配置)仍在盘上' -ForegroundColor Gray
Write-Host '    · VirtualMachinePlatform 与 Microsoft-Windows-Subsystem-Linux 一律不动' -ForegroundColor Gray
Write-Host '      (那两个是主机 WSL2 的命根子 —— 本脚本刻意绕开)' -ForegroundColor Gray
Write-Host ''
Write-Host '  🔴 本脚本不会重启你的电脑。撤销同样需要重启一次才生效,时机由你定。' -ForegroundColor Yellow
Write-Host ''

if ($WhatIfOnly) {
    Write-Log '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' 'INFO' 'Cyan'
    exit 0
}

$answer = Read-Host '  确认撤销请输入大写 YES(其它任何输入都会取消)'
Write-LogOnly ("CONFIRM input = '$answer'")
if ($answer -cne 'YES') {
    Write-Log '  已取消,没有做任何改动。' 'INFO' 'Cyan'
    exit 0
}

# ---------- 执行 ----------
Write-Host ''
Write-Log '【执行】禁用 Microsoft-Hyper-V-All…' 'INFO' 'Cyan'
$r = Invoke-Dism -Arguments @(
    '/Online', '/Disable-Feature',
    '/FeatureName:Microsoft-Hyper-V-All',
    '/NoRestart',
    ('/LogPath:{0}' -f $dismLog)
)
$r.Output | ForEach-Object { if ("$_".Trim()) { Write-Host ("    $_") -ForegroundColor DarkGray } }

if ($r.Success) {
    Write-Host ''
    Write-Log ("  ✅ 已禁用(DISM 退出码 {0})。" -f $r.ExitCode) 'INFO' 'Green'
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor Yellow
    Write-Host '  🔴 需要重启一次 Windows 才会真正生效 —— 请你自行择机重启。' -ForegroundColor Yellow
    Write-Host '     重启会中断 WSL 里所有正在运行的容器与终端。' -ForegroundColor Yellow
    Write-Host '     虚拟机的磁盘文件仍在 D:\HyperV\ 下,要回收空间请自己删那个目录。' -ForegroundColor Gray
    Write-Host ('=' * 74) -ForegroundColor Yellow
    Write-Host ''
    exit 0
} else {
    Write-Host ''
    Write-Log ("  ❌ 禁用失败,DISM 退出码 {0};详见 {1}" -f $r.ExitCode, $dismLog) 'ERROR' 'Red'
    exit 3
}
