# QTrade 安装引擎 —— 内核:落盘 / kcheck 预导入 / 验证 / 回滚
# 规格:docs/03 §2.6.1(KERNEL_STAGED)、§2.6.3(shutdown 时机,红线 6)、§2.6.4(验证判据 0~9 步)、
#       §2.6.5(回滚)、§2.6.6(不变量)、§2.6.7 表 A/B(K1~K7、W1~W17)、§2.6.8(一次成功率措施)、§7(超时值)
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
# 安全判定与安装根共用同一份实现(禁止位、必须的完全控制、不许有多余身份)——
# 这种东西存在第二份实现,迟早两边会漂移。
Import-Module (Join-Path $PSScriptRoot 'QTrade.Acl.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Wsl.psm1') -DisableNameChecking

# docs/03 §7 `[wsl]` 段超时值(本册追加键;改要先改 docs/03 §7)
$script:QtKernelTimeouts = [ordered]@{
    shutdown_timeout_s          = 60    # W5;超时 → KERNEL_SHUTDOWN_TIMEOUT
    shutdown_grace_s            = 8     # W9:WSL 8 s 空闲回收窗口,统一 8 s(rollback-kernel.ps1 的 3 s 偏短)
    kernel_boot_timeout_s       = 120   # 成品脚本 BootTimeout
    binder_check_timeout_s      = 60    # §2.6.4 第 5 步
    user_distro_check_timeout_s = 60    # §2.6.4 第 8 步,每个发行版
    kcheck_import_timeout_s     = 120   # §2.6.1 预导入
    record_timeout_s            = 30    # §2.6.4 第 6 步,只记不判
}

$script:QtKCheckDistro = 'qtrade-kcheck'
$script:QtKernelLine = '6.6'            # A-3:恒 6.6,5.15 线已砍

# 🔴 §2.6.4 第 5 步的 binder 判据原文 —— **命令串不含双引号**(W7:PowerShell 5.1 传原生程序时会弄坏引号)
$script:QtBinderCheckCommand = 'zcat /proc/config.gz | grep -q ^CONFIG_ANDROID_BINDER_IPC=y && grep -qw binder /proc/filesystems && mkdir -p /run/binderfs-check && mount -t binder binder /run/binderfs-check && ls /run/binderfs-check && umount /run/binderfs-check && echo BINDERFS_OK'

# W13 噪声白名单:诊断包与 kcheck **不以 dmesg 噪声判失败**
$script:QtDmesgNoiseAllowlist = @(
    'dxgk.*Ioctl failed'
    'getaddrinfo -5'
    'vmbus_driver_register failed: -19'
    'modprobe: FATAL'
)

function Get-QtKernelTimeouts { [CmdletBinding()] param() return $script:QtKernelTimeouts }
function Get-QtBinderCheckCommand { [CmdletBinding()][OutputType([string])] param() return $script:QtBinderCheckCommand }
function Get-QtKCheckDistroName { [CmdletBinding()][OutputType([string])] param() return $script:QtKCheckDistro }
function Get-QtKernelLine { [CmdletBinding()][OutputType([string])] param() return $script:QtKernelLine }
function Get-QtDmesgNoiseAllowlist { [CmdletBinding()] param() return , $script:QtDmesgNoiseAllowlist }

function Test-QtBinderCheckOutput {
    <#
    .SYNOPSIS
        §2.6.4 第 5 步判据:输出含 `BINDERFS_OK` **且** ls 列出 binder / hwbinder / vndbinder 三项。
        🔴 **不用 `ls /dev/binder` 判**(K5:6.x 开 binderfs 后不预建,v1 脚本据此把好内核误判失败)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Output)
    if ($Output -notmatch 'BINDERFS_OK') { return $false }
    foreach ($dev in @('binder', 'hwbinder', 'vndbinder')) {
        if ($Output -notmatch ('(?m)(^|\s)' + [regex]::Escape($dev) + '(\s|$)')) { return $false }
    }
    return $true
}

