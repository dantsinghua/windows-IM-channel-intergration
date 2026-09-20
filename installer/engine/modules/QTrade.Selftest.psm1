# QTrade 安装引擎 —— 连通性探测调用(§2.10)与自检 SELFTEST_OK(§2.11)
# 规格:docs/03 §2.10(调用方向恒 Agent → WinAgent,两条路径)、§2.11(三项自检 + 可选 napcat)、
#       §3.2(引擎调用的 API)、§7 `[probe] install_targets`、§2.3(失败码);验收 M1-16 / M1-17
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.WinAgent.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Distro.psm1') -DisableNameChecking

# docs/03 §7 `[probe] install_targets`:SELFTEST 经 Agent 探全部十个 probe_target(基线 §8.5)
$script:QtInstallTargets = @(
    'apk_url', 'mail_pop3', 'mail_imap', 'mail_smtp', 'qidian_msf', 'qq_servers',
    'wechat_servers', 'docker_registry', 'winagent_from_wsl', 'agent_from_windows'
)
# §2.10:Agent 不可达时只有 Windows 侧结果是真的,WSL 侧一律记 SKIPPED
$script:QtWindowsSideTargets = @('wechat_servers', 'agent_from_windows')
$script:QtSelftestContainer = 'qtrade-selftest'
$script:QtSelftestAdbPort = 16099        # §7 [selftest];账号段最后一个,永不分配给账号(基线 §3 v1.1)
$script:QtRedroidBootTimeoutSec = 180    # 只引用 02 `[runtime] boot_timeout_s`(C-43),本册不另设键

function Get-QtInstallTargets { [CmdletBinding()][OutputType([string[]])] param() return , $script:QtInstallTargets }
function Get-QtWindowsSideTargets { [CmdletBinding()][OutputType([string[]])] param() return , $script:QtWindowsSideTargets }
function Get-QtSelftestAdbPort { [CmdletBinding()][OutputType([int])] param() return $script:QtSelftestAdbPort }
function Get-QtRedroidBootTimeout { [CmdletBinding()][OutputType([int])] param() return $script:QtRedroidBootTimeoutSec }

function Split-QtProbeTargetsBySide {
    <#
    .SYNOPSIS
        纯函数:把探测目标分成「Windows 侧可探」与「WSL 侧(Agent 不可达时只能 SKIPPED)」。
        依据 docs/03 §2.10 第 2/3 行:WSL 侧目标 = apk_url / docker_registry / winagent_from_wsl /
        mail_* / qidian_msf / qq_servers 的 WSL 出网;Windows 侧 = wechat_servers / agent_from_windows。
    .OUTPUTS
        {Windows[], Wsl[]}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Targets)
    $win = @($Targets | Where-Object { $script:QtWindowsSideTargets -contains $_ })
    $wsl = @($Targets | Where-Object { $script:QtWindowsSideTargets -notcontains $_ })
    return [pscustomobject]@{ Windows = $win; Wsl = $wsl }
}

