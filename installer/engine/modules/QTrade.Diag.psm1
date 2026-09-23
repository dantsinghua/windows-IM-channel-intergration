# QTrade 安装引擎 —— 诊断包 `diag-<ts>.zip`(§5.3)
# 规格:docs/03 §5.3(收什么)、§6 与红线 2(**不含**什么)、§2.6.8 第 8 条(日志采集/WSL 事件日志)、
#       §7 `[install] diag_include_dmesg`
#
# 🔴 排除清单是**硬约束**,不是「尽量」:vault、任何密码/令牌、微信数据、消息正文一律不进包。
#    Test-QtDiagExclusions 把它做成会失败的断言,而不是注释。
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Kernel.psm1') -DisableNameChecking

# §5.3 收集清单(名字 → 怎么来)
$script:QtDiagItems = @(
    [pscustomobject]@{ name = 'install_state.json'; kind = 'file' }
    [pscustomobject]@{ name = 'engine-logs'; kind = 'files' }      # 引擎日志 + 各子日志
    [pscustomobject]@{ name = 'wslconfig-current'; kind = 'file' }
    [pscustomobject]@{ name = 'wslconfig-backups'; kind = 'files' }
    [pscustomobject]@{ name = 'wsl-version'; kind = 'cmd' }
    [pscustomobject]@{ name = 'wsl-status'; kind = 'cmd' }
    [pscustomobject]@{ name = 'wsl-list'; kind = 'cmd' }
    [pscustomobject]@{ name = 'optional-features'; kind = 'cmd' }
    [pscustomobject]@{ name = 'policy-keys'; kind = 'cmd' }
    [pscustomobject]@{ name = 'kernel-current.json'; kind = 'file' }
    [pscustomobject]@{ name = 'kcheck-dmesg'; kind = 'cmd' }       # [install] diag_include_dmesg 控制
    [pscustomobject]@{ name = 'docker-logs'; kind = 'cmd' }
    [pscustomobject]@{ name = 'wsl-eventlog'; kind = 'cmd' }       # §2.6.8 第 8 条:K1 型失败只在这里有痕迹
    [pscustomobject]@{ name = 'Hyper-V-Compute-Admin'; kind = 'cmd' }
    [pscustomobject]@{ name = 'Hyper-V-Worker-Admin'; kind = 'cmd' }
)

# 🔴 §5.3「**不含**」+ §6「日志不含」+ 红线 2:任何命中都必须被挡在包外
$script:QtDiagExcludePatterns = @(
    '\\winagent\\vault\\'          # vault blobs 与熵文件
    'entropy\.bin$'
    '\\xwechat_files\\'            # 微信数据
    '\\Tencent\\xwechat\\'
    'winagent\.token$'             # Agent → WinAgent 令牌
    '\.wslconfig\.bak-.*\.key$'
    '\\QTrade-WeChat-Backup\\'     # 微信备份目录
)

function Get-QtDiagItemList { [CmdletBinding()] param() return , $script:QtDiagItems }
function Get-QtDiagExcludePatterns { [CmdletBinding()][OutputType([string[]])] param() return , $script:QtDiagExcludePatterns }

function Test-QtDiagExclusions {
    <#
    .SYNOPSIS
        纯函数:给一组待打包的路径,回被排除清单命中的那些。
        🔴 调用方**必须**在压包前跑一遍并把命中项剔掉 —— 这是红线 2 的执行点,不是提醒。
    .OUTPUTS
        {Ok, Blocked[]}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Paths)
    $blocked = @()
    foreach ($p in $Paths) {
        foreach ($pat in $script:QtDiagExcludePatterns) {
            if ($p -match $pat) { $blocked += $p; break }
        }
    }
    return [pscustomobject]@{ Ok = ($blocked.Count -eq 0); Blocked = $blocked }
}

function Add-QtDiagText {
    <#
    .SYNOPSIS
        把一段命令输出写成 `<staging>\<name>.txt`(先抹令牌/口令,再落盘)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $StagingDir,
        [Parameter(Mandatory)][string] $Name,
        [AllowNull()][AllowEmptyString()][string] $Text
    )
    New-QtDirectory -Path $StagingDir | Out-Null
    $safe = Protect-QtLogText -Text $Text
    $path = Join-Path $StagingDir ($Name + '.txt')
    [IO.File]::WriteAllText($path, [string]$safe, (New-Object Text.UTF8Encoding($false)))
    return $path
}

