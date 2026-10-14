param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = "Stop"
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$target = Join-Path $PSScriptRoot "Deploy-And-Bind.ps1"
$arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ("`""+$target+"`""), "-PlanPath", ("`""+$PlanPath+"`""))
$file = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
try {
    $process = Start-Process -FilePath $file -ArgumentList $arguments -Verb RunAs -PassThru -WindowStyle Hidden
    $null = $process.Handle
    if (-not $process.WaitForExit(360000)) { throw "Elevated deployment still pending; inspect before retrying." }
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Elevated deployment failed; inspect deployment.json." }
    $result = Get-Content -LiteralPath (Join-Path $plan.base "deployment.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $result.success -or -not $result.credential_bound) { throw "Deployment did not verify the Agent credential." }
    Write-Output "DEPLOY_AND_BIND_VERIFIED"
    exit 0
} catch {
    Write-Output ("ADMINISTRATOR_STEP_NOT_COMPLETED: " + $_.Exception.Message)
    exit 1
}
