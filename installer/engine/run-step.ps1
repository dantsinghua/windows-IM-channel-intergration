# QTrade 安装引擎 —— 步骤派发器(Inno [Code] 的唯一后端入口)
# 用法:powershell.exe -NoProfile -ExecutionPolicy Bypass -File run-step.ps1 -Step <步名|动作> -OptionsJson <json>
# 契约:**stdout 最后一行**恒为一行 JSON `{"ok":bool,"state":"...","reason":"...","exit":int,"message":"...","data":{...}}`;
#       进程退出码 = docs/03 §3.4 表里的码(由 QTrade.Exit 统一映射)。Inno 侧只读这一行 + 退出码。
# 规格:docs/03 §2.3(状态机与幂等)、§2.12(可重入)、§3.1(日志)、§3.4(退出码)。
#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Step,
    [string] $OptionsJson = '{}',
    [string] $OptionsPath = '',
    [string] $Root = '',
    [string] $StageRoot = '',
    [string] $LogStamp = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$modulesDir = Join-Path $PSScriptRoot 'modules'
foreach ($m in @('QTrade.Log', 'QTrade.Native', 'QTrade.Exit', 'QTrade.State', 'QTrade.Payload',
        'QTrade.Preflight', 'QTrade.Wsl', 'QTrade.Kernel', 'QTrade.Distro', 'QTrade.WinAgent',
        'QTrade.Console', 'QTrade.WeChat', 'QTrade.Firewall', 'QTrade.Acl', 'QTrade.Selftest',
        'QTrade.Upgrade', 'QTrade.Diag', 'QTrade.Uninstall', 'QTrade.Stage')) {
    Import-Module (Join-Path $modulesDir ($m + '.psm1')) -DisableNameChecking
}

function Write-QtStepResult {
    param(
        [bool] $Ok,
        [string] $State = '',
        [string] $Reason = '',
        [string] $Message = '',
        $Data = $null,
        [string] $ExitName = 'OK'
    )
    $code = Get-QtExitCode -Name $ExitName
    $logMsg = ('reason={0} exit={1} message={2}' -f $Reason, $ExitName, $Message)
    try { Write-QtLog -Message $logMsg -Level $(if ($Ok) { 'INFO' } else { 'ERROR' }) -Step $(if ($State) { $State } else { 'ENGINE' }) } catch { }
    $payload = [ordered]@{
        ok      = $Ok
        state   = $State
        reason  = $Reason
        exit    = $code
        message = $Message
        data    = $Data
    }
    Write-Output ($payload | ConvertTo-Json -Depth 10 -Compress)
    exit $code
}

$paths = Get-QtPaths -Root $Root
$opt = @{}
try {
    if ($OptionsPath) {
        if (-not (Test-Path -LiteralPath $OptionsPath -PathType Leaf)) { throw 'Options file is missing.' }
        $OptionsJson = [IO.File]::ReadAllText($OptionsPath)
    }
    if ([string]::IsNullOrWhiteSpace($OptionsJson) -or -not $OptionsJson.TrimStart().StartsWith('{')) {
        throw 'Options must be a JSON object.'
    }
    $opt = ($OptionsJson | ConvertFrom-Json -ErrorAction Stop)
    if ($null -eq $opt -or $opt -isnot [pscustomobject]) { throw 'Options must be a JSON object.' }
} catch {
    Write-QtStepResult -Ok $false -State '' -Reason 'OPTIONS_INVALID' -ExitName 'E_INSTALL_INTERNAL' `
        -Message 'Installer options are missing or invalid. No installation state was changed.'
}

function Get-QtOpt {
    param([string] $Name, $Default = $null)
    if (Test-QtHasProperty -Object $opt -Name $Name) { return $opt.$Name }
    return $Default
}

function Remove-QtStepKCheck {
    # All step cleanup paths must consume a failed ownership check or unregister.
    $cleanup = Remove-QtKCheck -Directory $paths.KCheck
    if (-not $cleanup.Ok) {
        $failedStep = if ($Step -eq 'KERNEL_STAGED') { 'KERNEL_STAGED' } else { 'KERNEL_VERIFIED' }
        Set-QtState -State $state -To (New-QtFailedState -Step $failedStep -Reason 'KCHECK_IMPORT_FAILED') | Out-Null
        Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
        Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $cleanup.Reason `
            -ExitName 'E_INSTALL_KCHECK_IMPORT_FAILED' -Message 'The kernel check distribution could not be safely removed.' -Data $cleanup.Records
    }
}

# Initial validation and consent must not write into an existing installation.
if ($StageRoot) {
    try {
        $stageIdentity = Get-QtStageIdentity -StageRoot $StageRoot -LiveRoot $paths.Root
        $expectedStageParent = Join-Path (Get-QtEnvironmentPath -Name 'ProgramData') 'QTrade-Staging'
        if ((Split-Path -Parent $stageIdentity.StageRoot) -ne $expectedStageParent -or
            (Split-Path -Leaf $stageIdentity.StageRoot) -notmatch '\A[0-9a-fA-F-]{32,36}\z') {
            throw 'STAGE_LOCATION_INVALID'
        }
        $stageAcl = Set-QtInstallRootAcl -Path $stageIdentity.StageRoot
        if (-not $stageAcl.Ok) { throw 'STAGE_ACL_FAILED' }
        if ($Step -in @('bootstrap', 'PRECHECK', 'PAYLOAD_STAGED', 'clean-reinstall', 'wechat-detect', 'diag')) {
            $paths.StateFile = Join-Path $stageIdentity.StageRoot 'install\stage-state.json'
        }
        $paths.Logs = Join-Path $stageIdentity.StageRoot 'logs'
    } catch {
        Write-QtStepResult -Ok $false -Reason 'STAGE_INVALID' -ExitName 'E_INSTALL_INTERNAL' -Message $_.Exception.Message
    }
}

