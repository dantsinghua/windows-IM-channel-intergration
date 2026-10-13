# QTrade 安装引擎 —— 防火墙(只调 WinAgent,自己不建规则)
# 规格:docs/03 §2.8.3 第 7 步、§2.14 第 5 步、§6「防火墙」;规则语义与幂等口径归 docs/04 §2.6.3(唯一拥有者 = WinAgent 服务)。
# 🔴 硬约束(验收 M1-14):引擎与全部 ps1 里 **grep 零处 `netsh advfirewall` / `New-NetFirewallRule`**。
#    建/改规则只有一条路:`POST /wa/v1/firewall/ensure`。卸载兜底只允许按固定名 `QTrade-*` 删除。
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.WinAgent.psm1') -DisableNameChecking

# docs/04 §2.6.3 的固定规则名
$script:QtFirewallRuleNames = @('QTrade-WinAgent-17610-from-WSL', 'QTrade-Agent-17600-LAN')

function Get-QtFirewallRuleNames { [CmdletBinding()] param() return , $script:QtFirewallRuleNames }

function Invoke-QtFirewallEnsure {
    <#
    .SYNOPSIS
        §2.8.3 第 7 步:引擎**不自己写任何规则**,只调一次 WinAgent 服务 `POST /wa/v1/firewall/ensure`。
        返回 `created|updated|unchanged` 记 `install_state.firewall_rules`;
        `blocked_by_policy` **只记警告不失败**(完成页给 IT 话术,自检 `winagent_reachable` 会再验一次)。
    .OUTPUTS
        {Ok, Action, RuleName, Blocked, Message}
    #>
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17610', [int] $TimeoutSec = 30, [string] $Token = '')
    if (-not $Token) {
        $bootstrap = Get-QtBootstrapTokens
        if ($bootstrap.Ok) { $Token = $bootstrap.ConsoleToken }
    }
    if (-not $Token) {
        return [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_NOT_READY'; Action = ''; RuleName = ''; Blocked = $false; Message = 'BOOTSTRAP_NOT_READY' }
    }
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/wa/v1/firewall/ensure') -Method POST -Body '{}' -Headers @{ Authorization = ('Bearer ' + $Token) } -TimeoutSec $TimeoutSec
    if (-not $r.Ok) {
        return [pscustomobject]@{ Ok = $false; Reason = 'FIREWALL_REQUEST_FAILED'; Action = ''; RuleName = ''; Blocked = $false; Message = 'The authenticated firewall request failed.' }
    }
    $action = ''
    $ruleName = ''
    try {
        $j = $r.Body | ConvertFrom-Json -ErrorAction Stop
        if ($j -isnot [pscustomobject] -or -not (Test-QtHasProperty $j 'result') -or
            $j.result -isnot [string] -or $j.result -cnotin @('created', 'updated', 'unchanged', 'blocked_by_policy') -or
            -not (Test-QtHasProperty $j 'rule_name') -or $j.rule_name -isnot [string] -or
            [string]::IsNullOrWhiteSpace($j.rule_name)) { throw 'FIREWALL_RESPONSE_INVALID' }
        $action = $j.result
        $ruleName = $j.rule_name
    } catch {
        return [pscustomobject]@{ Ok = $false; Reason = 'FIREWALL_RESPONSE_INVALID'; Action = ''; RuleName = ''; Blocked = $false; Message = 'The firewall response did not match the service contract.' }
    }
    $blocked = ($action -eq 'blocked_by_policy')
    $msg = ''
    if ($blocked) {
        $msg = 'Policy blocked inbound port 17610. The Agent may be unable to reach WinAgent. Ask IT to allow the required rule.'
    }
    return [pscustomobject]@{ Ok = $true; Reason = ''; Action = $action; RuleName = $ruleName; Blocked = $blocked; Message = $msg }
}

function Invoke-QtFirewallDelete {
    <#
    .SYNOPSIS
        §2.14 第 5 步:卸载**在停服务之前**先调 `DELETE /wa/v1/firewall`(ensure 的逆操作)。
        服务已不可用时才由引擎按 `install_state.firewall_rules` 记录的固定规则名兜底删除(只删 `QTrade-*`)。
    .OUTPUTS
        {Ok, Via ∈ 'winagent'|'fallback'|'none', Removed[]}
    #>
    [CmdletBinding()]
    param(
        [string] $BaseUrl = 'http://127.0.0.1:17610',
        [string[]] $FallbackRuleNames = @(),
        [int] $TimeoutSec = 30,
        [string] $Token = ''
    )
    if (-not $Token) {
        $bootstrap = Get-QtBootstrapTokens
        if ($bootstrap.Ok) { $Token = $bootstrap.ConsoleToken }
    }
    $r = [pscustomobject]@{ Ok = $false }
    if ($Token) { $r = Invoke-QtHttp -Uri ($BaseUrl + '/wa/v1/firewall') -Method DELETE -Headers @{ Authorization = ('Bearer ' + $Token) } -TimeoutSec $TimeoutSec }
    if ($r.Ok) { return [pscustomobject]@{ Ok = $true; Via = 'winagent'; Removed = @() } }

    $names = @($FallbackRuleNames)
    if ($names.Count -eq 0) { $names = $script:QtFirewallRuleNames }
    $removed = @()
    foreach ($n in $names) {
        if ($n -notlike 'QTrade-*') { continue }   # §6:只删 QTrade-* 名字的
        Remove-QtFirewallRuleByName -DisplayName $n
        $removed += $n
    }
    return [pscustomobject]@{ Ok = $true; Via = 'fallback'; Removed = $removed }
}

Export-ModuleMember -Function Get-QtFirewallRuleNames, Invoke-QtFirewallEnsure, Invoke-QtFirewallDelete
