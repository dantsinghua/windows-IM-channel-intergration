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
    [string] $LogStamp = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$modulesDir = Join-Path $PSScriptRoot 'modules'
foreach ($m in @('QTrade.Log', 'QTrade.Native', 'QTrade.Exit', 'QTrade.State', 'QTrade.Payload',
        'QTrade.Preflight', 'QTrade.Wsl', 'QTrade.Kernel', 'QTrade.Distro', 'QTrade.WinAgent',
        'QTrade.Console', 'QTrade.WeChat', 'QTrade.Firewall', 'QTrade.Selftest',
        'QTrade.Upgrade', 'QTrade.Diag', 'QTrade.Uninstall')) {
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
if ($OptionsPath -and (Test-Path -LiteralPath $OptionsPath)) {
    # Inno 侧经临时文件传选项(JSON 里的双引号穿命令行会被弄坏,同 W7 的教训)
    $OptionsJson = [IO.File]::ReadAllText($OptionsPath)
}
try { $opt = ($OptionsJson | ConvertFrom-Json) } catch { $opt = [pscustomobject]@{} }

function Get-QtOpt {
    param([string] $Name, $Default = $null)
    if (Test-QtHasProperty -Object $opt -Name $Name) { return $opt.$Name }
    return $Default
}

Initialize-QtLog -Directory $paths.Logs -Prefix 'install' -Stamp $LogStamp | Out-Null
Set-QtLogStep -Step $Step

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
            Remove-QtKCheck -Directory $paths.KCheck | Out-Null
            $next = Get-QtResumeStep -State ([string]$state.state)
            # §2.13 首句:新版本 EXE 检测到 `install_state.state=DONE` 且 `package_version <` 自身即进 upgrade 模式;
            # `>` 则拒绝降级(122)。这里只**建议**模式,真正切换由 .iss 决定。
            $selfVersion = [string](Get-QtOpt -Name 'package_version' -Default '0.0.0')
            $cmp = Compare-QtPackageVersion -Installed ([string]$state.package_version) -Package $selfVersion
            $recommended = 'install'
            if ([string]$state.state -eq 'DONE') {
                if ($cmp.Kind -eq 'UPGRADE') { $recommended = 'upgrade' }
                elseif ($cmp.Kind -eq 'DOWNGRADE') { $recommended = 'downgrade-refused' }
                else { $recommended = 'repair' }
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
            $ctx = [pscustomobject]@{ ManifestPath = $paths.Manifest; StageRoot = $paths.Root }
            if (Test-QtStepComplete -Step 'PAYLOAD_STAGED' -Context $ctx) {
                Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                Set-QtState -State $state -To 'PAYLOAD_STAGED' -Note 'skip' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'PAYLOAD_STAGED' -Message '载荷已校验(跳过)'
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
            Write-QtStepResult -Ok $true -State 'PAYLOAD_STAGED' -Message ('已校验 {0} 个文件' -f $v.VerifiedCount)
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
            Set-QtKernelAcl -Path $kernelPath
            Write-QtKernelPointer -Path $paths.KernelPtr -KernelPath $kernelPath -Sha256 ([string]$k.sha256) -Version ([string]$k.version) -Line (Get-QtKernelLine) | Out-Null
            $imp = Import-QtKCheck -Directory $paths.KCheck -TarPath (Join-Path $paths.Wsl 'kcheck-rootfs.tar')
            if (-not $imp.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_STAGED' -Reason 'KCHECK_IMPORT_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'KCHECK_IMPORT_FAILED' -ExitName 'E_INSTALL_KCHECK_IMPORT_FAILED' -Message '内核验证用微型发行版导入失败'
            }
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

            # 断电保险(§2.6.3):写入前先置 substate 并把 RunOnce 指向 verify-kernel
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
                if ($ver.Reason -eq 'KERNEL_SHUTDOWN_TIMEOUT') {
                    # 🔴 不回滚(回滚也要 shutdown,同样会挂);配置已写、RunOnce 已指 verify-kernel
                    Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason 'KERNEL_SHUTDOWN_TIMEOUT') | Out-Null
                    Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'KERNEL_SHUTDOWN_TIMEOUT' -ExitName 'E_INSTALL_KERNEL_SHUTDOWN_TIMEOUT' `
                        -Message 'WSL 服务未响应,请重启电脑,登录后自动继续验证'
                }
                # 内核自身失败 → 自动回滚(§2.6.5)
                $rb = Invoke-QtKernelRollback -WslConfigPath $wslConfig -ShutdownConfirmed $true -KCheckDirectory $paths.KCheck
                Clear-QtRunOnce -State $state | Out-Null
                if (-not $rb.Ok) {
                    Set-QtState -State $state -To 'KERNEL_ROLLED_BACK' -Note 'rollback failed' | Out-Null
                    Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason 'KERNEL_ROLLBACK_FAILED') | Out-Null
                    Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'KERNEL_ROLLBACK_FAILED' -ExitName 'E_INSTALL_KERNEL_ROLLBACK_FAILED' `
                        -Message '回滚失败:请打开 %USERPROFILE%\.wslconfig 确认没有 kernel= 行,然后重启电脑'
                }
                Set-QtState -State $state -To 'KERNEL_ROLLED_BACK' -Note ('rolled back to ' + $rb.OfficialKernel) | Out-Null
                Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason $ver.Reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $ver.Reason -ExitName ('E_INSTALL_' + $ver.Reason) `
                    -Message ('切换失败({0}),已恢复原内核' -f $ver.Reason) -Data ([ordered]@{ official_kernel = $rb.OfficialKernel })
            }

            Remove-QtKCheck -Directory $paths.KCheck | Out-Null
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
            Import-QtKCheck -Directory $paths.KCheck -TarPath (Join-Path $paths.Wsl 'kcheck-rootfs.tar') | Out-Null
            $ver = Invoke-QtKernelVerify -ManifestVersion ([string]$k.version) -ShutdownConfirmed $true `
                -CustomKernelPolicyPresent $policy.CustomKernelForbidden -SkipShutdown
            if ($ver.Ok) {
                Remove-QtKCheck -Directory $paths.KCheck | Out-Null
                Clear-QtRunOnce -State $state | Out-Null
                Set-QtState -State $state -To 'KERNEL_VERIFIED' -Note 'verify-kernel resume' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'KERNEL_VERIFIED' -Message ('内核 {0} 已生效' -f $ver.Uname)
            }
            $rb = Invoke-QtKernelRollback -WslConfigPath $wslConfig -ShutdownConfirmed $true -KCheckDirectory $paths.KCheck
            Clear-QtRunOnce -State $state | Out-Null
            $reason = if ($rb.Ok) { $ver.Reason } else { 'KERNEL_ROLLBACK_FAILED' }
            Set-QtState -State $state -To 'KERNEL_ROLLED_BACK' -Note 'verify-kernel resume' | Out-Null
            Set-QtState -State $state -To (New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason $reason) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $reason -ExitName ('E_INSTALL_' + $reason) -Message '内核验证未通过,已按基线恢复'
        }

        # ── DISTRO_IMPORTED(§2.7)────────────────────────────────────────────
        'DISTRO_IMPORTED' {
            $name = [string]$state.distro.name
            $tar = Join-Path $paths.Wsl 'rootfs.tar'
            $manifest = Read-QtManifest -Path $paths.Manifest
            $rootfsVersion = ''
            $rf = @($manifest.files | Where-Object { ([string]$_.path) -eq 'wsl/rootfs.tar' })
            if ($rf.Count -gt 0 -and (Test-QtHasProperty -Object $rf[0] -Name 'version')) { $rootfsVersion = [string]$rf[0].version }

            # ── §2.7.1 同名冲突判定 ─────────────────────────────────────────
            $listed = Invoke-QtWsl -WslArgs @('--list', '--quiet') -TimeoutSec 60
            $exists = ($listed.StdOut -match ('(?m)^\s*' + [regex]::Escape($name) + '\s*$'))
            if ($exists) {
                $reg = @(Get-QtRegisteredDistro | Where-Object { $_.name -eq $name })
                $basePath = ''
                if ($reg.Count -gt 0) { $basePath = [string]$reg[0].base_path }
                $marker = Read-QtImportedMarker -DistroName $name
                $markerVer = ''
                if ($null -ne $marker -and (Test-QtHasProperty -Object $marker -Name 'rootfs_version')) { $markerVer = [string]$marker.rootfs_version }
                $conflict = Resolve-QtDistroConflict -Exists $true `
                    -HasStateRecord ([bool]$state.distro.imported_at) -BasePath $basePath -ExpectedBasePath $paths.Distro `
                    -ImportedRootfsVersion $markerVer -PackageRootfsVersion $rootfsVersion

                switch ($conflict.Kind) {
                    'REUSE' {
                        Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                        Set-QtState -State $state -To 'DISTRO_IMPORTED' -Note 'skip (already imported, same rootfs_version)' | Out-Null
                        Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                        Write-QtStepResult -Ok $true -State 'DISTRO_IMPORTED' -Message 'qtrade 已存在且版本一致,跳过导入' -Data ([ordered]@{ conflict = 'REUSE' })
                    }
                    'UPGRADE' {
                        # 🔴 §2.7.1:**不在首装流程里静默换 rootfs** —— 只跳过并要求走 §2.13 升级路径
                        Write-QtLog -Level 'WARN' -Message ('已存在的 qtrade 的 rootfs 版本低于本包({0} < {1}),请以 /QT_MODE=upgrade 运行' -f $markerVer, $rootfsVersion)
                        Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                        Set-QtState -State $state -To 'DISTRO_IMPORTED' -Note 'skip (needs upgrade)' | Out-Null
                        Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                        Write-QtStepResult -Ok $true -State 'DISTRO_IMPORTED' -Message '已存在的发行版版本较低,需要走升级流程' -Data ([ordered]@{ conflict = 'UPGRADE'; needs_upgrade = $true })
                    }
                    'FOREIGN' {
                        # 停下问用户(§2.7.1):【注销它并重新导入】/【取消安装】;**不改名绕过**
                        $choice = [string](Get-QtOpt -Name 'distro_conflict_choice' -Default '')
                        if ($choice -eq 'cancel') {
                            Set-QtState -State $state -To (New-QtFailedState -Step 'DISTRO_IMPORTED' -Reason 'DISTRO_NAME_CONFLICT_DECLINED') | Out-Null
                            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'DISTRO_NAME_CONFLICT_DECLINED' -ExitName 'E_INSTALL_DISTRO_NAME_CONFLICT_DECLINED' `
                                -Message ('发现已存在名为 {0} 的 WSL 发行版(位置 {1}),用户选择取消安装' -f $name, $basePath)
                        }
                        if ($choice -ne 'unregister') {
                            Set-QtParked -State $state -Step 'DISTRO_IMPORTED' -Reason 'WAIT_DISTRO_CONFLICT' | Out-Null
                            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WAIT_DISTRO_CONFLICT' -ExitName 'E_INSTALL_WAIT_USER' `
                                -Message ('发现已存在名为 {0} 的 WSL 发行版(位置 {1}),不是本安装程序创建的或记录已丢失,需要用户选择如何处理' -f $name, $basePath) `
                                -Data ([ordered]@{ conflict = 'FOREIGN'; base_path = $basePath; reason = $conflict.Reason })
                        }
                        # 🔴 先导出再注销;**导出失败则不注销**(验收 M1-13)
                        $exp = Export-QtDistroBackup -DistroName $name -WslDir $paths.Wsl
                        if (-not $exp.Ok) {
                            Set-QtState -State $state -To (New-QtFailedState -Step 'DISTRO_IMPORTED' -Reason 'IMPORT_FAILED') | Out-Null
                            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                            Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'IMPORT_FAILED' -ExitName 'E_INSTALL_IMPORT_FAILED' `
                                -Message '导出已有同名发行版失败,已放弃注销(不会动你的数据)'
                        }
                        Write-QtLog -Message ('已导出同名发行版到 {0},即将注销后重新导入' -f $exp.TarPath)
                        Unregister-QtDistro -Name $name | Out-Null
                    }
                }
            }

            $imp = Import-QtDistro -Name $name -Directory $paths.Distro -TarPath $tar
            if (-not $imp.Ok) {
                $reason = $imp.Reason
                Set-QtState -State $state -To (New-QtFailedState -Step 'DISTRO_IMPORTED' -Reason $reason) | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $reason -ExitName ('E_INSTALL_' + $reason) -Message '发行版导入失败'
            }
            $sysd = Wait-QtDistroSystemd -Name $name
            if (-not $sysd.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'DISTRO_IMPORTED' -Reason 'SYSTEMD_NOT_READY') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'SYSTEMD_NOT_READY' -ExitName 'E_INSTALL_SYSTEMD_NOT_READY' -Message '发行版首启未就绪'
            }
            # 🔴 R5-9:选段锚在「导入完成、首起 dockerd 前」,**不以 .imported 为前置**
            $cidr = [string](Get-QtOpt -Name 'docker_cidr' -Default '')
            if (-not $cidr) {
                $candidates = @(Get-QtOpt -Name 'docker_pool_candidates' -Default @('10.213.0.0/16', '10.231.0.0/16', '10.247.0.0/16', '10.199.0.0/16'))
                $occupied = @(Get-QtOpt -Name 'occupied_prefixes' -Default @())
                $pick = Select-QtDockerPool -Candidates $candidates -OccupiedPrefixes $occupied
                $cidr = $pick.Cidr
                if ($pick.AllConflict) { Write-QtLog -Level 'WARN' -Message ('docker 地址池候选全冲突,取第一个 {0}({1})' -f $cidr, $pick.Warn) }
            }
            # 🔴 R5-9:选段结果必须在**首起 dockerd 之前**就位 —— 写 install.env,
            #    发行版里的 `qtrade-docker-config.service`(Before=docker.service)据此写 daemon.json
            Write-QtInstallEnv -Path (Join-Path $paths.Install 'install.env') -DockerCidr $cidr `
                -ApkUrl ([string](Get-QtOpt -Name 'apk_url' -Default '')) | Out-Null
            $state.env.docker_cidr = $cidr
            $state.distro.dir = $paths.Distro
            $state.distro.imported_at = Get-QtIso8601
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
            $svc = Install-QtWinAgentService -ExePath $svcExe -Name $names.service_name -DisplayName $names.display_name
            if (-not $svc.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason 'SERVICE_INSTALL_FAILED') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'SERVICE_INSTALL_FAILED' -ExitName 'E_INSTALL_SERVICE_INSTALL_FAILED' -Message ('服务注册失败')
            }
            Register-QtWinAgentUserTask -UserExePath (Join-Path $paths.WinAgent ('app\' + $names.user_exe)) `
                -InstallUserSid ([string]$state.env.install_user_sid) -StartNow | Out-Null
            Invoke-QtSc -ScArgs @('start', $names.service_name) | Out-Null
            $h = Wait-QtWinAgentHealthy -BaseUrl $names.base_url -TimeoutSec 30
            if (-not $h.Ok) {
                Set-QtState -State $state -To (New-QtFailedState -Step 'WINAGENT_INSTALLED' -Reason 'WINAGENT_NOT_READY') | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WINAGENT_NOT_READY' -ExitName 'E_INSTALL_WINAGENT_NOT_READY' -Message '服务启动失败或 30 秒内未就绪'
            }
            $fw = Invoke-QtFirewallEnsure -BaseUrl $names.base_url
            $state.firewall_rules = [string]$fw.Action
            if ($fw.Blocked) { Write-QtLog -Level 'WARN' -Message $fw.Message }
            Set-QtState -State $state -To 'WINAGENT_INSTALLED' -Note ('firewall ' + $fw.Action) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            # 🔴 R3-16:user_agent:false 属正常态,不判失败
            $msg = if ($h.UserAgent) { '服务已启动;会话代理已上线' } else { '服务已启动。首次使用需登录 Windows 桌面(可锁屏,不要注销),账号才会运行' }
            Write-QtStepResult -Ok $true -State 'WINAGENT_INSTALLED' -Message $msg -Data ([ordered]@{ user_agent = $h.UserAgent; firewall = $fw.Action; firewall_blocked = $fw.Blocked })
        }

        # ── 卸载 `/QT_MODE=uninstall`(§2.14)────────────────────────────────
        'uninstall' {
            $keepData = [bool](Get-QtOpt -Name 'keep_data' -Default $true)
            $plan = Get-QtUninstallPlan -State $state -KeepData $keepData -Root $Root
            $stamp = Get-QtTimestamp
            $data = Invoke-QtUninstallData -KeepData $keepData -WslDir $paths.Wsl -Stamp $stamp
            Invoke-QtUninstallDistro -KCheckDir $paths.KCheck | Out-Null
            Invoke-QtUninstallWslConfig -WslConfigPath ([string]$state.env.wslconfig_path) -BackupDir $paths.Wsl `
                -KeysAdded $plan.WslConfig.RemoveKeys -Stamp $stamp | Out-Null
            # 顺序:防火墙 → hosts → 服务(两者都必须在删服务之前,§2.14 第 5/6 步)
            $fw = Invoke-QtFirewallDelete -FallbackRuleNames (Get-QtFirewallRuleNames)
            $hosts = Invoke-QtWeChatUpdateBlock -Enable $false
            Uninstall-QtWinAgent | Out-Null
            Stop-QtConsole | Out-Null
            Clear-QtRunOnce -State $state | Out-Null
            Remove-QtShortcuts | Out-Null
            $orphan = Get-QtOrphanQtbakDirs -SearchRoots @()
            $rm = Remove-QtInstallFiles -Plan $plan
            Write-QtStepResult -Ok $true -State 'DONE' -Message '卸载完成' -Data ([ordered]@{
                    backup_tar    = $data.BackupTar
                    firewall      = $fw.Via
                    hosts_block   = $hosts.HostsBlock
                    removed_count = $rm.Removed.Count
                    kept          = $rm.Kept
                    qtbak_orphans = $orphan
                })
        }


        # ── IMAGES_LOADED(§2.7.3 末 / §2.2.1 G-10)──────────────────────────
        'IMAGES_LOADED' {
            $manifest = Read-QtManifest -Path $paths.Manifest
            $name = [string]$state.distro.name
            $expected = Get-QtExpectedImageRefs -Manifest $manifest
            $ctx = [pscustomobject]@{ DistroName = $name; ExpectedImages = $expected }
            if ($expected.Count -gt 0 -and (Test-QtStepComplete -Step 'IMAGES_LOADED' -Context $ctx)) {
                Set-QtSubstate -State $state -Substate 'skip' | Out-Null
                Set-QtState -State $state -To 'IMAGES_LOADED' -Note 'skip' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $true -State 'IMAGES_LOADED' -Message '镜像与 Agent 已就绪(跳过)'
            }

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
                Write-QtLog -Level 'WARN' -Message ('docker bridge 子网 {0} 不在选定段 {1} 内(R5-9:选段可能晚于 dockerd 首启)' -f $bridge, $state.env.docker_cidr)
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
            $version = ''
            $ambiguous = $false
            $installPath = ''
            if ($installs.Count -ge 1) {
                $installPath = [string]$installs[0].path
                $v = Get-QtWeChatVersion -InstallPath $installPath -DataRoot $dataRoot.DataRoot
                $version = $v.Version
                $ambiguous = $v.Ambiguous
            }
            $match = Get-QtWeChatMatch -InstallCount $installs.Count -Version $version
            $plan = Get-QtWeChatPlan -Mode $mode -Match $match.Match -UserApproved $approved -BackupMode $backupMode
            $error = ''

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
                    $setup = Start-QtWeChatSetup -SetupExe (Join-Path $paths.Pkg 'wechat\weixin_4.1.12.26.exe') `
                        -InstallPath $installPath -DataRoot $dataRoot.DataRoot `
                        -SilentSetup ([bool](Get-QtOpt -Name 'wechat_silent_setup' -Default $false))
                    if (-not $setup.Ok) {
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
                $r = Invoke-QtHttp -Uri ($agentBase + '/api/v1/settings/runtime') -Method PATCH `
                    -Body (@{ apk_url = $apkUrl } | ConvertTo-Json -Compress) -TimeoutSec 30
                if ($r.Ok) {
                    $pr = Invoke-QtProbe -Targets @('apk_url') -Trigger 'install' -Detail 'clients'
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
                    reinstall_mode  = 'overwrite'
                    backup_mode     = $backupMode
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

        # ── SELFTEST_OK(§2.11 + §2.10)──────────────────────────────────────
        'SELFTEST_OK' {
            $agentBase = Get-QtAgentBaseUrl
            $waBase = (Get-QtWinAgentNames).base_url
            $manifest = Read-QtManifest -Path $paths.Manifest
            $napcatRef = ''
            $refs = Get-QtExpectedImageRefs -Manifest $manifest
            if ($refs.Count -ge 2) { $napcatRef = $refs[1] }

            $st = Invoke-QtSelftest -AgentBaseUrl $agentBase -WinAgentBaseUrl $waBase `
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
            $shortcuts = Install-QtConsoleShortcuts -ConsoleDir $paths.Console
            # A-6:删暂存 rootfs.tar,**不询问**
            $freed = Remove-QtStagedRootfs -WslDir $paths.Wsl

            $wechat = ''
            if ($state.clients -and (Test-QtHasProperty -Object $state.clients -Name 'wechat')) {
                $wechat = ('{0} / {1}' -f $state.clients.wechat.match, $state.clients.wechat.action)
            }
            $summary = [pscustomobject]@{
                package_version = [string]$state.package_version
                kernel_version  = ''
                wsl_version     = [string]$state.env.wsl_version
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
            Write-QtInstallSummary -Path $paths.SummaryFile -Summary $summary | Out-Null

            Set-QtState -State $state -To 'DONE' -Note 'done' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'DONE' -Message '安装完成' -Data ([ordered]@{
                    shortcuts    = $shortcuts.Created
                    rootfs_freed = $freed.FreedBytes
                    summary      = $paths.SummaryFile
                })
        }

        # ── 升级 `/QT_MODE=upgrade`(§2.13)────────────────────────────────────
        'upgrade' {
            $selfVersion = [string](Get-QtOpt -Name 'package_version' -Default '0.0.0')
            $cmp = Compare-QtPackageVersion -Installed ([string]$state.package_version) -Package $selfVersion
            if ($cmp.Kind -eq 'DOWNGRADE') {
                # 🔴 §2.13 兼容矩阵:**不支持降级**,文案指向卸载再装
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'DOWNGRADE_REFUSED' -ExitName 'E_INSTALL_DOWNGRADE_REFUSED' `
                    -Message ('本机已装 {0},高于本包 {1};不支持降级,请先卸载再安装' -f $state.package_version, $selfVersion)
            }

            # 升级前按同一硬门槛复检磁盘(§2.13 末「磁盘(E-18)」)
            $free = Get-QtDriveFreeBytes -DriveLetter ($paths.Root.Substring(0, 1))
            $disk = Test-QtDiskThreshold -FreeBytes $free
            if (-not $disk.Ok) {
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'DISK_FULL' -ExitName 'E_INSTALL_DISK_FULL' `
                    -Message $disk.Message
            }

            $manifest = Read-QtManifest -Path $paths.Manifest
            $breaking = $false
            if (Test-QtHasProperty -Object $manifest -Name 'schema_breaking') { $breaking = [bool]$manifest.schema_breaking }
            $prompt = Get-QtSchemaBreakingPrompt -SchemaBreaking $breaking
            if ($prompt.RequireConfirm -and -not [bool](Get-QtOpt -Name 'accept_schema_breaking' -Default $false)) {
                Set-QtParked -State $state -Step 'DONE' -Reason 'WAIT_SCHEMA_BREAKING_CONFIRM' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WAIT_SCHEMA_BREAKING_CONFIRM' -ExitName 'E_INSTALL_WAIT_USER' `
                    -Message $prompt.Text
            }

            # 第 1 步 drain(红线 6:会中断账号,须明示确认)
            $confirmed = [bool](Get-QtOpt -Name 'accept_shutdown' -Default $false)
            if (-not $confirmed) {
                Set-QtParked -State $state -Step 'DONE' -Reason 'WAIT_SHUTDOWN_CONFIRM' | Out-Null
                Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'WAIT_SHUTDOWN_CONFIRM' -ExitName 'E_INSTALL_WAIT_USER' `
                    -Message '升级会停掉所有账号容器,需要你确认时机(静默模式带 /QT_ACCEPT_SHUTDOWN=1)'
            }
            Invoke-QtDrain -Confirmed $true | Out-Null

            # 第 3~6 步:变了才做
            $plan = Get-QtUpgradePlan -Current ([pscustomobject](Get-QtOpt -Name 'current_fingerprint' -Default ([pscustomobject]@{}))) `
                -Next ([pscustomobject](Get-QtOpt -Name 'next_fingerprint' -Default ([pscustomobject]@{})))
            Write-QtLog -Message ('升级计划:' + ($plan.Steps -join ' → '))

            $backupTar = ''
            if ($plan.Rootfs -or $prompt.RequireConfirm) {
                $bk = Backup-QtAgentData -WslDir $paths.Wsl -DistroName ([string]$state.distro.name)
                if (-not $bk.Ok) {
                    # 🔴 没备份成功绝不 --unregister
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason 'UPGRADE_DATA_BACKUP_FAILED' -ExitName 'E_INSTALL_UPGRADE_DATA_BACKUP_FAILED' `
                        -Message '升级前的数据备份失败,已中止(未动发行版)'
                }
                $backupTar = $bk.TarPath
            }

            # 第 7 步 G-01:停 → 覆盖 → 起(顺序固定)
            $stopped = Stop-QtTriad
            if ($plan.Rootfs) {
                Unregister-QtDistro -Name ([string]$state.distro.name) | Out-Null
                $imp = Import-QtDistro -Name ([string]$state.distro.name) -Directory $paths.Distro -TarPath (Join-Path $paths.Wsl 'rootfs.tar')
                if (-not $imp.Ok) {
                    Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $imp.Reason -ExitName ('E_INSTALL_' + $imp.Reason) `
                        -Message ('升级导入新 rootfs 失败;可用 /QT_MODE=repair --restore-data {0} 恢复' -f $backupTar)
                }
                Wait-QtDistroSystemd -Name ([string]$state.distro.name) | Out-Null
                if ($backupTar) { Restore-QtAgentData -TarPath $backupTar -DistroName ([string]$state.distro.name) | Out-Null }
            } elseif ($plan.AgentCode) {
                Update-QtAgentCode -AgentTarPath (Join-Path $paths.Wsl 'agent\agent.tar') -DistroName ([string]$state.distro.name) | Out-Null
            }
            $started = Start-QtTriad -ConsoleExePath (Join-Path $paths.Console (Get-QtConsoleNames).exe)

            $state.package_version = $selfVersion
            Add-QtHistory -State $state -From 'DONE' -To 'DONE' -Note ('upgrade from ' + $cmp.Reason) | Out-Null
            Set-QtState -State $state -To 'DONE' -Note ('upgrade to ' + $selfVersion) | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State 'DONE' -Message ('已升级到 {0}' -f $selfVersion) -Data ([ordered]@{
                    plan          = $plan.Steps
                    backup_tar    = $backupTar
                    console_killed = $stopped.ConsoleKilled
                    user_agent    = $started.UserAgent
                })
        }

        # ── 修复 `/QT_MODE=repair`(§5.2)─────────────────────────────────────
        'repair' {
            $r = Invoke-QtRepair -DistroDir $paths.Distro -RootfsTar (Join-Path $paths.Wsl 'rootfs.tar') `
                -WslDir $paths.Wsl -DistroName ([string]$state.distro.name) `
                -RestoreDataTar ([string](Get-QtOpt -Name 'restore_data' -Default ''))
            if (-not $r.Ok) {
                Write-QtStepResult -Ok $false -State ([string]$state.state) -Reason $r.Reason -ExitName 'E_INSTALL_IMPORT_FAILED' -Message '修复失败'
            }
            Add-QtHistory -State $state -From ([string]$state.state) -To ([string]$state.state) -Note 'repair' | Out-Null
            Write-QtInstallState -State $state -Path $paths.StateFile | Out-Null
            Write-QtStepResult -Ok $true -State ([string]$state.state) -Message '修复完成' `
                -Data ([ordered]@{ restored = $r.Restored; backup_used = $r.BackupUsed })
        }

        # ── 诊断包(§5.3;失败页【导出诊断信息】)────────────────────────────
        'diag' {
            $d = New-QtDiagBundle -Root $Root -IncludeDmesg ([bool](Get-QtOpt -Name 'diag_include_dmesg' -Default $true)) `
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
