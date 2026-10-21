# Installed entry; its location, not the original build machine, determines the product root.
#requires -Version 5.1
[CmdletBinding()]
param([string]$Root='',[switch]$ServiceOnly,[switch]$NoUi)
$ErrorActionPreference='Stop'
if (-not $Root) { $Root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
Import-Module (Join-Path $PSScriptRoot 'modules\QTrade.Launch.psm1') -Force -DisableNameChecking
$mutex=New-Object Threading.Mutex($false,'Local\QTradeInstalledRuntimeStart')
$owned=$false
try {
    # The elevated service-only child must not acquire the parent startup mutex.
    if (-not $ServiceOnly) {
        try { $owned=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned=$true }
        if (-not $owned) {
            if (-not $NoUi) { $shell=New-Object -ComObject WScript.Shell; $null=$shell.Popup('Another QTrade startup is in progress. Check QTrade startup logs.',10,'QTrade startup',64) }
            exit 29
        }
    }
    $result=Start-QtInstalledRuntime -Root $Root -LauncherPath $PSCommandPath -ServiceOnly:$ServiceOnly
    if (-not $ServiceOnly) {
        $logs=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'QTrade\logs'
        $null=New-Item -ItemType Directory -Path $logs -Force
        $record=[ordered]@{time=(Get-Date -Format o);success=$result.Ok;stage=$result.Stage;reason=$result.Reason;native_code=$result.NativeCode;http_status=$result.StatusCode}
        [IO.File]::WriteAllText((Join-Path $logs 'startup-result.json'),($record|ConvertTo-Json),(New-Object Text.UTF8Encoding($false)))
        if (-not $result.Ok -and -not $NoUi) {
            $shell=New-Object -ComObject WScript.Shell
            $null=$shell.Popup(('QTrade could not start.'+"`n"+'Stage: '+$result.Stage+"`n"+'Cause: '+$result.Reason+"`n"+'OS code: '+$result.NativeCode+"`n"+'Log: '+(Join-Path $logs 'startup-result.json')),0,'QTrade startup',16)
        }
    }
    $result | ConvertTo-Json -Compress | Write-Output
    if ($result.Ok) { exit 0 }
    if ($result.NativeCode -ne 0) { exit $result.NativeCode }
    exit 1
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
