<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 第 0 步:启用 Hyper-V 之前的只读体检。

.DESCRIPTION
    本脚本【只读】。它不启用任何 Windows 功能、不建虚拟机、不改注册表、不重启,
    只把「这台机器能不能按计划跑测试虚拟机」相关的事实查出来并打印。
    可以在任何时候重复运行,运行多少次都不会改动这台机器。

    建议以管理员身份运行:非管理员也能跑,但功能状态只能走 WMI 降级查询(精度略低)。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\00-只读体检.ps1
#>

[CmdletBinding()]
param(
    # 安装介质 ISO 路径(建 VM 时挂载用),可按需改
    [string] $IsoPath = 'D:\Win11_25H2_Chinese_Simplified_x64_v2.iso',
    # 虚拟机将落盘的目录(本步只检查余量与是否已存在,不创建)
    [string] $VmRoot  = 'D:\HyperV\QTrade-Test'
)

$ErrorActionPreference = 'Continue'

# ---------- 输出辅助 ----------
function Write-Head([string] $Text) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
    Write-Host ("  $Text") -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
}
function Write-Item([string] $Name, [string] $Value, [string] $Level = 'info') {
    $color = switch ($Level) {
        'ok'   { 'Green' }
        'warn' { 'Yellow' }
        'bad'  { 'Red' }
        default { 'Gray' }
    }
    $pad = $Name.PadRight(26)
    Write-Host "  $pad : " -NoNewline -ForegroundColor DarkGray
    Write-Host $Value -ForegroundColor $color
}
function Write-Note([string] $Text) {
    Write-Host "      ↳ $Text" -ForegroundColor DarkGray
}

# 汇总结论收集器
$script:Blockers = New-Object System.Collections.ArrayList
$script:Warnings = New-Object System.Collections.ArrayList
function Add-Blocker([string] $t) { [void]$script:Blockers.Add($t) }
function Add-Warning2([string] $t) { [void]$script:Warnings.Add($t) }

