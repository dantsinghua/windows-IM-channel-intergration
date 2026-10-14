param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = "Stop"
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$root = "C:\ProgramData\QTrade-E2E\winagent"
$live = Join-Path $root "app"
$svcExe = Join-Path $live "qtrade-winagent-svc.exe"
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$identity = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Administrator approval required." }
if (Test-Path "C:\ProgramData\QTrade") { throw "Unexpected formal root." }
$svc = Get-CimInstance Win32_Service -Filter "Name='QTradeWinAgent'"
$expected = $svcExe + " --service --root " + $root + " --install-user-sid " + $sid
if (-not $svc -or $svc.PathName -cne $expected) { throw "Service ownership mismatch." }
$assets = Get-Content -LiteralPath (Join-Path $plan.base "artifacts.json") -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in $assets.PSObject.Properties) { if ((Get-FileHash -LiteralPath (Join-Path $plan.base $entry.Name) -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value) { throw "Frozen artifact changed." } }
$record = Join-Path $plan.base "deployment.json"
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$backup = Join-Path $root ("app.before-local-e2e-"+$stamp)
$next = Join-Path $root ("app.next-local-e2e-"+$stamp)
$configHash = (Get-FileHash (Join-Path $root "winagent.toml") -Algorithm SHA256).Hash
$swapped = $false; $stopped = $false; $success = $false; $reason = ""; $bound = $false
try {
  $nativeEntries = @($assets.PSObject.Properties | Where-Object { $_.Name.StartsWith("native\") })
  if ($nativeEntries.Count -lt 100) { throw "Incomplete native manifest." }
  $same = $true
  foreach ($entry in $nativeEntries) { $p=Join-Path $live $entry.Name.Substring(7); if (-not (Test-Path -LiteralPath $p) -or (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value) { $same=$false;break } }
  $users = @(Get-CimInstance Win32_Process -Filter "Name='qtrade-winagent-user.exe'" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.Equals((Join-Path $live "qtrade-winagent-user.exe"),[StringComparison]::OrdinalIgnoreCase) })
  foreach ($u in $users) { Stop-Process -Id $u.ProcessId -ErrorAction Stop; Wait-Process -Id $u.ProcessId -Timeout 20 -ErrorAction SilentlyContinue }
  if ($svc.State -ne "Stopped") { Stop-Service QTradeWinAgent; (Get-Service QTradeWinAgent).WaitForStatus("Stopped",[TimeSpan]::FromSeconds(40)) }
  $stopped=$true
  if (-not $same) {
    if ((Test-Path $backup) -or (Test-Path $next)) { throw "Deployment backup already exists." }
    Copy-Item -LiteralPath $plan.native -Destination $next -Recurse
    foreach ($entry in $nativeEntries) { if ((Get-FileHash -LiteralPath (Join-Path $next $entry.Name.Substring(7)) -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value) { throw "Copied native artifact changed." } }
    Move-Item -LiteralPath $live -Destination $backup
    try { Move-Item -LiteralPath $next -Destination $live; $swapped=$true } catch { Move-Item -LiteralPath $backup -Destination $live; throw }
  }
  Start-Service QTradeWinAgent; (Get-Service QTradeWinAgent).WaitForStatus("Running",[TimeSpan]::FromSeconds(45))
  $modules = Join-Path $plan.source "installer\engine\modules"
  Import-Module (Join-Path $modules "QTrade.Native.psm1") -DisableNameChecking
  Import-Module (Join-Path $modules "QTrade.WinAgent.psm1") -DisableNameChecking
  $credentials = Get-QtBootstrapTokens -TimeoutSec 30
  if (-not $credentials.Ok) { throw ("Credential bootstrap failed: "+$credentials.Reason) }
  $guest = $plan.linux_release + "/source/scripts/local_e2e/guest.py"
  $result = Invoke-QtProcess -FilePath "$env:WINDIR\System32\wsl.exe" -ArgumentList @("-d","Ubuntu-24.04","--user","anlin","--exec","/home/anlin/.venvs/qtrade/bin/python","-B",$guest,"bind") -StandardInput $credentials.WinAgentToken -TimeoutSec 40
  if ($result.TimedOut -or $result.ExitCode -ne 0) { throw "Agent-role credential binding failed; no credential is included in this log." }
  $binding=$result.StdOut | ConvertFrom-Json
  if (-not $binding.bound -or $binding.agent_role_time -ne 200) { throw "Role-specific verification failed." }
  $bound=$true; $credentials=$null
  if ((Get-FileHash (Join-Path $root "winagent.toml") -Algorithm SHA256).Hash -cne $configHash) { throw "Service configuration changed." }
  $success=$true
} catch {
  $reason=$_.Exception.Message
  if ($swapped) {
    try { Stop-Service QTradeWinAgent -ErrorAction SilentlyContinue; (Get-Service QTradeWinAgent).WaitForStatus("Stopped",[TimeSpan]::FromSeconds(30)); Move-Item -LiteralPath $live -Destination ($live+".failed-"+$stamp); Move-Item -LiteralPath $backup -Destination $live; Start-Service QTradeWinAgent } catch { $reason += " Rollback requires inspection." }
  } elseif ($stopped) { try { Start-Service QTradeWinAgent -ErrorAction SilentlyContinue } catch {} }
}
$now=Get-CimInstance Win32_Service -Filter "Name='QTradeWinAgent'"
$out=[ordered]@{success=$success;reason=$reason;revision=$plan.revision;service_state=$now.State;service_pid=$now.ProcessId;credential_bound=$bound;native_replaced=$swapped;backup=$(if($swapped){$backup}else{$null});config_preserved=((Get-FileHash (Join-Path $root "winagent.toml") -Algorithm SHA256).Hash -ceq $configHash)}
[IO.File]::WriteAllText($record,($out|ConvertTo-Json -Depth 5),(New-Object Text.UTF8Encoding($false)))
if (-not $success) { exit 1 }
exit 0
