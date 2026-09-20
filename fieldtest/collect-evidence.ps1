# QTrade 真机验收 —— 一键只读取证
#
# 用途:在目标机上出问题时,一条命令把「能证明现场」的东西收齐,打成 zip 放桌面。
# 规格出处:
#   - 收什么:docs/03 §5.3 诊断包清单、§2.15 检测命令速查、§2.14 卸载还原清单(取证要能对账)
#   - 不收什么:docs/03 §5.3「不含」+ §6 红线 2;排除正则逐条对齐
#     installer/engine/modules/QTrade.Diag.psm1 的 $QtDiagExcludePatterns
#
# 🔴 本脚本只读:不改注册表、不改服务、不改防火墙、不改 .wslconfig、
#    不执行 wsl --shutdown、不启停任何容器。全部命令都是查询类。
#
# 跑法(建议管理员身份,否则部分项会记「权限不足」但不中断):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\collect-evidence.ps1
#requires -Version 5.1

[CmdletBinding()]
param(
    # zip 落在哪(默认当前用户桌面)
    [string] $OutDir = [Environment]::GetFolderPath('Desktop'),
    # 安装根(默认 %ProgramData%\QTrade,docs/03 §4 写死)
    [string] $QTradeRoot = (Join-Path $env:ProgramData 'QTrade'),
    # 事件日志往前捞几小时
    [int] $EventHours = 24
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# ── 🔴 排除清单(硬约束,不是「尽量」)──────────────────────────────
# 逐条对齐 QTrade.Diag.psm1 $QtDiagExcludePatterns:
#   vault blobs 与熵文件 / 微信数据 / Agent→WinAgent 令牌 / 密钥材料 / 微信备份目录
$script:ExcludePatterns = @(
    '\\winagent\\vault\\'
    'entropy\.bin$'
    '\\xwechat_files\\'
    '\\Tencent\\xwechat\\'
    'winagent\.token$'
    '\.wslconfig\.bak-.*\.key$'
    '\\QTrade-WeChat-Backup\\'
    # 本脚本自加的保险绳(同属红线 2 语义:凭据/密钥材料一律不进包)
    '\.pfx$'
    '\.pem$'
    '\.key$'
    'token'
    'secret'
    'password'
)

function Test-Excluded {
    param([Parameter(Mandatory)][string] $Path)
    foreach ($pat in $script:ExcludePatterns) {
        if ($Path -match $pat) { return $true }
    }
    return $false
}

$ts      = Get-Date -Format 'yyyyMMdd-HHmmss'
$staging = Join-Path ([IO.Path]::GetTempPath()) ("qtrade-evidence-" + $ts)
$null    = New-Item -ItemType Directory -Path $staging -Force
$excluded = New-Object System.Collections.Generic.List[string]
$notes    = New-Object System.Collections.Generic.List[string]

function Write-Section {
    param([Parameter(Mandatory)][string] $Name,
          [Parameter(Mandatory)][scriptblock] $Body)
    $dst = Join-Path $staging ($Name + '.txt')
    Write-Host ("  收集 " + $Name + " ...") -NoNewline
    try {
        $out = & $Body 2>&1 | Out-String -Width 4096
        if ($null -eq $out) { $out = '' }
        [IO.File]::WriteAllText($dst, $out, (New-Object Text.UTF8Encoding($true)))
        Write-Host ' OK' -ForegroundColor Green
    } catch {
        $msg = "采集失败: " + $_.Exception.Message
        [IO.File]::WriteAllText($dst, $msg, (New-Object Text.UTF8Encoding($true)))
        $notes.Add(($Name + ': ' + $_.Exception.Message))
        Write-Host ' 失败(已记录)' -ForegroundColor Yellow
    }
}

function Copy-Evidence {
    param([Parameter(Mandatory)][string] $Src,
          [Parameter(Mandatory)][string] $RelDst)
    if (-not (Test-Path -LiteralPath $Src)) { $notes.Add(("不存在,跳过: " + $Src)); return }
    if (Test-Excluded -Path $Src) { $excluded.Add($Src); return }
    $dst = Join-Path $staging $RelDst
    $dir = Split-Path $dst -Parent
    if (-not (Test-Path -LiteralPath $dir)) { $null = New-Item -ItemType Directory -Path $dir -Force }
    try { Copy-Item -LiteralPath $Src -Destination $dst -Force } catch { $notes.Add(("拷贝失败 " + $Src + ": " + $_.Exception.Message)) }
}

Write-Host ''
Write-Host '=== QTrade 真机验收 · 只读取证 ===' -ForegroundColor Cyan
Write-Host ("暂存目录: " + $staging)
Write-Host ''

# ── 1. 系统信息(docs/03 §2.4.1 / §2.15)─────────────────────────────
Write-Section 'system-os' {
    Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' |
        Select-Object CurrentBuildNumber, UBR, DisplayVersion, EditionID, ProductName | Format-List
    'PROCESSOR_ARCHITECTURE = ' + $env:PROCESSOR_ARCHITECTURE
    'Is64BitOperatingSystem = ' + [Environment]::Is64BitOperatingSystem
    'ComputerName           = ' + $env:COMPUTERNAME
    'CurrentUser            = ' + [Security.Principal.WindowsIdentity]::GetCurrent().Name
    'IsAdmin                = ' + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')
}

Write-Section 'system-hardware' {
    Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory, HypervisorPresent, Manufacturer, Model | Format-List
    Get-CimInstance Win32_Processor | Select-Object Name, VirtualizationFirmwareEnabled | Format-List
    Get-PSDrive -PSProvider FileSystem |
        Select-Object Name, @{n='UsedGB';e={[math]::Round($_.Used/1GB,2)}}, @{n='FreeGB';e={[math]::Round($_.Free/1GB,2)}} |
        Format-Table -AutoSize
}

Write-Section 'security-products' {
    '--- AntiVirusProduct(SecurityCenter2)---'
    Get-CimInstance -Namespace root\SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
        Select-Object displayName, productState, pathToSignedProductExe | Format-List
    '--- 可能的 EDR/杀软服务 ---'
    Get-Service -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'Defender|CrowdStrike|SentinelOne|Carbon|Cylance|Sophos|McAfee|Symantec|Trend|Kaspersky|ESET|奇安信|火绒|360|深信服' } |
        Select-Object Name, DisplayName, Status | Format-Table -AutoSize
}

