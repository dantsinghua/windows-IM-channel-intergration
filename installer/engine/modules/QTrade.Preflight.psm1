# QTrade 安装引擎 —— PRECHECK 预检:环境矩阵与检测方法
# 规格:docs/03 §2.4.1(版本/架构/权限/内存磁盘/数据盘选择)、§2.4.2(虚拟化与策略)、§2.4.3(共存)、
#       §2.4.4(wsl_state)、§2.4.5(.wslconfig 与 kernel_state)、§2.4.6(net_state + 基础探测)、
#       §2.2.2(磁盘门槛 E-18:硬 16 / 建议 20)、§2.15(命令速查)、§7([install] 磁盘键)
# 🔴 预检只读,**不改任何东西**(docs/03 §2.4 首句)。
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Wsl.psm1') -DisableNameChecking

# docs/03 §7 `[install]` 与 §2.4.1 的门槛常量
$script:QtMinWindowsBuild = 19044      # A-3:最低 Windows 10 21H2;19041–19043 不支持(5.15 线已砍)
$script:QtDiskHardGB = 16              # E-18 硬门槛:目标盘可用 < 此值即拒(退出码 26)
$script:QtDiskRecommendGB = 20         # E-18 建议值:16–20 之间只警告 low_headroom
$script:QtSfxDiskRoughGB = 6           # §2.1:SFX 解压前的粗判(退出码 26),暂存改不了盘(P0-12)
$script:QtMinMemoryGB = 8              # 主文档 §3.4 最低档
$script:QtWarnMemoryGB = 12            # 8–12 GB 只警告「只能稳定跑 1 个企点账号」

# docs/03 §2.4.2 的 WSL 企业策略键(同族一并读出记档)
$script:QtWslPolicyKeys = @(
    'AllowCustomKernelUserSetting', 'AllowInboxWSL', 'AllowWSL1',
    'AllowKernelCommandLineUserSetting', 'AllowCustomSystemDistroUserSetting', 'AllowNestedVirtualization'
)
$script:QtWslPolicyPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WSL'

# 基线 §8.5 v1.1 的 policy_reason 四值(C-36)
$script:QtPolicyReasons = @('FEATURE_PAYLOAD_REMOVED', 'WSUS_BLOCKS_FOD', 'CUSTOM_KERNEL_FORBIDDEN', 'APPLOCKER')

# docs/03 §7 `[probe] precheck_targets`(99b ①)
$script:QtPrecheckTargets = @('apk_url', 'mail_pop3', 'mail_imap', 'mail_smtp', 'qidian_msf', 'qq_servers', 'wechat_servers')

function Get-QtPreflightThresholds {
    [CmdletBinding()]
    param()
    return [ordered]@{
        min_windows_build = $script:QtMinWindowsBuild
        disk_hard_gb      = $script:QtDiskHardGB
        disk_recommend_gb = $script:QtDiskRecommendGB
        sfx_disk_rough_gb = $script:QtSfxDiskRoughGB
        min_memory_gb     = $script:QtMinMemoryGB
        warn_memory_gb    = $script:QtWarnMemoryGB
    }
}
function Get-QtPolicyReasons { [CmdletBinding()] param() return , $script:QtPolicyReasons }
function Get-QtPrecheckTargets { [CmdletBinding()] param() return , $script:QtPrecheckTargets }

#region 纯判定函数(单测直接喂事实,不碰真机)
function Test-QtWindowsBuild {
    <#
    .SYNOPSIS
        docs/03 §2.4.1:`< 19044` → `WIN_TOO_OLD`;`≥ 19044` 正常;Win11(≥ 22000)同规则。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][int] $Build)
    if ($Build -lt $script:QtMinWindowsBuild) {
        return [pscustomobject]@{ Ok = $false; Reason = 'WIN_TOO_OLD'; Message = ('需要 Windows 10 21H2({0})或更高,请先升级 Windows(当前 {1})' -f $script:QtMinWindowsBuild, $Build) }
    }
    return [pscustomobject]@{ Ok = $true; Reason = ''; Message = '' }
}