function Test-QtKernelVersionExact {
    <#
    .SYNOPSIS
        §2.6.4 第 4 步:`uname -r` 输出与 manifest `version` **逐字相等**。
        🔴 K4:不再只判「含 binder」—— `…-binder+` 也含 binder,判不出来。
    #>
    [CmdletBinding()][OutputType([bool])]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $UnameOutput,
        [Parameter(Mandatory)][string] $ManifestVersion
    )
    return (($UnameOutput).Trim() -ceq $ManifestVersion.Trim())
}

function Test-QtUnameLooksLikeVersion {
    <#
    .SYNOPSIS
        uname -r 的标准输出是否像内核版本串。HCS 超时文案、空输出都不算。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Text)
    $t = (($Text -replace '[\r\n]+', ' ') -replace '\s+', ' ').Trim()
    if ($t -match 'HCS_E_CONNECTION_TIMEOUT') { return $false }
    return [bool]($t -match '^\d+\.\d+')
}

function Test-QtKernelPreconditions {
    <#
    .SYNOPSIS
        §2.6.8 第 9 条「写配置前的三道前置校验」:①bzImage sha256 = manifest;②`.wslconfig` 可解析(W3);
        ③策略键(W12)。任一不过就**不写、不 shutdown**。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $KernelPath,
        [Parameter(Mandatory)][string] $ExpectedSha256,
        [Parameter(Mandatory)][string] $WslConfigPath,
        [bool] $CustomKernelForbidden = $false
    )
    if (-not (Test-QtPath -Path $KernelPath)) { return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_SHA_MISMATCH' } }
    if ((Get-QtFileHash -Path $KernelPath) -ne $ExpectedSha256.ToLowerInvariant()) {
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_SHA_MISMATCH' }
    }
    $parse = Test-QtWslConfigParsable -Path $WslConfigPath
    if (-not $parse.Ok) { return [pscustomobject]@{ Ok = $false; Reason = 'WSLCONFIG_PARSE_FAILED' } }
    if ($CustomKernelForbidden) { return [pscustomobject]@{ Ok = $false; Reason = 'POLICY_BLOCKED' } }
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Write-QtKernelPointer {
    <#
    .SYNOPSIS
        `kernel\current.json` —— **后续所有地方(WinAgent 环境页、卸载、升级)只认这个指针**(docs/03 §2.6.1)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $KernelPath,
        [Parameter(Mandatory)][string] $Sha256,
        [Parameter(Mandatory)][string] $Version,
        [string] $Line = '6.6',
        [string] $VerifiedAt = ''
    )
    New-QtDirectory -Path (Split-Path -Parent $Path) | Out-Null
    $obj = [ordered]@{ line = $Line; path = $KernelPath; sha256 = $Sha256; version = $Version }
    if ($VerifiedAt) { $obj['verified_at'] = $VerifiedAt }
    [IO.File]::WriteAllText($Path, ($obj | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    return $Path
}

function Set-QtKernelAcl {
    <#
    .SYNOPSIS
        docs/03 §2.6.1:`Administrators` 完全控制、`Users` 只读 ——
        内核文件被普通用户替换等于任意内核代码执行。
    .NOTES
        走 Native 接缝(Get-QtAcl/Set-QtAcl)而不是直接 Get-Acl/Set-Acl ——
        直接调的话 Pester 没法 Mock,单测就只能去碰真机的 ACL。

        🔴 写完**读回复核**(总控 2026-09-20 裁决 ②,与安装根同一口径):
           `Set-Acl` 不抛异常 **≠** DACL 真的变成了你要的样子 —— 被组策略或安全软件
           挡下来时它可能静默无效。而这条 ACL 守的是「内核文件被普通用户替换
           = 任意内核代码执行」,静默失效是不可接受的。
           复核不过由调用方按 KERNEL_STAGED 既有的失败路径处置。
    .OUTPUTS
        { Ok; Changed; Path; Problems[] }
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)

    try {
        $acl = Get-QtAcl -Path $Path
    } catch {
        return [pscustomobject]@{ Ok = $false; Changed = $false; Path = $Path; Problems = @(('读 ACL 失败:{0}' -f $_.Exception.Message)) }
    }

    # 幂等:已经是收紧后的样子就不写
    if ((Test-QtKernelFileAcl -Acl $acl).Ok) {
        return [pscustomobject]@{ Ok = $true; Changed = $false; Path = $Path; Problems = @() }
    }

    try {
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }
        foreach ($item in (Get-QtKernelFileAclSpec)) {
            # 内核文件是**文件**,ACE 不带继承标志(三参构造器)
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
                (New-Object Security.Principal.SecurityIdentifier($item.Sid)),
                $item.Rights,
                [Security.AccessControl.AccessControlType]::Allow)))
        }
        Set-QtAcl -Path $Path -AclObject $acl
    } catch {
        return [pscustomobject]@{ Ok = $false; Changed = $false; Path = $Path; Problems = @(('写 ACL 失败:{0}' -f $_.Exception.Message)) }
    }

    try {
        $after = Test-QtKernelFileAcl -Acl (Get-QtAcl -Path $Path)
    } catch {
        return [pscustomobject]@{ Ok = $false; Changed = $true; Path = $Path; Problems = @(('写完读回失败:{0}' -f $_.Exception.Message)) }
    }
    return [pscustomobject]@{ Ok = $after.Ok; Changed = $true; Path = $Path; Problems = @($after.Problems) }
}

