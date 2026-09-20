# QTrade 安装引擎 —— 安装期状态机 / install_state.json / 可重入
# 规格:docs/03 §2.3(schema、状态图、每步幂等判据)、§2.12(重启续跑)、§3.1(文件出处)、§3.3(镜像进 winagent.db 的列)
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking

$script:QtStateSchema = 1

# 每步的幂等判据(docs/03 §2.3「已完成判据」列)。各功能模块在加载时经 Register-QtStepCheck 注册自己那条;
# 判据故意做成可注册的 scriptblock,单测可以只注册假判据、不碰真机。
$script:QtStepChecks = @{}

function Get-QtPaths {
    <#
    .SYNOPSIS
        基线 §4 的固定目录布局。$Root 缺省 `%ProgramData%\QTrade`(SFX `-o` 写死,用户不可选,docs/03 §2.1)。
    #>
    [CmdletBinding()]
    param([string] $Root)
    if (-not $Root) { $Root = Join-Path (Get-QtEnvironmentPath -Name 'ProgramData') 'QTrade' }
    return [pscustomobject]@{
        Root        = $Root
        Install     = (Join-Path $Root 'install')
        Engine      = (Join-Path $Root 'install\engine')
        Diag        = (Join-Path $Root 'install\diag')
        Kernel      = (Join-Path $Root 'kernel')
        Wsl         = (Join-Path $Root 'wsl')
        Distro      = (Join-Path $Root 'wsl\distro')
        KCheck      = (Join-Path $Root 'wsl\kcheck')
        Pkg         = (Join-Path $Root 'pkg')
        WinAgent    = (Join-Path $Root 'winagent')
        Console     = (Join-Path $Root 'console')
        Logs        = (Join-Path $Root 'logs')
        WeChat      = (Join-Path $Root 'wechat')
        StateFile   = (Join-Path $Root 'install\install_state.json')
        Manifest    = (Join-Path $Root 'install\manifest.json')
        KernelPtr   = (Join-Path $Root 'kernel\current.json')
        EngineExe   = (Join-Path $Root 'install\engine\qtrade-setup-engine.exe')
        SummaryFile = (Join-Path $Root 'logs\install-summary.txt')
    }
}

function New-QtInstallState {
    <#
    .SYNOPSIS
        建一份空的 install_state(docs/03 §2.3 schema 逐字段)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $PackageVersion,
        [datetime] $At
    )
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }
    $iso = Get-QtIso8601 -At $At
    return [pscustomobject]@{
        schema          = $script:QtStateSchema
        package_version = $PackageVersion
        started_at      = $iso
        updated_at      = $iso
        state           = 'PRECHECK'
        substate        = ''
        parked          = $null
        env             = [pscustomobject]@{}
        wslconfig       = [pscustomobject]@{ backup = ''; keys_added = @(); kept = @(); changed = @(); replaced_kernel = ''; kernel_line_written = '' }
        distro          = [pscustomobject]@{ name = 'qtrade'; dir = ''; imported_at = ''; became_default = $false }
        clients         = [pscustomobject]@{}
        probes          = @()
        firewall_rules  = ''
        resume          = [pscustomobject]@{ runonce_armed = $false; engine = ''; source_exe = '' }
        history         = @()
    }
}

function Read-QtInstallState {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { return $null }
    $text = Read-QtTextFile -Path $Path
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return ($text | ConvertFrom-Json)
}

function Write-QtInstallState {
    <#
    .SYNOPSIS
        **原子写**:写临时文件再替换(docs/03 §2.3)。每次写都刷 updated_at。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [Parameter(Mandatory)][string] $Path,
        [datetime] $At
    )
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }
    $State.updated_at = Get-QtIso8601 -At $At
    $dir = Split-Path -Parent $Path
    New-QtDirectory -Path $dir | Out-Null
    $tmp = $Path + '.tmp'
    $json = $State | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-QtFileAtomic -Source $tmp -Destination $Path | Out-Null
    return $Path
}