function Test-QtDiskThreshold {
    <#
    .SYNOPSIS
        docs/03 §2.2.2 / §2.4.1(E-18):硬门槛 16 GB,建议值 20 GB;16–20 只警告 `low_headroom`,不拒。
    .OUTPUTS
        {Ok, Reason, Warn, FreeGB, NeedGB, DeficitGB, Message}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][long] $FreeBytes)
    $freeGB = [math]::Round($FreeBytes / 1GB, 1)
    if ($freeGB -lt $script:QtDiskHardGB) {
        $deficit = [math]::Round($script:QtDiskHardGB - $freeGB, 1)
        return [pscustomobject]@{
            Ok        = $false; Reason = 'DISK_LOW'; Warn = ''
            FreeGB    = $freeGB; NeedGB = $script:QtDiskHardGB; DeficitGB = $deficit
            Message   = ('需要 {0} GB(建议 {1} GB),当前剩余 {2} GB' -f $script:QtDiskHardGB, $script:QtDiskRecommendGB, $freeGB)
        }
    }
    if ($freeGB -lt $script:QtDiskRecommendGB) {
        return [pscustomobject]@{
            Ok      = $true; Reason = ''; Warn = 'low_headroom'
            FreeGB  = $freeGB; NeedGB = $script:QtDiskHardGB; DeficitGB = 0
            Message = ('低于建议值 {0} GB,运行余量偏紧(当前剩余 {1} GB)' -f $script:QtDiskRecommendGB, $freeGB)
        }
    }
    return [pscustomobject]@{ Ok = $true; Reason = ''; Warn = ''; FreeGB = $freeGB; NeedGB = $script:QtDiskHardGB; DeficitGB = 0; Message = '' }
}

function Test-QtMemoryThreshold {
    <#
    .SYNOPSIS
        docs/03 §2.4.1:内存 `< 8 GB` → `MEM_LOW`;`8–12 GB` 只警告。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][long] $TotalBytes)
    $gb = [math]::Round($TotalBytes / 1GB, 1)
    if ($gb -lt $script:QtMinMemoryGB) {
        return [pscustomobject]@{ Ok = $false; Reason = 'MEM_LOW'; Warn = ''; TotalGB = $gb; Message = ('需要至少 {0} GB 内存,当前 {1} GB' -f $script:QtMinMemoryGB, $gb) }
    }
    if ($gb -lt $script:QtWarnMemoryGB) {
        return [pscustomobject]@{ Ok = $true; Reason = ''; Warn = 'low_memory'; TotalGB = $gb; Message = '内存偏小,只能稳定跑 1 个企点账号' }
    }
    return [pscustomobject]@{ Ok = $true; Reason = ''; Warn = ''; TotalGB = $gb; Message = '' }
}

function Get-QtPolicyBlock {
    <#
    .SYNOPSIS
        docs/03 §2.4.2:把四类策略事实汇成 `{blocked, policy_reason}`(基线 §8.5 v1.1 四值,C-36)。
        判定顺序固定:自定义内核禁用 → 功能载荷移除 → WSUS 挡载荷 → AppLocker。
    .PARAMETER Facts
        {CustomKernelForbidden, FeaturePayloadRemoved, WsusBlocksFod, AppLockerBlocked}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Facts)
    if ($Facts.CustomKernelForbidden) { return [pscustomobject]@{ blocked = $true; policy_reason = 'CUSTOM_KERNEL_FORBIDDEN' } }
    if ($Facts.FeaturePayloadRemoved) { return [pscustomobject]@{ blocked = $true; policy_reason = 'FEATURE_PAYLOAD_REMOVED' } }
    if ($Facts.WsusBlocksFod) { return [pscustomobject]@{ blocked = $true; policy_reason = 'WSUS_BLOCKS_FOD' } }
    if ($Facts.AppLockerBlocked) { return [pscustomobject]@{ blocked = $true; policy_reason = 'APPLOCKER' } }
    return [pscustomobject]@{ blocked = $false; policy_reason = '' }
}

function Get-QtNetState {
    <#
    .SYNOPSIS
        docs/03 §2.4.6 / 基线 §8.5:`SYSTEM_PROXY|VPN_ACTIVE|VPN_ACTIVE_WITH_PROXY|OFFLINE|DIRECT`。只识别不改。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [bool] $ProxyActive = $false,
        [bool] $VpnActive = $false,
        [bool] $HasDefaultGateway = $true
    )
    if (-not $HasDefaultGateway) { return 'OFFLINE' }
    if ($VpnActive -and $ProxyActive) { return 'VPN_ACTIVE_WITH_PROXY' }
    if ($VpnActive) { return 'VPN_ACTIVE' }
    if ($ProxyActive) { return 'SYSTEM_PROXY' }
    return 'DIRECT'
}