function Remove-QtKCheck {
    <#
    .SYNOPSIS
        §2.6.6 不变量②:任何路径结束时 `qtrade-kcheck` 都被注销(引擎启动时也顺手清残留)。
    #>
    [CmdletBinding()]
    param([string] $Directory, [int] $TimeoutSec = 60)
    $r = Unregister-QtDistro -Name $script:QtKCheckDistro -TimeoutSec $TimeoutSec
    if ($Directory -and (Test-QtPath -Path $Directory)) {
        Remove-QtItem -Path $Directory -Recurse
    }
    return $r
}

function Import-QtKCheck {
    <#
    .SYNOPSIS
        §2.6.1 / §2.6.8 第 5 条(W14):**在官方内核上预先导入** kcheck。
        放在切内核之后的话,新内核起不来会先表现为「导入失败」,原因码与回滚路径全错;预先导入还省一次 shutdown。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Directory,
        [Parameter(Mandatory)][string] $TarPath,
        [int] $TimeoutSec = 0
    )
    if ($TimeoutSec -le 0) { $TimeoutSec = $script:QtKernelTimeouts.kcheck_import_timeout_s }
    # 残留先注销(§2.6.6 不变量②)
    Remove-QtKCheck -Directory $Directory -TimeoutSec 60 | Out-Null
    New-QtDirectory -Path $Directory | Out-Null
    $r = Invoke-QtWsl -WslArgs @('--import', $script:QtKCheckDistro, $Directory, $TarPath, '--version', '2') -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) {
        return [pscustomobject]@{ Ok = $false; Reason = 'KCHECK_IMPORT_FAILED' }
    }
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Invoke-QtWslShutdown {
    <#
    .SYNOPSIS
        🔴 红线 6:`wsl --shutdown` 只在用户已明示确认(向导【现在切换】或 `/QT_ACCEPT_SHUTDOWN=1`)后才可调。
        本函数强制要求 -Confirmed,**没有确认就抛**,把红线钉在代码里而不是注释里。
        W9:shutdown 后固定 `sleep 8`(WSL 空闲回收窗口)。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][bool] $Confirmed,
        [int] $TimeoutSec = 0,
        [int] $GraceSec = -1
    )
    if (-not $Confirmed) {
        throw '拒绝执行 wsl --shutdown:红线 6 要求用户明示确认(向导【现在切换】或 /QT_ACCEPT_SHUTDOWN=1)'
    }
    if ($TimeoutSec -le 0) { $TimeoutSec = $script:QtKernelTimeouts.shutdown_timeout_s }
    if ($GraceSec -lt 0) { $GraceSec = $script:QtKernelTimeouts.shutdown_grace_s }
    $r = Invoke-QtWsl -WslArgs @('--shutdown') -TimeoutSec $TimeoutSec
    if ($r.TimedOut) { return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_SHUTDOWN_TIMEOUT' } }
    Start-QtSleep -Seconds $GraceSec
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Invoke-QtKernelVerify {
    <#
    .SYNOPSIS
        §2.6.4 验证段(第 2/4/5 步定成败;第 6 步只记档;第 8 步只报不回滚;第 9 步注销 kcheck)。
    .PARAMETER ShutdownConfirmed
        红线 6 的确认。false 直接抛(调用方必须先拿到确认)。
    .PARAMETER UserDistros
        §2.6.4 第 8 步的被动检查对象:`env.distros` 里 VERSION 2 的非 `docker-*` 发行版名。
    .OUTPUTS
        {Ok, Reason, Uname, BinderOutput, Records, UserDistroFailures[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $ManifestVersion,
        [Parameter(Mandatory)][bool] $ShutdownConfirmed,
        [string[]] $UserDistros = @(),
        [bool] $CustomKernelPolicyPresent = $false,
        [switch] $SkipShutdown
    )
    $t = $script:QtKernelTimeouts
    $records = [ordered]@{}
    $failures = @()

    # 2. wsl --shutdown(≤60 s)+ sleep 8
    if (-not $SkipShutdown) {
        $sd = Invoke-QtWslShutdown -Confirmed $ShutdownConfirmed
        if (-not $sd.Ok) {
            # 🔴 此刻**不回滚**(回滚也要 shutdown,同样会挂);配置已写、RunOnce 已指 verify-kernel
            return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_SHUTDOWN_TIMEOUT'; Message = ''; Uname = ''; BinderOutput = ''; Records = $records; UserDistroFailures = @() }
        }
    }

    # 4. 启动 + 内核名(≤120 s)
    $u = Invoke-QtWsl -WslArgs @('-d', $script:QtKCheckDistro, '--exec', 'uname', '-r') -TimeoutSec $t.kernel_boot_timeout_s
    if ($u.TimedOut) {
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_TIMEOUT'; Message = ''; Uname = ''; BinderOutput = ''; Records = $records; UserDistroFailures = @() }
    }
    if ($u.ExitCode -ne 0) {
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_FAILED'; Message = ''; Uname = $u.StdOut; BinderOutput = ''; Records = $records; UserDistroFailures = @() }
    }
    if (-not (Test-QtUnameLooksLikeVersion -Text $u.StdOut)) {
        # B6:此刻只知道「QTrade 内核下 WSL2 没起来」,**不能**下「与 QTrade 内核无关」的结论 ——
        #     要等回滚后原装内核的复验结果才分得清(见 Resolve-QtKernelSwitchFailure)
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_FAILED'; Message = 'QTrade 内核下 WSL2 未能正常启动(uname 输出不是版本串)'; Uname = $u.StdOut; BinderOutput = ''; Records = $records; UserDistroFailures = @() }
    }
    if (-not (Test-QtKernelVersionExact -UnameOutput $u.StdOut -ManifestVersion $ManifestVersion)) {
        # W12:不等且策略键存在 → 原因码改 POLICY_BLOCKED(否则排查方向全错)
        $reason = if ($CustomKernelPolicyPresent) { 'POLICY_BLOCKED' } else { 'KERNEL_NO_BINDER' }
        return [pscustomobject]@{ Ok = $false; Reason = $reason; Message = ''; Uname = $u.StdOut.Trim(); BinderOutput = ''; Records = $records; UserDistroFailures = @() }
    }

    # 5. binder 判据(≤60 s,以 root、sh -c,命令串不含双引号 W7)
    $b = Invoke-QtWsl -WslArgs @('-d', $script:QtKCheckDistro, '--user', 'root', '--exec', 'sh', '-c', $script:QtBinderCheckCommand) -TimeoutSec $t.binder_check_timeout_s
    if ($b.TimedOut -or -not (Test-QtBinderCheckOutput -Output $b.StdOut)) {
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_NO_BINDER'; Message = ''; Uname = $u.StdOut.Trim(); BinderOutput = $b.StdOut; Records = $records; UserDistroFailures = @() }
    }

    # 6. 记档(≤30 s,只记不判;K1 真机痕迹 / K7 / K3)
    foreach ($item in @(
            @{ Key = 'hv_sock'; Cmd = 'dmesg | grep -c registering driver hv_sock' }
            @{ Key = 'cmdline'; Cmd = 'cat /proc/cmdline' }
            @{ Key = 'alg_selftests'; Cmd = 'dmesg | grep -c alg: self-tests' }
            @{ Key = 'config'; Cmd = 'zcat /proc/config.gz | grep -E BINDER|VSOCKETS|CRYPTO_TEST|LOCALVERSION' }
        )) {
        $r = Invoke-QtWsl -WslArgs @('-d', $script:QtKCheckDistro, '--user', 'root', '--exec', 'sh', '-c', $item.Cmd) -TimeoutSec $t.record_timeout_s
        $records[$item.Key] = $r.StdOut.Trim()
    }

    # 8. 用户已有发行版的被动检查(只报不回滚,B-4 / P-22)
    foreach ($d in $UserDistros) {
        $r = Invoke-QtWsl -WslArgs @('-d', $d, '--exec', 'uname', '-r') -TimeoutSec $t.user_distro_check_timeout_s
        if ($r.TimedOut -or $r.ExitCode -ne 0) {
            $failures += [pscustomobject]@{ name = $d; summary = (($r.StdErr + ' ' + $r.StdOut).Trim()) }
        }
    }

    return [pscustomobject]@{
        Ok                 = $true
        Reason             = ''
        Uname              = $u.StdOut.Trim()
        BinderOutput       = $b.StdOut
        Records            = $records
        UserDistroFailures = $failures
    }
}

function Invoke-QtKernelRollback {
    <#
    .SYNOPSIS
        §2.6.5 回滚段。🔴 触发条件**只有内核自身验证失败**(§2.6.4 第 2/4/5 步);
        用户发行版起不来(第 8 步)**不触发**,只给按钮(B-4)。
        回滚基线 = 当前文件去掉所有 `kernel=` 行(W4),**不是旧备份文件**。
    .OUTPUTS
        {Ok, Reason, OfficialKernel, Stage}
        Stage(B6):'' = 成功;'shutdown' = 回滚那次 shutdown 挂住(分不清原装内核好坏);
                   'verify' = 已回到原装内核、但原装内核下 kcheck 也起不来(uname 超时 / 非 0 / 不像版本串)
                   ——只有这一种才能说「与 QTrade 内核无关」。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $WslConfigPath,
        [Parameter(Mandatory)][bool] $ShutdownConfirmed,
        [string] $KCheckDirectory
    )
    $t = $script:QtKernelTimeouts
    # 1. 用回滚基线覆写(Write-Utf8NoBom)
    $cur = ''
    if (Test-QtPath -Path $WslConfigPath) { $cur = Read-QtTextFile -Path $WslConfigPath }
    $baseline = Get-QtWslConfigRollbackBaseline -Text $cur
    Write-QtUtf8NoBom -Path $WslConfigPath -Text $baseline | Out-Null

    # 2. shutdown(再一次,原子段内最多两次 —— W14)
    $sd = Invoke-QtWslShutdown -Confirmed $ShutdownConfirmed
    if (-not $sd.Ok) {
        # 🔴 §2.6.6 不变量②:任何路径结束时 kcheck 都被注销 —— 提前 return 也不能漏
        Remove-QtKCheck -Directory $KCheckDirectory | Out-Null
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_ROLLBACK_FAILED'; OfficialKernel = ''; Stage = 'shutdown' }
    }

    # 3. kcheck uname -r 应为官方版本串
    $u = Invoke-QtWsl -WslArgs @('-d', $script:QtKCheckDistro, '--exec', 'uname', '-r') -TimeoutSec $t.kernel_boot_timeout_s
    $ok = (-not $u.TimedOut) -and ($u.ExitCode -eq 0) -and (Test-QtUnameLooksLikeVersion -Text $u.StdOut)

    # 4. 注销 kcheck(不论成败,§2.6.6 不变量②)
    Remove-QtKCheck -Directory $KCheckDirectory | Out-Null

    if (-not $ok) {
        # .wslconfig 已是无 kernel= 的基线 —— 原装内核下也起不来,才与我们无关(文案给手工步骤)
        return [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_ROLLBACK_FAILED'; OfficialKernel = ''; Stage = 'verify' }
    }
    return [pscustomobject]@{ Ok = $true; Reason = ''; OfficialKernel = $u.StdOut.Trim(); Stage = '' }
}

function Resolve-QtKernelSwitchFailure {
    <#
    .SYNOPSIS
        B6:KERNEL_SWITCH 内核自身验证失败、回滚之后,给用户看的原因码与文案。纯函数。
        🔴「与 QTrade 内核无关」**只**在回滚后原装内核也起不来(Rollback.Stage = 'verify')时才说;
           回滚成功 = 原装内核好好的 ⇒ 问题就在 QTrade 内核上,文案必须这么说、并保留原因码,
           否则排查方向会被带反(评审 B6)。
    .PARAMETER Verify
        Invoke-QtKernelVerify 的结果({Reason, Message, ...})。
    .PARAMETER Rollback
        Invoke-QtKernelRollback 的结果({Ok, Reason, OfficialKernel, Stage})。
    .OUTPUTS
        {Reason, ExitName, Message}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Verify,
        [Parameter(Mandatory)] $Rollback
    )
    $vr = [string]$Verify.Reason
    $detail = ''
    if ((Test-QtHasProperty -Object $Verify -Name 'Message') -and $Verify.Message) { $detail = [string]$Verify.Message }
    $stage = ''
    if (Test-QtHasProperty -Object $Rollback -Name 'Stage') { $stage = [string]$Rollback.Stage }

    if ([bool]$Rollback.Ok) {
        $official = [string]$Rollback.OfficialKernel
        if ($vr -eq 'KERNEL_BOOT_FAILED' -or $vr -eq 'KERNEL_BOOT_TIMEOUT') {
            $msg = ('QTrade 内核未能启动,已恢复原内核{0}(原因码 {1})' -f $(if ($official) { ' ' + $official } else { '' }), $vr)
        } else {
            $msg = ('切换失败({0}),已恢复原内核' -f $vr)
        }
        if ($detail) { $msg = $msg + ':' + $detail }
        return [pscustomobject]@{ Reason = $vr; ExitName = ('E_INSTALL_' + $vr); Message = $msg }
    }

    if ($stage -eq 'verify') {
        $msg = ('已去掉 .wslconfig 里的 kernel= 行、回到原装内核,但原装内核下本机 WSL2 仍无法启动,与 QTrade 内核无关' +
            '(QTrade 内核验证原因码 {0})。请先让 WSL2 本身恢复正常(可运行 wsl --status 查看),必要时重启电脑后再重试安装' -f $vr)
    } else {
        $msg = ('回滚失败(QTrade 内核验证原因码 {0}):请打开 %USERPROFILE%\.wslconfig 确认没有 kernel= 行,然后重启电脑' -f $vr)
    }
    return [pscustomobject]@{ Reason = 'KERNEL_ROLLBACK_FAILED'; ExitName = 'E_INSTALL_KERNEL_ROLLBACK_FAILED'; Message = $msg }
}

function Save-QtKCheckDmesg {
    <#
    .SYNOPSIS
        9/22 预演 #16 / 评审 C:**注销 kcheck 之前**把 `dmesg | tail -200` 落进日志目录,诊断包再带上。
        采集晚了(kcheck 已注销)就只剩「发行版不存在」一句,正是预演里的情形。
        只记不判;任何失败都不抛(诊断材料不能反过来把失败路径打断)。
    .OUTPUTS
        写成的文件路径;写不成回 ''。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Directory,
        [string] $Phase = '',
        [string] $Stamp = '',
        [int] $TimeoutSec = 0
    )
    try {
        if ($TimeoutSec -le 0) { $TimeoutSec = $script:QtKernelTimeouts.record_timeout_s }
        if (-not $Stamp) { $Stamp = Get-QtTimestamp }
        $r = Invoke-QtWsl -WslArgs @('-d', $script:QtKCheckDistro, '--user', 'root', '--exec', 'sh', '-c', 'dmesg | tail -200') -TimeoutSec $TimeoutSec
        $head = ('# {0} 的 dmesg(注销前抓取;阶段 {1};exit={2};timed_out={3})' -f $script:QtKCheckDistro, $Phase, $r.ExitCode, $r.TimedOut)
        $body = [string]$r.StdOut
        if ($r.TimedOut) { $body = ('(抓取超时 {0} s)' -f $TimeoutSec) }
        elseif ([string]::IsNullOrWhiteSpace($body)) { $body = '(dmesg 无输出)' + "`n" + [string]$r.StdErr }
        New-QtDirectory -Path $Directory | Out-Null
        $path = Join-Path $Directory ('kcheck-dmesg-{0}.txt' -f $Stamp)
        Write-QtUtf8NoBom -Path $path -Text (Protect-QtLogText -Text ($head + "`n" + $body)) | Out-Null
        return $path
    } catch {
        return ''
    }
}