if (-not $StageRoot -and $Step -eq 'bootstrap') {
    $resumeState = Read-QtInstallState -Path $paths.StateFile
    $resumeMode = [string](Get-QtOpt 'mode' 'install')
    if ($resumeMode -in @('install', 'upgrade') -or $null -eq $resumeState) {
        Write-QtStepResult -Ok $false -Reason 'FULL_INSTALLER_REQUIRED' -ExitName 'E_INSTALL_WAIT_USER' `
            -Message 'Run the complete installer. An installed engine is only for resuming its own interrupted installation.'
    }
    if (-not (Test-QtHasProperty $resumeState 'env') -or
        -not (Test-QtHasProperty $resumeState.env 'install_user_sid') -or
        $resumeState.env.install_user_sid -ne (Get-QtCurrentUserSid)) {
        Write-QtStepResult -Ok $false -Reason 'INSTALL_OWNERSHIP_UNKNOWN' -ExitName 'E_INSTALL_WAIT_USER' `
            -Message 'The installation does not belong to the current Windows user.'
    }
    if ($resumeMode -in @('resume', 'verify-kernel')) {
        if ($resumeState.state -eq 'DONE' -or
            $resumeState.package_version -ne [string](Get-QtOpt 'package_version' '0.0.0') -or
            -not (Test-QtHasProperty $resumeState 'package_manifest_sha256') -or
            -not (Test-QtPath -Path $paths.Manifest) -or
            $resumeState.package_manifest_sha256 -ne (Get-QtFileHash -Path $paths.Manifest)) {
            Write-QtStepResult -Ok $false -Reason 'RESUME_IDENTITY_MISMATCH' -ExitName 'E_INSTALL_WAIT_USER' `
                -Message 'Only an unfinished installation from this exact package can be resumed.'
        }
    }
}

Initialize-QtLog -Directory $paths.Logs -Prefix 'install' -Stamp $LogStamp | Out-Null
Set-QtLogStep -Step $Step
Set-QtWslLogContext -Path (Get-QtSubLogPath -Name 'wsl') -Step $Step

# ── 载入或新建 install_state ────────────────────────────────────────────────
$state = Read-QtInstallState -Path $paths.StateFile
if ($null -eq $state) {
    $pkg = [string](Get-QtOpt -Name 'package_version' -Default '0.0.0')
    $state = New-QtInstallState -PackageVersion $pkg
    Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
}

try {
    switch ($Step) {

        # ── 引擎启动自检:互斥体 + 残留 kcheck 清理 + 决定从哪步续跑 ──────────
        'bootstrap' {
            # ⚠️ 并发保护(§2.12 的 `Global\QTradeSetup` 互斥体)**不在这里**做:
            #    本进程跑完这一步就退出,互斥体随之释放,等于没保护。
            #    它由长驻的引擎进程在 .iss 的 InitializeSetup 里持有(退出码 29)。
            # §2.6.6 不变量②:引擎启动时顺手清理残留 kcheck
            # Cleanup is owned by the verified clean-reinstall transaction.
            $next = Get-QtResumeStep -State ([string]$state.state)
            # §2.13 首句:新版本 EXE 检测到 `install_state.state=DONE` 且 `package_version <` 自身即进 upgrade 模式;
            # `>` 则拒绝降级(122)。这里只**建议**模式,真正切换由 .iss 决定。
            $selfVersion = [string](Get-QtOpt -Name 'package_version' -Default '0.0.0')
            $cmp = Compare-QtPackageVersion -Installed ([string]$state.package_version) -Package $selfVersion
            $recommended = 'install'
            if ([string]$state.state -eq 'DONE') {
                if ($cmp.Kind -eq 'UPGRADE') { $recommended = 'clean-reinstall' }
                elseif ($cmp.Kind -eq 'DOWNGRADE') { $recommended = 'downgrade-refused' }
                else { $recommended = 'clean-reinstall' }
            } elseif ([string]$state.state -ne 'PRECHECK') { $recommended = 'resume' }
            Write-QtLog -Message ('上次状态 {0}(更新于 {1});本次从 {2} 继续;建议模式 {3}' -f $state.state, $state.updated_at, $next, $recommended)
            Write-QtStepResult -Ok $true -State ([string]$state.state) -Message '引擎就绪' -Data ([ordered]@{
                    resume_step      = $next
                    parked           = $state.parked
                    log              = (Get-QtLogPath)
                    recommended_mode = $recommended
                    installed_version = [string]$state.package_version
                })
        }

        # ── PRECHECK(§2.4):只读,每次都重跑 ────────────────────────────────
        'PRECHECK' {
            $drive = [string](Get-QtOpt -Name 'target_drive' -Default ($paths.Root.Substring(0, 1)))
            $pre = Invoke-QtPreflight -TargetDriveLetter $drive -OurKernelSha256 ([string](Get-QtOpt -Name 'kernel_sha256' -Default ''))
            $state.env = [pscustomobject]$pre.Env
            if (-not $pre.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'PRECHECK' -Reason $pre.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $pre.Reason -ExitName ('E_INSTALL_' + $pre.Reason) -Message '预检未通过'
            }
            Set-QtState -State $state -To 'PRECHECK' -Note 'precheck ok' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'PRECHECK' -Message '预检通过' -Data ([ordered]@{ warnings = $pre.Warnings; env = $pre.Env })
        }

        # ── PAYLOAD_STAGED(§2.2.1 / §2.1):外壳已解压,这里只做 sha256 复核 ─
        'PAYLOAD_STAGED' {
            if ($StageRoot) {
                $manifestPath = Join-Path $StageRoot 'install\manifest.json'
                $manifest = Read-QtManifest -Path $manifestPath
                $v = Invoke-QtPayloadVerify -Manifest $manifest -StageRoot $StageRoot -DestinationRoot $paths.Root
                if (-not $v.Ok -or -not (Test-QtManifestCoredumpL2 -Manifest $manifest)) {
                    Write-QtStepResult -Ok $false -Reason 'PAYLOAD_CORRUPT' -ExitName 'E_INSTALL_PAYLOAD_CORRUPT' `
                        -Message 'Staged payload verification failed. The existing installation was not changed.'
                }
                Write-QtStepResult -Ok $true -State 'PAYLOAD_STAGED' -Message 'Staged payload verified.'
            }
            <#  🔴 裁决 11:收紧安装根 ACL —— 引擎落地后的**第一个**动作。
                位置在 sha256 复核**之前**、也在「已完成就跳过」之前,理由有三:
                  1. 安装根是引擎进程的当前目录(自编存根 SetCurrentDir + 子进程继承),
                     而当前目录在默认 DLL 搜索序列里。%ProgramData% 默认允许 Users 建文件,
                     等于给「以管理员跑的引擎」开了一条 DLL 植入路径 —— 越早关上越好;
                  2. 后面每一步都要往安装根里写状态和载荷,先收紧再写,不给中间窗口;
                  3. 放在跳过判断之前,是为了让**续跑**也走一遍 —— 它幂等(先读回比对,
                     已经对了就一个字节都不写),成本只有一次 Get-Acl。
                🔴 **失败即阻断**(总控 2026-09-20 裁决,退出码 31 E_INSTALL_ACL_HARDEN_FAILED):
                提权运行的引擎、以及随后以 LocalSystem 起的服务,工作目录都在安装根;
                这个目录能被普通用户写 = 一条现成的本地提权路径(当前目录在默认 DLL 搜索序列里)。
                带病装完比装不上更糟 —— 用户会以为自己装好了。
                失败前先把结果落进 install_state(acl_hardened / acl_problems),诊断包才带得走。#>
            $aclRes = Set-QtInstallRootAcl -Path $paths.Root
            Set-QtAclHardened -State $state -Result $aclRes | Out-Null
            if ($aclRes.Ok) {
                Write-QtLog -Level 'INFO' -Message ('安装根 ACL {0}:{1}' -f $(if ($aclRes.Changed) { '已收紧' } else { '本就合规' }), $paths.Root)
            } else {
                Write-QtLog -Level 'ERROR' -Message ('🔴 安装根 ACL 收紧失败({0}):{1}' -f $paths.Root, ($aclRes.Problems -join '; '))
                Set-QtState -State $state -To (New-QtFailedState -Step 'PAYLOAD_STAGED' -Reason 'ACL_HARDEN_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'ACL_HARDEN_FAILED' -ExitName 'E_INSTALL_ACL_HARDEN_FAILED' `
                    -Message ('安装目录权限收紧失败,安装已中止。请确认以管理员身份运行,且该目录未被组策略或安全软件锁定:{0}' -f $paths.Root) `
                    -Data ([ordered]@{ acl_hardened = $false; acl_problems = @($aclRes.Problems); path = $paths.Root })
            }

            $ctx = [pscustomobject]@{ ManifestPath = $paths.Manifest; StageRoot = $paths.Root }
            if (Test-QtStepComplete -Step 'PAYLOAD_STAGED' -Context $ctx) {
                Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                Set-QtState -State $state -To 'PAYLOAD_STAGED' -Note 'skip' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'PAYLOAD_STAGED' -Message '载荷已校验(跳过)' -Data ([ordered]@{ acl_hardened = [bool]$aclRes.Ok; acl_problems = @($aclRes.Problems) })
            }
            $manifest = Read-QtManifest -Path $paths.Manifest
            if (-not (Test-QtManifestCoredumpL2 -Manifest $manifest)) {
                Write-QtLog -Level 'ERROR' -Message 'manifest 的 coredump_l2 不是 "D"(R6-38:只认方案 D),这份包不是合法交付形态'
                Set-QtState -State $state -To (New-QtFailedState -Step 'PAYLOAD_STAGED' -Reason 'PAYLOAD_CORRUPT') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'PAYLOAD_CORRUPT' -ExitName 'E_INSTALL_PAYLOAD_CORRUPT' -Message '安装包损坏(内核 coredump_l2 标记不合法)'
            }
            $v = Invoke-QtPayloadVerify -Manifest $manifest -StageRoot $paths.Root
            if (-not $v.Ok) {
                $removed = Clear-QtInconsistentPayload -VerifyResult $v
                Write-QtLog -Level 'ERROR' -Message ('载荷校验失败:缺 {0} 个、不一致 {1} 个;已清理 {2} 个不一致文件' -f $v.Missing.Count, $v.Mismatched.Count, $removed.Count)
                Set-QtState -State $state -To (New-QtFailedState -Step 'PAYLOAD_STAGED' -Reason 'PAYLOAD_CORRUPT') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                $bad = @($v.CriticalFailed | ForEach-Object { $_.path })
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'PAYLOAD_CORRUPT' -ExitName 'E_INSTALL_PAYLOAD_CORRUPT' `
                    -Message ('安装包损坏({0}),请重新获取安装包' -f ($bad -join ', ')) -Data ([ordered]@{ missing = @($v.Missing | ForEach-Object { $_.path }); mismatched = @($v.Mismatched | ForEach-Object { $_.path }) })
            }
            Set-QtState -State $state -To 'PAYLOAD_STAGED' -Note ('verified {0}' -f $v.VerifiedCount) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'PAYLOAD_STAGED' -Message ('已校验 {0} 个文件' -f $v.VerifiedCount) -Data ([ordered]@{ acl_hardened = [bool]$aclRes.Ok; acl_problems = @($aclRes.Problems) })
        }

        # ── WSL_FEATURE(§2.5.1)──────────────────────────────────────────────
        'WSL_FEATURE' {
            if (Test-QtWslFeaturesEnabled) {
                Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                Set-QtState -State $state -To 'WSL_FEATURE' -Note 'skip' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'WSL_FEATURE' -Message 'Windows 组件已启用(跳过)'
            }
            $r = Enable-QtWslFeatures
            if (-not $r.Ok) {
                $reason = 'FEATURE_ENABLE_FAILED'
                if ($r.HResult -in @('0x800F0954', '0x800F081F')) { $reason = 'POLICY_BLOCKED' }
                Set-QtState -State $state -To (New-QtFailedState -Step 'WSL_FEATURE' -Reason $reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $reason -ExitName ('E_INSTALL_' + $reason) -Message ('启用 Windows 组件失败({0})' -f $r.HResult)
            }
            if ($r.RestartNeeded) {
                Set-QtRunOnce -State $state -EnginePath $paths.EngineExe -Mode 'resume' | Out-Null
                Set-QtState -State $state -To 'REBOOT_PENDING' -Note 'features need restart' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Install-QtResumeShortcut -EnginePath $paths.EngineExe | Out-Null
                Write-QtStepResult -Ok $true -State 'REBOOT_PENDING' -ExitName 'E_INSTALL_REBOOT_REQUIRED' `
                    -Message 'Windows 组件已启用,需要重启一次。重启并登录后安装会自动继续(会弹出一次权限确认)。'
            }
            Set-QtState -State $state -To 'WSL_FEATURE' -Note 'enabled' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'WSL_FEATURE' -Message 'Windows 组件已启用'
        }

        # ── WSL_MSI(§2.5.2)──────────────────────────────────────────────────
        'WSL_MSI' {
            $ver = Invoke-QtWsl -WslArgs @('--version') -TimeoutSec 30
            $cur = Get-QtWslVersionString -Output $ver.StdOut
            $bundled = [string](Get-QtOpt -Name 'wsl_msi_version' -Default '2.6.1')
            if ($cur -and ([version]$cur -ge [version]$bundled)) {
                Set-QtSubstate -State $state -Substate 'skip' | Out-Null   # 只升不降
                Set-QtState -State $state -To 'WSL_MSI' -Note ('skip, current ' + $cur) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'WSL_MSI' -Message ('WSL {0}(已是新版,不动)' -f $cur)
            }
            $msi = Join-Path $paths.Wsl 'wsl.msi'
            $log = Get-QtSubLogPath -Name 'msiexec'
            $r = Install-QtWslMsi -MsiPath $msi -LogPath $log
            if (-not $r.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WSL_MSI' -Reason 'WSL_MSI_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WSL_MSI_FAILED' -ExitName 'E_INSTALL_WSL_MSI_FAILED' -Message ('WSL 安装失败,日志:{0}' -f $log)
            }
            if ($r.RebootRequired) {
                Set-QtRunOnce -State $state -EnginePath $paths.EngineExe -Mode 'resume' | Out-Null
                Set-QtState -State $state -To 'REBOOT_PENDING' -Note 'msi 3010' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'REBOOT_PENDING' -ExitName 'E_INSTALL_REBOOT_REQUIRED' -Message 'WSL 安装完成,需要重启一次'
            }
            Invoke-QtWsl -WslArgs @('--set-default-version', '2') -TimeoutSec 60 | Out-Null
            $ver2 = Invoke-QtWsl -WslArgs @('--version') -TimeoutSec 30
            $now = Get-QtWslVersionString -Output $ver2.StdOut
            if (-not (Test-QtWslVersionAtLeast -Version $now)) {
                $reason = 'WSL_BROKEN'
                Set-QtState -State $state -To (New-QtFailedState -Step 'WSL_MSI' -Reason $reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $reason -ExitName 'E_INSTALL_WSL_BROKEN' `
                    -Message ('WSL 不可用({0})' -f (Get-QtWslBrokenReason -Output ($ver2.StdOut + $ver2.StdErr)))
            }
            $state.env.wsl_version = $now
            Set-QtState -State $state -To 'WSL_MSI' -Note ('installed ' + $now) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'WSL_MSI' -Message ('WSL {0}' -f $now)
        }

        # ── KERNEL_STAGED(§2.6.1):落盘 + ACL + 指针 + 官方内核上预导入 kcheck ─
        'KERNEL_STAGED' {
            $manifest = Read-QtManifest -Path $paths.Manifest
            $k = Get-QtManifestKernel -Manifest $manifest -Line (Get-QtKernelLine)
            $kernelPath = Join-Path $paths.Kernel ('bzImage-{0}' -f (Get-QtKernelLine))
            if (-not (Test-QtPath -Path $kernelPath) -or (Get-QtFileHash -Path $kernelPath) -ne ([string]$k.sha256).ToLowerInvariant()) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_STAGED' -Reason 'KERNEL_SHA_MISMATCH') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'KERNEL_SHA_MISMATCH' -ExitName 'E_INSTALL_KERNEL_SHA_MISMATCH' -Message '内核文件校验失败,请重新获取安装包'
            }
            # 🔴 §2.6.1 内核文件 ACL:写完读回复核,不过就按本步既有的失败路径中止。
            #    内核文件被普通用户替换 = 任意内核代码执行,静默失效不可接受。
            $kAcl = Set-QtKernelAcl -Path $kernelPath
            if (-not $kAcl.Ok) {
                Write-QtLog -Level 'ERROR' -Message ('🔴 内核文件 ACL 收紧失败({0}):{1}' -f $kernelPath, ($kAcl.Problems -join '; '))
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_STAGED' -Reason 'ACL_HARDEN_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'ACL_HARDEN_FAILED' -ExitName 'E_INSTALL_ACL_HARDEN_FAILED' `
                    -Message ('内核文件权限收紧失败,安装已中止。请确认以管理员身份运行,且该文件未被安全软件锁定:{0}' -f $kernelPath) `
                    -Data ([ordered]@{ acl_problems = @($kAcl.Problems); path = $kernelPath })
            }
            $imp = Import-QtKCheck -Directory $paths.KCheck -TarPath (Join-Path $paths.Wsl 'kcheck-rootfs.tar')
            if (-not $imp.Ok) {
                Remove-QtStepKCheck
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_STAGED' -Reason 'KCHECK_IMPORT_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'KCHECK_IMPORT_FAILED' -ExitName 'E_INSTALL_KCHECK_IMPORT_FAILED' -Message 'qtrade-kcheck import failed; the kernel configuration was not changed.' -Data $imp.Records
            }
            $baseline = Test-QtKCheckBaseline
            if (-not $baseline.Ok) {
                Remove-QtStepKCheck
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_STAGED' -Reason $baseline.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $baseline.Reason -ExitName ('E_INSTALL_' + $baseline.Reason) `
                    -Message 'qtrade-kcheck could not start under the current kernel; kernel configuration and WSL shutdown were not changed.' -Data $baseline.Records
            }
            Write-QtKernelPointer -Path $paths.KernelPtr -KernelPath $kernelPath -Sha256 ([string]$k.sha256) -Version ([string]$k.version) -Line (Get-QtKernelLine) | Out-Null
            Set-QtState -State $state -To 'KERNEL_STAGED' -Note ('kernel ' + $k.version) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'KERNEL_STAGED' -Message ('内核已就位:{0}' -f $k.version) -Data ([ordered]@{ kernel_path = $kernelPath; version = [string]$k.version; sha256 = [string]$k.sha256 })
        }

        # ── WSLCONFIG_WRITTEN + KERNEL_VERIFIED:§2.6.3 的**一个原子段** ──────
        # 🔴 红线 6:配置只在用户按下【现在切换】(或 /QT_ACCEPT_SHUTDOWN=1)那一刻才写入,紧接着 shutdown、验证、回滚。
        'KERNEL_SWITCH' {
            $confirmed = [bool](Get-QtOpt -Name 'accept_shutdown' -Default $false)
            if (-not $confirmed) {
                Set-QtParked -State $state -Step 'WSLCONFIG_WRITTEN' -Reason 'WAIT_SHUTDOWN_CONFIRM' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Install-QtResumeShortcut -EnginePath $paths.EngineExe | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WAIT_SHUTDOWN_CONFIRM' -ExitName 'E_INSTALL_WAIT_USER' `
                    -Message '等待用户确认切换内核的时机(静默模式需 /QT_ACCEPT_SHUTDOWN=1)'
            }

            $manifest = Read-QtManifest -Path $paths.Manifest
            $k = Get-QtManifestKernel -Manifest $manifest -Line (Get-QtKernelLine)
            $kernelPath = Join-Path $paths.Kernel ('bzImage-{0}' -f (Get-QtKernelLine))
            $wslConfig = [string]$state.env.wslconfig_path
            if (-not $wslConfig) { $wslConfig = Join-Path (Get-QtEnvironmentPath -Name 'UserProfile') '.wslconfig' }
            $policy = Get-QtWslPolicyFacts

            # 三道前置校验(§2.6.8 第 9 条):任一不过就**不写、不 shutdown**
            $pre = Test-QtKernelPreconditions -KernelPath $kernelPath -ExpectedSha256 ([string]$k.sha256) `
                -WslConfigPath $wslConfig -CustomKernelForbidden $policy.CustomKernelForbidden
            if (-not $pre.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WSLCONFIG_WRITTEN' -Reason $pre.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $pre.Reason -ExitName ('E_INSTALL_' + $pre.Reason) -Message '写入 .wslconfig 的前置校验未通过,未做任何改动'
            }

            # R6-82: recheck a resumed stage before changing the kernel configuration.
            $baseline = Test-QtKCheckBaseline
            if (-not $baseline.Ok) {
                Remove-QtStepKCheck
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_STAGED' -Reason $baseline.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $baseline.Reason -ExitName ('E_INSTALL_' + $baseline.Reason) `
                    -Message 'qtrade-kcheck startup preflight failed; kernel configuration and WSL shutdown were not changed.' -Data $baseline.Records
            }

            # Set the resume marker before writing the configuration.
            Set-QtSubstate -State $state -Substate 'wslconfig_writing' | Out-Null
            Set-QtRunOnce -State $state -EnginePath $paths.EngineExe -Mode 'verify-kernel' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null

            $backup = Backup-QtWslConfig -Path $wslConfig -BackupDir $paths.Wsl
            $text = ''
            if (Test-QtPath -Path $wslConfig) { $text = Read-QtTextFile -Path $wslConfig }
            $memWanted = [string](Get-QtOpt -Name 'wsl_memory' -Default '')
            if (-not $memWanted) {
                $memWanted = Get-QtMemoryByPhysical -PhysicalMB ([int]$state.env.mem_total_mb) -WeChatOn:([bool](Get-QtOpt -Name 'wechat_on' -Default $false))
            }
            $desired = @{
                kernel            = $kernelPath
                memory            = $memWanted
                maxCrashDumpCount = [string](Get-QtOpt -Name 'crash_dumps' -Default '2')
            }
            $dumpDir = [string](Get-QtOpt -Name 'crash_dump_dir' -Default '')
            if ($dumpDir) { $desired['crashDumpFolder'] = ($dumpDir -replace '\\', '\\') }

            $adjust = if ([bool](Get-QtOpt -Name 'keep_wsl_memory' -Default $false)) { 'never' } else { 'raise_if_below' }
            $merged = Merge-QtWslConfig -Text $text -Desired $desired -KernelState ([string]$state.env.kernel_state) `
                -MemoryAdjust $adjust -ReplaceOtherKernel:([bool](Get-QtOpt -Name 'replace_other_kernel' -Default $false))
            Write-QtUtf8NoBom -Path $wslConfig -Text $merged.Text | Out-Null

            $state.wslconfig = [pscustomobject]@{
                backup              = $backup
                keys_added          = $merged.KeysAdded
                kept                = $merged.Kept
                changed             = $merged.Changed
                replaced_kernel     = $merged.ReplacedKernel
                kernel_line_written = $merged.KernelLineWritten
            }
            Set-QtState -State $state -To 'WSLCONFIG_WRITTEN' -Note 'merged' | Out-Null
            Set-QtSubstate -State $state -Substate 'wslconfig_writing' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null

            # 验证段
            $userDistros = @($state.env.distros | Where-Object { $_.version -eq 2 -and $_.name -notlike 'docker-*' -and $_.name -ne 'qtrade' } | ForEach-Object { $_.name })
            $ver = Invoke-QtKernelVerify -ManifestVersion ([string]$k.version) -ShutdownConfirmed $true `
                -UserDistros $userDistros -CustomKernelPolicyPresent $policy.CustomKernelForbidden

            if (-not $ver.Ok) {
                if ($ver.Reason -in @('KERNEL_SHUTDOWN_TIMEOUT', 'KERNEL_SHUTDOWN_FAILED')) {
                    # 🔴 不回滚(回滚也要 shutdown,同样会挂);配置已写、RunOnce 已指 verify-kernel
                    Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason $ver.Reason) | Out-Null
                    Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $ver.Reason -ExitName ('E_INSTALL_' + $ver.Reason) `
                        -Message ('WSL shutdown did not succeed ({0}); verification is parked for resume after a restart.' -f $ver.Reason) -Data $ver.Records
                }
                # 预演 #16:回滚会注销 kcheck ⇒ **先**把 QTrade 内核下的 dmesg 落进日志目录,诊断包再带上
                $dmesgFile = Save-QtKCheckDmesg -Directory $paths.Logs -Phase ('KERNEL_SWITCH ' + $ver.Reason)
                Write-QtLog -Message ('注销 kcheck 前的 dmesg:{0}' -f $(if ($dmesgFile) { $dmesgFile } else { '(未能写入)' }))
                # 内核自身失败 → 自动回滚(§2.6.5)
                $rb = Invoke-QtKernelRollback -WslConfigPath $wslConfig -ShutdownConfirmed $true -KCheckDirectory $paths.KCheck
                Clear-QtRunOnce -State $state | Out-Null
                $note = if ([string]::IsNullOrWhiteSpace([string]$rb.OfficialKernel)) {
                    'rollback: 原装内核复验未通过'
                } else {
                    'rolled back to ' + $rb.OfficialKernel
                }
                # Normal and resume failures use the same evidence-based classification.
                $fail = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
                Set-QtState -State $state -To 'KERNEL_ROLLED_BACK' -Note $note | Out-Null
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason $fail.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $fail.Reason -ExitName $fail.ExitName `
                    -Message $fail.Message -Data ([ordered]@{ official_kernel = $rb.OfficialKernel; verify_reason = $ver.Reason; dmesg = $dmesgFile; records = $ver.Records; rollback_records = $rb.Records })
            }

            Remove-QtStepKCheck
            Clear-QtRunOnce -State $state | Out-Null
            Write-QtKernelPointer -Path $paths.KernelPtr -KernelPath $kernelPath -Sha256 ([string]$k.sha256) `
                -Version ([string]$k.version) -Line (Get-QtKernelLine) -VerifiedAt (Get-QtIso8601) | Out-Null
            if ($ver.UserDistroFailures.Count -gt 0) {
                # B-4:用户发行版起不来**只报不回滚**,安装继续
                $state.env | Add-Member -NotePropertyName 'user_distro_failures' -NotePropertyValue $ver.UserDistroFailures -Force
            }
            Set-QtState -State $state -To 'KERNEL_VERIFIED' -Note ('uname ' + $ver.Uname) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'KERNEL_VERIFIED' -Message ('内核 {0} 已生效,binder 可用' -f $ver.Uname) `
                -Data ([ordered]@{ uname = $ver.Uname; user_distro_failures = $ver.UserDistroFailures; records = $ver.Records })
        }

        # ── /QT_MODE=verify-kernel 的自愈入口(§5.2)────────────────────────
        'verify-kernel' {
            $manifest = Read-QtManifest -Path $paths.Manifest
            $k = Get-QtManifestKernel -Manifest $manifest -Line (Get-QtKernelLine)
            $wslConfig = [string]$state.env.wslconfig_path
            $policy = Get-QtWslPolicyFacts
            # 断电保险后续跑:kcheck 可能已被清,先在当前内核上补导一次
            $imp = Import-QtKCheck -Directory $paths.KCheck -TarPath (Join-Path $paths.Wsl 'kcheck-rootfs.tar')
            if (-not $imp.Ok) {
                Remove-QtStepKCheck
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason $imp.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $imp.Reason -ExitName ('E_INSTALL_' + $imp.Reason) `
                    -Message 'qtrade-kcheck import failed during resume; kernel verification did not run.' -Data $imp.Records
            }
            $ver = Invoke-QtKernelVerify -ManifestVersion ([string]$k.version) -ShutdownConfirmed $true `
                -CustomKernelPolicyPresent $policy.CustomKernelForbidden -SkipShutdown
            if ($ver.Ok) {
                Remove-QtStepKCheck
                Clear-QtRunOnce -State $state | Out-Null
                Set-QtState -State $state -To 'KERNEL_VERIFIED' -Note 'verify-kernel resume' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'KERNEL_VERIFIED' -Message ('内核 {0} 已生效' -f $ver.Uname)
            }
            # 同 KERNEL_SWITCH:回滚会注销 kcheck,先抓 dmesg
            $dmesgFile = Save-QtKCheckDmesg -Directory $paths.Logs -Phase ('verify-kernel ' + $ver.Reason)
            $rb = Invoke-QtKernelRollback -WslConfigPath $wslConfig -ShutdownConfirmed $true -KCheckDirectory $paths.KCheck
            Clear-QtRunOnce -State $state | Out-Null
            $fail = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
            Set-QtState -State $state -To 'KERNEL_ROLLED_BACK' -Note 'verify-kernel resume' | Out-Null
            Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason $fail.Reason) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $fail.Reason -ExitName $fail.ExitName -Message $fail.Message `
                -Data ([ordered]@{ official_kernel = $rb.OfficialKernel; verify_reason = $ver.Reason; dmesg = $dmesgFile; records = $ver.Records; rollback_records = $rb.Records })
        }

        # ── DISTRO_IMPORTED(§2.7)────────────────────────────────────────────
        'DISTRO_IMPORTED' {
            $name = [string]$state.distro.name
            $tar = Join-Path $paths.Wsl 'rootfs.tar'
            $manifest = Read-QtManifest -Path $paths.Manifest
            $rootfsVersion = ''
            $rf = @($manifest.files | Where-Object { ([string]$_.path) -eq 'wsl/rootfs.tar' })
            if ($rf.Count -gt 0 -and (Test-QtHasProperty -Object $rf[0] -Name 'version')) { $rootfsVersion = [string]$rf[0].version }

            # An interrupted import is resumed only from this package and this install user.
            $listed = Invoke-QtWsl -WslArgs @('--list', '--quiet') -TimeoutSec 60
            if ($listed.TimedOut -or $listed.ExitCode -ne 0) {
                Write-QtStepResult -Ok $false -Reason 'IMPORT_FAILED' -ExitName 'E_INSTALL_IMPORT_FAILED' `
                    -Message 'Could not enumerate WSL distributions. No import was attempted.'
            }
            $exists = ($listed.StdOut -match ('(?m)^\s*' + [regex]::Escape($name) + '\s*$'))
            if ($exists) {
                $allDistros = Get-QtRegisteredDistro
                $reg = @($allDistros | Where-Object { $_.name -eq $name })
                $basePath = ''
                if ($reg.Count -eq 1) {
                    $basePath = ([string]$reg[0].base_path).TrimEnd('\')
                    if ($basePath.StartsWith('\\?\')) { $basePath = $basePath.Substring(4) }
                }
                $ours = $reg.Count -eq 1 -and $basePath -eq $paths.Distro -and
                    $state.distro.dir -eq $paths.Distro -and $state.distro.imported_at -and
                    $state.env.install_user_sid -eq (Get-QtCurrentUserSid) -and
                    (Test-QtHasProperty $state 'package_manifest_sha256') -and
                    $state.package_manifest_sha256 -eq (Get-QtFileHash -Path $paths.Manifest)
                if (-not $ours) {
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'DISTRO_NAME_CONFLICT_DECLINED' `
                        -ExitName 'E_INSTALL_DISTRO_NAME_CONFLICT_DECLINED' `
                        -Message 'The existing WSL distribution is not owned by this installation. No distribution was removed.'
                }
            } else {
                $imp = Import-QtDistro -Name $name -Directory $paths.Distro -TarPath $tar
                if (-not $imp.Ok) {
                    $reason = $imp.Reason
                    Set-QtState -State $state -To (New-QtFailedState -Step 'DISTRO_IMPORTED' -Reason $reason) | Out-Null
                    Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $reason -ExitName ('E_INSTALL_' + $reason) -Message 'WSL import failed.'
                }
                # Persist ownership before any guest bootstrap that may fail or time out.
                $state.distro.dir = $paths.Distro
                $state.distro.imported_at = Get-QtIso8601
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            }
            $boot = Initialize-QtDistroBootstrap -Name $name -InstallEnvPath (Join-Path $paths.Install 'install.env') `
                -PackageVersion ([string]$state.package_version) `
                -DockerCidr ([string](Get-QtOpt 'docker_cidr' '')) -ApkUrl ([string](Get-QtOpt 'apk_url' '')) `
                -Candidates @(Get-QtOpt 'docker_pool_candidates' @('10.213.0.0/16', '10.231.0.0/16', '10.247.0.0/16', '10.199.0.0/16')) `
                -OccupiedPrefixes @(Get-QtOpt 'occupied_prefixes' @())
            if (-not $boot.Ok) {
                $bootExit = switch ([string]$boot.Reason) {
                    'DOCKER_CIDR_EXHAUSTED' { 'E_INSTALL_DOCKER_CIDR_EXHAUSTED' }
                    'SYSTEMD_NOT_READY' { 'E_INSTALL_SYSTEMD_NOT_READY' }
                    default { 'E_INSTALL_INTERNAL' }
                }
                $bootStateReason = $bootExit -replace '^E_INSTALL_', ''
                Set-QtState -State $state -To (New-QtFailedState -Step 'DISTRO_IMPORTED' -Reason $bootStateReason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $boot.Reason `
                    -ExitName $bootExit -Message 'Guest bootstrap failed. Product services remain gated.'
            }
            $cidr = [string]$boot.Cidr
            $sysd = $boot.Systemd
            $state.env | Add-Member -NotePropertyName docker_cidr -NotePropertyValue $cidr -Force
            Set-QtState -State $state -To 'DISTRO_IMPORTED' -Note ('docker_cidr ' + $cidr) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'DISTRO_IMPORTED' -Message ('qtrade 已导入(docker 网段 {0})' -f $cidr) -Data ([ordered]@{ docker_cidr = $cidr; systemd = $sysd.Status })
        }

        # ── WINAGENT_INSTALLED(§2.8.3)───────────────────────────────────────
        'WINAGENT_INSTALLED' {
            $vc = Install-QtVcRedist -ExePath (Join-Path $paths.Pkg 'vcredist\VC_redist.x64.exe')
            if (-not $vc.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason 'VCREDIST_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'VCREDIST_FAILED' -ExitName 'E_INSTALL_VCREDIST_FAILED' -Message 'VC++ 运行库安装失败'
            }
            $names = Get-QtWinAgentNames
            $svcExe = Join-Path $paths.WinAgent ('app\' + $names.svc_exe)
            $svc = Install-QtWinAgentService -ExePath $svcExe -Name $names.service_name -DisplayName $names.display_name `
                -Root $paths.WinAgent -InstallUserSid ([string]$state.env.install_user_sid)
            if (-not $svc.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason 'SERVICE_INSTALL_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'SERVICE_INSTALL_FAILED' -ExitName 'E_INSTALL_SERVICE_INSTALL_FAILED' -Message ('服务注册失败')
            }
            Register-QtWinAgentUserTask -UserExePath (Join-Path $paths.WinAgent ('app\' + $names.user_exe)) `
                -InstallUserSid ([string]$state.env.install_user_sid) -StartNow | Out-Null
            Invoke-QtSc -ScArgs @('start', $names.service_name) | Out-Null
            $tokens = Get-QtBootstrapTokens -TimeoutSec 30
            if (-not $tokens.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason $tokens.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $tokens.Reason -ExitName 'E_INSTALL_WINAGENT_NOT_READY' `
                    -Message 'Local bootstrap credentials could not be obtained.'
            }
            $h = Wait-QtWinAgentHealthy -BaseUrl $names.base_url -Token $tokens.ConsoleToken -TimeoutSec 30
            $healthCause = if ((Test-QtHasProperty -Object $h -Name 'Cause') -and $h.Cause) { [string]$h.Cause } else { 'WINAGENT_NOT_READY' }
            if (-not $h.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason $healthCause) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $healthCause -ExitName 'E_INSTALL_WINAGENT_NOT_READY' -Message ('WinAgent health check failed: '+$healthCause)
            }
            $waToken = Write-QtWinAgentToken -Token $tokens.WinAgentToken -DistroName ([string]$state.distro.name)
            if (-not $waToken.Ok -or -not (Set-QtAgentConsoleToken -Token $tokens.ConsoleToken -DistroName ([string]$state.distro.name))) {
                Write-QtStepResult -Ok $false -Reason 'TOKEN_BOOTSTRAP_FAILED' -ExitName 'E_INSTALL_WINAGENT_NOT_READY' `
                    -Message 'Credential binding failed. Product firstboot was not started.'
            }
            $firstboot = Start-QtDistroFirstboot -Name ([string]$state.distro.name) `
                -PackageVersion ([string]$state.package_version) -TimeoutSec 1800
            if (-not $firstboot.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason $firstboot.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $firstboot.Reason `
                    -ExitName 'E_INSTALL_AGENT_NOT_READY' -Message 'Product firstboot did not complete.'
            }
            $fw = Invoke-QtFirewallEnsure -BaseUrl $names.base_url -Token $tokens.ConsoleToken
            if (-not $fw.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason 'WINAGENT_NOT_READY') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $fw.Reason `
                    -ExitName 'E_INSTALL_WINAGENT_NOT_READY' -Message $fw.Message
            }
            $state.firewall_rules = [string]$fw.Action
            if ($fw.Blocked) { Write-QtLog -Level 'WARN' -Message $fw.Message }
            Set-QtState -State $state -To 'WINAGENT_INSTALLED' -Note ('firewall ' + $fw.Action) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            # 🔴 R3-16:user_agent:false 属正常态,不判失败
            $msg = if ($h.UserAgent) { '服务已启动;会话代理已上线' } else { '服务已启动。首次使用需登录 Windows 桌面(可锁屏,不要注销),账号才会运行' }
            if ($fw.Blocked) { $msg += (' ' + $fw.Message) }
            Write-QtStepResult -Ok $true -State 'WINAGENT_INSTALLED' -Message $msg -Data ([ordered]@{ user_agent = $h.UserAgent; firewall = $fw.Action; firewall_blocked = $fw.Blocked; firewall_message = $fw.Message })
        }

        # ── 卸载 `/QT_MODE=uninstall`(§2.14)────────────────────────────────
        'uninstall' {
            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'UNINSTALL_DISABLED' `
                -ExitName 'E_INSTALL_WAIT_USER' -Message 'This legacy uninstall entry is disabled. Run the complete installer for verified clean reinstall.'
        }

        # ── IMAGES_LOADED(§2.7.3 末 / §2.2.1 G-10)──────────────────────────
        'IMAGES_LOADED' {
            $manifest = Read-QtManifest -Path $paths.Manifest
            $name = [string]$state.distro.name
            $expected = Get-QtExpectedImageRefs -Manifest $manifest
            # Re-entry repeats read-only integrity and network checks; it never reloads images.
            # 首启 oneshot 由发行版内的 systemd 做(§2.7.3 双 unit);引擎只等结果、只判判据。
            $present = Get-QtDockerImages -DistroName $name
            if ($present.Count -eq 0) {
                # docker 起不来:§2.7.3 末「这是成品脚本第 5 步的判定点」——
                # 若此处失败且 kcheck 曾通过,原因码带 after_kernel_switch=true,向导给【回滚内核】
                $afterSwitch = ([string]$state.env.kernel_state -in @('OURS', 'OURS_STALE'))
                Set-QtState -State $state -To (New-QtFailedState -Step 'IMAGES_LOADED' -Reason 'DOCKER_NOT_READY') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'DOCKER_NOT_READY' -ExitName 'E_INSTALL_DOCKER_NOT_READY' `
                    -Message 'docker 在发行版里没有就绪' -Data ([ordered]@{ after_kernel_switch = $afterSwitch })
            }
            if (-not (Test-QtImageRefsPresent -Present $present -Expected $expected)) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'IMAGES_LOADED' -Reason 'IMAGE_LOAD_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'IMAGE_LOAD_FAILED' -ExitName 'E_INSTALL_IMAGE_LOAD_FAILED' `
                    -Message '预载镜像未全部加载' -Data ([ordered]@{ expected = $expected; present = $present })
            }
            # G-10:rootfs_contents 逐项复核(sha256 / digest),不一致附不一致项
            $verify = Invoke-QtRootfsContentsVerify -Manifest $manifest -DistroName $name
            if (-not $verify.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'IMAGES_LOADED' -Reason 'IMAGE_LOAD_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'IMAGE_LOAD_FAILED' -ExitName 'E_INSTALL_IMAGE_LOAD_FAILED' `
                    -Message 'rootfs 内容校验不一致' -Data ([ordered]@{ mismatched = $verify.Mismatched })
            }
            if (-not (Test-QtAgentActive -DistroName $name)) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'IMAGES_LOADED' -Reason 'AGENT_NOT_READY') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'AGENT_NOT_READY' -ExitName 'E_INSTALL_AGENT_NOT_READY' -Message 'qtrade-agent 未启动'
            }
            # 验收 M1-12(R5-9):bridge 子网必须落在选定段内且不是 172.17 —— 选段真的在 dockerd 首启前生效了
            $bridge = Get-QtDockerBridgeSubnet -DistroName $name
            $poolOk = Test-QtDockerPoolApplied -BridgeSubnet $bridge -SelectedCidr ([string]$state.env.docker_cidr)
            if (-not $poolOk) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'IMAGES_LOADED' -Reason 'DOCKER_NOT_READY') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'DOCKER_NOT_READY' `
                    -ExitName 'E_INSTALL_DOCKER_NOT_READY' -Message 'Docker bridge does not match the selected installation network.' `
                    -Data ([ordered]@{ bridge_subnet = $bridge; docker_pool_applied = $false })
            }
            Set-QtState -State $state -To 'IMAGES_LOADED' -Note ('images ok, bridge ' + $bridge) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'IMAGES_LOADED' -Message 'docker 与 Agent 就绪' `
                -Data ([ordered]@{ images = $present; bridge_subnet = $bridge; docker_pool_applied = $poolOk })
        }

        # ── CLIENTS_CHECKED(§2.9)────────────────────────────────────────────
        'CLIENTS_CHECKED' {
            $ctx = [pscustomobject]@{ Clients = $state.clients }
            if (Test-QtStepComplete -Step 'CLIENTS_CHECKED' -Context $ctx) {
                Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                Set-QtState -State $state -To 'CLIENTS_CHECKED' -Note 'skip' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'CLIENTS_CHECKED' -Message '客户端检查已完成(跳过)'
            }

            $silent = [bool](Get-QtOpt -Name 'silent' -Default $false)
            $mode = Resolve-QtWeChatMode -Requested ([string](Get-QtOpt -Name 'wechat' -Default 'check')) -Silent $silent
            $approved = [bool](Get-QtOpt -Name 'wechat_user_approved' -Default $false)
            $backupMode = [string](Get-QtOpt -Name 'wechat_backup_mode' -Default 'auto')

            $installs = Get-QtWeChatInstalls
            $dataRoot = Get-QtWeChatDataRoot
            $details = Get-QtWeChatInstallDetails -Installs $installs -DataRoot $dataRoot.DataRoot
            $version = ''
            $ambiguous = $false
            $installPath = ''
            $uninstallString = ''
            $selectedMs = 0

            # §2.9.3 末:MULTIPLE_INSTALLS 时用户选**一处**作为 QTrade 使用的微信;其余**不卸、不改**。
            # 向导把选中的路径经 `wechat_selected_path` 传回来;没选就用第一处(单安装时就是它)。
            $selected = [string](Get-QtOpt -Name 'wechat_selected_path' -Default '')
            $pick = $null
            if ($selected) {
                $hit = @($details | Where-Object { ([string]$_.path).TrimEnd('\') -eq $selected.TrimEnd('\') })
                if ($hit.Count -gt 0) { $pick = $hit[0]; $selectedMs = (Get-QtEpochMs) }
            }
            if ($null -eq $pick -and $details.Count -ge 1) { $pick = $details[0] }
            if ($null -ne $pick) {
                $installPath = [string]$pick.path
                $version = [string]$pick.version
                $ambiguous = [bool]$pick.ambiguous
                $uninstallString = [string]$pick.uninstall_string
            }

            # 🔴 选过之后按 §2.9.2 **对选中的那一处再判**(而不是继续拿「装了几处」当结论)
            $effectiveCount = $installs.Count
            if ($selected -and $null -ne $pick) { $effectiveCount = 1 }
            $match = Get-QtWeChatMatch -InstallCount $effectiveCount -Version $version
            $plan = Get-QtWeChatPlan -Mode $mode -Match $match.Match -UserApproved $approved -BackupMode $backupMode
            $error = ''
            $reinstallMode = 'overwrite'

            if ($plan.NeedReinstall) {
                # §2.9.3 第 3 步:备份(copy / rename / skip)
                $sizes = Get-QtDirectorySize -Path $dataRoot.DataDir
                $chosenBackup = Select-QtWeChatBackupMode -Requested $backupMode -TotalBytes $sizes.bytes `
                    -DataRootKnown ([bool]$dataRoot.DataRoot)
                if ($plan.NeedBackup -and $chosenBackup -eq 'copy') {
                    $dir = [string](Get-QtOpt -Name 'wechat_backup_dir' -Default '')
                    if (-not $dir) { $dir = Join-Path ($dataRoot.DataRoot.Substring(0, 2) + '\') ('QTrade-WeChat-Backup\' + (Get-QtTimestamp)) }
                    $bk = Invoke-QtWeChatBackupCopy -Source $dataRoot.DataDir -Destination (Join-Path $dir 'xwechat_files') `
                        -LogPath (Get-QtSubLogPath -Name 'robocopy')
                    if (-not $bk.Ok) {
                        # 🔴 §5.1:备份失败**不卸载**、不再动微信;记 error,安装继续(可跳过项不作退出码)
                        $error = 'WECHAT_BACKUP_FAILED'
                        $plan = Get-QtWeChatPlan -Mode 'skip' -Match $match.Match
                    }
                }
                if (-not $error) {
                    # §2.9.3 第 4 步:方案①(覆盖安装)→ 失败且用户确认过则转方案②(交互式卸载再装)。
                    # 🔴 方案② 永远不带 /S(Assert-QtNoSilentUninstall 守门)。
                    $setup = Invoke-QtWeChatReinstall `
                        -SetupExe (Join-Path $paths.Pkg 'wechat\weixin_4.1.12.26.exe') `
                        -InstallPath $installPath -DataRoot $dataRoot.DataRoot `
                        -UninstallString $uninstallString -CurrentVersion $version `
                        -AllowInteractiveUninstall (-not $silent) `
                        -UserConfirmedUninstall ([bool](Get-QtOpt -Name 'wechat_uninstall_confirmed' -Default $false)) `
                        -SilentSetup ([bool](Get-QtOpt -Name 'wechat_silent_setup' -Default $false))
                    $reinstallMode = $setup.Mode
                    if (-not $setup.Ok) {
                        if ($setup.Reason -eq 'need_uninstall_confirm' -or $setup.UninstallTimedOut) {
                            # §2.9.3:卸载超时要给【我已卸载,继续】/【跳过微信通道】——由向导问,不由这里替用户决定
                            Set-QtParked -State $state -Step 'CLIENTS_CHECKED' -Reason 'WAIT_WECHAT_UNINSTALL' | Out-Null
                            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WAIT_WECHAT_UNINSTALL' -ExitName 'E_INSTALL_WAIT_USER' `
                                -Message '需要你在微信卸载程序里完成卸载(记得保持「保留本地数据」是勾上的),完成后回到这里继续' `
                                -Data ([ordered]@{ uninstall_string = $uninstallString; timed_out = $setup.UninstallTimedOut })
                        }
                        $error = 'WECHAT_REINSTALL_FAILED'
                        $plan = Get-QtWeChatPlan -Mode 'skip' -Match $match.Match
                    } else {
                        $version = $setup.FinalVersion
                        $match = Get-QtWeChatMatch -InstallCount 1 -Version $version
                    }
                }
            }

            # B 层 hosts 屏蔽(§2.9.3 第 5 步;`/QT_WECHAT_HOSTS_BLOCK=0` 或未授权则不调)
            $hostsBlock = 'skipped_no_domains'
            if ($plan.Action -ne 'BLOCK' -and [bool](Get-QtOpt -Name 'wechat_hosts_block' -Default $true)) {
                $hb = Invoke-QtWeChatUpdateBlock -Enable $true
                $hostsBlock = $hb.HostsBlock
                if (-not $hb.Ok) { Write-QtLog -Level 'WARN' -Message ('hosts 屏蔽降级:{0}' -f $hb.FailedReason) }
            }

            # §2.9.4 企点:地址写 02 [runtime] apk_url(C-43 唯一出处),再经 Agent 探一次
            $apkProbe = 'SKIPPED'
            $apkUrl = [string](Get-QtOpt -Name 'apk_url' -Default '')
            if ($apkUrl) {
                $agentBase = Get-QtAgentBaseUrl
                $tokens = Get-QtBootstrapTokens -TimeoutSec 30
                if (-not $tokens.Ok) { throw 'TOKEN_BOOTSTRAP_FAILED' }
                $r = Invoke-QtHttp -Uri ($agentBase + '/api/v1/settings/runtime') -Method PATCH `
                    -Headers @{ Authorization = ('Bearer ' + $tokens.ConsoleToken) } `
                    -Body (@{ apk_url = $apkUrl } | ConvertTo-Json -Compress) -TimeoutSec 30
                if ($r.Ok) {
                    $pr = Invoke-QtProbe -Targets @('apk_url') -Trigger 'install' -Detail 'clients' -ConsoleToken $tokens.ConsoleToken
                    if ($pr.Results.Count -gt 0) { $apkProbe = [string]$pr.Results[0].result }
                }
            }

            $state.clients = [pscustomobject]@{
                wechat     = [pscustomobject]@{
                    match           = $match.Match
                    action          = $plan.Action
                    version         = $version
                    path            = $installPath
                    dll             = $match.Dll
                    reinstall_mode  = $reinstallMode
                    backup_mode     = $backupMode
                    selected_ms     = $selectedMs
                    install_count   = $installs.Count
                    hosts_block     = $hostsBlock
                    ambiguous       = $ambiguous
                    error           = $error
                }
                qidian_apk = [pscustomobject]@{ probe = $apkProbe; sha256_verified = $false }
            }
            Set-QtState -State $state -To 'CLIENTS_CHECKED' -Note ('wechat ' + $plan.Action) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            $msg = if ($plan.Action -eq 'KEEP') { ('微信 {0}:可用({1})' -f $version, $match.Dll) }
            elseif ($plan.Action -eq 'BLOCK') { '已跳过微信通道' }
            else { ('微信已换到 {0}' -f $version) }
            Write-QtStepResult -Ok $true -State 'CLIENTS_CHECKED' -Message $msg `
                -Data ([ordered]@{ match = $match.Match; action = $plan.Action; hosts_block = $hostsBlock; apk_probe = $apkProbe; error = $error })
        }


        # ── 微信检测(**只读**,供向导在 MULTIPLE_INSTALLS 时弹选择页;§2.9.3 末)──
        # 回值用**扁平键** install_0_path / install_0_label …,让 Inno 的 Pascal 侧不用写 JSON 数组解析。
        'wechat-detect' {
            $dataRoot = Get-QtWeChatDataRoot
            $details = Get-QtWeChatInstallDetails -DataRoot $dataRoot.DataRoot
            $data = [ordered]@{ count = $details.Count; data_root = $dataRoot.DataRoot }
            for ($i = 0; $i -lt $details.Count; $i++) {
                $d = $details[$i]
                $ver = $(if ($d.version) { $d.version } else { '版本未知' })
                $run = $(if ($d.running) { '正在运行' } else { '未运行' })
                $data[('install_{0}_path' -f $i)] = [string]$d.path
                $data[('install_{0}_label' -f $i)] = ('{0}  ({1},{2})' -f $d.path, $ver, $run)
                $data[('install_{0}_version' -f $i)] = [string]$d.version
            }
            $m = Get-QtWeChatMatch -InstallCount $details.Count -Version $(if ($details.Count -ge 1) { [string]$details[0].version } else { '' })
            $data['match'] = $m.Match
            Write-QtStepResult -Ok $true -State ([string]$state.state) -Message ('检测到 {0} 处微信安装' -f $details.Count) -Data $data
        }

        # ── SELFTEST_OK(§2.11 + §2.10)──────────────────────────────────────
        'SELFTEST_OK' {
            $agentBase = Get-QtAgentBaseUrl
            $waBase = (Get-QtWinAgentNames).base_url
            $manifest = Read-QtManifest -Path $paths.Manifest
            $napcatRef = ''
            $refs = Get-QtExpectedImageRefs -Manifest $manifest
            if ($refs.Count -ge 2) { $napcatRef = $refs[1] }

            $tokens = Get-QtBootstrapTokens -TimeoutSec 30
            if (-not $tokens.Ok) { throw 'TOKEN_BOOTSTRAP_FAILED' }
            $st = Invoke-QtSelftest -AgentBaseUrl $agentBase -WinAgentBaseUrl $waBase -ConsoleToken $tokens.ConsoleToken `
                -DistroName ([string]$state.distro.name) -NapcatImageRef $napcatRef `
                -SkipNapcat:(-not [bool](Get-QtOpt -Name 'napcat_check' -Default $true))

            if ($null -ne $st.Probes) { $state.probes = @($st.Probes.Results) }
            if (-not $st.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'SELFTEST_OK' -Reason $st.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                $data = [ordered]@{ warnings = $st.Warnings }
                if ($null -ne $st.Redroid) { $data['redroid_logs'] = $st.Redroid.Logs }
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $st.Reason -ExitName ('E_INSTALL_' + $st.Reason) `
                    -Message '自检未通过' -Data $data
            }
            Set-QtState -State $state -To 'SELFTEST_OK' -Note 'selftest ok' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'SELFTEST_OK' `
                -Message ('临时安卓实例 {0} 秒启动完成;Agent/WinAgent 正常' -f $st.Redroid.ElapsedSec) `
                -Data ([ordered]@{ elapsed_sec = $st.Redroid.ElapsedSec; warnings = $st.Warnings; probe_via = $st.Probes.Via })
        }

        # ── DONE(§2.11 第 5 项)──────────────────────────────────────────────
        'DONE' {
            Clear-QtRunOnce -State $state | Out-Null
            $shortcuts = Install-QtConsoleShortcuts -ConsoleDir $paths.Console -LauncherPath (Join-Path (Split-Path -Parent $paths.EngineExe) 'launch-installed.ps1')
            # A-6:删暂存 rootfs.tar,**不询问**
            $freed = Remove-QtStagedRootfs -WslDir $paths.Wsl

            $wechat = ''
            if ($state.clients -and (Test-QtHasProperty -Object $state.clients -Name 'wechat')) {
                $wechat = ('{0} / {1}' -f $state.clients.wechat.match, $state.clients.wechat.action)
            }
            $summary = [pscustomobject]@{
                package_version = [string]$state.package_version
                kernel_version  = ''
                wsl_version     = $(if ($state.env.PSObject.Properties['wsl_version']) { [string]$state.env.wsl_version } else { '' })
                distro          = [string]$state.distro.name
                images          = ''
                winagent        = [string]$state.firewall_rules
                wechat          = $wechat
                docker_cidr     = [string]$state.env.docker_cidr
                disk            = ('硬门槛 16 GB / 建议 20 GB;ext4.vhdx 只增不减,清缓存不还盘,注销发行版才释放')
                memory          = ''
                firewall        = [string]$state.firewall_rules
                hosts_block     = ''
                probes          = ''
                notes           = 'QTrade 的 WSL 与 Agent 在安装账号登录后由 WinAgent 会话代理拉起,不随开机启动'
            }
            if (Test-QtPath -Path $paths.KernelPtr) {
                try { $summary.kernel_version = [string]((Read-QtTextFile -Path $paths.KernelPtr | ConvertFrom-Json).version) } catch { }
            }
            $firewallMessage = ''
            if ($state.firewall_rules -eq 'blocked_by_policy') {
                $firewallMessage = 'Policy blocked inbound port 17610. The Agent may be unable to reach WinAgent. Ask IT to allow the required rule.'
                $summary.notes += (' ' + $firewallMessage)
            }
            Write-QtInstallSummary -Path $paths.SummaryFile -Summary $summary | Out-Null

            Set-QtState -State $state -To 'DONE' -Note 'done' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'DONE' -Message '安装完成' -Data ([ordered]@{
                    shortcuts    = $shortcuts.Created
                    rootfs_freed = $freed.FreedBytes
                    summary      = $paths.SummaryFile
                    firewall_message = $firewallMessage
                })
        }

        # ── 升级 `/QT_MODE=upgrade`(§2.13)────────────────────────────────────
        'clean-reinstall' {
            if (-not $StageRoot) {
                Write-QtStepResult -Ok $false -Reason 'FULL_INSTALLER_REQUIRED' -ExitName 'E_INSTALL_WAIT_USER' `
                    -Message 'Run the complete installer to clean and reinstall QTrade.'
            }
            if (-not (Test-QtHasProperty $state.env 'install_user_sid') -or
                $state.env.install_user_sid -ne (Get-QtCurrentUserSid)) {
                Write-QtStepResult -Ok $false -Reason 'PRECHECK_REQUIRED' -ExitName 'E_INSTALL_WAIT_USER' `
                    -Message 'Complete preflight with the installing Windows user before clean reinstall.'
            }
            $cleanConsent = Get-QtOpt 'accept_clean_reinstall' $false
            $r = Invoke-QtCleanReinstall -StageRoot $StageRoot -LiveRoot $paths.Root `
                -PackageVersion ([string](Get-QtOpt 'package_version' '0.0.0')) `
                -AcceptCleanReinstall ($cleanConsent -is [bool] -and $cleanConsent)
            if (-not $r.Ok) {
                $exitName = if ($r.RequiresConsent) { 'E_INSTALL_WAIT_USER' } else { 'E_INSTALL_INTERNAL' }
                Write-QtStepResult -Ok $false -Reason $r.Reason -ExitName $exitName `
                    -Message 'Clean reinstall was not completed. Account settings, login state and collected records in QTrade will be deleted only after explicit confirmation.' `
                    -Data ([ordered]@{ requires_consent = $r.RequiresConsent })
            }
            $livePaths = Get-QtPaths -Root $paths.Root
            if (-not $r.Resumed) {
                $r.State.env = $state.env
                $r.State.state = 'PAYLOAD_STAGED'
                Write-QtInstallState -State $r.State -Path $livePaths.StateFile | Out-Null
            }
            Write-QtStepResult -Ok $true -State ([string]$r.State.state) -Message 'Verified payload is ready for installation.' `
                -Data ([ordered]@{ resumed = $r.Resumed })
        }

        'upgrade' {
            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'FULL_INSTALLER_REQUIRED' `
                -ExitName 'E_INSTALL_WAIT_USER' -Message 'In-place upgrade is disabled. Run the complete installer and confirm clean reinstall.'
        }

        # ── 修复 `/QT_MODE=repair`(§5.2)─────────────────────────────────────
        'repair' {
            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'REPAIR_DISABLED' `
                -ExitName 'E_INSTALL_WAIT_USER' -Message 'WSL repair is disabled because its recovery path cannot guarantee data safety. Use the complete installer for a confirmed clean reinstall.'
        }

        # ── 诊断包(§5.3;失败页【导出诊断信息】)────────────────────────────
        'diag' {
            $diagRoot = if ($StageRoot) { $StageRoot } else { $Root }
            if ($StageRoot) {
                $liveStateFile = (Get-QtPaths -Root $Root).StateFile
                if (Test-QtPath -Path $liveStateFile) {
                    Copy-Item -LiteralPath $liveStateFile -Destination (Join-Path $StageRoot 'install\install_state.json') -Force
                }
            }
            $d = New-QtDiagBundle -Root $diagRoot -IncludeDmesg ([bool](Get-QtOpt -Name 'diag_include_dmesg' -Default $true)) `
                -DistroName ([string]$state.distro.name)
            Write-QtStepResult -Ok $d.Ok -State ([string]$state.state) -Message ('诊断包:' + $d.ZipPath) `
                -Data ([ordered]@{ zip = $d.ZipPath; included = $d.Included; skipped = $d.Skipped; blocked = $d.Blocked })
        }

        default {
            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'INTERNAL' -ExitName 'E_INSTALL_INTERNAL' -Message ('未知步骤:{0}' -f $Step)
        }
    }
} catch {
    $msg = $_.Exception.Message
    Write-QtLog -Level 'ERROR' -Message ('未归类异常:{0} | {1}' -f $msg, $_.ScriptStackTrace)
    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'INTERNAL' -ExitName 'E_INSTALL_INTERNAL' -Message $msg
}