function Get-QtDataDriveSuggestion {
    <#
    .SYNOPSIS
        docs/03 §2.4.1「数据盘选择(E-18)」:系统盘 < 建议值、但另有本地固定盘 ≥ 建议值时提示。
        🔴 **只提示不自动做**;迁的只有 `ext4.vhdx` 与 `crashDumpFolder`,**暂存恒落系统盘**(P0-12)。
    .OUTPUTS
        {Suggest:bool, Drive, FreeGB}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][long] $SystemDriveFreeBytes,
        [Parameter(Mandatory)] $Drives,
        [string] $SystemDriveLetter = 'C'
    )
    $sysGB = [math]::Round($SystemDriveFreeBytes / 1GB, 1)
    if ($sysGB -ge $script:QtDiskRecommendGB) { return [pscustomobject]@{ Suggest = $false; Drive = ''; FreeGB = 0 } }
    $cands = @($Drives |
        Where-Object { $_.Letter -ne $SystemDriveLetter -and ($_.FreeBytes / 1GB) -ge $script:QtDiskRecommendGB } |
        Sort-Object -Property FreeBytes -Descending)
    if ($cands.Count -eq 0) { return [pscustomobject]@{ Suggest = $false; Drive = ''; FreeGB = 0 } }
    return [pscustomobject]@{ Suggest = $true; Drive = $cands[0].Letter; FreeGB = [math]::Round($cands[0].FreeBytes / 1GB, 1) }
}
#endregion

#region 采事实(经 Native,单测整模块 Mock)
function Get-QtWindowsFacts {
    [CmdletBinding()]
    param()
    $cv = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $build = 0
    [void][int]::TryParse([string](Get-QtRegistryValue -Path $cv -Name 'CurrentBuildNumber'), [ref]$build)
    $ubr = Get-QtRegistryValue -Path $cv -Name 'UBR'
    $edition = [string](Get-QtRegistryValue -Path $cv -Name 'EditionID')
    return [pscustomobject]@{
        win_build       = $build
        win_ubr         = if ($null -eq $ubr) { 0 } else { [int]$ubr }
        display_version = [string](Get-QtRegistryValue -Path $cv -Name 'DisplayVersion')
        edition         = $edition
        product_name    = [string](Get-QtRegistryValue -Path $cv -Name 'ProductName')
        ltsc            = ($edition -in @('EnterpriseS', 'IoTEnterpriseS'))
        x64             = ([Environment]::Is64BitOperatingSystem -and $env:PROCESSOR_ARCHITECTURE -eq 'AMD64')
        uac_enabled     = ((Get-QtRegistryValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name 'EnableLUA') -ne 0)
    }
}

function Get-QtVirtualizationOk {
    <#
    .SYNOPSIS
        docs/03 §2.4.2 / §2.15:`VirtualizationFirmwareEnabled` **或** `HypervisorPresent`(OR 判 —— Hyper-V 已在跑时前者读 false)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param()
    $cpu = @(Get-QtCim -ClassName 'Win32_Processor')
    $cs = @(Get-QtCim -ClassName 'Win32_ComputerSystem')
    $a = ($cpu.Count -gt 0 -and $cpu[0].VirtualizationFirmwareEnabled)
    $b = ($cs.Count -gt 0 -and $cs[0].HypervisorPresent)
    return [bool]($a -or $b)
}

function Get-QtWslPolicyFacts {
    [CmdletBinding()]
    param()
    $values = [ordered]@{}
    foreach ($k in $script:QtWslPolicyKeys) {
        $values[$k] = Get-QtRegistryValue -Path $script:QtWslPolicyPath -Name $k
    }
    $wsus = ((Get-QtRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -Name 'UseWUServer') -eq 1) -and
            ((Get-QtRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -Name 'RepairContentServerSource') -ne 1)
    return [pscustomobject]@{
        Values                = $values
        CustomKernelForbidden = ($null -ne $values['AllowCustomKernelUserSetting'] -and [int]$values['AllowCustomKernelUserSetting'] -eq 0)
        WsusBlocksFod         = [bool]$wsus
    }
}

