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
        [string] $DisplayName = 'QTrade WinAgent',
        [string] $InstallUserSid = '',
        [string] $Root = ''
    )
    $cmds = @()
    if ($InstallUserSid -notmatch '\AS-1-(?:5-21|12-1)(?:-\d+){4}\z') {
        return [pscustomobject]@{ Ok = $false; Reason = 'INSTALL_USER_SID_INVALID'; Commands = $cmds }
    }
    if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $ExePath) }
    $binary = (@($ExePath, '--service', '--root', $Root, '--install-user-sid', $InstallUserSid) |
        ForEach-Object { ConvertTo-QtProcessArgument -Value $_ }) -join ' '
    # WinAgent provides diagnostics even when WSL is unavailable. Do not depend
    # on a Windows WSL service name that differs across supported releases.
    $create = @('create', $Name, 'binPath=', $binary, 'start=', 'delayed-auto', 'obj=', 'LocalSystem',
        'DisplayName=', $DisplayName)
    $cmds += ($create -join ' ')
    $r = Invoke-QtSc -ScArgs $create
    if ($r.ExitCode -ne 0 -and $r.ExitCode -ne 1073) {
        # 1073 = 服务已存在,幂等重跑属正常
        return [pscustomobject]@{ Ok = $false; Reason = 'SERVICE_INSTALL_FAILED'; Commands = $cmds }
    }
    if ($r.ExitCode -eq 1073) {
        $update = @('config', $Name, 'binPath=', $binary, 'start=', 'delayed-auto', 'obj=', 'LocalSystem', 'depend=', '/')
        $cmds += ($update -join ' ')
        $updated = Invoke-QtSc -ScArgs $update
        if ($updated.ExitCode -ne 0) {
            return [pscustomobject]@{ Ok = $false; Reason = 'SERVICE_INSTALL_FAILED'; Commands = $cmds }
        }
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
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17610', [int] $TimeoutSec = 3, [AllowEmptyString()][string] $Token = '')
    $result = [pscustomobject]@{Ok=$false;StatusCode=0;UserAgent=$false;UserAgentKnown=$false;Vault='';Version='';Raw='';Cause='WINAGENT_TRANSPORT_UNAVAILABLE';NativeCode=0}
    $deadline = (Get-QtNow).AddSeconds($TimeoutSec)
    if (-not $Token) {
        $bootstrap = Get-QtBootstrapTokens -TimeoutSec $TimeoutSec
        if (-not $bootstrap.Ok) { $result.Cause=$bootstrap.Reason; return $result }
        $Token = $bootstrap.ConsoleToken
    }
    $remaining = [int][Math]::Ceiling(($deadline-(Get-QtNow)).TotalSeconds)
    if ($remaining -le 0) { $result.Cause='WINAGENT_HEALTH_TIMEOUT'; return $result }
    $reply = Invoke-QtHttp -Uri ($BaseUrl+'/wa/v1/health') -Method GET -Headers @{Authorization=('Bearer '+$Token)} -TimeoutSec $remaining
    $result.StatusCode = $reply.StatusCode
    if (Test-QtHasProperty -Object $reply -Name 'NativeCode') { $result.NativeCode=$reply.NativeCode }
    if ($reply.StatusCode -eq 401) { $result.Cause='WINAGENT_AUTH_FAILED'; return $result }
    if ($reply.StatusCode -eq 403) { $result.Cause='WINAGENT_FORBIDDEN'; return $result }
    if (-not $reply.Ok -or $reply.StatusCode -ne 200) {
        if ($reply.StatusCode -gt 0) { $result.Cause='WINAGENT_HTTP_'+[string]$reply.StatusCode }
        elseif ((Test-QtHasProperty -Object $reply -Name 'Reason') -and $reply.Reason) { $result.Cause=$reply.Reason }
        return $result
    }
    $result.Cause = 'WINAGENT_HEALTH_INVALID'
    try {
        $body = $reply.Body | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $body -or $body -is [array] -or $body -isnot [pscustomobject]) { return $result }
        if ((Test-QtHasProperty -Object $body -Name 'user_agent') -and $body.user_agent -is [bool]) {
            $result.UserAgentKnown=$true; $result.UserAgent=$body.user_agent
        }
        if ((Test-QtHasProperty -Object $body -Name 'version') -and $body.version -is [string]) { $result.Version=$body.version }
        if ((Test-QtHasProperty -Object $body -Name 'modules') -and (Test-QtHasProperty -Object $body.modules -Name 'vault') -and $body.modules.vault -is [string]) {
            $result.Vault=$body.modules.vault
        }
        if (-not (Test-QtHasProperty -Object $body -Name 'ok') -or $body.ok -isnot [bool] -or -not $result.UserAgentKnown) { return $result }
        if (-not $body.ok) { $result.Cause='WINAGENT_HEALTH_REPORTED_FAILURE'; return $result }
        if ($result.Vault -cne 'ok') { $result.Cause='WINAGENT_VAULT_NOT_READY'; return $result }
        $result.Ok=$true; $result.Cause=''; $result.Raw=$reply.Body
    } catch { return $result }
    return $result
}

