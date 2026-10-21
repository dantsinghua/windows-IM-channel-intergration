# Installed runtime launcher. No Python, Node, Git, or developer profile is required.
#requires -Version 5.1
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.WinAgent.psm1') -DisableNameChecking

function New-QtLaunchResult {
    param([bool]$Ok=$false,[string]$Stage='',[string]$Reason='',[int]$NativeCode=0,[int]$StatusCode=0)
    return [pscustomobject]@{Ok=$Ok;Stage=$Stage;Reason=$Reason;NativeCode=$NativeCode;StatusCode=$StatusCode}
}

function Get-QtLaunchNativeReason {
    param([int]$Code)
    switch ($Code) {
        2 { return 'FILE_MISSING' }
        3 { return 'PATH_MISSING' }
        5 { return 'ACCESS_DENIED' }
        126 { return 'NATIVE_DEPENDENCY_MISSING' }
        193 { return 'EXECUTABLE_ARCHITECTURE_INVALID' }
        577 { return 'EXECUTABLE_SIGNATURE_REJECTED' }
        1058 { return 'SERVICE_DISABLED' }
        1060 { return 'SERVICE_NOT_INSTALLED' }
        1068 { return 'SERVICE_DEPENDENCY_FAILED' }
        1223 { return 'UAC_CANCELLED' }
        1260 { return 'EXECUTION_POLICY_BLOCKED' }
        10048 { return 'PORT_IN_USE' }
        default { return 'OS_OPERATION_FAILED' }
    }
}

function Get-QtLaunchService {
    [CmdletBinding()] param()
    return Get-CimInstance -ClassName Win32_Service -Filter "Name='QTradeWinAgent'" -ErrorAction Stop
}

function Get-QtLaunchIdentity {
    return [pscustomobject]@{Sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;Session=[Diagnostics.Process]::GetCurrentProcess().SessionId}
}

