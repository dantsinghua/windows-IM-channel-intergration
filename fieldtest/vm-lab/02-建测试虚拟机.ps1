<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 第 2 步:创建测试用的 Windows 11 虚拟机。

.DESCRIPTION
    ⚠️ 本脚本会【改动这台机器】:它会在 D:\HyperV\QTrade-Test\ 下创建一台 Hyper-V 虚拟机
    与一个动态扩展虚拟磁盘。除此之外不碰主机的任何设置。

    前置条件:01 脚本已跑过,并且你已经重启过一次 Windows。

    虚拟机规格(为「在虚拟机里跑 QTrade 安装器」量身定):
      · 第 2 代(UEFI),Windows 11 必需
      · 内存 16 GB【静态】—— 嵌套虚拟化要求关闭动态内存
      · 8 个虚拟处理器
      · 虚拟磁盘 120 GB【动态扩展】(初始只占几 GB,随装随涨)
      · vTPM + 安全启动 —— Windows 11 安装程序会检查这两样
      · 🔴 ExposeVirtualizationExtensions = $true —— 嵌套虚拟化开关,
        这是能在虚拟机里跑 WSL2(进而跑 QTrade)的关键,少了它虚拟机里装不上 WSL2
      · 网络只接 Hyper-V 自带的 Default Switch(NAT)
        —— 不新建外部交换机、不绑定物理网卡、不碰主机 WSL 的网络设置
      · 关闭自动检查点(每次开机自动打快照会拖慢测试、吃磁盘)
      · 开机自动启动动作设为「无」(主机重启时它不会自己起来抢 16 GB 内存)

    幂等:同名虚拟机已存在时,只打印现状、不重复创建、不修改它。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\02-建测试虚拟机.ps1 -WhatIfOnly
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\02-建测试虚拟机.ps1
.EXAMPLE
    # 连同无人值守应答 ISO 一起挂上(应答 ISO 由 02b 脚本生成)
    powershell -NoProfile -ExecutionPolicy Bypass -File .\02-建测试虚拟机.ps1 -AnswerIsoPath 'D:\HyperV\QTrade-Test\autounattend.iso'
#>

[CmdletBinding()]
param(
    [string] $VMName       = 'QTrade-Test-Win11',
    [string] $VmRoot       = 'D:\HyperV\QTrade-Test',
    [string] $IsoPath      = 'D:\Win11_25H2_Chinese_Simplified_x64_v2.iso',
    [string] $AnswerIsoPath = '',
    [int]    $MemoryGB     = 16,
    [int]    $CpuCount     = 8,
    [int]    $DiskGB       = 120,
    [string] $SwitchName   = 'Default Switch',
    [switch] $WhatIfOnly
)

$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'

function Write-Head([string] $Text) {
    Write-Host ''
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
    Write-Host ("  $Text") -ForegroundColor Cyan
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
}
function Write-Cmd([string] $Text) {
    Write-Host ("    > {0}" -f $Text) -ForegroundColor White
}
function Write-Ok([string] $Text)   { Write-Host ("      ✔ {0}" -f $Text) -ForegroundColor Green }
function Write-Warn2([string] $Text) { Write-Host ("      ⚠ {0}" -f $Text) -ForegroundColor Yellow }
function Write-Info([string] $Text) { Write-Host ("      · {0}" -f $Text) -ForegroundColor Gray }

Write-Host ''
Write-Host 'QTrade 测试虚拟机实验室 —— 02 创建测试虚拟机' -ForegroundColor White
Write-Host ("时间:{0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray

# ---------- 前置:管理员 ----------
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

# ---------- 前置:Hyper-V 可用 ----------
Write-Head '前置检查'
if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    Write-Host '  ❌ 找不到 Hyper-V 的 PowerShell 命令(Get-VM)。' -ForegroundColor Red
    Write-Host '     说明 Hyper-V 还没启用,或启用后还没重启过。' -ForegroundColor Yellow
    Write-Host '     请先跑 01-启用HyperV.ps1,然后【自行择机重启一次 Windows】,再回来跑本脚本。' -ForegroundColor Yellow
    Write-Host ''
    exit 2
}
try {
    [void] (Get-VMHost -ErrorAction Stop)
    Write-Ok 'Hyper-V 服务可用'
} catch {
    Write-Host ('  ❌ Hyper-V 服务不可用:{0}' -f $_.Exception.Message) -ForegroundColor Red
    Write-Host '     多半是启用 Hyper-V 后还没重启。请重启后再试。' -ForegroundColor Yellow
    exit 2
}

