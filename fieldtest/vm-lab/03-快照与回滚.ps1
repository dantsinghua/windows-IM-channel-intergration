<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 第 3 步:基线检查点与一键回滚。

.DESCRIPTION
    ⚠️ 本脚本会【改动虚拟机】(打快照 / 回滚),但【不碰主机】的任何设置。

    用法三选一:
      (默认)       在虚拟机上打基线检查点 clean-baseline
      -List         列出这台虚拟机现有的全部检查点
      -Restore      一键回滚到基线检查点(每轮安装测试前用)

    为什么要基线:QTrade 安装器会改动系统(装 WSL、切内核、注册服务、写 ProgramData)。
    要反复验「装 → 卸 → 还原干净没有」,每轮都必须从同一个干净起点开始,
    否则上一轮的残留会污染这一轮的判断。

    回滚有多快:Hyper-V 检查点回滚通常几秒到十几秒,比重装系统快两个数量级。

.EXAMPLE
    # 系统刚装好、还没装任何 QTrade 东西时,打基线
    powershell -NoProfile -ExecutionPolicy Bypass -File .\03-快照与回滚.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\03-快照与回滚.ps1 -List

.EXAMPLE
    # 下一轮测试之前,回到干净起点
    powershell -NoProfile -ExecutionPolicy Bypass -File .\03-快照与回滚.ps1 -Restore
#>

[CmdletBinding(DefaultParameterSetName = 'Create')]
param(
    [string] $VMName   = 'QTrade-Test-Win11',
    [string] $Name     = 'clean-baseline',
    [Parameter(ParameterSetName = 'List')]    [switch] $List,
    [Parameter(ParameterSetName = 'Restore')] [switch] $Restore,
    [switch] $WhatIfOnly
)

$ErrorActionPreference = 'Stop'

function Write-Head([string] $Text) {
    Write-Host ''
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
    Write-Host ("  $Text") -ForegroundColor Cyan
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
}
function Write-Cmd([string] $t)  { Write-Host ("    > {0}" -f $t) -ForegroundColor White }
function Write-Ok([string] $t)   { Write-Host ("      ✔ {0}" -f $t) -ForegroundColor Green }
function Write-Warn2([string] $t) { Write-Host ("      ⚠ {0}" -f $t) -ForegroundColor Yellow }
function Write-Info([string] $t) { Write-Host ("      · {0}" -f $t) -ForegroundColor Gray }

Write-Host ''
Write-Host 'QTrade 测试虚拟机实验室 —— 03 快照与回滚' -ForegroundColor White

# ---------- 前置 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ''
    Write-Host '  ❌ 本脚本需要管理员权限(Hyper-V 命令要求提权)。' -ForegroundColor Red
    Write-Host '     请以管理员身份打开 PowerShell 后重试。' -ForegroundColor Yellow
    exit 1
}
if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    Write-Host '  ❌ 找不到 Hyper-V 命令。请先跑 01 并重启,再跑 02 建虚拟机。' -ForegroundColor Red
    exit 2
}
$vm = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if (-not $vm) {
    Write-Host ("  ❌ 找不到虚拟机「{0}」。请先跑 02-建测试虚拟机.ps1。" -f $VMName) -ForegroundColor Red
    exit 2
}
Write-Ok ("虚拟机:{0}(当前状态 {1})" -f $vm.Name, $vm.State)