function Wait-QtWinAgentHealthy {
    [CmdletBinding()]
    param([string] $BaseUrl='http://127.0.0.1:17610', [ValidateRange(1,120)][int] $TimeoutSec=30,
          [ValidateRange(1,120)][int] $IntervalSec=2, [AllowEmptyString()][string] $Token='')
    $deadline=(Get-QtNow).AddSeconds($TimeoutSec)
    $last=$null
    while ((Get-QtNow) -lt $deadline) {
        $remaining=[int][Math]::Ceiling(($deadline-(Get-QtNow)).TotalSeconds)
        if ($remaining -le 0) { break }
        $last=Get-QtWinAgentHealth -BaseUrl $BaseUrl -Token $Token -TimeoutSec ([Math]::Min(5,$remaining))
        if ($last.Ok) { return [pscustomobject]@{Ok=$true;UserAgent=$last.UserAgent;Reason='';Cause='';StatusCode=$last.StatusCode} }
        if ((Test-QtHasProperty -Object $last -Name 'Cause') -and $last.Cause -in @('WINAGENT_AUTH_FAILED','WINAGENT_FORBIDDEN','BOOTSTRAP_PIPE_UNTRUSTED')) { break }
        $remaining=[int][Math]::Floor(($deadline-(Get-QtNow)).TotalSeconds)
        if ($remaining -le 0) { break }
        Start-QtSleep -Seconds ([Math]::Min($IntervalSec,$remaining))
    }
    $userAgent=$false; $cause='WINAGENT_HEALTH_TIMEOUT'; $http=0
    if ($null -ne $last) {
        $userAgent=$last.UserAgent
        if (Test-QtHasProperty -Object $last -Name 'StatusCode') { $http=$last.StatusCode }
        if ((Test-QtHasProperty -Object $last -Name 'Cause') -and $last.Cause) { $cause=$last.Cause }
    }
    # Reason preserves the public legacy exit-code family. Cause names the actual failure.
    return [pscustomobject]@{Ok=$false;UserAgent=$userAgent;Reason='WINAGENT_NOT_READY';Cause=$cause;StatusCode=$http}
}

function Test-QtWinAgentInstalled {
    <#
    .SYNOPSIS
        Require the registered service and authenticated health to be ready.
        The user agent may be offline.
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    if ((Get-QtServiceStatus -Name $script:QtServiceName) -ne 'Running') { return $false }
    $h = Get-QtWinAgentHealth -BaseUrl $script:QtWinAgentBase
    return $h.Ok
}