function Test-QtKernelStaged {
    <#
    .SYNOPSIS
        KERNEL_STAGED 幂等判据(docs/03 §2.3):`kernel\bzImage-6.6` 存在且 sha256 一致;`wsl -l` 有 `qtrade-kcheck`。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    if (-not (Test-QtPath -Path $Context.KernelPath)) { return $false }
    if ((Get-QtFileHash -Path $Context.KernelPath) -ne ([string]$Context.KernelSha256).ToLowerInvariant()) { return $false }
    $l = Invoke-QtWsl -WslArgs @('--list', '--quiet') -TimeoutSec 60
    return ($l.StdOut -match [regex]::Escape($script:QtKCheckDistro))
}

function Test-QtWslConfigWritten {
    <#
    .SYNOPSIS
        WSLCONFIG_WRITTEN 幂等判据:文件含我们的 `kernel=` 行,值与 `kernel_line_written` 一致(docs/03 §2.3)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    if (-not (Test-QtPath -Path $Context.WslConfigPath)) { return $false }
    $text = Read-QtTextFile -Path $Context.WslConfigPath
    if ([string]::IsNullOrWhiteSpace($Context.KernelLineWritten)) { return $false }
    foreach ($line in ($text -split "`r?`n")) {
        if ($line.Trim() -ceq ([string]$Context.KernelLineWritten).Trim()) { return $true }
    }
    return $false
}