# ---------- -List ----------
if ($List) {
    Write-Head ("检查点列表:{0}" -f $VMName)
    Write-Cmd ("Get-VMSnapshot -VMName '{0}'" -f $VMName)
    $snaps = @(Get-VMSnapshot -VMName $VMName -ErrorAction SilentlyContinue)
    if ($snaps.Count -eq 0) {
        Write-Info '一个检查点都没有。'
        Write-Host ''
        Write-Host ("  提示:系统装好、还没装 QTrade 任何东西时,先跑一次本脚本打基线:" ) -ForegroundColor Yellow
        Write-Host ("     .\03-快照与回滚.ps1") -ForegroundColor Gray
    } else {
        Write-Host ''
        foreach ($s in ($snaps | Sort-Object CreationTime)) {
            $mark = if ($s.Name -eq $Name) { ' ← 基线' } else { '' }
            Write-Host ("    {0,-28} {1}  [{2}]{3}" -f $s.Name, $s.CreationTime, $s.SnapshotType, $mark) -ForegroundColor White
            if ($s.ParentSnapshotName) {
                Write-Host ("        父检查点:{0}" -f $s.ParentSnapshotName) -ForegroundColor DarkGray
            }
        }
        Write-Host ''
        Write-Info ("共 {0} 个。检查点会占用磁盘(差异盘 avhdx),测试做完记得清理不用的。" -f $snaps.Count)
        Write-Host ("    清理某一个:Remove-VMSnapshot -VMName '{0}' -Name '<名字>'" -f $VMName) -ForegroundColor DarkGray
    }
    Write-Host ''
    exit 0
}

# ---------- -Restore ----------
if ($Restore) {
    Write-Head ("回滚到基线检查点:{0}" -f $Name)
    $snap = Get-VMSnapshot -VMName $VMName -Name $Name -ErrorAction SilentlyContinue
    if (-not $snap) {
        Write-Host ("  ❌ 找不到名为「{0}」的检查点。" -f $Name) -ForegroundColor Red
        Write-Host ("     先用 -List 看看有哪些,或先打一个基线。") -ForegroundColor Yellow
        exit 3
    }
    Write-Info ("检查点创建于:{0}(类型 {1})" -f $snap.CreationTime, $snap.SnapshotType)

    $plan = @()
    if ($vm.State -ne 'Off') {
        $plan += ("Stop-VM -Name '{0}' -TurnOff -Force   # 直接断电,回滚反正要丢弃当前状态" -f $VMName)
    }
    $plan += ("Restore-VMSnapshot -VMName '{0}' -Name '{1}' -Confirm:`$false" -f $VMName, $Name)

    Write-Host ''
    Write-Host '  将要执行的命令:' -ForegroundColor Yellow
    foreach ($c in $plan) { Write-Cmd $c }
    Write-Host ''
    Write-Host '  🔴 这会【丢弃虚拟机里基线之后的全部改动】:装过的 QTrade、WSL、日志、文件,全没。' -ForegroundColor Red
    Write-Host '     要留证据请先把日志拷出来(04 脚本有取回日志的命令骨架)。' -ForegroundColor Yellow
    Write-Host '     主机不受任何影响:主机的 WSL、容器、文件一律不动。' -ForegroundColor Gray
    Write-Host ''

    if ($WhatIfOnly) {
        Write-Host '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' -ForegroundColor Cyan
        exit 0
    }

    $answer = Read-Host '  确认回滚请输入大写 YES(其它任何输入都会取消)'
    if ($answer -cne 'YES') {
        Write-Host '  已取消,虚拟机原样未动。' -ForegroundColor Cyan
        exit 0
    }

    if ($vm.State -ne 'Off') {
        Write-Cmd ("Stop-VM -Name '{0}' -TurnOff -Force" -f $VMName)
        Stop-VM -Name $VMName -TurnOff -Force
        Write-Ok '虚拟机已断电'
    }
    Write-Cmd ("Restore-VMSnapshot -VMName '{0}' -Name '{1}' -Confirm:`$false" -f $VMName, $Name)
    Restore-VMSnapshot -VMName $VMName -Name $Name -Confirm:$false
    Write-Ok '已回滚到基线'

    $vm2 = Get-VM -Name $VMName
    Write-Info ("当前状态:{0}" -f $vm2.State)
    Write-Host ''
    Write-Host '  下一步:' -ForegroundColor White
    Write-Host ("    Start-VM -Name '{0}'; vmconnect.exe localhost '{0}'" -f $VMName) -ForegroundColor Gray
    Write-Host '    然后跑 04-把安装包送进虚拟机.ps1 开始下一轮测试。' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  说明:Hyper-V 回滚后,基线之后的旧检查点仍留在列表里(成为另一条分支),' -ForegroundColor DarkGray
    Write-Host '        不影响使用;想清理用 Remove-VMSnapshot。' -ForegroundColor DarkGray
    Write-Host ''
    exit 0
}