# ---------- 前置:ISO ----------
if (-not (Test-Path -LiteralPath $IsoPath)) {
    Write-Host ("  ❌ 找不到安装介质 ISO:{0}" -f $IsoPath) -ForegroundColor Red
    Write-Host '     请用 -IsoPath 指定正确路径。' -ForegroundColor Yellow
    exit 3
}
Write-Ok ("安装介质 ISO:{0}({1} GB)" -f $IsoPath, [math]::Round((Get-Item -LiteralPath $IsoPath).Length / 1GB, 2))

if ($AnswerIsoPath -and -not (Test-Path -LiteralPath $AnswerIsoPath)) {
    Write-Host ("  ❌ 指定了应答 ISO 但文件不存在:{0}" -f $AnswerIsoPath) -ForegroundColor Red
    Write-Host '     先跑 02b-制作应答ISO.ps1 生成它,或者去掉 -AnswerIsoPath 参数手动装系统。' -ForegroundColor Yellow
    exit 3
}
if ($AnswerIsoPath) { Write-Ok ("应答 ISO:{0}" -f $AnswerIsoPath) }

# ---------- 前置:虚拟交换机 ----------
$sw = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
if (-not $sw) {
    Write-Host ("  ❌ 找不到虚拟交换机「{0}」。" -f $SwitchName) -ForegroundColor Red
    Write-Host '     本机现有的交换机:' -ForegroundColor Yellow
    Get-VMSwitch -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host ("       - {0}(类型 {1})" -f $_.Name, $_.SwitchType) -ForegroundColor Gray
    }
    Write-Host ''
    Write-Host '     🔴 本脚本【刻意不会】替你新建交换机 —— 新建外部交换机会绑定物理网卡、' -ForegroundColor Yellow
    Write-Host '        重置主机网络,这台机器有过网络特性引发全系统卡死的历史,不冒这个险。' -ForegroundColor Yellow
    Write-Host '        Default Switch 通常在 Hyper-V 启用并重启后自动出现;若确实没有,请先确认已重启。' -ForegroundColor Yellow
    exit 4
}
Write-Ok ("虚拟交换机:{0}(类型 {1},NAT,不绑定物理网卡)" -f $sw.Name, $sw.SwitchType)

# ---------- 幂等:同名虚拟机 ----------
$existing = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Head ("同名虚拟机已存在 —— 只打印现状,不做任何修改")
    Write-Info ("名称        : {0}" -f $existing.Name)
    Write-Info ("状态        : {0}" -f $existing.State)
    Write-Info ("代数        : 第 {0} 代" -f $existing.Generation)
    Write-Info ("内存        : {0} GB(动态内存:{1})" -f [math]::Round($existing.MemoryStartup / 1GB, 1), $existing.DynamicMemoryEnabled)
    Write-Info ("处理器      : {0} 个" -f $existing.ProcessorCount)
    Write-Info ("配置路径    : {0}" -f $existing.Path)
    try {
        $p = Get-VMProcessor -VMName $VMName -ErrorAction Stop
        Write-Info ("嵌套虚拟化  : ExposeVirtualizationExtensions = {0}" -f $p.ExposeVirtualizationExtensions)
        if (-not $p.ExposeVirtualizationExtensions) {
            Write-Warn2 '嵌套虚拟化没开 —— 虚拟机里装不了 WSL2。'
            Write-Warn2 ('修法(需先把虚拟机关机):Set-VMProcessor -VMName ''{0}'' -ExposeVirtualizationExtensions $true' -f $VMName)
        }
    } catch { }
    try {
        Get-VMHardDiskDrive -VMName $VMName -ErrorAction Stop | ForEach-Object {
            Write-Info ("虚拟磁盘    : {0}" -f $_.Path)
        }
        Get-VMDvdDrive -VMName $VMName -ErrorAction Stop | ForEach-Object {
            Write-Info ("光驱        : {0}" -f $(if ($_.Path) { $_.Path } else { '(空)' }))
        }
    } catch { }
    Write-Host ''
    Write-Host '  要重建的话,请先自己确认虚拟机里没有要留的东西,然后手动执行:' -ForegroundColor Yellow
    Write-Host ("     Stop-VM -Name '{0}' -TurnOff; Remove-VM -Name '{0}' -Force" -f $VMName) -ForegroundColor Gray
    Write-Host ("     Remove-Item -Recurse -Force '{0}'   # 这会删掉虚拟磁盘,不可恢复" -f $VmRoot) -ForegroundColor Gray
    Write-Host ''
    exit 0
}