# ── 2. Windows 功能与策略(docs/03 §2.4.2 / §2.5.1)──────────────────
Write-Section 'windows-features' {
    Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform, Microsoft-Windows-Subsystem-Linux -ErrorAction SilentlyContinue |
        Select-Object FeatureName, State | Format-Table -AutoSize
}

Write-Section 'policy-keys' {
    '--- HKLM\SOFTWARE\Policies\Microsoft\Windows\WSL(AllowCustomKernelUserSetting 等)---'
    Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WSL' -ErrorAction SilentlyContinue | Format-List
    '--- WindowsUpdate\AU(UseWUServer / FOD 载荷)---'
    Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction SilentlyContinue | Format-List
    '--- EnableLUA ---'
    Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name EnableLUA, EnableInstallerDetection -ErrorAction SilentlyContinue | Format-List
}

# ── 3. WSL 现状(docs/03 §2.4.4 / §2.15;wsl.exe 输出为 UTF-16,统一设 WSL_UTF8=1)──
Write-Section 'wsl-state' {
    $env:WSL_UTF8 = '1'
    '--- wsl --version ---'
    & wsl.exe --version 2>&1
    '--- wsl --status ---'
    & wsl.exe --status 2>&1
    '--- wsl -l -v ---'
    & wsl.exe --list --verbose 2>&1
    '--- wsl -l --running ---'
    & wsl.exe --list --running 2>&1
    '--- 注册表 Lxss(发行版登记)---'
    Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ItemProperty $_.PSPath | Select-Object DistributionName, Version, BasePath, Flags } | Format-List
}

