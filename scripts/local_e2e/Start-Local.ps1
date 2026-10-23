# Development entry only. The installed package uses launch-installed.ps1.
#requires -Version 5.1
[CmdletBinding()]
param([string]$Context='',[switch]$CheckOnly)
$ErrorActionPreference='Stop'
try {
    if(-not $Context){$Context=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'QTrade\development.json'}
    if(-not(Test-Path -LiteralPath $Context -PathType Leaf)){throw 'CONTEXT_MISSING: use a local development profile'}
    $profile=Get-Content -LiteralPath $Context -Raw -Encoding UTF8 | ConvertFrom-Json
    if($null -eq $profile -or $profile -isnot [pscustomobject]){throw 'CONTEXT_INVALID_JSON'}
    if(($profile.schema -isnot [int] -and $profile.schema -isnot [long]) -or $profile.schema -ne 1){throw 'CONTEXT_SCHEMA_UNSUPPORTED: schema'}
    if($profile.windows_python -isnot [string] -or [string]::IsNullOrWhiteSpace($profile.windows_python)){throw 'CONTEXT_FIELD_REQUIRED: windows_python'}
    if(-not(Test-Path -LiteralPath $profile.windows_python -PathType Leaf)){throw 'DEVELOPMENT_PYTHON_MISSING: windows_python'}
    $entry=if($CheckOnly){Join-Path $PSScriptRoot 'context.py'}else{Join-Path $PSScriptRoot 'launch.py'}
    & $profile.windows_python -B $entry --context $Context
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    if(-not $CheckOnly){$shell=New-Object -ComObject WScript.Shell;$null=$shell.Popup($_.Exception.Message,0,'QTrade development startup',16)}
    exit 2
}