# ---------- 计划 ----------
$vhdPath = Join-Path $VmRoot ("{0}.vhdx" -f $VMName)
$plan = @(
    ("New-Item -ItemType Directory -Path '{0}' -Force" -f $VmRoot),
    ("New-VM -Name '{0}' -Generation 2 -MemoryStartupBytes {1}GB -Path '{2}' -NewVHDPath '{3}' -NewVHDSizeBytes {4}GB -SwitchName '{5}'" -f $VMName, $MemoryGB, $VmRoot, $vhdPath, $DiskGB, $SwitchName),
    ("Set-VMMemory -VMName '{0}' -DynamicMemoryEnabled `$false -StartupBytes {1}GB" -f $VMName, $MemoryGB),
    ("Set-VMProcessor -VMName '{0}' -Count {1} -ExposeVirtualizationExtensions `$true" -f $VMName, $CpuCount),
    ("Set-VMKeyProtector -VMName '{0}' -NewLocalKeyProtector" -f $VMName),
    ("Enable-VMTPM -VMName '{0}'" -f $VMName),
    ("Set-VMFirmware -VMName '{0}' -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows" -f $VMName),
    ("Add-VMDvdDrive -VMName '{0}' -Path '{1}'" -f $VMName, $IsoPath)
)
if ($AnswerIsoPath) {
    $plan += ("Add-VMDvdDrive -VMName '{0}' -Path '{1}'   # 无人值守应答 ISO" -f $VMName, $AnswerIsoPath)
}
$plan += @(
    ("Set-VMFirmware -VMName '{0}' -FirstBootDevice <安装 ISO 光驱>" -f $VMName),
    ("Set-VM -Name '{0}' -AutomaticCheckpointsEnabled `$false -CheckpointType Standard -AutomaticStartAction Nothing -AutomaticStopAction ShutDown" -f $VMName),
    ("Set-VMNetworkAdapter -VMName '{0}' -DeviceNaming On" -f $VMName)
)

Write-Head '将要执行的命令(逐条)'
$n = 0
foreach ($c in $plan) { $n++; Write-Host ("  [{0,2}] " -f $n) -NoNewline -ForegroundColor DarkGray; Write-Cmd $c }

Write-Host ''
Write-Host '  会改动主机的什么:' -ForegroundColor Yellow
Write-Host ("    · 在 {0} 下新建虚拟机配置与 {1} GB 动态扩展虚拟磁盘(初始只占几 GB)" -f $VmRoot, $DiskGB) -ForegroundColor Gray
Write-Host ("    · Hyper-V 里多出一台名为 {0} 的虚拟机(未开机前不占内存)" -f $VMName) -ForegroundColor Gray
Write-Host '    · 不新建虚拟交换机、不改主机网络、不碰 WSL、不重启' -ForegroundColor Gray
Write-Host '  撤销办法:' -ForegroundColor Yellow
Write-Host ("    Stop-VM -Name '{0}' -TurnOff; Remove-VM -Name '{0}' -Force; Remove-Item -Recurse -Force '{1}'" -f $VMName, $VmRoot) -ForegroundColor Gray
Write-Host ''
Write-Host ("  🔴 开机后这台虚拟机会占用主机 {0} GB 内存(静态,不还)。" -f $MemoryGB) -ForegroundColor Yellow
Write-Host '     建议开机前先关掉一部分 WSL 容器。' -ForegroundColor Yellow
Write-Host ''