Write-Host ''
Write-Host 'QTrade 测试虚拟机实验室 —— 00 只读体检' -ForegroundColor White
Write-Host ("时间:{0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray
Write-Host '本脚本只查不改:不启用功能、不建虚拟机、不重启。' -ForegroundColor DarkGray

# ---------- 是否管理员 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Head '0. 运行身份'
if ($isAdmin) {
    Write-Item '管理员权限' '是(功能状态走 DISM,结果最准)' 'ok'
} else {
    Write-Item '管理员权限' '否(功能状态降级用 WMI 查询)' 'warn'
    Write-Note '想拿最准的结果,请右键 PowerShell →「以管理员身份运行」后重跑本脚本。'
}

# ---------- 1. 系统版本 ----------
Write-Head '1. 系统版本与架构'
$cv = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$build = 0
try {
    $k = Get-ItemProperty -Path $cv -ErrorAction Stop
    $build = [int] $k.CurrentBuildNumber
    Write-Item '产品名'   ("{0}" -f $k.ProductName)
    Write-Note 'ProductName 在 Windows 11 上仍写着「Windows 10 …」是微软的已知行为,以下面的版本号为准。'
    Write-Item '版本号'   ("{0}(build {1}.{2})" -f $k.DisplayVersion, $k.CurrentBuildNumber, $k.UBR)
    Write-Item 'EditionID' ("{0}" -f $k.EditionID)
    # Core=家庭版 / CoreSingleLanguage=家庭单语言版 / CoreCountrySpecific=家庭中文版(中国特供)
    if ($k.EditionID -like 'Core*') {
        Write-Note '家庭版系列:Hyper-V 不在「启用或关闭 Windows 功能」列表里,需要用 01 脚本逐个 DISM /Add-Package 补上组件包。'
    }
} catch {
    Write-Item '系统版本' ("读取失败:{0}" -f $_.Exception.Message) 'bad'
    Add-Blocker '读不到系统版本注册表键'
}
Write-Item '架构' ("{0}" -f $env:PROCESSOR_ARCHITECTURE) $(if ($env:PROCESSOR_ARCHITECTURE -eq 'AMD64') { 'ok' } else { 'bad' })
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { Add-Blocker '非 x64 架构,本方案不适用' }
if ($build -gt 0 -and $build -lt 19041) {
    Add-Blocker ("构建号 {0} 过低,Hyper-V + WSL2 嵌套方案要求 19041+" -f $build)
}

# ---------- 2. Hyper-V 功能当前状态 ----------
Write-Head '2. Hyper-V / 虚拟化相关 Windows 功能的当前状态'
$featureNames = @(
    'Microsoft-Hyper-V-All',
    'Microsoft-Hyper-V',
    'Microsoft-Hyper-V-Hypervisor',
    'Microsoft-Hyper-V-Services',
    'Microsoft-Hyper-V-Management-PowerShell',
    'Microsoft-Hyper-V-Management-Clients',
    'Microsoft-Hyper-V-Tools-All',
    'HypervisorPlatform',
    'VirtualMachinePlatform',
    'Microsoft-Windows-Subsystem-Linux'
)

$featureState = @{}
if ($isAdmin) {
    foreach ($fn in $featureNames) {
        try {
            $f = Get-WindowsOptionalFeature -Online -FeatureName $fn -ErrorAction Stop
            $featureState[$fn] = [string] $f.State
        } catch {
            $featureState[$fn] = 'NotPresent(查询报错:该功能名在本 SKU 上不存在)'
        }
    }
} else {
    # 降级:Win32_OptionalFeature 普通用户可读。InstallState: 1=已启用 2=已禁用 3=不存在
    $wmiMap = @{}
    try {
        Get-CimInstance -ClassName Win32_OptionalFeature -ErrorAction Stop | ForEach-Object {
            $wmiMap[$_.Name] = $_.InstallState
        }
    } catch {
        Write-Item 'WMI 功能查询' ("失败:{0}" -f $_.Exception.Message) 'warn'
    }
    foreach ($fn in $featureNames) {
        if ($wmiMap.ContainsKey($fn)) {
            $featureState[$fn] = switch ([int] $wmiMap[$fn]) {
                1 { 'Enabled' }
                2 { 'Disabled' }
                3 { 'Absent' }
                default { ("InstallState={0}" -f $wmiMap[$fn]) }
            }
        } else {
            $featureState[$fn] = 'NotListed(WMI 未列出 = 本 SKU 上不可见,家庭版的 Hyper-V 正是这种)'
        }
    }
}

foreach ($fn in $featureNames) {
    $st = $featureState[$fn]
    $lvl = if ($st -eq 'Enabled') { 'ok' } elseif ($st -like 'Disabled*') { 'warn' } else { 'info' }
    Write-Item $fn $st $lvl
}

$hvAllState = [string] $featureState['Microsoft-Hyper-V-All']
if ($hvAllState -eq 'Enabled') {
    Write-Note 'Hyper-V 已经启用 —— 01 脚本可以跳过,直接做 02 建虚拟机。'
} else {
    Write-Note 'Hyper-V 尚未启用 —— 需要先跑 01 脚本(会要求重启一次,时机由你定)。'
}

# ---------- 3. 组件包数量 ----------
Write-Head '3. Hyper-V 组件包(.mum)清点'
$pkgDir = Join-Path $env:SystemRoot 'servicing\Packages'
try {
    $mums = @(Get-ChildItem -Path $pkgDir -Filter '*Hyper-V*.mum' -File -ErrorAction Stop)
    Write-Item '组件包目录' $pkgDir
    Write-Item '*Hyper-V*.mum 数量' ("{0} 个" -f $mums.Count) $(if ($mums.Count -gt 0) { 'ok' } else { 'bad' })
    if ($mums.Count -eq 0) {
        Add-Blocker '本机没有 Hyper-V 的 .mum 组件包,家庭版 DISM 启用路径走不通'
    } else {
        Write-Note '家庭版就是靠这些包逐个 /Add-Package 后再 /Enable-Feature 的(见 01 脚本)。'
        $sample = $mums | Select-Object -First 3 -ExpandProperty Name
        Write-Note ("示例:{0} …" -f ($sample -join ' / '))
    }
} catch {
    Write-Item 'Hyper-V 组件包' ("枚举失败:{0}" -f $_.Exception.Message) 'bad'
    Add-Blocker '无法枚举 servicing\Packages 目录'
}

# ---------- 4. 固件虚拟化 ----------
Write-Head '4. CPU 与固件虚拟化'
$cs = $null; $cpu = $null
try { $cs  = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop } catch { }
try { $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1 } catch { }

if ($cpu) {
    Write-Item 'CPU' ("{0}" -f $cpu.Name.Trim())
    Write-Item '逻辑处理器' ("{0}(物理核 {1})" -f $cs.NumberOfLogicalProcessors, $cpu.NumberOfCores)
    Write-Item 'VirtualizationFirmwareEnabled' ("{0}" -f $cpu.VirtualizationFirmwareEnabled)
}
if ($cs) {
    Write-Item 'HypervisorPresent' ("{0}" -f $cs.HypervisorPresent) $(if ($cs.HypervisorPresent) { 'ok' } else { 'info' })
    $memGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
    Write-Item '物理内存' ("{0} GB" -f $memGB) $(if ($memGB -ge 32) { 'ok' } elseif ($memGB -ge 24) { 'warn' } else { 'bad' })
    if ($memGB -lt 24) { Add-Blocker ("物理内存只有 {0} GB,给虚拟机分 16 GB 后主机会很紧张" -f $memGB) }
    elseif ($memGB -lt 32) { Add-Warning2 ("物理内存 {0} GB,16 GB 静态分给虚拟机时请先关掉部分 WSL 容器" -f $memGB) }
}
Write-Note '重要:主机上一旦跑着 Hyper-V 监控程序(WSL2 也是),VirtualizationFirmwareEnabled 常年报 False —— 这不代表 BIOS 里没开 VT-x。'
Write-Note '本机的判据以「WSL2 现在跑得起来」为准(见第 7 节):WSL2 能跑 = 固件虚拟化必然是开的。'

# ---------- 5. 磁盘余量 ----------
Write-Head '5. 磁盘余量'
foreach ($letter in @('C', 'D')) {
    try {
        $d = Get-PSDrive -Name $letter -PSProvider FileSystem -ErrorAction Stop
        $freeGB = [math]::Round($d.Free / 1GB, 1)
        $lvl = if ($freeGB -ge 200) { 'ok' } elseif ($freeGB -ge 150) { 'warn' } else { 'bad' }
        Write-Item ("{0}: 可用" -f $letter) ("{0} GB" -f $freeGB) $lvl
    } catch {
        Write-Item ("{0}: 可用" -f $letter) '该盘不存在' 'info'
    }
}
Write-Note '虚拟机规划:动态扩展 120 GB 虚拟磁盘(初始仅几 GB,随装随涨)+ 安装包 2.26 GB 拷贝副本。'
Write-Note ("虚拟机落盘目录计划为:{0}" -f $VmRoot)
if (Test-Path -LiteralPath $VmRoot) {
    Write-Item '虚拟机目录' '已存在(02 脚本会按幂等处理,不会重复建)' 'warn'
    Add-Warning2 ("{0} 已存在,02 脚本会先打印现状再决定" -f $VmRoot)
} else {
    Write-Item '虚拟机目录' '尚不存在(02 脚本会创建)' 'info'
}

# ---------- 6. 安装介质 ISO ----------
Write-Head '6. Windows 安装介质 ISO'
if (Test-Path -LiteralPath $IsoPath) {
    $iso = Get-Item -LiteralPath $IsoPath
    Write-Item 'ISO 路径' $iso.FullName 'ok'
    Write-Item 'ISO 体积' ("{0} GB" -f [math]::Round($iso.Length / 1GB, 2))
    # 只读探测:能否打开并读出前 16 字节(不挂载、不解压)
    try {
        $fs = [System.IO.File]::Open($iso.FullName, 'Open', 'Read', 'ReadWrite')
        $buf = New-Object byte[] 16
        [void] $fs.Read($buf, 0, 16)
        $fs.Close()
        Write-Item 'ISO 可读性' '可打开并读取(只读探测通过)' 'ok'
    } catch {
        Write-Item 'ISO 可读性' ("打不开:{0}" -f $_.Exception.Message) 'bad'
        Add-Blocker 'ISO 文件无法读取'
    }
} else {
    Write-Item 'ISO 路径' ("不存在:{0}" -f $IsoPath) 'bad'
    Add-Blocker ("找不到安装介质 ISO:{0}" -f $IsoPath)
}

# ---------- 7. WSL 与 Docker 现状(重启影响面) ----------
Write-Head '7. 当前 WSL / Docker 现状(启用 Hyper-V 后要重启,这些都会被中断)'
$prevEnc = [Console]::OutputEncoding
try {
    # wsl.exe 默认输出 UTF-16LE,不切编码会读成夹杂空字符的乱码
    [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
    $wslOut = & wsl.exe -l -v 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Item 'wsl -l -v' '如下' 'ok'
        $wslOut | ForEach-Object { if ("$_".Trim()) { Write-Host ("      $_") -ForegroundColor Gray } }
        $running = @($wslOut | Where-Object { "$_" -match 'Running' })
        Write-Item '运行中的发行版' ("{0} 个" -f $running.Count) $(if ($running.Count -gt 0) { 'warn' } else { 'info' })
    } else {
        Write-Item 'wsl -l -v' ("命令返回 {0},WSL 可能未安装" -f $LASTEXITCODE) 'warn'
    }
} catch {
    Write-Item 'wsl -l -v' ("调用失败:{0}" -f $_.Exception.Message) 'warn'
} finally {
    [Console]::OutputEncoding = $prevEnc
}

try {
    $dockerExe = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($dockerExe) {
        $ids = & docker.exe ps -q 2>$null
        $cnt = @($ids | Where-Object { "$_".Trim() }).Count
        Write-Item 'docker 运行中容器' ("{0} 个" -f $cnt) $(if ($cnt -gt 0) { 'warn' } else { 'info' })
        if ($cnt -gt 0) { Add-Warning2 ("Windows 侧 docker 有 {0} 个容器在跑,重启会全部中断" -f $cnt) }
    } else {
        Write-Item 'docker(Windows 侧)' '未找到 docker.exe(容器可能只跑在 WSL 发行版里)' 'info'
        Write-Note '若容器跑在 WSL 内部,请在对应发行版里用 `docker ps` 自行确认数量。'
    }
} catch {
    Write-Item 'docker' ("查询失败:{0}" -f $_.Exception.Message) 'info'
}

Write-Host ''
Write-Host '  🔴 重启影响提醒:' -ForegroundColor Yellow
Write-Host '     启用 Hyper-V 必须重启一次 Windows。重启会中断 WSL 里所有正在跑的容器与终端。' -ForegroundColor Yellow
Write-Host '     重启时机完全由你决定 —— 01 脚本绝不会自动重启。' -ForegroundColor Yellow

# ---------- 8. WSL 策略键(只读) ----------
Write-Head '8. WSL 企业策略键(只读;影响嵌套环境里能否切自定义内核)'
$wslPolicy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WSL'
if (Test-Path $wslPolicy) {
    $p = Get-ItemProperty -Path $wslPolicy -ErrorAction SilentlyContinue
    foreach ($n in @('AllowCustomKernelUserSetting', 'AllowNestedVirtualization', 'AllowInboxWSL', 'AllowWSL1')) {
        if ($null -ne $p.$n) {
            $lvl = if ($n -eq 'AllowCustomKernelUserSetting' -and [int]$p.$n -eq 0) { 'bad' } else { 'info' }
            Write-Item $n ("{0}" -f $p.$n) $lvl
        }
    }
} else {
    Write-Item 'WSL 策略键' '不存在(= 无企业策略限制,正常)' 'ok'
    Write-Note '装 QTrade 需要自定义内核(AllowCustomKernelUserSetting 不能为 0);当前无策略键 = 不受限。'
}

# ---------- 9. 其它虚拟化软件 ----------
Write-Head '9. 其它虚拟化软件(启用 Hyper-V 后可能受影响)'
$coexist = @(
    @{ Name = 'VMware Workstation'; Svc = 'VMAuthdService' },
    @{ Name = 'VirtualBox';         Svc = 'VBoxSVC' },
    @{ Name = 'Docker Desktop';     Svc = 'com.docker.service' }
)
$foundAny = $false
foreach ($c in $coexist) {
    $svc = Get-Service -Name $c.Svc -ErrorAction SilentlyContinue
    if ($svc) {
        $foundAny = $true
        Write-Item $c.Name ("服务 {0} 存在,状态 {1}" -f $c.Svc, $svc.Status) 'warn'
    }
}
if (-not $foundAny) {
    Write-Item '共存检查' '未发现 VMware / VirtualBox / Docker Desktop 服务' 'ok'
} else {
    Add-Warning2 '机上存在其它虚拟化软件,启用 Hyper-V 后它们可能性能下降或起不了虚拟机'
}

# ---------- 10. Hyper-V 管理工具 / ADK oscdimg ----------
Write-Head '10. 工具可用性'
$hvMod = Get-Module -ListAvailable -Name Hyper-V -ErrorAction SilentlyContinue
if ($hvMod) {
    Write-Item 'Hyper-V PowerShell 模块' ("已存在(版本 {0})" -f ($hvMod | Select-Object -First 1).Version) 'ok'
} else {
    Write-Item 'Hyper-V PowerShell 模块' '不存在(启用 Hyper-V 并重启后才会有)' 'info'
}

$oscdimgCandidates = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe",
    "${env:ProgramFiles}\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe"
)
$oscdimg = $oscdimgCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($oscdimg) {
    Write-Item 'oscdimg.exe(ADK)' $oscdimg 'ok'
    Write-Note '有它就能把 autounattend.xml 打成小 ISO(见 02b 脚本),实现真正的无人值守安装。'
} else {
    Write-Item 'oscdimg.exe(ADK)' '未安装' 'info'
    Write-Note '没有也行:02b 脚本会自动退回「用虚拟软盘 VFD」方案;再不行就手动点完安装向导(约 15 分钟),不必为此去装 ADK。'
}

# ---------- 汇总 ----------
Write-Head '体检结论'
if ($script:Blockers.Count -eq 0) {
    Write-Host '  ✅ 没有发现阻断项。' -ForegroundColor Green
} else {
    Write-Host '  ❌ 发现阻断项,请先解决:' -ForegroundColor Red
    $script:Blockers | ForEach-Object { Write-Host ("     - {0}" -f $_) -ForegroundColor Red }
}
if ($script:Warnings.Count -gt 0) {
    Write-Host '  ⚠️  提醒事项:' -ForegroundColor Yellow
    $script:Warnings | ForEach-Object { Write-Host ("     - {0}" -f $_) -ForegroundColor Yellow }
}

Write-Host ''
Write-Host '  下一步:' -ForegroundColor White
if ($hvAllState -eq 'Enabled') {
    Write-Host '     Hyper-V 已启用 → 直接跑 02-建测试虚拟机.ps1(管理员)。' -ForegroundColor White
} else {
    Write-Host '     跑 01-启用HyperV.ps1(管理员)→ 它会打印命令并等你输入 YES → 跑完后你自己挑时间重启。' -ForegroundColor White
}
Write-Host ''