function Test-QtDmesgNoiseOnly {
    <#
    .SYNOPSIS
        W13:判断一段 dmesg 是否只含噪声白名单里的行(用于诊断包摘要,不参与成败判定)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Text)
    foreach ($line in ($Text -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $known = $false
        foreach ($p in $script:QtDmesgNoiseAllowlist) { if ($line -match $p) { $known = $true; break } }
        if (-not $known) { return $false }
    }
    return $true
}

Register-QtStepCheck -Step 'KERNEL_STAGED' -Check { param($ctx) Test-QtKernelStaged -Context $ctx }
Register-QtStepCheck -Step 'WSLCONFIG_WRITTEN' -Check { param($ctx) Test-QtWslConfigWritten -Context $ctx }

Export-ModuleMember -Function Get-QtKernelTimeouts, Get-QtBinderCheckCommand, Get-QtKCheckDistroName,
Get-QtKernelLine, Get-QtDmesgNoiseAllowlist, Test-QtBinderCheckOutput, Test-QtKernelVersionExact,
Test-QtKernelPreconditions, Write-QtKernelPointer, Set-QtKernelAcl, Import-QtKCheck, Remove-QtKCheck,
Invoke-QtWslShutdown, Invoke-QtKernelVerify, Invoke-QtKernelRollback, Resolve-QtKernelSwitchFailure, Save-QtKCheckDmesg,
Test-QtUnameLooksLikeVersion, Test-QtKernelStaged,
Test-QtWslConfigWritten, Test-QtDmesgNoiseOnly