Write-Section 'wsl-kernel-check' {
    $env:WSL_UTF8 = '1'
    '--- qtrade 发行版 uname -r(应与 manifest version 逐字相等)---'
    & wsl.exe -d qtrade --exec uname -r 2>&1
    '--- binder 判据(只读探测)---'
    & wsl.exe -d qtrade --user root --exec sh -c "grep -w binder /proc/filesystems; zcat /proc/config.gz | grep -c ^CONFIG_ANDROID_BINDER_IPC=y" 2>&1
    '--- core_pattern(L2 方案 D 应为非管道的文件模式)---'
    & wsl.exe -d qtrade --exec cat /proc/sys/kernel/core_pattern 2>&1
}

# .wslconfig 正本(内含用户配置,不含密钥;docs/03 §5.3 明列要收)
Copy-Evidence -Src (Join-Path $env:USERPROFILE '.wslconfig') -RelDst 'wslconfig\_current.wslconfig'
Write-Section 'wslconfig-hash' {
    $p = Join-Path $env:USERPROFILE '.wslconfig'
    if (Test-Path -LiteralPath $p) {
        (Get-FileHash -Algorithm SHA256 -LiteralPath $p) | Format-List
        '--- 前 3 字节(EF BB BF 表示有 BOM,规格要求无 BOM)---'
        (([IO.File]::ReadAllBytes($p))[0..2] -join ',')
    } else { 'NONE(文件不存在)' }
}