function Get-QtCoexistenceFacts {
    <#
    .SYNOPSIS
        docs/03 §2.4.3:Docker Desktop / Rancher / Podman / VMware / VirtualBox / 第三方安卓模拟器。**只读、只提示**。
    #>
    [CmdletBinding()]
    param([string[]] $DistroNames = @())
    $pf = Get-QtEnvironmentPath -Name 'ProgramFiles'
    return [pscustomobject]@{
        docker_desktop = [bool](($DistroNames -contains 'docker-desktop') -or ((Get-QtServiceStatus -Name 'com.docker.service') -ne 'NotInstalled'))
        rancher        = [bool](@($DistroNames | Where-Object { $_ -like 'rancher-desktop*' -or $_ -like 'podman-machine-*' }).Count -gt 0)
        vmware         = [bool](((Get-QtServiceStatus -Name 'VMwareHostd') -ne 'NotInstalled') -or ((Get-QtServiceStatus -Name 'VMAuthdService') -ne 'NotInstalled'))
        virtualbox     = [bool](((Get-QtServiceStatus -Name 'VBoxSVC') -ne 'NotInstalled') -or (Test-QtPath -Path (Join-Path $pf 'Oracle\VirtualBox')))
        emulators      = @((Get-QtProcessByName -Name @('dnplayer', 'Nox', 'MuMuPlayer', 'LdVBoxHeadless')) | ForEach-Object { $_.Name })
    }
}

function Get-QtRegisteredDistro {
    <#
    .SYNOPSIS
        注册表 `HKCU\…\Lxss\{guid}` 的发行版清单(docs/03 §2.4.4 / §2.15)。**用户已有发行版一律只读**。
    #>
    [CmdletBinding()]
    param([string] $LxssPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss')
    $rows = @()
    if (-not (Test-QtRegistryKey -Path $LxssPath)) { return , $rows }
    $defaultGuid = [string](Get-QtRegistryValue -Path $LxssPath -Name 'DefaultDistribution')
    foreach ($sub in (Get-QtRegistrySubKeyName -Path $LxssPath)) {
        $p = Join-Path $LxssPath $sub
        $name = Get-QtRegistryValue -Path $p -Name 'DistributionName'
        if (-not $name) { continue }
        $rows += [pscustomobject]@{
            name      = [string]$name
            version   = [int](Get-QtRegistryValue -Path $p -Name 'Version')
            base_path = [string](Get-QtRegistryValue -Path $p -Name 'BasePath')
            default   = ($sub -eq $defaultGuid)
        }
    }
    return , $rows
}

function Test-QtElevatedUserMatchesLogon {
    <#
    .SYNOPSIS
        docs/03 §2.4.1「提权账号≠登录账号」:比提权进程 SID 与本会话 explorer.exe 所有者 SID。
        取不到交互 SID(无人值守、无桌面)视为**一致**(不拦静默安装)。
    .OUTPUTS
        {Ok, ElevatedSid, LogonSid}
    #>
    [CmdletBinding()]
    param()
    $elevated = Get-QtCurrentUserSid
    $logon = Get-QtInteractiveUserSid
    if ([string]::IsNullOrWhiteSpace($logon)) {
        return [pscustomobject]@{ Ok = $true; ElevatedSid = $elevated; LogonSid = '' }
    }
    return [pscustomobject]@{ Ok = ($elevated -eq $logon); ElevatedSid = $elevated; LogonSid = $logon }
}

function Invoke-QtPrecheckProbe {
    <#
    .SYNOPSIS
        docs/03 §2.4.6(99b ①):PRECHECK 时 Agent 与 WinAgent 都还没装,引擎自带 PowerShell 做
        **只有 Windows 侧、只有 DNS+TCP 两层**的基础探测。结论只出 `OK/DNS_FAIL/TCP_TIMEOUT/TCP_REFUSED`。
        🔴 结果只写 `env_json.precheck_probes[]`,**不写 `probe_results` 表**(那张表只由 WinAgent 落,C-31)。
        🔴 **任何探测结果都不阻断安装**。
    .PARAMETER Targets
        [{target, host, port}]
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Targets,
        [int] $DnsTimeoutSec = 5,
        [int] $TcpTimeoutSec = 5
    )
    $out = @()
    foreach ($t in $Targets) {
        $at = Get-QtIso8601
        $result = 'OK'
        $addresses = @()
        try {
            $addresses = @([System.Net.Dns]::GetHostAddresses([string]$t.host))
        } catch { $addresses = @() }
        if ($addresses.Count -eq 0) {
            $result = 'DNS_FAIL'
        } else {
            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $ar = $client.BeginConnect([string]$t.host, [int]$t.port, $null, $null)
                if (-not $ar.AsyncWaitHandle.WaitOne($TcpTimeoutSec * 1000, $false)) { $result = 'TCP_TIMEOUT' }
                else { $client.EndConnect($ar) }
            } catch { $result = 'TCP_REFUSED' } finally { $client.Close() }
        }
        $out += [pscustomobject]@{ target = [string]$t.target; result = $result; host = [string]$t.host; port = [int]$t.port; at = $at }
    }
    return , $out
}
#endregion

