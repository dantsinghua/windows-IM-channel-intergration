# QTrade 安装引擎 —— WINAGENT_INSTALLED:VC 运行库 / 服务 / 用户会话代理计划任务 / 令牌
# 规格:docs/03 §2.8.1(两进程结构 C-02)、§2.8.2(多用户策略 G-06)、§2.8.3(安装动作 1~7)、
#       §2.3(幂等判据:服务 Running 且 /wa/v1/health 200,**不要求 user_agent:true** —— R3-16)、§2.15、§7
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking

$script:QtServiceName = 'QTradeWinAgent'
$script:QtServiceDisplayName = 'QTrade WinAgent'
$script:QtSvcExeName = 'qtrade-winagent-svc.exe'      # R-14 实名:服务
$script:QtUserExeName = 'qtrade-winagent-user.exe'    # R-14 实名:用户会话代理(不开任何入站口)
$script:QtTaskPath = '\QTrade\'
$script:QtTaskName = 'WinAgentUser'
$script:QtWinAgentBase = 'http://127.0.0.1:17610'

function Get-QtWinAgentNames {
    [CmdletBinding()]
    param()
    return [ordered]@{
        service_name  = $script:QtServiceName
        display_name  = $script:QtServiceDisplayName
        svc_exe       = $script:QtSvcExeName
        user_exe      = $script:QtUserExeName
        task_path     = $script:QtTaskPath
        task_name     = $script:QtTaskName
        base_url      = $script:QtWinAgentBase
    }
}

function Install-QtVcRedist {
    <#
    .SYNOPSIS
        §2.8.3 第 1 步:`VC_redist.x64.exe /install /quiet /norestart`。
        `1638` = 已装更高版本,**视为成功**;`3010` 记 REBOOT_PENDING 但**不阻断**(chatlog 首次用时再起效)。
    .OUTPUTS
        {Ok, ExitCode, RebootPending, Reason}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $ExePath, [int] $TimeoutSec = 600)
    if (-not (Test-QtPath -Path $ExePath)) { return [pscustomobject]@{ Ok = $false; ExitCode = -1; RebootPending = $false; Reason = 'VCREDIST_FAILED' } }
    $r = Invoke-QtProcess -FilePath $ExePath -ArgumentList @('/install', '/quiet', '/norestart') -TimeoutSec $TimeoutSec
    switch ($r.ExitCode) {
        0 { return [pscustomobject]@{ Ok = $true; ExitCode = 0; RebootPending = $false; Reason = '' } }
        1638 { return [pscustomobject]@{ Ok = $true; ExitCode = 1638; RebootPending = $false; Reason = 'AlreadyNewer' } }
        3010 { return [pscustomobject]@{ Ok = $true; ExitCode = 3010; RebootPending = $true; Reason = 'RebootPending' } }
        default { return [pscustomobject]@{ Ok = $false; ExitCode = $r.ExitCode; RebootPending = $false; Reason = 'VCREDIST_FAILED' } }
    }
}