# .wslconfig 备份(排除 *.key 变体)
if (Test-Path -LiteralPath (Join-Path $QTradeRoot 'wsl')) {
    Get-ChildItem (Join-Path $QTradeRoot 'wsl') -Filter '.wslconfig.bak-*' -File -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Evidence -Src $_.FullName -RelDst ('wslconfig\backups\' + $_.Name) }
}

# ── 4. 安装状态与日志(docs/03 §3.1 / §5.3)──────────────────────────
Copy-Evidence -Src (Join-Path $QTradeRoot 'install\install_state.json') -RelDst 'qtrade\install_state.json'
Copy-Evidence -Src (Join-Path $QTradeRoot 'install\manifest.json')      -RelDst 'qtrade\manifest.json'
Copy-Evidence -Src (Join-Path $QTradeRoot 'kernel\current.json')        -RelDst 'qtrade\kernel-current.json'
Copy-Evidence -Src (Join-Path $QTradeRoot 'logs\last-exit-code.txt')    -RelDst 'qtrade\last-exit-code.txt'
Copy-Evidence -Src (Join-Path $QTradeRoot 'logs\install-summary.txt')   -RelDst 'qtrade\install-summary.txt'

$logDir = Join-Path $QTradeRoot 'logs'
if (Test-Path -LiteralPath $logDir) {
    Get-ChildItem $logDir -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.log', '.txt') } |
        ForEach-Object { Copy-Evidence -Src $_.FullName -RelDst ('qtrade\logs\' + $_.Name) }
} else {
    $notes.Add('安装日志目录不存在: ' + $logDir)
}

Write-Section 'qtrade-tree' {
    if (Test-Path -LiteralPath $QTradeRoot) {
        '--- 安装根一级目录 ---'
        Get-ChildItem $QTradeRoot -Force -ErrorAction SilentlyContinue |
            Select-Object Mode, LastWriteTime, Length, Name | Format-Table -AutoSize
        '--- 安装根 ACL(退出码 31 排障用;只打印,不修改)---'
        (& icacls.exe $QTradeRoot 2>&1)
        '--- 内核文件 ACL ---'
        Get-ChildItem (Join-Path $QTradeRoot 'kernel') -Filter 'bzImage-*' -File -ErrorAction SilentlyContinue |
            ForEach-Object { & icacls.exe $_.FullName 2>&1 }
    } else { '安装根不存在: ' + $QTradeRoot }
}

# ── 5. 服务 / 计划任务 / 进程(docs/03 §2.8.2 / §2.8.3)───────────────
Write-Section 'service-and-task' {
    '--- 服务 QTradeWinAgent ---'
    Get-Service QTradeWinAgent -ErrorAction SilentlyContinue | Select-Object Name, Status, StartType | Format-List
    '--- sc qc(启动类型 / 账号 / 依赖)---'
    & sc.exe qc QTradeWinAgent 2>&1
    '--- sc qfailure(恢复策略)---'
    & sc.exe qfailure QTradeWinAgent 2>&1
    '--- 计划任务 \QTrade\ ---'
    $t = Get-ScheduledTask -TaskPath '\QTrade\' -ErrorAction SilentlyContinue
    if ($t) {
        $t | Select-Object TaskName, State | Format-Table -AutoSize
        $t | ForEach-Object {
            'TaskName  = ' + $_.TaskName
            'Principal = ' + ($_.Principal | Out-String)
            'Triggers  = ' + ($_.Triggers  | Out-String)
        }
    } else { '无 \QTrade\ 计划任务' }
    '--- 我方进程 ---'
    Get-Process qtrade-winagent-svc, qtrade-winagent-user, qtrade-setup-engine -ErrorAction SilentlyContinue |
        Select-Object Id, ProcessName, Path, StartTime | Format-List
}

# ── 6. 网络:防火墙规则 / WSL 网卡(docs/04 §2.6.3)────────────────────
Write-Section 'firewall-qtrade' {
    $rules = Get-NetFirewallRule -DisplayName 'QTrade-*' -ErrorAction SilentlyContinue
    if ($rules) {
        foreach ($r in $rules) {
            'DisplayName = ' + $r.DisplayName
            'Enabled/Dir/Action/Profile = ' + $r.Enabled + ' / ' + $r.Direction + ' / ' + $r.Action + ' / ' + $r.Profile
            ($r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Select-Object Program | Out-String)
            ($r | Get-NetFirewallPortFilter        -ErrorAction SilentlyContinue | Select-Object Protocol, LocalPort | Out-String)
            ($r | Get-NetFirewallAddressFilter     -ErrorAction SilentlyContinue | Select-Object RemoteAddress | Out-String)
            '---'
        }
    } else { '无 QTrade-* 防火墙规则' }
}

Write-Section 'network-adapters' {
    '--- vEthernet (WSL*) 地址 ---'
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object InterfaceAlias -like 'vEthernet (WSL*' |
        Select-Object InterfaceAlias, IPAddress, PrefixLength | Format-Table -AutoSize
    '--- 全部 Up 网卡 ---'
    Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' |
        Select-Object Name, InterfaceDescription, LinkSpeed | Format-Table -AutoSize
    '--- 系统代理(只读)---'
    Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue |
        Select-Object ProxyEnable, ProxyServer, AutoConfigURL | Format-List
    & netsh.exe winhttp show proxy 2>&1
}

# ── 7. hosts:只取我方带行尾标记的行(docs/04 §2.5.4;用户行不收)─────
Write-Section 'hosts-qtrade-lines' {
    $hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
    if (Test-Path -LiteralPath $hosts) {
        'sha256 = ' + (Get-FileHash -Algorithm SHA256 -LiteralPath $hosts).Hash
        '只读属性 = ' + ((Get-Item -LiteralPath $hosts).IsReadOnly)
        '--- 带 # QTrade-wechat-update-block 行尾标记的行(其余行不收集)---'
        $m = Select-String -LiteralPath $hosts -Pattern 'QTrade-wechat-update-block' -ErrorAction SilentlyContinue
        if ($m) { $m | ForEach-Object { '第 ' + $_.LineNumber + ' 行: ' + $_.Line } } else { '(无我方标记行)' }
    } else { 'hosts 不存在?' }
}

# ── 8. 事件日志(docs/03 §2.15:K1 型失败只在 WSL/Operational 有痕迹)──
Write-Section 'eventlog-wsl' {
    Get-WinEvent -LogName 'Microsoft-Windows-WSL/Operational' -MaxEvents 50 -ErrorAction SilentlyContinue |
        Select-Object TimeCreated, Id, LevelDisplayName, Message | Format-List
}

Write-Section 'eventlog-system-errors' {
    $since = (Get-Date).AddHours(-1 * $EventHours)
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 2, 3; StartTime = $since } -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match 'QTrade|Lxss|WSL|Hyper-V|vmcompute|LxssManager' } |
        Select-Object TimeCreated, Id, ProviderName, LevelDisplayName, Message | Format-List
}

Write-Section 'eventlog-application-errors' {
    $since = (Get-Date).AddHours(-1 * $EventHours)
    Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Level = 2, 3; StartTime = $since } -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match 'QTrade|qtrade-winagent|qtrade-setup' } |
        Select-Object TimeCreated, Id, ProviderName, LevelDisplayName, Message | Format-List
}