function Add-QtHistory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [Parameter(Mandatory)][AllowEmptyString()][string] $From,
        [Parameter(Mandatory)][string] $To,
        [string] $Note = '',
        [datetime] $At
    )
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }
    $entry = [pscustomobject]@{ at = (Get-QtIso8601 -At $At); from = $From; to = $To; note = $Note }
    $State.history = @($State.history) + $entry
    return $State
}

function Set-QtState {
    <#
    .SYNOPSIS
        迁状态 + 记 history + 清 substate。$To 可以是状态名,也可以是 `FAILED:<步>:<原因码>`。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [Parameter(Mandatory)][string] $To,
        [string] $Note = '',
        [datetime] $At
    )
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }
    if ($To -notlike 'FAILED:*' -and (Get-QtStateNames) -notcontains $To) {
        throw ('未知状态:{0}(基线 §8.2 / docs/03 §2.3 键名)' -f $To)
    }
    $from = [string]$State.state
    $State.state = $To
    $State.substate = ''
    Add-QtHistory -State $State -From $from -To $To -Note $Note -At $At | Out-Null
    return $State
}

function Set-QtSubstate {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $State, [Parameter(Mandatory)][AllowEmptyString()][string] $Substate)
    $State.substate = $Substate
    return $State
}

function Set-QtParked {
    <#
    .SYNOPSIS
        停车 —— **不是状态**,记在 parked 字段(docs/03 §2.3 图末行、基线 §8.2 v1.1 C-36)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [Parameter(Mandatory)][string] $Step,
        [Parameter(Mandatory)][string] $Reason,
        [datetime] $At
    )
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }
    if ((Get-QtStateNames) -notcontains $Step) { throw ('未知步名:{0}' -f $Step) }
    $State.parked = [pscustomobject]@{ step = $Step; reason = $Reason; since = (Get-QtIso8601 -At $At) }
    return $State
}

function Clear-QtParked {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $State)
    $State.parked = $null
    return $State
}

function Test-QtParked {
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $State)
    return ($null -ne $State.parked)
}

function Split-QtFailedState {
    <#
    .SYNOPSIS
        拆 `FAILED:<步名>:<原因码>`。非 FAILED 回 $null。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $State)
    $m = [regex]::Match($State, '^FAILED:([A-Z_]+):([A-Z0-9_]+)$')
    if (-not $m.Success) { return $null }
    return [pscustomobject]@{ Step = $m.Groups[1].Value; Reason = $m.Groups[2].Value }
}

function Get-QtResumeStep {
    <#
    .SYNOPSIS
        可重入总则(docs/03 §2.3):引擎启动 → 读 state → 决定从哪一步开始。
    .DESCRIPTION
        - `FAILED:<步>:<码>`        → 重做该步
        - `KERNEL_ROLLED_BACK`      → 回到 `WSLCONFIG_WRITTEN`(用户点【重试切换】走 §2.6.3 确认页)
        - `REBOOT_PENDING`          → 回 `WSL_FEATURE` 复核(功能 Enabled 且不再要求重启则进 WSL_MSI,§2.5.1)
        - `DONE`                    → $null(没有下一步;升级/修复由 /QT_MODE 决定)
        - 其余正常状态 S            → 链上 S 的下一步
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $State)
    $failed = Split-QtFailedState -State $State
    if ($failed) { return $failed.Step }
    if ($State -eq 'KERNEL_ROLLED_BACK') { return 'WSLCONFIG_WRITTEN' }
    if ($State -eq 'REBOOT_PENDING') { return 'WSL_FEATURE' }
    if ($State -eq 'DONE') { return $null }
    $chain = Get-QtStateChain
    $i = [array]::IndexOf($chain, $State)
    if ($i -lt 0) { throw ('未知状态:{0}' -f $State) }
    if ($i -ge $chain.Count - 1) { return $null }
    return $chain[$i + 1]
}