function Merge-QtWinAgentToml {
    <#
    .SYNOPSIS
        §2.8.3 第 2 步:`winagent.toml` 按模板生成;**已存在则只补缺省键**(不覆盖用户/上一版改过的值)。
        逐行文本合并,保留原注释与顺序。
    .PARAMETER Defaults
        段 → (键 → 值) 的有序表,如 @{ install = @{ package_version = '1.0.0' } }
    .OUTPUTS
        {Text, KeysAdded[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $ExistingText,
        [Parameter(Mandatory)] $Defaults
    )
    $lines = [System.Collections.ArrayList]::new()
    foreach ($l in ($ExistingText -split "`r?`n")) { [void]$lines.Add($l) }
    if ($lines.Count -eq 1 -and [string]::IsNullOrWhiteSpace($lines[0])) { [void]$lines.Clear() }
    $added = @()

    foreach ($section in $Defaults.Keys) {
        # 找段
        $secIdx = -1
        $secEnd = $lines.Count
        for ($i = 0; $i -lt $lines.Count; $i++) {
            $m = [regex]::Match($lines[$i], '^\s*\[(?<s>[^\]]+)\]\s*$')
            if ($m.Success) {
                if ($m.Groups['s'].Value.Trim() -eq $section) { $secIdx = $i; $secEnd = $lines.Count }
                elseif ($secIdx -ge 0) { $secEnd = $i; break }
            }
        }
        if ($secIdx -lt 0) {
            if ($lines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($lines[$lines.Count - 1])) { [void]$lines.Add('') }
            [void]$lines.Add('[' + $section + ']')
            $secIdx = $lines.Count - 1
            $secEnd = $lines.Count
        }
        foreach ($key in $Defaults[$section].Keys) {
            $found = $false
            for ($i = $secIdx + 1; $i -lt $secEnd; $i++) {
                if ($lines[$i] -match ('^\s*' + [regex]::Escape($key) + '\s*=')) { $found = $true; break }
            }
            if ($found) { continue }
            $value = $Defaults[$section][$key]
            $rendered = if ($value -is [bool]) { $value.ToString().ToLowerInvariant() }
            elseif ($value -is [int] -or $value -is [long] -or $value -is [double]) { [string]$value }
            elseif ($value -is [array]) { '[' + (($value | ForEach-Object { '"' + $_ + '"' }) -join ', ') + ']' }
            else { '"' + ([string]$value).Replace('\', '\\') + '"' }
            $lines.Insert($secEnd, ('{0} = {1}' -f $key, $rendered))
            $secEnd++
            $added += ('{0}.{1}' -f $section, $key)
        }
    }
    return [pscustomobject]@{ Text = ($lines -join "`r`n"); KeysAdded = $added }
}

function Install-QtWinAgentService {
    <#
    .SYNOPSIS
        §2.8.3 第 3 步:用 **`sc.exe`** 建服务(不用 New-Service —— 后者不设恢复策略,§2.15)。
        `delayed-auto` 而不是 `auto`:开机时 LxssManager 与网络就绪要一会儿,提前起来只会报错重启。
    .OUTPUTS
        {Ok, Reason, Commands[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $ExePath,
        [string] $Name = 'QTradeWinAgent',
        [string] $DisplayName = 'QTrade WinAgent'
    )
    $cmds = @()
    $create = @('create', $Name, 'binPath=', ('"{0}"' -f $ExePath), 'start=', 'delayed-auto', 'obj=', 'LocalSystem',
        'DisplayName=', $DisplayName, 'depend=', 'LxssManager')
    $cmds += ($create -join ' ')
    $r = Invoke-QtSc -ScArgs $create
    if ($r.ExitCode -ne 0 -and $r.ExitCode -ne 1073) {
        # 1073 = 服务已存在,幂等重跑属正常
        return [pscustomobject]@{ Ok = $false; Reason = 'SERVICE_INSTALL_FAILED'; Commands = $cmds }
    }
    $desc = @('description', $Name, 'QTrade WinAgent 服务:vault / monitor / netprobe / 防火墙 / installer-ops')
    $cmds += ($desc -join ' ')
    Invoke-QtSc -ScArgs $desc | Out-Null

    # 失败恢复:重启 5 s / 30 s / 60 s;failureflag 1 = 非崩溃退出也算失败
    $failure = @('failure', $Name, 'reset=', '86400', 'actions=', 'restart/5000/restart/30000/restart/60000')
    $cmds += ($failure -join ' ')
    Invoke-QtSc -ScArgs $failure | Out-Null
    $flag = @('failureflag', $Name, '1')
    $cmds += ($flag -join ' ')
    Invoke-QtSc -ScArgs $flag | Out-Null

    return [pscustomobject]@{ Ok = $true; Reason = ''; Commands = $cmds }
}

function Register-QtWinAgentUserTask {
    <#
    .SYNOPSIS
        §2.8.2 / §2.8.3 第 4 步:计划任务 `\QTrade\WinAgentUser` —— Principal = **安装用户 SID**、
        触发器 = **该用户**登录(不是「任意用户」)、`RunLevel Limited`(不提权)、RestartOnFailure 3 次。
        安装期**立即 Start-ScheduledTask** 一次(安装用户此刻正登录着)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $UserExePath,
        [Parameter(Mandatory)][string] $InstallUserSid,
        [switch] $StartNow
    )
    Register-QtScheduledTask -TaskPath $script:QtTaskPath -TaskName $script:QtTaskName -Execute $UserExePath -UserId $InstallUserSid
    if ($StartNow) {
        try { Start-QtScheduledTask -TaskPath $script:QtTaskPath -TaskName $script:QtTaskName } catch { }
    }
    return [pscustomobject]@{ Ok = $true; TaskPath = $script:QtTaskPath; TaskName = $script:QtTaskName }
}

function Get-QtWinAgentHealth {
    <#
    .SYNOPSIS
        `GET /wa/v1/health` → {Ok, StatusCode, UserAgent, Vault, Version, Raw}
    #>
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17610', [int] $TimeoutSec = 5)
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/wa/v1/health') -Method GET -TimeoutSec $TimeoutSec
    $userAgent = $false
    $vault = ''
    $version = ''
    if ($r.Ok -and $r.Body) {
        try {
            $j = $r.Body | ConvertFrom-Json
            if (Test-QtHasProperty -Object $j -Name 'user_agent') { $userAgent = [bool]$j.user_agent }
            if (Test-QtHasProperty -Object $j -Name 'vault') { $vault = [string]$j.vault }
            if (Test-QtHasProperty -Object $j -Name 'version') { $version = [string]$j.version }
        } catch { }
    }
    return [pscustomobject]@{
        Ok         = ($r.Ok -and $r.StatusCode -eq 200)
        StatusCode = $r.StatusCode
        UserAgent  = $userAgent
        Vault      = $vault
        Version    = $version
        Raw        = $r.Body
    }
}

function Wait-QtWinAgentHealthy {
    <#
    .SYNOPSIS
        §2.8.3 第 5 步:启动服务 → 等 `/wa/v1/health` 200(30 s)。
        🔴 R3-16:**服务 200 即视为本步成功,不再以 `user_agent:true` 为门** —— 静默/无人值守安装下
        用户未登录、会话代理不在属正常态;`E_INSTALL_WINAGENT_NOT_READY` 只留给「服务起不来或 30 s 内不 200」。
    .OUTPUTS
        {Ok, UserAgent, Reason}
    #>
    [CmdletBinding()]
    param(
        [string] $BaseUrl = 'http://127.0.0.1:17610',
        [int] $TimeoutSec = 30,
        [int] $IntervalSec = 2
    )
    $deadline = (Get-QtNow).AddSeconds($TimeoutSec)
    $last = $null
    while ((Get-QtNow) -lt $deadline) {
        $last = Get-QtWinAgentHealth -BaseUrl $BaseUrl
        if ($last.Ok) { return [pscustomobject]@{ Ok = $true; UserAgent = $last.UserAgent; Reason = '' } }
        Start-QtSleep -Seconds $IntervalSec
    }
    $ua = $false
    if ($null -ne $last) { $ua = $last.UserAgent }
    return [pscustomobject]@{ Ok = $false; UserAgent = $ua; Reason = 'WINAGENT_NOT_READY' }
}

function Test-QtWinAgentInstalled {
    <#
    .SYNOPSIS
        WINAGENT_INSTALLED 幂等判据(docs/03 §2.3):服务 `Running` 且 `/wa/v1/health` 200
        (**不要求 `user_agent:true`**,R3-16)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    if ((Get-QtServiceStatus -Name $script:QtServiceName) -ne 'Running') { return $false }
    $h = Get-QtWinAgentHealth -BaseUrl $script:QtWinAgentBase
    return $h.Ok
}

function Write-QtWinAgentToken {
    <#
    .SYNOPSIS
        §2.8.3 第 6 步:服务生成令牌 → 引擎经 `/mnt/c` 落发行版内 `/etc/qtrade/winagent.token`(0600 root)。
        基线 §11.1 [CRED] 例外(C-05)。🔴 令牌本身**绝不进日志**(QTrade.Log 的 Protect-QtLogText 也会兜一层)。
    .PARAMETER Token
        由 WinAgent 服务签发。本函数只负责落盘,不生成、不回显。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Token,
        [string] $DistroName = 'qtrade',
        [int] $TimeoutSec = 60
    )
    if ([string]::IsNullOrWhiteSpace($Token)) { return [pscustomobject]@{ Ok = $false; Reason = 'EMPTY_TOKEN' } }
    # Send exact UTF-8 bytes through stdin, without a shell or a plaintext temp file.
    $script = 'umask 077 && mkdir -p /etc/qtrade && cat > /etc/qtrade/winagent.token && chmod 600 /etc/qtrade/winagent.token && chown root:root /etc/qtrade/winagent.token'
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c', $script) -StandardInput $Token -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return [pscustomobject]@{ Ok = $false; Reason = 'TOKEN_WRITE_FAILED' } }
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Uninstall-QtWinAgent {
    <#
    .SYNOPSIS
        §2.14 第 4 步:服务经管道让会话代理退出 → 停服务 → `sc delete` → 删计划任务。
    #>
    [CmdletBinding()]
    param([int] $StopTimeoutSec = 30)
    Unregister-QtScheduledTask -TaskPath $script:QtTaskPath -TaskName $script:QtTaskName
    Invoke-QtSc -ScArgs @('stop', $script:QtServiceName) -TimeoutSec $StopTimeoutSec | Out-Null
    $del = Invoke-QtSc -ScArgs @('delete', $script:QtServiceName) -TimeoutSec $StopTimeoutSec
    return [pscustomobject]@{ Ok = ($del.ExitCode -eq 0 -or $del.ExitCode -eq 1060); ExitCode = $del.ExitCode }
}

Register-QtStepCheck -Step 'WINAGENT_INSTALLED' -Check { param($ctx) Test-QtWinAgentInstalled -Context $ctx }

Export-ModuleMember -Function Get-QtWinAgentNames, Install-QtVcRedist, Merge-QtWinAgentToml,
Install-QtWinAgentService, Register-QtWinAgentUserTask, Get-QtWinAgentHealth, Wait-QtWinAgentHealthy,
Test-QtWinAgentInstalled, Write-QtWinAgentToken, Uninstall-QtWinAgent