if ($WhatIfOnly) {
    Write-Host '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' -ForegroundColor Cyan
    Write-Host ''
    exit 0
}

$answer = Read-Host '  确认创建请输入大写 YES(其它任何输入都会取消)'
if ($answer -cne 'YES') {
    Write-Host '  已取消,没有做任何改动。' -ForegroundColor Cyan
    exit 0
}

# ---------- 执行 ----------
Write-Head '开始创建'

Write-Cmd ("New-Item -ItemType Directory -Path '{0}' -Force" -f $VmRoot)
New-Item -ItemType Directory -Path $VmRoot -Force | Out-Null
Write-Ok '目录就绪'

Write-Cmd ("New-VM -Name '{0}' -Generation 2 …" -f $VMName)
$vm = New-VM -Name $VMName -Generation 2 `
             -MemoryStartupBytes ($MemoryGB * 1GB) `
             -Path $VmRoot `
             -NewVHDPath $vhdPath `
             -NewVHDSizeBytes ($DiskGB * 1GB) `
             -SwitchName $SwitchName
Write-Ok ("虚拟机已创建;虚拟磁盘 {0}(动态扩展 {1} GB)" -f $vhdPath, $DiskGB)

Write-Cmd ("Set-VMMemory -VMName '{0}' -DynamicMemoryEnabled `$false -StartupBytes {1}GB" -f $VMName, $MemoryGB)
Set-VMMemory -VMName $VMName -DynamicMemoryEnabled $false -StartupBytes ($MemoryGB * 1GB)
Write-Ok ("内存 {0} GB 静态(嵌套虚拟化要求关闭动态内存)" -f $MemoryGB)

Write-Cmd ("Set-VMProcessor -VMName '{0}' -Count {1} -ExposeVirtualizationExtensions `$true" -f $VMName, $CpuCount)
Set-VMProcessor -VMName $VMName -Count $CpuCount -ExposeVirtualizationExtensions $true
Write-Ok ("{0} 个虚拟处理器,嵌套虚拟化已开启(虚拟机里才跑得了 WSL2)" -f $CpuCount)

# vTPM:家庭版上的 Hyper-V 不保证支持,失败时给出替代方案而不是直接崩
$tpmOk = $false
try {
    Write-Cmd ("Set-VMKeyProtector -VMName '{0}' -NewLocalKeyProtector" -f $VMName)
    Set-VMKeyProtector -VMName $VMName -NewLocalKeyProtector -ErrorAction Stop
    Write-Cmd ("Enable-VMTPM -VMName '{0}'" -f $VMName)
    Enable-VMTPM -VMName $VMName -ErrorAction Stop
    Write-Ok 'vTPM 已启用(Windows 11 安装程序要检查它)'
    $tpmOk = $true
} catch {
    Write-Warn2 ("vTPM 启用失败:{0}" -f $_.Exception.Message)
    Write-Warn2 '这在家庭版上可能发生。虚拟机仍可创建,但 Win11 安装程序会报「这台电脑无法运行 Windows 11」。'
    Write-Warn2 '替代办法(安装界面出现后按 Shift+F10 打开命令行):'
    Write-Warn2 '  regedit → HKEY_LOCAL_MACHINE\SYSTEM\Setup 下新建项 LabConfig,'
    Write-Warn2 '  在其中建 DWORD:BypassTPMCheck=1、BypassSecureBootCheck=1、BypassRAMCheck=1,关掉 regedit 继续安装。'
}

try {
    Write-Cmd ("Set-VMFirmware -VMName '{0}' -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows" -f $VMName)
    Set-VMFirmware -VMName $VMName -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows -ErrorAction Stop
    Write-Ok '安全启动已开启(模板 MicrosoftWindows)'
} catch {
    Write-Warn2 ("安全启动设置失败:{0}" -f $_.Exception.Message)
}