function Register-QtStepCheck {
    <#
    .SYNOPSIS
        注册某步的幂等判据(docs/03 §2.3「已完成判据」列)。判据 scriptblock 收一个 $Context,回 [bool]。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Step,
        [Parameter(Mandatory)][scriptblock] $Check
    )
    if ((Get-QtStateNames) -notcontains $Step) { throw ('未知步名:{0}' -f $Step) }
    $script:QtStepChecks[$Step] = $Check
}

function Get-QtRegisteredStepCheck {
    [CmdletBinding()]
    param([string] $Step)
    if ($Step) {
        if ($script:QtStepChecks.ContainsKey($Step)) { return $script:QtStepChecks[$Step] }
        return $null
    }
    return $script:QtStepChecks
}

function Clear-QtStepChecks { [CmdletBinding()] param() $script:QtStepChecks = @{} }

function Test-QtStepComplete {
    <#
    .SYNOPSIS
        跑某步的幂等判据。**PRECHECK 无判据**(docs/03 §2.3:「无(每次都重跑,便宜)」)恒回 $false。
        未注册判据的步同样回 $false(宁可重做,不可误 skip)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param(
        [Parameter(Mandatory)][string] $Step,
        [Parameter(Mandatory)] $Context
    )
    if ($Step -eq 'PRECHECK') { return $false }
    $check = Get-QtRegisteredStepCheck -Step $Step
    if ($null -eq $check) { return $false }
    return [bool](& $check $Context)
}

function Set-QtRunOnce {
    <#
    .SYNOPSIS
        写/清 RunOnce `QTradeSetupResume`(docs/03 §2.5.1 / §2.6.3 / §2.12 / §2.15)。
        值恒指向**落盘引擎**,不指向原 EXE。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [Parameter(Mandatory)][string] $EnginePath,
        [ValidateSet('resume', 'verify-kernel')][string] $Mode = 'resume',
        [string] $RegistryPath = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'
    )
    $value = '"{0}" /QT_MODE={1}' -f $EnginePath, $Mode
    Set-QtRegistryValue -Path $RegistryPath -Name 'QTradeSetupResume' -Value $value
    $State.resume.runonce_armed = $true
    $State.resume.engine = $EnginePath
    return $value
}

function Clear-QtRunOnce {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [string] $RegistryPath = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'
    )
    Remove-QtRegistryValue -Path $RegistryPath -Name 'QTradeSetupResume'
    $State.resume.runonce_armed = $false
    return $State
}

function ConvertTo-QtWinAgentRow {
    <#
    .SYNOPSIS
        把 install_state 摊成 winagent.db.install_state 的单行列(docs/03 §3.3,C-36:镜像即整体覆盖)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $State, [datetime] $At)
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }
    $parkedJson = 'null'
    if ($null -ne $State.parked) { $parkedJson = ($State.parked | ConvertTo-Json -Depth 6 -Compress) }
    return [ordered]@{
        key             = 'current'
        state           = [string]$State.state
        substate        = [string]$State.substate
        parked_json     = $parkedJson
        package_version = [string]$State.package_version
        env_json        = ($State.env | ConvertTo-Json -Depth 10 -Compress)
        wslconfig_json  = ($State.wslconfig | ConvertTo-Json -Depth 10 -Compress)
        distro_json     = ($State.distro | ConvertTo-Json -Depth 10 -Compress)
        clients_json    = ($State.clients | ConvertTo-Json -Depth 10 -Compress)
        resume_json     = ($State.resume | ConvertTo-Json -Depth 10 -Compress)
        updated_ms      = (Get-QtEpochMs -At $At)
    }
}

Export-ModuleMember -Function Get-QtPaths, New-QtInstallState, Read-QtInstallState, Write-QtInstallState,
Add-QtHistory, Set-QtState, Set-QtSubstate, Set-QtParked, Clear-QtParked, Test-QtParked,
Split-QtFailedState, Get-QtResumeStep, Register-QtStepCheck, Get-QtRegisteredStepCheck,
Clear-QtStepChecks, Test-QtStepComplete, Set-QtRunOnce, Clear-QtRunOnce, ConvertTo-QtWinAgentRow