function Test-QtLaunchOwnership {
    param([Parameter(Mandatory)]$Service,[Parameter(Mandatory)][string]$Root)
    $expectedExe=Join-Path $Root 'winagent\app\qtrade-winagent-svc.exe'
    $binary=[string]$Service.PathName
    if ($binary -notmatch '--install-user-sid\s+"?(S-1-(?:5-21|12-1)(?:-\d+){4})"?\s*$') { return $null }
    $sid=$Matches[1]
    $expected=(@($expectedExe,'--service','--root',(Join-Path $Root 'winagent'),'--install-user-sid',$sid) | ForEach-Object {ConvertTo-QtProcessArgument -Value $_}) -join ' '
    if (-not $binary.Equals($expected,[StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $sid
}

function Request-QtLaunchService {
    [CmdletBinding()] param([string]$Root,[string]$LauncherPath)
    $powershell=Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $arguments=(@('-NoProfile','-ExecutionPolicy','Bypass','-File',$LauncherPath,'-Root',$Root,'-ServiceOnly','-NoUi') | ForEach-Object {ConvertTo-QtProcessArgument -Value $_}) -join ' '
    $process=Start-Process -FilePath $powershell -ArgumentList $arguments -Verb RunAs -PassThru -WindowStyle Hidden -ErrorAction Stop
    $null=$process.Handle
    if (-not $process.WaitForExit(90000)) { return New-QtLaunchResult -Stage 'service_start' -Reason 'ELEVATED_START_PENDING' }
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { return New-QtLaunchResult -Stage 'service_start' -Reason (Get-QtLaunchNativeReason $process.ExitCode) -NativeCode $process.ExitCode }
    return New-QtLaunchResult -Ok $true -Stage 'service_start'
}

function Start-QtOwnedUser {
    [CmdletBinding()] param([string]$Root,[int]$Session)
    $exe=Join-Path $Root 'winagent\app\qtrade-winagent-user.exe'
    $processes=@(Get-CimInstance Win32_Process -Filter "Name='qtrade-winagent-user.exe'" -ErrorAction Stop | Where-Object {$_.SessionId -eq $Session})
    foreach($p in $processes) {
        if (-not $p.ExecutablePath) { throw 'USER_AGENT_OWNERSHIP_UNKNOWN' }
        if ([string]$p.ExecutablePath -ieq $exe) { return }
    }
    $arguments=(@('--root',(Join-Path $Root 'winagent')) | ForEach-Object {ConvertTo-QtProcessArgument -Value $_}) -join ' '
    $process=Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $exe) -PassThru -ErrorAction Stop
    if ($process.WaitForExit(1500)) { throw ('USER_AGENT_EXITED:'+[string]$process.ExitCode) }
}

function Start-QtInstalledConsole {
    param([string]$Root)
    $exe=Join-Path $Root 'console\QTrade.exe'
    $null=Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe) -PassThru -ErrorAction Stop
}

function Start-QtInstalledRuntime {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Root,[Parameter(Mandatory)][string]$LauncherPath,
          [ValidateRange(1,600)][int]$TimeoutSec=180,[switch]$ServiceOnly)
    $stage='installation_files'
    try {
        if ($Root -notmatch '^[A-Za-z]:[\\/]' -or $Root -match '[\\/]\.\.[\\/]') { return New-QtLaunchResult -Stage $stage -Reason 'INSTALLATION_ROOT_INVALID' }
        $Root=[IO.Path]::GetFullPath($Root).TrimEnd('\')
        foreach($relative in @('winagent\app\qtrade-winagent-svc.exe','winagent\app\qtrade-winagent-user.exe','winagent\winagent.toml','console\QTrade.exe')) {
            if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) { return New-QtLaunchResult -Stage $stage -Reason ('PAYLOAD_FILE_MISSING:'+$relative) }
        }
        $stage='service_identity'
        $service=Get-QtLaunchService
        if ($null -eq $service) { return New-QtLaunchResult -Stage $stage -Reason 'SERVICE_NOT_INSTALLED' -NativeCode 1060 }
        $sid=Test-QtLaunchOwnership -Service $service -Root $Root
        if (-not $sid) { return New-QtLaunchResult -Stage $stage -Reason 'SERVICE_OWNERSHIP_MISMATCH' }
        $stage='service_start'
        if ($ServiceOnly) {
            if ($service.State -ne 'Running') {
                Start-Service -Name 'QTradeWinAgent' -ErrorAction Stop
                (Get-Service 'QTradeWinAgent').WaitForStatus('Running',[TimeSpan]::FromSeconds(50))
            }
            return New-QtLaunchResult -Ok $true -Stage $stage
        }
        $identity=Get-QtLaunchIdentity
        if ($identity.Session -eq 0) { return New-QtLaunchResult -Stage 'user_session' -Reason 'INTERACTIVE_SESSION_REQUIRED' }
        if ($identity.Sid -cne $sid) { return New-QtLaunchResult -Stage 'user_session' -Reason 'INSTALL_USER_SID_MISMATCH' }
        if ($service.State -ne 'Running') {
            $elevated=Request-QtLaunchService -Root $Root -LauncherPath $LauncherPath
            if (-not $elevated.Ok) { return $elevated }
        }
        $deadline=(Get-QtNow).AddSeconds($TimeoutSec)
        $last=New-QtLaunchResult -Stage 'authentication' -Reason 'CONSOLE_PIPE_NOT_READY'
        $startedUser=$false
        while ((Get-QtNow) -lt $deadline) {
            $stage='authentication'
            $bundle=Get-QtConsoleBundle -TimeoutSec 3
            if (-not $bundle.Ok) {
                $last=New-QtLaunchResult -Stage $stage -Reason $bundle.Reason
                if ($bundle.Reason -in @('BOOTSTRAP_PIPE_UNTRUSTED','CONSOLE_PIPE_FORBIDDEN','CONSOLE_PIPE_INVALID')) { return $last }
                Start-QtSleep -Seconds 2; continue
            }
            $health=Get-QtWinAgentHealth -Token $bundle.ConsoleToken -TimeoutSec 3
            if (-not $health.Ok) {
                $last=New-QtLaunchResult -Stage 'winagent_health' -Reason $health.Cause -StatusCode $health.StatusCode
                if ($health.StatusCode -in @(401,403)) { return $last }
                Start-QtSleep -Seconds 2; continue
            }
            if (-not $health.UserAgent) {
                $stage='user_session'
                if (-not $startedUser) { Start-QtOwnedUser -Root $Root -Session $identity.Session; $startedUser=$true }
                $last=New-QtLaunchResult -Stage $stage -Reason 'USER_AGENT_HEARTBEAT_NOT_READY'
                Start-QtSleep -Seconds 2; continue
            }
            $stage='agent_discovery'
            if (-not $bundle.AgentBaseUrl -or -not $bundle.AgentToken) {
                $last=New-QtLaunchResult -Stage $stage -Reason 'AGENT_ENDPOINT_NOT_READY'
                Start-QtSleep -Seconds 2; continue
            }
            $stage='agent_health'
            $reply=Invoke-QtHttp -Uri ($bundle.AgentBaseUrl+'/api/v1/system/health') -Headers @{Authorization=('Bearer '+$bundle.AgentToken)} -TimeoutSec 3
            $reason='AGENT_HEALTH_INVALID'
            $launchReady=$false
            if ($reply.StatusCode -in @(401,403)) {
                $reason=if($reply.StatusCode -eq 401){'AGENT_AUTH_FAILED'}else{'AGENT_FORBIDDEN'}
                return New-QtLaunchResult -Stage $stage -Reason $reason -StatusCode $reply.StatusCode
            }
            if (-not $reply.Ok) {
                if ((Test-QtHasProperty -Object $reply -Name 'Reason') -and $reply.Reason) { $reason=$reply.Reason }
                else { $reason='AGENT_HTTP_'+[string]$reply.StatusCode }
            } else {
                try {
                    $body=$reply.Body | ConvertFrom-Json -ErrorAction Stop
                    $ok=(Test-QtHasProperty -Object $body -Name 'ok') -and $body.ok -is [bool] -and $body.ok
                    $docker=(Test-QtHasProperty -Object $body -Name 'dockerd') -and $body.dockerd -is [bool] -and $body.dockerd
                    $wa=(Test-QtHasProperty -Object $body -Name 'winagent') -and (Test-QtHasProperty -Object $body.winagent -Name 'online') -and $body.winagent.online -is [bool] -and $body.winagent.online
                    $ua=$wa -and (Test-QtHasProperty -Object $body.winagent -Name 'user_agent') -and $body.winagent.user_agent -is [bool] -and $body.winagent.user_agent
                    if ($ok -and $docker -and $wa -and $ua) { $launchReady=$true }
                    if ($ok -and -not $docker) { $reason='DOCKER_NOT_READY' }
                    elseif ($ok -and -not $wa) { $reason='AGENT_WINAGENT_HEALTH_UNAVAILABLE' }
                    elseif ($ok -and -not $ua) { $reason='AGENT_USER_HEALTH_UNAVAILABLE' }
                } catch { $reason='AGENT_HEALTH_INVALID' }
            }
            if ($launchReady) {
                $stage='console_start'
                Start-QtInstalledConsole -Root $Root
                return New-QtLaunchResult -Ok $true -Stage 'ready'
            }
            $last=New-QtLaunchResult -Stage $stage -Reason $reason -StatusCode $reply.StatusCode
            Start-QtSleep -Seconds 2
        }
        return $last
    } catch {
        $native=0; $errorObject=$_.Exception
        while ($null -ne $errorObject) {
            if (Test-QtHasProperty -Object $errorObject -Name 'NativeErrorCode') { $native=[int]$errorObject.NativeErrorCode }
            $errorObject=$errorObject.InnerException
        }
        $reason=Get-QtLaunchNativeReason -Code $native
        if ($_.Exception.Message -match '^(USER_AGENT_OWNERSHIP_UNKNOWN|USER_AGENT_EXITED:[0-9-]+)$') { $reason=$_.Exception.Message }
        return New-QtLaunchResult -Stage $stage -Reason $reason -NativeCode $native
    }
}

Export-ModuleMember -Function Start-QtInstalledRuntime,Get-QtLaunchNativeReason,Test-QtLaunchOwnership