function Invoke-QtProbe {
    <#
    .SYNOPSIS
        §2.10:**调用方向恒 Agent → WinAgent**,安装器不让 WinAgent 反向找 Agent。

        - Agent 可达   → `POST /api/v1/system/probe { targets, trigger:"install" }`(主路径,Agent 分侧)
        - Agent 不可达 → 直调 WinAgent `POST /wa/v1/probe { targets }`(备用路径),
          **WSL 侧目标一律记 `SKIPPED` + `detail="agent unreachable"`**(基线 §8.5 v1.1 新增值,04-P1),
          完成页把这些行标「未探测(Agent 未就绪)」**而不是「不通」**(验收 M1-17)。

        🔴 任何探测结果都不阻断安装(§2.4.6 末)。
    .OUTPUTS
        {Via ∈ agent|winagent, Results[{target,result,via,detail}]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]] $Targets,
        [string] $Trigger = 'install',
        [string] $Detail = 'selftest',
        [string] $AgentBaseUrl = 'http://127.0.0.1:17600',
        [string] $WinAgentBaseUrl = 'http://127.0.0.1:17610',
        [int] $TimeoutSec = 120
    )
    $health = Get-QtAgentHealth -BaseUrl $AgentBaseUrl
    if ($health.Ok) {
        $body = (@{ targets = $Targets; trigger = $Trigger } | ConvertTo-Json -Depth 4 -Compress)
        $r = Invoke-QtHttp -Uri ($AgentBaseUrl + '/api/v1/system/probe') -Method POST -Body $body -TimeoutSec $TimeoutSec
        if ($r.Ok) {
            return [pscustomobject]@{ Via = 'agent'; Results = (ConvertFrom-QtProbeBody -Body $r.Body -Via 'agent' -Detail $Detail) }
        }
    }

    # 备用路径:只有 Windows 侧是真结论
    $split = Split-QtProbeTargetsBySide -Targets $Targets
    $results = @()
    if ($split.Windows.Count -gt 0) {
        $body = (@{ targets = $split.Windows } | ConvertTo-Json -Depth 4 -Compress)
        $r = Invoke-QtHttp -Uri ($WinAgentBaseUrl + '/wa/v1/probe') -Method POST -Body $body -TimeoutSec $TimeoutSec
        if ($r.Ok) { $results += (ConvertFrom-QtProbeBody -Body $r.Body -Via 'winagent' -Detail $Detail) }
    }
    foreach ($t in $split.Wsl) {
        $results += [pscustomobject]@{ target = $t; result = 'SKIPPED'; via = 'winagent'; detail = 'agent unreachable' }
    }
    return [pscustomobject]@{ Via = 'winagent'; Results = $results }
}

function ConvertFrom-QtProbeBody {
    <#
    .SYNOPSIS
        把端点回的 `probe_result[]` 归一成 {target, result, via, detail}。解析不了回空集合(不抛)。
    #>
    [CmdletBinding()]
    param(
        [AllowNull()][AllowEmptyString()][string] $Body,
        [Parameter(Mandatory)][string] $Via,
        [AllowEmptyString()][string] $Detail = ''
    )
    $out = @()
    if ([string]::IsNullOrWhiteSpace($Body)) { return , $out }
    try { $j = $Body | ConvertFrom-Json } catch { return , $out }
    $rows = $j
    if (Test-QtHasProperty -Object $j -Name 'results') { $rows = $j.results }
    foreach ($row in @($rows)) {
        if ($null -eq $row -or -not (Test-QtHasProperty -Object $row -Name 'target')) { continue }
        $res = ''
        if (Test-QtHasProperty -Object $row -Name 'result') { $res = [string]$row.result }
        $d = $Detail
        if ((Test-QtHasProperty -Object $row -Name 'detail') -and $row.detail) { $d = [string]$row.detail }
        $out += [pscustomobject]@{ target = [string]$row.target; result = $res; via = $Via; detail = $d }
    }
    return , $out
}