function Invoke-QtBootstrapPipe {
    [CmdletBinding()]
    param([ValidateRange(1, 120)][int] $TimeoutSec = 30, [ValidateSet('issue_bootstrap','issue_console_token')][string] $Operation = 'issue_bootstrap')
    $result = [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_PIPE_FAILED'; Response = $null }
    $pipe = $null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        $servicePid = Get-QtServiceProcessId -Name $script:QtServiceName
        if ($servicePid -le 0) { $result.Reason = 'WINAGENT_NOT_READY'; return $result }
        if (-not ('QTrade.InstallerPipeIdentity' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace QTrade {
    public static class InstallerPipeIdentity {
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetNamedPipeServerProcessId(IntPtr pipe, out uint processId);
    }
}
'@ -ErrorAction Stop
        }
        $pipe = New-Object IO.Pipes.NamedPipeClientStream('.', 'qtrade-winagent', [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous, [Security.Principal.TokenImpersonationLevel]::Identification)
        $pipe.Connect([Math]::Max(1, $TimeoutSec * 1000 - [int]$clock.ElapsedMilliseconds))
        [uint32]$serverPid = 0
        if (-not [QTrade.InstallerPipeIdentity]::GetNamedPipeServerProcessId($pipe.SafePipeHandle.DangerousGetHandle(), [ref]$serverPid) -or $serverPid -ne $servicePid -or $serverPid -ne (Get-QtServiceProcessId -Name $script:QtServiceName)) {
            $result.Reason = 'BOOTSTRAP_PIPE_UNTRUSTED'; return $result
        }
        $request = [Text.Encoding]::UTF8.GetBytes((@{op=$Operation}|ConvertTo-Json -Compress) + "`n")
        $write = $pipe.WriteAsync($request, 0, $request.Length)
        if (-not $write.Wait([Math]::Max(0, $TimeoutSec * 1000 - [int]$clock.ElapsedMilliseconds))) { $result.Reason = 'BOOTSTRAP_PIPE_TIMEOUT'; return $result }
        $bytes = New-Object 'System.Collections.Generic.List[byte]'
        $buffer = New-Object byte[] 1024
        $complete = $false
        while (-not $complete) {
            $read = $pipe.ReadAsync($buffer, 0, $buffer.Length)
            if (-not $read.Wait([Math]::Max(0, $TimeoutSec * 1000 - [int]$clock.ElapsedMilliseconds))) { $result.Reason = 'BOOTSTRAP_PIPE_TIMEOUT'; return $result }
            $count = $read.Result
            if ($count -eq 0) { return $result }
            for ($i = 0; $i -lt $count; $i++) {
                if ($buffer[$i] -eq 10) { $complete = $true; break }
                $bytes.Add($buffer[$i])
                if ($bytes.Count -gt 16384) { $result.Reason = 'BOOTSTRAP_PIPE_INVALID'; return $result }
            }
        }
        $encoding = New-Object Text.UTF8Encoding($false, $true)
        $response = $encoding.GetString($bytes.ToArray()) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $response -or $response -is [array] -or $response -isnot [pscustomobject]) { $result.Reason = 'BOOTSTRAP_PIPE_INVALID'; return $result }
        $result.Ok = $true; $result.Reason = ''; $result.Response = $response
        return $result
    } catch {
        # Never expose exception text, request bytes, or a response credential.
        return $result
    } finally {
        if ($null -ne $pipe) { $pipe.Dispose() }
        $clock.Stop()
    }
}

function Get-QtBootstrapTokens {
    [CmdletBinding()]
    param([ValidateRange(1, 120)][int] $TimeoutSec = 30)
    $result = [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_TOKEN_INVALID'; ConsoleToken = ''; WinAgentToken = '' }
    $deadline = (Get-QtNow).AddSeconds($TimeoutSec)
    while ($true) {
        $remaining = [int][Math]::Ceiling(($deadline - (Get-QtNow)).TotalSeconds)
        if ($remaining -le 0) { return $result }
        $reply = Invoke-QtBootstrapPipe -TimeoutSec ([Math]::Min(5, $remaining))
        $retry = $false
        if (-not $reply.Ok) {
            $result.Reason = $reply.Reason
            $retry = $reply.Reason -in @('WINAGENT_NOT_READY', 'BOOTSTRAP_PIPE_FAILED', 'BOOTSTRAP_PIPE_TIMEOUT')
        } elseif (Test-QtHasProperty -Object $reply.Response -Name 'error') {
            $result.Reason = 'BOOTSTRAP_TOKEN_INVALID'
            if ([string]$reply.Response.error -in @('FORBIDDEN', 'NOT_READY', 'INVALID_ARGS')) { $result.Reason = [string]$reply.Response.error }
            $retry = $result.Reason -eq 'NOT_READY'
        } else { break }
        if (-not $retry -or (Get-QtNow) -ge $deadline) { return $result }
        Start-QtSleep -Seconds 1
    }
    $result.Reason = 'BOOTSTRAP_TOKEN_INVALID'
    foreach ($key in @('console_token', 'winagent_token')) {
        if (-not (Test-QtHasProperty -Object $reply.Response -Name $key) -or $reply.Response.$key -isnot [string] -or $reply.Response.$key -cnotmatch '^[A-Za-z0-9_-]{32,4096}$') { return $result }
    }
    $result.Ok = $true; $result.Reason = ''
    $result.ConsoleToken = $reply.Response.console_token; $result.WinAgentToken = $reply.Response.winagent_token
    return $result
}

function Set-QtAgentConsoleToken {
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][string] $Token, [string] $DistroName = 'qtrade', [int] $TimeoutSec = 60)
    if ($DistroName -cne 'qtrade' -or $Token -cnotmatch '^[A-Za-z0-9_-]{32,4096}$') { return $false }
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', '/opt/qtrade/agent/venv/bin/python', '-m', 'qtrade_agent.main', '--bootstrap-console-token-stdin') -StandardInput $Token -TimeoutSec $TimeoutSec
    return (-not $r.TimedOut -and $r.ExitCode -eq 0)
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
    if ($DistroName -cne 'qtrade') { return [pscustomobject]@{ Ok = $false; Reason = 'DISTRO_NAME_INVALID' } }
    if ([string]::IsNullOrWhiteSpace($Token)) { return [pscustomobject]@{ Ok = $false; Reason = 'EMPTY_TOKEN' } }
    # Send exact UTF-8 bytes through stdin, without a shell or a plaintext temp file.
    $script = 'set -e; umask 077; test ! -L /etc/qtrade; mkdir -p /etc/qtrade; test ! -L /etc/qtrade/winagent.token; tmp=$(mktemp /etc/qtrade/winagent.token.XXXXXX); cat > "$tmp"; chmod 600 "$tmp"; chown root:root "$tmp"; mv -f -- "$tmp" /etc/qtrade/winagent.token'
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
Test-QtWinAgentInstalled, Write-QtWinAgentToken, Uninstall-QtWinAgent,
Get-QtBootstrapTokens, Set-QtAgentConsoleToken

function Get-QtConsoleBundle {
    [CmdletBinding()]
    param([ValidateRange(1,120)][int] $TimeoutSec=5)
    $out=[pscustomobject]@{Ok=$false;Reason='CONSOLE_PIPE_INVALID';ConsoleToken='';AgentToken=$null;AgentBaseUrl=$null}
    $reply=Invoke-QtBootstrapPipe -TimeoutSec $TimeoutSec -Operation 'issue_console_token'
    if (-not $reply.Ok) { $out.Reason=$reply.Reason; return $out }
    $body=$reply.Response
    if (Test-QtHasProperty -Object $body -Name 'error') {
        if ([string]$body.error -in @('FORBIDDEN','NOT_READY')) { $out.Reason='CONSOLE_PIPE_'+[string]$body.error }
        return $out
    }
    if (-not (Test-QtHasProperty -Object $body -Name 'winagent_token') -or $body.winagent_token -isnot [string] -or $body.winagent_token -cnotmatch '^[A-Za-z0-9_-]{32,4096}$') { return $out }
    if (-not (Test-QtHasProperty -Object $body -Name 'expires_at') -or $body.expires_at -is [string] -or [double]$body.expires_at -le [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) { return $out }
    $out.ConsoleToken=$body.winagent_token
    if ((Test-QtHasProperty -Object $body -Name 'agent_token') -and (Test-QtHasProperty -Object $body -Name 'agent_base_url') -and $null -eq $body.agent_token -and $null -eq $body.agent_base_url) {
        $out.Ok=$true; $out.Reason=''; return $out
    }
    if (-not (Test-QtHasProperty -Object $body -Name 'agent_token') -or $body.agent_token -isnot [string] -or $body.agent_token -cnotmatch '^[A-Za-z0-9_-]{32,4096}$') { return $out }
    try {
        $uri=[Uri]([string]$body.agent_base_url)
        if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'http' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -notin @('','/')) { return $out }
        $ip=$null
        if (-not [Net.IPAddress]::TryParse($uri.Host,[ref]$ip) -or $ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { return $out }
        $b=$ip.GetAddressBytes()
        if (-not ([Net.IPAddress]::IsLoopback($ip) -or $b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or ($b[0] -eq 192 -and $b[1] -eq 168))) { return $out }
        $out.AgentBaseUrl=$uri.GetLeftPart([UriPartial]::Authority)
    } catch { return $out }
    $out.AgentToken=$body.agent_token
    $out.Ok=$true; $out.Reason=''
    return $out
}

Export-ModuleMember -Function Get-QtConsoleBundle