Write-Cmd ("Add-VMDvdDrive -VMName '{0}' -Path '{1}'" -f $VMName, $IsoPath)
Add-VMDvdDrive -VMName $VMName -Path $IsoPath
Write-Ok '安装 ISO 已挂到光驱'

if ($AnswerIsoPath) {
    Write-Cmd ("Add-VMDvdDrive -VMName '{0}' -Path '{1}'" -f $VMName, $AnswerIsoPath)
    Add-VMDvdDrive -VMName $VMName -Path $AnswerIsoPath
    Write-Ok '无人值守应答 ISO 已挂到第二个光驱'
}

# 首启动项 = 装着安装 ISO 的那个光驱
$installDvd = Get-VMDvdDrive -VMName $VMName | Where-Object { $_.Path -eq $IsoPath } | Select-Object -First 1
Write-Cmd ("Set-VMFirmware -VMName '{0}' -FirstBootDevice <安装 ISO 光驱>" -f $VMName)
Set-VMFirmware -VMName $VMName -FirstBootDevice $installDvd
Write-Ok '光驱已设为首启动项'

Write-Cmd ("Set-VM -Name '{0}' -AutomaticCheckpointsEnabled `$false -CheckpointType Standard -AutomaticStartAction Nothing -AutomaticStopAction ShutDown" -f $VMName)
Set-VM -Name $VMName -AutomaticCheckpointsEnabled $false -CheckpointType Standard `
       -AutomaticStartAction Nothing -AutomaticStopAction ShutDown
Write-Ok '自动检查点已关闭;检查点类型 Standard(保存内存状态,回滚最彻底)'
Write-Ok '主机重启时该虚拟机不会自动启动(不抢内存)'

try {
    Write-Cmd ("Set-VMNetworkAdapter -VMName '{0}' -DeviceNaming On" -f $VMName)
    Set-VMNetworkAdapter -VMName $VMName -DeviceNaming On -ErrorAction Stop
    Write-Ok '网卡设备命名已开启(便于虚拟机里识别网卡)'
} catch {
    Write-Warn2 ("网卡设备命名设置失败(不影响使用):{0}" -f $_.Exception.Message)
}

# ---------- 结果 ----------
Write-Head '创建完成 —— 当前配置'
$vm = Get-VM -Name $VMName
$proc = Get-VMProcessor -VMName $VMName
Write-Info ("名称          : {0}" -f $vm.Name)
Write-Info ("代数          : 第 {0} 代(UEFI)" -f $vm.Generation)
Write-Info ("内存          : {0} GB,动态内存 = {1}" -f [math]::Round($vm.MemoryStartup / 1GB, 1), $vm.DynamicMemoryEnabled)
Write-Info ("处理器        : {0} 个" -f $vm.ProcessorCount)
Write-Info ("嵌套虚拟化    : {0}" -f $proc.ExposeVirtualizationExtensions)
Write-Info ("vTPM          : {0}" -f $(if ($tpmOk) { '已启用' } else { '未启用(见上面的替代办法)' }))
Write-Info ("虚拟磁盘      : {0}" -f $vhdPath)
Write-Info ("网络          : {0}" -f $SwitchName)
Write-Info ("自动检查点    : {0}" -f $vm.AutomaticCheckpointsEnabled)

Write-Host ''
Write-Host '  下一步:' -ForegroundColor White
Write-Host ("    1) 开机并连上控制台: Start-VM -Name '{0}'; vmconnect.exe localhost '{0}'" -f $VMName) -ForegroundColor Gray
Write-Host '       (开机后屏幕提示 Press any key to boot from CD 时,必须在几秒内按一下键盘)' -ForegroundColor DarkGray
Write-Host '    2) 装完 Windows 后,跑 03-快照与回滚.ps1 打基线检查点 clean-baseline' -ForegroundColor Gray
Write-Host '    3) 再跑 04-把安装包送进虚拟机.ps1 把 QTrade 安装器送进去' -ForegroundColor Gray
Write-Host ''
exit 0