function Invoke-QtPreflight {
    <#
    .SYNOPSIS
        PRECHECK 编排:采事实 → 跑纯判定 → 组 `install_state.env`(docs/03 §2.3 schema `env` 字段)。
        🔴 只读,不改任何东西;失败时回第一个命中的原因码(判定顺序 = docs/03 §2.4 小节顺序)。
    .OUTPUTS
        {Ok, Reason, Env, Warnings[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $TargetDriveLetter,
        [string] $WslConfigPath,
        [string] $OurKernelSha256 = '',
        [object[]] $ProbeTargets = @()
    )
    $warnings = @()
    $win = Get-QtWindowsFacts
    $env = [ordered]@{
        win_build         = $win.win_build
        win_ubr           = $win.win_ubr
        edition           = $win.edition
        ltsc              = $win.ltsc
        x64               = $win.x64
        install_user_sid  = ''
        elevated_user_sid = ''
        virt              = 'OK'
        policy            = [pscustomobject]@{ blocked = $false; policy_reason = '' }
        wsl_state         = ''
        wsl_version       = ''
        wsl_kernel_line   = (Get-QtKernelLine)
        distros           = @()
        docker_desktop    = $false
        vmware            = $false
        virtualbox        = $false
        kernel_state      = 'DEFAULT'
        wslconfig_exists  = $false
        wslconfig_path    = ''
        vhdx_path         = ''
        other_distros     = @()
        net_state         = ''
        mem_total_mb      = 0
        disk_free_mb      = 0
        docker_cidr       = ''
        precheck_probes   = @()
    }

    $b = Test-QtWindowsBuild -Build $win.win_build
    if (-not $b.Ok) { return [pscustomobject]@{ Ok = $false; Reason = $b.Reason; Env = $env; Warnings = $warnings } }
    if (-not $win.x64) { return [pscustomobject]@{ Ok = $false; Reason = 'NOT_X64'; Env = $env; Warnings = $warnings } }
    if (-not (Test-QtAdmin)) { return [pscustomobject]@{ Ok = $false; Reason = 'NOT_ADMIN'; Env = $env; Warnings = $warnings } }

    $ids = Test-QtElevatedUserMatchesLogon
    $env.elevated_user_sid = $ids.ElevatedSid
    $env.install_user_sid = if ($ids.LogonSid) { $ids.LogonSid } else { $ids.ElevatedSid }
    if (-not $ids.Ok) { return [pscustomobject]@{ Ok = $false; Reason = 'ELEVATED_AS_OTHER_USER'; Env = $env; Warnings = $warnings } }

    if (-not (Get-QtVirtualizationOk)) {
        $env.virt = 'VIRT_DISABLED'
        return [pscustomobject]@{ Ok = $false; Reason = 'VIRT_DISABLED'; Env = $env; Warnings = $warnings }
    }

    $pol = Get-QtWslPolicyFacts
    $vmpState = Get-QtOptionalFeatureState -FeatureName 'VirtualMachinePlatform'
    $wslState = Get-QtOptionalFeatureState -FeatureName 'Microsoft-Windows-Subsystem-Linux'
    $block = Get-QtPolicyBlock -Facts ([pscustomobject]@{
            CustomKernelForbidden = $pol.CustomKernelForbidden
            FeaturePayloadRemoved = ($vmpState -eq 'DisabledWithPayloadRemoved' -or $wslState -eq 'DisabledWithPayloadRemoved')
            WsusBlocksFod         = $pol.WsusBlocksFod
            AppLockerBlocked      = $false
        })
    $env.policy = $block
    if ($block.blocked) { return [pscustomobject]@{ Ok = $false; Reason = 'POLICY_BLOCKED'; Env = $env; Warnings = $warnings } }

    $cs = @(Get-QtCim -ClassName 'Win32_ComputerSystem')
    $memBytes = if ($cs.Count -gt 0) { [long]$cs[0].TotalPhysicalMemory } else { [long]0 }
    $env.mem_total_mb = [int]($memBytes / 1MB)
    $mem = Test-QtMemoryThreshold -TotalBytes $memBytes
    if ($mem.Warn) { $warnings += $mem.Message }
    if (-not $mem.Ok) { return [pscustomobject]@{ Ok = $false; Reason = 'MEM_LOW'; Env = $env; Warnings = $warnings } }

    $free = Get-QtDriveFreeBytes -DriveLetter $TargetDriveLetter
    $env.disk_free_mb = [int]($free / 1MB)
    $disk = Test-QtDiskThreshold -FreeBytes $free
    if ($disk.Warn) { $warnings += $disk.Message; $env['disk_warn'] = $disk.Warn }
    if (-not $disk.Ok) { return [pscustomobject]@{ Ok = $false; Reason = 'DISK_LOW'; Env = $env; Warnings = $warnings } }

    $distros = Get-QtRegisteredDistro
    $env.distros = $distros
    $env.other_distros = @($distros | Where-Object { $_.name -ne 'qtrade' -and $_.name -notlike 'docker-*' } | ForEach-Object { $_.name })
    $coex = Get-QtCoexistenceFacts -DistroNames @($distros | ForEach-Object { $_.name })
    $env.docker_desktop = $coex.docker_desktop
    $env.vmware = $coex.vmware
    $env.virtualbox = $coex.virtualbox

    $ver = Invoke-QtWsl -WslArgs @('--version') -TimeoutSec 30
    $status = Invoke-QtWsl -WslArgs @('--status') -TimeoutSec 30
    $wslVersion = Get-QtWslVersionString -Output $ver.StdOut
    $env.wsl_version = $wslVersion
    $env.wsl_state = Get-QtWslStateFromProbe -Probe ([pscustomobject]@{
            VirtOk         = $true
            PolicyBlocked  = $false
            FeatureVmp     = $vmpState
            FeatureWsl     = $wslState
            WslExeExists   = (Test-QtPath -Path (Join-Path (Get-QtEnvironmentPath -Name 'SystemRoot') 'System32\wsl.exe'))
            VersionOk      = ($ver.ExitCode -eq 0 -and $wslVersion -ne '')
            VersionString  = $wslVersion
            StatusOk       = ($status.ExitCode -eq 0)
            AllDistrosV1   = ($distros.Count -gt 0 -and @($distros | Where-Object { $_.version -ne 1 }).Count -eq 0)
            DefaultVersion = 2
        })

    if (-not $WslConfigPath) { $WslConfigPath = Join-Path (Get-QtEnvironmentPath -Name 'UserProfile') '.wslconfig' }
    $env.wslconfig_path = $WslConfigPath
    $env.wslconfig_exists = Test-QtPath -Path $WslConfigPath
    $kernelValue = $null
    if ($env.wslconfig_exists) {
        $parsed = ConvertFrom-QtWslConfig -Text (Read-QtTextFile -Path $WslConfigPath)
        $kernelValue = Get-QtWslConfigValue -Parsed $parsed -Section 'wsl2' -Key 'kernel'
    }
    $actualSha = ''
    if ($kernelValue) {
        $kp = ConvertFrom-QtKernelLineValue -Value $kernelValue
        if (Test-QtPath -Path $kp) { $actualSha = Get-QtFileHash -Path $kp }
    }
    $env.kernel_state = Get-QtKernelState -KernelValue $kernelValue -OurKernelSha256 $OurKernelSha256 -ActualSha256 $actualSha

    if ($ProbeTargets.Count -gt 0) {
        $env.precheck_probes = Invoke-QtPrecheckProbe -Targets $ProbeTargets
    }

    return [pscustomobject]@{ Ok = $true; Reason = ''; Env = $env; Warnings = $warnings }
}

Export-ModuleMember -Function Get-QtPreflightThresholds, Get-QtPolicyReasons, Get-QtPrecheckTargets,
Test-QtWindowsBuild, Test-QtDiskThreshold, Test-QtMemoryThreshold, Get-QtPolicyBlock, Get-QtNetState,
Get-QtDataDriveSuggestion, Get-QtWindowsFacts, Get-QtVirtualizationOk, Get-QtWslPolicyFacts,
Get-QtCoexistenceFacts, Get-QtRegisteredDistro, Test-QtElevatedUserMatchesLogon,
Invoke-QtPrecheckProbe, Invoke-QtPreflight