# ---------- 默认:创建基线 ----------
Write-Head ("创建基线检查点:{0}" -f $Name)

$exists = Get-VMSnapshot -VMName $VMName -Name $Name -ErrorAction SilentlyContinue
if ($exists) {
    Write-Warn2 ("同名检查点已存在(创建于 {0})—— 不重复创建。" -f $exists.CreationTime)
    Write-Host ''
    Write-Host '  如果你确实想用当前状态【替换】旧基线(旧基线会被删掉,不可恢复):' -ForegroundColor Yellow
    Write-Host ("     Remove-VMSnapshot -VMName '{0}' -Name '{1}'" -f $VMName, $Name) -ForegroundColor Gray
    Write-Host ("     .\03-快照与回滚.ps1 -VMName '{0}' -Name '{1}'" -f $VMName, $Name) -ForegroundColor Gray
    Write-Host ''
    exit 0
}

if ($vm.State -ne 'Off') {
    Write-Warn2 ("虚拟机当前是「{0}」状态。" -f $vm.State)
    Write-Info '建议在【关机状态】打基线:这样每次回滚都回到一个干净的关机态,最省心。'
    Write-Info '在运行状态打也可以(Standard 检查点会连内存一起存),但回滚后会恢复成当时的运行态。'
    Write-Host ''
    Write-Host ("  想先关机:Stop-VM -Name '{0}'(在虚拟机里正常关机也行)" -f $VMName) -ForegroundColor Gray
    Write-Host ''
}

Write-Host '  将要执行的命令:' -ForegroundColor Yellow
Write-Cmd ("Checkpoint-VM -Name '{0}' -SnapshotName '{1}'" -f $VMName, $Name)
Write-Host ''
Write-Host '  会改动什么:' -ForegroundColor Yellow
Write-Host ("    · 虚拟机多一个检查点,磁盘上多一个差异盘 avhdx(随后续写入慢慢增大)") -ForegroundColor Gray
Write-Host ("    · 主机的 WSL、容器、网络、注册表一律不动") -ForegroundColor Gray
Write-Host '  撤销办法:' -ForegroundColor Yellow
Write-Host ("    Remove-VMSnapshot -VMName '{0}' -Name '{1}'" -f $VMName, $Name) -ForegroundColor Gray
Write-Host ''
Write-Host '  ✅ 打基线的正确时机:Windows 刚装完、Hyper-V 来宾服务正常、' -ForegroundColor Green
Write-Host '     但【还没装任何 QTrade 相关的东西】(没装 WSL、没跑安装器)。' -ForegroundColor Green
Write-Host ''

if ($WhatIfOnly) {
    Write-Host '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' -ForegroundColor Cyan
    exit 0
}

$answer = Read-Host '  确认打基线请输入大写 YES(其它任何输入都会取消)'
if ($answer -cne 'YES') {
    Write-Host '  已取消,虚拟机原样未动。' -ForegroundColor Cyan
    exit 0
}

Write-Cmd ("Checkpoint-VM -Name '{0}' -SnapshotName '{1}'" -f $VMName, $Name)
Checkpoint-VM -Name $VMName -SnapshotName $Name
Write-Ok '基线检查点已创建'

$snap = Get-VMSnapshot -VMName $VMName -Name $Name
Write-Info ("名称:{0}" -f $snap.Name)
Write-Info ("创建于:{0}" -f $snap.CreationTime)
Write-Info ("类型:{0}" -f $snap.SnapshotType)
Write-Host ''
Write-Host '  以后每轮安装测试之前,跑这一句回到干净起点:' -ForegroundColor White
Write-Host ("     .\03-快照与回滚.ps1 -Restore") -ForegroundColor Gray
Write-Host ''
exit 0