# ── 9. 🔴 压包前的排除断言(红线 2 的执行点,不是提醒)────────────────
Write-Host ''
Write-Host '  复核排除清单 ...' -NoNewline
$hits = @()
Get-ChildItem $staging -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    if (Test-Excluded -Path $_.FullName) {
        $hits += $_.FullName
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
    }
}
foreach ($h in $hits) { $excluded.Add($h) }
if ($hits.Count -gt 0) {
    Write-Host (' 剔除 ' + $hits.Count + ' 项') -ForegroundColor Yellow
} else {
    Write-Host ' 干净' -ForegroundColor Green
}

# 说明文件:这个包里有什么、刻意不收什么
$readme = @()
$readme += 'QTrade 真机验收取证包'
$readme += ('生成时间 : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
$readme += ('生成机器 : ' + $env:COMPUTERNAME)
$readme += ('生成账号 : ' + [Security.Principal.WindowsIdentity]::GetCurrent().Name)
$readme += ('安装根   : ' + $QTradeRoot)
$readme += ''
$readme += '本包只读采集,未对本机做任何修改。'
$readme += ''
$readme += '🔴 刻意不收(对齐 docs/03 §5.3「不含」与红线 2):'
$readme += '  - WinAgent vault blobs 与熵文件(winagent\vault\、entropy.bin)'
$readme += '  - Agent 与 WinAgent 之间的令牌(winagent.token)'
$readme += '  - 微信数据(xwechat_files、%APPDATA%\Tencent\xwechat)与微信备份目录'
$readme += '  - 任何密码 / 凭据 / 密钥材料(*.key *.pem *.pfx,以及名字里带 token/secret/password 的文件)'
$readme += '  - 任何消息正文 / 聊天内容'
$readme += '  - hosts 只收我方带行尾标记 # QTrade-wechat-update-block 的行,用户自己的行不收'
$readme += ''
if ($excluded.Count -gt 0) {
    $readme += '本次被排除清单挡下的路径:'
    foreach ($e in $excluded) { $readme += ('  - ' + $e) }
    $readme += ''
}
if ($notes.Count -gt 0) {
    $readme += '采集过程备注(缺项 / 权限不足 / 命令失败):'
    foreach ($n in $notes) { $readme += ('  - ' + $n) }
}
[IO.File]::WriteAllLines((Join-Path $staging 'README.txt'), $readme, (New-Object Text.UTF8Encoding($true)))

# ── 10. 打包到桌面 ───────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $OutDir)) { $null = New-Item -ItemType Directory -Path $OutDir -Force }
$zip = Join-Path $OutDir ('qtrade-evidence-' + $env:COMPUTERNAME + '-' + $ts + '.zip')
try {
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -Force
    Write-Host ''
    Write-Host ('取证包已生成: ' + $zip) -ForegroundColor Green
    Write-Host ('大小: ' + [math]::Round((Get-Item -LiteralPath $zip).Length / 1MB, 2) + ' MB')
} catch {
    Write-Host ''
    Write-Host ('打包失败: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ('未打包的采集结果仍在: ' + $staging) -ForegroundColor Yellow
    exit 1
}

Write-Host ''
Write-Host '🔴 发出去之前自己打开看一眼:不该有密码、令牌、聊天内容、vault 下的任何东西。' -ForegroundColor Yellow
Write-Host ('暂存目录可自行删除: ' + $staging)
exit 0