function Test-QtSelftestWinAgent {
    <#
    .SYNOPSIS
        §2.11 第 1 项:`GET /wa/v1/health` 200 且 `vault:ok`。
        🔴 R4-10:`user_agent` **允许 `false`** —— 静默/无人值守安装时安装用户尚未登录、会话代理不在属正常态;
        此前 §2.11 漏改仍要 `true`,无人值守装到这一步永远到不了 DONE。
    .OUTPUTS
        {Ok, Reason, UserAgent, Vault}
    #>
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17610')
    $h = Get-QtWinAgentHealth -BaseUrl $BaseUrl
    if (-not $h.Ok) { return [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_WINAGENT'; UserAgent = $h.UserAgent; Vault = $h.Vault } }
    if ($h.Vault -ne 'ok') { return [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_WINAGENT'; UserAgent = $h.UserAgent; Vault = $h.Vault } }
    return [pscustomobject]@{ Ok = $true; Reason = ''; UserAgent = $h.UserAgent; Vault = $h.Vault }
}

function Test-QtSelftestAgent {
    <#
    .SYNOPSIS
        §2.11 第 2 项:`GET /api/v1/system/health` 200 且 `docker:ok`、`db:ok`、`winagent_reachable:true`
        (后者同时就是 `winagent_from_wsl` 探测,§2.10 不重复探)。
    .OUTPUTS
        {Ok, Reason, Health}
    #>
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17600')
    $h = Get-QtAgentHealth -BaseUrl $BaseUrl
    $ok = ($h.Ok -and $h.Docker -eq 'ok' -and $h.Db -eq 'ok' -and $h.WinAgentReachable)
    return [pscustomobject]@{ Ok = $ok; Reason = $(if ($ok) { '' } else { 'SELFTEST_AGENT' }); Health = $h }
}

function Invoke-QtSelftestRedroid {
    <#
    .SYNOPSIS
        §2.11 第 3 项:`POST /api/v1/system/selftest` → `202 {run_id}`,再轮询 `GET …/selftest/{run_id}`。
        容器 `qtrade-selftest` 绑 `127.0.0.1:16099`,轮询 `getprop sys.boot_completed`=1,
        超时取 02 `[runtime] boot_timeout_s` = **180 s**(C-43,本册不另设键);完了 `docker rm -f`。
        失败 → `SELFTEST_REDROID_BOOT`,附 `docker logs` 前 40 行。
    .OUTPUTS
        {Ok, Reason, RunId, ElapsedSec, Logs}
    #>
    [CmdletBinding()]
    param(
        [string] $BaseUrl = 'http://127.0.0.1:17600',
        [int] $TimeoutSec = 0,
        [int] $PollSec = 5,
        [string] $DistroName = 'qtrade'
    )
    if ($TimeoutSec -le 0) { $TimeoutSec = $script:QtRedroidBootTimeoutSec }
    $start = Get-QtNow
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/api/v1/system/selftest') -Method POST -Body '{}' -TimeoutSec 30
    if (-not $r.Ok -or $r.StatusCode -ne 202) {
        return [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_REDROID_BOOT'; RunId = ''; ElapsedSec = 0; Logs = $r.Body }
    }
    $runId = ''
    try {
        $j = $r.Body | ConvertFrom-Json
        if (Test-QtHasProperty -Object $j -Name 'run_id') { $runId = [string]$j.run_id }
    } catch { }
    if (-not $runId) {
        return [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_REDROID_BOOT'; RunId = ''; ElapsedSec = 0; Logs = '202 未带 run_id' }
    }

    $deadline = $start.AddSeconds($TimeoutSec)
    while ((Get-QtNow) -lt $deadline) {
        $q = Invoke-QtHttp -Uri ($BaseUrl + '/api/v1/system/selftest/' + $runId) -Method GET -TimeoutSec 30
        $status = ''
        if ($q.Ok -and $q.Body) {
            try {
                $j = $q.Body | ConvertFrom-Json
                if (Test-QtHasProperty -Object $j -Name 'status') { $status = [string]$j.status }
            } catch { }
        }
        if ($status -in @('ok', 'passed', 'succeeded')) {
            $elapsed = [int]((Get-QtNow) - $start).TotalSeconds
            return [pscustomobject]@{ Ok = $true; Reason = ''; RunId = $runId; ElapsedSec = $elapsed; Logs = '' }
        }
        if ($status -in @('failed', 'error')) { break }
        Start-QtSleep -Seconds $PollSec
    }
    $logs = Get-QtContainerLogsTail -Container $script:QtSelftestContainer -Lines 40 -DistroName $DistroName
    $elapsed = [int]((Get-QtNow) - $start).TotalSeconds
    return [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_REDROID_BOOT'; RunId = $runId; ElapsedSec = $elapsed; Logs = $logs }
}

function Invoke-QtSelftestNapcat {
    <#
    .SYNOPSIS
        §2.11 第 4 项(**可选**,`[selftest] napcat_check`):`docker run --rm … --version`,轻,10 s;
        🔴 失败**只警告**,不影响 SELFTEST_OK。
    #>
    [CmdletBinding()]
    param([string] $DistroName = 'qtrade', [string] $ImageRef = '', [int] $TimeoutSec = 30)
    if (-not $ImageRef) { return [pscustomobject]@{ Ok = $true; Skipped = $true; Message = '未给镜像 ref,跳过' } }
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'run', '--rm', $ImageRef, '--version') -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) {
        return [pscustomobject]@{ Ok = $false; Skipped = $false; Message = 'napcat 自检未通过(只警告,不阻断)' }
    }
    return [pscustomobject]@{ Ok = $true; Skipped = $false; Message = '' }
}

function Invoke-QtSelftest {
    <#
    .SYNOPSIS
        §2.11 编排:三项自检(顺序固定,首个失败即定原因码)+ 可选 napcat + §2.10 全量探测。
        三项全过才 `SELFTEST_OK`。
    .OUTPUTS
        {Ok, Reason, WinAgent, Agent, Redroid, Napcat, Probes, Warnings[]}
    #>
    [CmdletBinding()]
    param(
        [string] $AgentBaseUrl = 'http://127.0.0.1:17600',
        [string] $WinAgentBaseUrl = 'http://127.0.0.1:17610',
        [string] $DistroName = 'qtrade',
        [string] $NapcatImageRef = '',
        [switch] $SkipNapcat,
        [string[]] $ProbeTargets = @()
    )
    $warnings = @()
    $wa = Test-QtSelftestWinAgent -BaseUrl $WinAgentBaseUrl
    if (-not $wa.Ok) {
        return [pscustomobject]@{ Ok = $false; Reason = $wa.Reason; WinAgent = $wa; Agent = $null; Redroid = $null; Napcat = $null; Probes = $null; Warnings = $warnings }
    }
    if (-not $wa.UserAgent) {
        # R4-10 / R3-16:不判失败,只在完成页明示
        $warnings += '会话代理未上线:首次使用需登录 Windows 桌面(可锁屏,不要注销),账号才会运行'
    }

    $ag = Test-QtSelftestAgent -BaseUrl $AgentBaseUrl
    if (-not $ag.Ok) {
        # Agent 不可达时仍给一份网络汇总(§2.10 第 3 行);WSL 侧记 SKIPPED
        $probes = Invoke-QtProbe -Targets $(if ($ProbeTargets.Count -gt 0) { $ProbeTargets } else { $script:QtInstallTargets }) `
            -AgentBaseUrl $AgentBaseUrl -WinAgentBaseUrl $WinAgentBaseUrl
        return [pscustomobject]@{ Ok = $false; Reason = $ag.Reason; WinAgent = $wa; Agent = $ag; Redroid = $null; Napcat = $null; Probes = $probes; Warnings = $warnings }
    }

    $rd = Invoke-QtSelftestRedroid -BaseUrl $AgentBaseUrl -DistroName $DistroName
    if (-not $rd.Ok) {
        return [pscustomobject]@{ Ok = $false; Reason = $rd.Reason; WinAgent = $wa; Agent = $ag; Redroid = $rd; Napcat = $null; Probes = $null; Warnings = $warnings }
    }

    $np = $null
    if (-not $SkipNapcat) {
        $np = Invoke-QtSelftestNapcat -DistroName $DistroName -ImageRef $NapcatImageRef
        if (-not $np.Ok) { $warnings += $np.Message }
    }

    $probes = Invoke-QtProbe -Targets $(if ($ProbeTargets.Count -gt 0) { $ProbeTargets } else { $script:QtInstallTargets }) `
        -AgentBaseUrl $AgentBaseUrl -WinAgentBaseUrl $WinAgentBaseUrl
    return [pscustomobject]@{ Ok = $true; Reason = ''; WinAgent = $wa; Agent = $ag; Redroid = $rd; Napcat = $np; Probes = $probes; Warnings = $warnings }
}

function Test-QtSelftestComplete {
    <#
    .SYNOPSIS
        SELFTEST_OK 幂等判据(§2.3):三项 OK。自检便宜,直接重跑前两项 + 不重起容器。
        🔴 第 3 项(临时 redroid)**不作幂等判据** —— 它是一次性动作、跑完就 `docker rm -f`,
        用「容器还在不在」判会把「已清理干净」误判成「没做过」。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    $wa = Test-QtSelftestWinAgent -BaseUrl ([string]$Context.WinAgentBaseUrl)
    if (-not $wa.Ok) { return $false }
    return (Test-QtSelftestAgent -BaseUrl ([string]$Context.AgentBaseUrl)).Ok
}

Register-QtStepCheck -Step 'SELFTEST_OK' -Check { param($ctx) Test-QtSelftestComplete -Context $ctx }

Export-ModuleMember -Function Get-QtInstallTargets, Get-QtWindowsSideTargets, Get-QtSelftestAdbPort,
Get-QtRedroidBootTimeout, Split-QtProbeTargetsBySide, Invoke-QtProbe, ConvertFrom-QtProbeBody,
Test-QtSelftestWinAgent, Test-QtSelftestAgent, Invoke-QtSelftestRedroid, Invoke-QtSelftestNapcat,
Invoke-QtSelftest, Test-QtSelftestComplete