function Copy-QtDiagFile {
    <#
    .SYNOPSIS
        把一个文件拷进暂存区;命中排除清单则**拒绝**(回 '' 而不是静默跳过 —— 调用方要记账)。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $StagingDir,
        [Parameter(Mandatory)][string] $Source,
        [string] $AsName = ''
    )
    if (-not (Test-QtPath -Path $Source)) { return '' }
    $check = Test-QtDiagExclusions -Paths @($Source)
    if (-not $check.Ok) { return '' }
    New-QtDirectory -Path $StagingDir | Out-Null
    $name = $AsName
    if (-not $name) { $name = Split-Path -Leaf $Source }
    $dest = Join-Path $StagingDir $name
    Copy-Item -LiteralPath $Source -Destination $dest -Force
    return $dest
}

function New-QtDiagBundle {
    <#
    .SYNOPSIS
        §5.3:失败页【导出诊断包】→ `%ProgramData%\QTrade\logs\diag-<ts>.zip`。
    .PARAMETER IncludeDmesg
        `[install] diag_include_dmesg`(§7);为 $false 时不取 kcheck 的 dmesg。
    .OUTPUTS
        {Ok, ZipPath, Included[], Skipped[], Blocked[]}
    #>
    [CmdletBinding()]
    param(
        [string] $Root = '',
        [string] $Stamp = '',
        [bool] $IncludeDmesg = $true,
        [string] $DistroName = 'qtrade',
        [switch] $SkipWslCommands
    )
    $paths = Get-QtPaths -Root $Root
    if (-not $Stamp) { $Stamp = Get-QtTimestamp }
    $staging = Join-Path $paths.Diag ('diag-{0}' -f $Stamp)
    $zip = Join-Path $paths.Logs ('diag-{0}.zip' -f $Stamp)
    New-QtDirectory -Path $staging | Out-Null

    $included = @()
    $skipped = @()
    $blocked = @()

    # ── 文件类 ────────────────────────────────────────────────────────────
    foreach ($f in @(
            @{ Src = $paths.StateFile; As = 'install_state.json' }
            @{ Src = $paths.KernelPtr; As = 'kernel-current.json' }
            @{ Src = $paths.Manifest; As = 'manifest.json' }
        )) {
        $r = Copy-QtDiagFile -StagingDir $staging -Source $f.Src -AsName $f.As
        if ($r) { $included += $f.As } else { $skipped += $f.As }
    }

    # 引擎日志 + 子日志
    if (Test-QtPath -Path $paths.Logs) {
        $logDir = Join-Path $staging 'logs'
        New-QtDirectory -Path $logDir | Out-Null
        foreach ($lf in @(Get-ChildItem -LiteralPath $paths.Logs -Filter 'install-*.log' -File -ErrorAction SilentlyContinue)) {
            $r = Copy-QtDiagFile -StagingDir $logDir -Source $lf.FullName
            if ($r) { $included += ('logs/' + $lf.Name) }
        }
    }

    # `.wslconfig` 当前与备份
    $wslConfig = Join-Path (Get-QtEnvironmentPath -Name 'UserProfile') '.wslconfig'
    if (Copy-QtDiagFile -StagingDir $staging -Source $wslConfig -AsName 'wslconfig-current.txt') { $included += 'wslconfig-current.txt' }
    if (Test-QtPath -Path $paths.Wsl) {
        $bakDir = Join-Path $staging 'wslconfig-backups'
        New-QtDirectory -Path $bakDir | Out-Null
        foreach ($bf in @(Get-ChildItem -LiteralPath $paths.Wsl -Filter '.wslconfig.bak-*' -File -ErrorAction SilentlyContinue)) {
            if (Copy-QtDiagFile -StagingDir $bakDir -Source $bf.FullName) { $included += ('wslconfig-backups/' + $bf.Name) }
        }
    }

    # ── 命令类 ────────────────────────────────────────────────────────────
    if (-not $SkipWslCommands) {
        foreach ($c in @(
                @{ Name = 'wsl-version'; Args = @('--version') }
                @{ Name = 'wsl-status'; Args = @('--status') }
                @{ Name = 'wsl-list'; Args = @('--list', '--verbose') }
            )) {
            $r = Invoke-QtWsl -WslArgs $c.Args -TimeoutSec 30
            Add-QtDiagText -StagingDir $staging -Name $c.Name -Text ($r.StdOut + "`n" + $r.StdErr) | Out-Null
            $included += $c.Name
        }
        if ($IncludeDmesg) {
            $kname = Get-QtKCheckDistroName
            $listed = Invoke-QtWsl -WslArgs @('--list', '--quiet') -TimeoutSec 30
            $present = (-not $listed.TimedOut) -and ($listed.ExitCode -eq 0) -and ($listed.StdOut -match [regex]::Escape($kname))
            if (-not $present) {
                Add-QtDiagText -StagingDir $staging -Name 'kcheck-dmesg' -Text '采集时发行版已注销' | Out-Null
                $included += 'kcheck-dmesg'
            } else {
                $r = Invoke-QtWsl -WslArgs @('-d', $kname, '--user', 'root', '--exec', 'sh', '-c', 'dmesg | tail -200') -TimeoutSec 60
                if (-not $r.TimedOut -and $r.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($r.StdOut)) {
                    Add-QtDiagText -StagingDir $staging -Name 'kcheck-dmesg' -Text $r.StdOut | Out-Null
                    $included += 'kcheck-dmesg'
                } else {
                    Add-QtDiagText -StagingDir $staging -Name 'kcheck-dmesg' -Text '采集时发行版已注销' | Out-Null
                    $included += 'kcheck-dmesg'
                }
            }
        } else { $skipped += 'kcheck-dmesg(diag_include_dmesg=false)' }

        $dl = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c',
            'for c in $(docker ps -aq 2>/dev/null | head -5); do echo ===== $c; docker logs --tail 40 $c 2>&1; done') -TimeoutSec 120
        Add-QtDiagText -StagingDir $staging -Name 'docker-logs' -Text ($dl.StdOut + "`n" + $dl.StdErr) | Out-Null
        $included += 'docker-logs'
    }

    # Windows 侧:可选功能状态、WSL 策略键、WSL 事件日志
    $feat = @()
    foreach ($n in @('VirtualMachinePlatform', 'Microsoft-Windows-Subsystem-Linux')) {
        $feat += ('{0} = {1}' -f $n, (Get-QtOptionalFeatureState -FeatureName $n))
    }
    Add-QtDiagText -StagingDir $staging -Name 'optional-features' -Text ($feat -join "`n") | Out-Null
    $included += 'optional-features'

    $policy = @()
    foreach ($k in @('AllowCustomKernelUserSetting', 'AllowInboxWSL', 'AllowWSL1',
            'AllowKernelCommandLineUserSetting', 'AllowCustomSystemDistroUserSetting', 'AllowNestedVirtualization')) {
        $v = Get-QtRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WSL' -Name $k
        $policy += ('{0} = {1}' -f $k, $(if ($null -eq $v) { '<未设置>' } else { [string]$v }))
    }
    Add-QtDiagText -StagingDir $staging -Name 'policy-keys' -Text ($policy -join "`n") | Out-Null
    $included += 'policy-keys'

    # §2.6.8 第 8 条:`CreateVm`/`WSAENOTCONN` 这类 K1 型失败**只在这里**有痕迹
    $evt = ''
    try {
        $evt = (Get-WinEvent -LogName 'Microsoft-Windows-WSL/Operational' -MaxEvents 50 -ErrorAction Stop |
            ForEach-Object { '{0} {1} {2}' -f $_.TimeCreated, $_.Id, $_.Message }) -join "`n"
    } catch { $evt = '(该事件日志不存在或不可读)' }
    Add-QtDiagText -StagingDir $staging -Name 'wsl-eventlog' -Text $evt | Out-Null
    $included += 'wsl-eventlog'

    foreach ($hv in @(
            @{ Name = 'Hyper-V-Compute-Admin'; Log = 'Microsoft-Windows-Hyper-V-Compute-Admin' }
            @{ Name = 'Hyper-V-Worker-Admin'; Log = 'Microsoft-Windows-Hyper-V-Worker-Admin' }
        )) {
        $hvText = ''
        try {
            $hvText = (Get-WinEvent -LogName $hv.Log -MaxEvents 40 -ErrorAction Stop |
                ForEach-Object { '{0} {1} {2}' -f $_.TimeCreated, $_.Id, $_.Message }) -join "`n"
        } catch { $hvText = '(该事件日志不存在或不可读)' }
        if ([string]::IsNullOrWhiteSpace($hvText)) { $hvText = '(该事件日志不存在或不可读)' }
        Add-QtDiagText -StagingDir $staging -Name $hv.Name -Text $hvText | Out-Null
        $included += $hv.Name
    }

    # ── 压包前最后一道排除检查(红线 2 的执行点)────────────────────────
    $all = @(Get-ChildItem -LiteralPath $staging -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $check = Test-QtDiagExclusions -Paths $all
    foreach ($b in $check.Blocked) {
        Remove-QtItem -Path $b
        $blocked += $b
    }

    if (Test-QtPath -Path $zip) { Remove-QtItem -Path $zip }
    New-QtDirectory -Path $paths.Logs | Out-Null
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -Force
    Remove-QtItem -Path $staging -Recurse

    return [pscustomobject]@{ Ok = (Test-QtPath -Path $zip); ZipPath = $zip; Included = $included; Skipped = $skipped; Blocked = $blocked }
}

Export-ModuleMember -Function Get-QtDiagItemList, Get-QtDiagExcludePatterns, Test-QtDiagExclusions,
Add-QtDiagText, Copy-QtDiagFile, New-QtDiagBundle
