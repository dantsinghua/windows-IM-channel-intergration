# QTrade staged clean reinstall. No backup, migration, or foreign-object removal.
#requires -Version 5.1
Set-StrictMode -Version Latest
foreach ($name in @('Native', 'State', 'Payload', 'Acl', 'Preflight', 'WinAgent', 'Wsl', 'Console')) {
    Import-Module (Join-Path $PSScriptRoot ('QTrade.' + $name + '.psm1')) -DisableNameChecking
}

function Test-QtStagePath {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path, [switch] $Tree)
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-QtPath -Path $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
    if ($Tree -and (Test-QtPath -Path $Path)) {
        # Enumerate one level at a time; never traverse a junction to inspect it.
        $pending = [Collections.Generic.Queue[string]]::new()
        $pending.Enqueue($Path)
        while ($pending.Count) {
            foreach ($item in (Get-ChildItem -LiteralPath $pending.Dequeue() -Force -ErrorAction Stop)) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
                if ($item.PSIsContainer) { $pending.Enqueue($item.FullName) }
            }
        }
    }
    return $true
}

function Get-QtStageIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $StageRoot, [Parameter(Mandatory)][string] $LiveRoot)
    $stage = [IO.Path]::GetFullPath($StageRoot).TrimEnd('\', '/')
    $live = [IO.Path]::GetFullPath($LiveRoot).TrimEnd('\', '/')
    if ($stage -eq $live -or $stage.StartsWith($live + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $live.StartsWith($stage + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $stage.StartsWith($live + '/', [StringComparison]::OrdinalIgnoreCase) -or
        $live.StartsWith($stage + '/', [StringComparison]::OrdinalIgnoreCase)) { throw 'STAGE_NOT_ISOLATED' }
    if (-not (Test-QtStagePath -Path $stage -Tree) -or -not (Test-QtStagePath -Path $live -Tree)) {
        throw 'REPARSE_POINT_REFUSED'
    }
    return [pscustomobject]@{ StageRoot = $stage; LiveRoot = $live
        Journal = (Join-Path $stage 'install\clean-reinstall.json')
        Manifest = (Join-Path $stage 'install\manifest.json') }
}

function Get-QtCleanTask {
    [CmdletBinding()]
    param()
    # Query errors differ from absence. The child returns structured, locale-free facts.
    $scriptText = '$ErrorActionPreference="Stop"; try { $t=@(Get-ScheduledTask | Where-Object { $_.TaskPath -eq "\QTrade\" -and $_.TaskName -eq "WinAgentUser" }); if($t.Count -eq 0){ @{present=$false}|ConvertTo-Json -Compress } elseif($t.Count -ne 1){exit 2} else { $a=@($t[0].Actions); @{present=$true;execute=[string]$a[0].Execute;arguments=[string]$a[0].Arguments;user_id=[string]$t[0].Principal.UserId;multiple_actions=($a.Count -ne 1)}|ConvertTo-Json -Compress } } catch { exit 3 }'
    $r = Invoke-QtProcess -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', $scriptText) -TimeoutSec 30
    if ($r.TimedOut -or $r.ExitCode -ne 0) { throw 'TASK_QUERY_FAILED' }
    return ($r.StdOut | ConvertFrom-Json -ErrorAction Stop)
}

function Get-QtCleanOwnership {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $LiveRoot, $InstalledState, [Parameter(Mandatory)][string] $InstallSid)
    $exists = Test-QtPath -Path $LiveRoot
    if ($exists) {
        if ($null -eq $InstalledState -or -not (Test-QtHasProperty $InstalledState 'env') -or
            -not (Test-QtHasProperty $InstalledState.env 'install_user_sid') -or
            [string]$InstalledState.env.install_user_sid -ne $InstallSid) { throw 'INSTALL_OWNERSHIP_UNKNOWN' }
    }
    $names = Get-QtWinAgentNames
    $svcExe = Join-Path $LiveRoot ('winagent\app\' + $names.svc_exe)
    $userExe = Join-Path $LiveRoot ('winagent\app\' + $names.user_exe)
    $consoleExe = Join-Path $LiveRoot ('console\' + (Get-QtConsoleNames).exe)
    $sc = Invoke-QtSc -ScArgs @('qc', $names.service_name)
    if ($sc.TimedOut -or $sc.ExitCode -notin @(0, 1060)) { throw 'SERVICE_QUERY_FAILED' }
    $service = $sc.ExitCode -eq 0
    if ($service) {
        $svc = @(Get-QtCim -ClassName 'Win32_Service' -Filter ("Name='" + $names.service_name + "'"))
        $newBinary = (@($svcExe, '--service', '--root', (Join-Path $LiveRoot 'winagent'), '--install-user-sid', $InstallSid) |
            ForEach-Object { ConvertTo-QtProcessArgument -Value $_ }) -join ' '
        $allowedBinaries = @(('"' + $svcExe + '"'), $newBinary)
        if (-not $exists -or $svc.Count -ne 1 -or [string]$svc[0].PathName -notin $allowedBinaries -or
            [string]$svc[0].StartName -ne 'LocalSystem') {
            throw 'SERVICE_OWNERSHIP_UNKNOWN'
        }
    }
    $task = Get-QtCleanTask
    if ($task.present) {
        if (-not $exists -or $task.multiple_actions -or $task.execute -ne $userExe -or
            $task.user_id -ne $InstallSid -or
            ((Test-QtHasProperty $task 'arguments') -and $task.arguments)) { throw 'TASK_OWNERSHIP_UNKNOWN' }
    }
    $allDistros = Get-QtRegisteredDistro
    $distros = @($allDistros | Where-Object { $_.name -in @('qtrade', 'qtrade-kcheck') })
    foreach ($d in $distros) {
        $expected = Join-Path $LiveRoot $(if ($d.name -eq 'qtrade') { 'wsl\distro' } else { 'wsl\kcheck' })
        $basePath = ([string]$d.base_path).TrimEnd('\')
        if ($basePath.StartsWith('\\?\')) { $basePath = $basePath.Substring(4) }
        if (-not $exists -or $basePath -ne $expected -or
            ($d.name -eq 'qtrade' -and
            ($InstalledState.distro.name -ne 'qtrade' -or $InstalledState.distro.dir -ne $expected -or
                -not $InstalledState.distro.imported_at))) { throw 'DISTRO_OWNERSHIP_UNKNOWN' }
    }
    $processes = @(Get-QtCim -ClassName 'Win32_Process' | Where-Object {
        $_.ExecutablePath -in @($svcExe, $userExe, $consoleExe)
    })
    if (-not $exists -and $processes.Count) { throw 'PROCESS_OWNERSHIP_UNKNOWN' }
    $uninstallKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{9B2F1C64-2F1E-4E1A-9A1B-0C7A5E3D1B02}_is1'
    $hasUninstallKey = Test-QtRegistryKey -Path $uninstallKey
    if ($hasUninstallKey) {
        $location = [string](Get-QtRegistryValue -Path $uninstallKey -Name 'InstallLocation')
        $uninstallCommand = [string](Get-QtRegistryValue -Path $uninstallKey -Name 'UninstallString')
        $expectedUninstaller = Join-Path $LiveRoot 'unins000.exe'
        if (-not $exists -or $location.TrimEnd('\') -ne $LiveRoot -or
            $uninstallCommand -notin @($expectedUninstaller, ('"' + $expectedUninstaller + '"'))) {
            throw 'UNINSTALL_ENTRY_OWNERSHIP_UNKNOWN'
        }
    }
    $configPath = ''; $configText = $null; $configOriginalText = $null
    if ($exists) {
        $configPath = Join-Path (Get-QtEnvironmentPath -Name 'UserProfile') '.wslconfig'
        if (-not (Test-QtStagePath -Path $configPath)) { throw 'REPARSE_POINT_REFUSED' }
        if (Test-QtPath -Path $configPath) {
            $text = Read-QtTextFile -Path $configPath
            $parsed = ConvertFrom-QtWslConfig -Text $text
            $kernel = Get-QtWslConfigValue -Parsed $parsed -Section 'wsl2' -Key 'kernel'
            if ($kernel -and (($kernel -replace '\\\\', '\') -like ($LiveRoot + '\*'))) {
                if (-not (Test-QtHasProperty $InstalledState 'wslconfig') -or
                    ('kernel=' + $kernel) -ne [string]$InstalledState.wslconfig.kernel_line_written) {
                    throw 'KERNEL_POINTER_OWNERSHIP_UNKNOWN'
                }
                $configOriginalText = $text
                $configText = Remove-QtWslConfigKeys -Text $text -KeysAdded @() -RemoveKernelLine
            }
        }
    }
    return [pscustomobject]@{ Exists = $exists; Service = $service; Task = [bool]$task.present
        Distros = $distros; Processes = $processes; Names = $names
        UninstallKey = $(if ($hasUninstallKey) { $uninstallKey } else { '' })
        ConfigPath = $configPath; ConfigText = $configText; ConfigOriginalText = $configOriginalText }
}

function Write-QtStageJournal {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Journal, [Parameter(Mandatory)][string] $Path)
    Write-QtUtf8NoBom -Path ($Path + '.tmp') -Text ($Journal | ConvertTo-Json -Depth 16) | Out-Null
    Move-QtFileAtomic -Source ($Path + '.tmp') -Destination $Path | Out-Null
}

function Assert-QtCleanRootIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Journal)
    if (-not (Test-QtHasProperty $Journal 'cleanup_id') -or
        [string]$Journal.cleanup_id -notmatch '\A[0-9a-f]{32}\z' -or
        -not (Test-QtHasProperty $Journal 'root_created_ticks') -or
        -not $Journal.root_created_ticks) { throw 'CLEAN_RECOVERY_EVIDENCE_MISSING' }
    if (-not (Test-QtStagePath -Path $Journal.live_root -Tree)) { throw 'REPARSE_POINT_REFUSED' }
    $item = Get-Item -LiteralPath $Journal.live_root -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or
        [string]$item.CreationTimeUtc.Ticks -ne [string]$Journal.root_created_ticks) { throw 'CLEAN_ROOT_IDENTITY_CHANGED' }
}

function Assert-QtCleanMarker {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Journal, [Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { throw 'CLEAN_RECOVERY_EVIDENCE_MISSING' }
    $marker = Read-QtTextFile -Path $Path | ConvertFrom-Json -ErrorAction Stop
    foreach ($key in @('cleanup_id', 'manifest_sha256', 'install_sid', 'live_root', 'root_created_ticks')) {
        if (-not (Test-QtHasProperty $marker $key) -or [string]$marker.$key -cne [string]$Journal.$key) {
            throw 'CLEAN_MARKER_MISMATCH'
        }
    }
}

function Assert-QtResumeEvidenceAcl {
    param([Parameter(Mandatory)][string] $Path)
    foreach ($ace in (Get-QtAcl -Path $Path).Access) {
        if ($ace.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $owner = ConvertTo-QtAclSid -Identity $ace.IdentityReference
        if ($owner -notin @('S-1-5-18', 'S-1-5-32-544') -and
            ([int]$ace.FileSystemRights -band (-bnot [int]([Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize)))) {
            throw 'STAGE_ACL_FAILED'
        }
    }
}

function Find-QtResumeStage {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $StageRoot, [Parameter(Mandatory)][string] $LiveRoot,
        [Parameter(Mandatory)][string] $ManifestHash, [Parameter(Mandatory)][string] $InstallSid)
    $parent = Split-Path -Parent $StageRoot
    $candidates = @()
    $liveMarkerPath = Join-Path $LiveRoot '.qtrade-clean-owner.json'
    $liveMarker = $null
    if (Test-QtPath -Path $liveMarkerPath) {
        if (-not (Test-QtStagePath -Path $liveMarkerPath)) { throw 'REPARSE_POINT_REFUSED' }
        Assert-QtResumeEvidenceAcl -Path $liveMarkerPath
        $liveMarker = Read-QtTextFile -Path $liveMarkerPath | ConvertFrom-Json -ErrorAction Stop
    }
    foreach ($directory in @(Get-ChildItem -LiteralPath $parent -Directory -Force -ErrorAction Stop)) {
        if ($directory.FullName -eq $StageRoot -or $directory.Name -notmatch '\A[0-9a-fA-F-]{32,36}\z') { continue }
        if (-not (Test-QtStagePath -Path $directory.FullName)) { throw 'REPARSE_POINT_REFUSED' }
        $journalPath = Join-Path $directory.FullName 'install\clean-reinstall.json'
        if (-not (Test-QtPath -Path $journalPath)) { continue }
        if (-not (Test-QtStagePath -Path $journalPath)) { throw 'REPARSE_POINT_REFUSED' }
        if (-not (Test-QtInstallRootAcl -Acl (Get-QtAcl -Path $directory.FullName)).Ok) { throw 'STAGE_ACL_FAILED' }
        # Descendant evidence may inherit the protected root, but must not grant writes.
        foreach ($evidence in @((Split-Path -Parent $journalPath), $journalPath)) {
            Assert-QtResumeEvidenceAcl -Path $evidence
        }
        $journal = Read-QtTextFile -Path $journalPath | ConvertFrom-Json -ErrorAction Stop
        if ($journal.live_root -ne $LiveRoot) { continue }
        if ($null -ne $liveMarker -and $journal.cleanup_id -cne $liveMarker.cleanup_id) { continue }
        $installed = Read-QtInstallState -Path (Get-QtPaths -Root $LiveRoot).StateFile
        if ($journal.phase -eq 'prepared' -and $null -ne $installed -and $installed.state -eq 'DONE') { continue }
        if ($journal.manifest_sha256 -ne $ManifestHash -or $journal.install_sid -ne $InstallSid) { throw 'STAGE_IDENTITY_MISMATCH' }
        $oldManifest = Join-Path $directory.FullName 'install\manifest.json'
        if (-not (Test-QtStagePath -Path $oldManifest) -or (Get-QtFileHash -Path $oldManifest) -ne $ManifestHash) { throw 'STAGE_IDENTITY_MISMATCH' }
        Assert-QtResumeEvidenceAcl -Path $oldManifest
        if ($journal.phase -notin @('cleaning', 'removing', 'root_empty', 'copying', 'prepared')) { throw 'STAGE_PHASE_INVALID' }
        $marker = Join-Path $LiveRoot '.qtrade-clean-owner.json'
        if (Test-QtPath -Path $LiveRoot) {
            if (-not (Test-QtInstallRootAcl -Acl (Get-QtAcl -Path $LiveRoot)).Ok) { throw 'STAGE_ACL_FAILED' }
            Assert-QtCleanRootIdentity -Journal $journal
        } elseif ($journal.phase -notin @('root_empty', 'copying', 'cleaning')) { throw 'CLEAN_ROOT_IDENTITY_CHANGED' }
        if (-not (Test-QtPath -Path $marker)) { $marker = Join-Path $directory.FullName 'install\clean-owner.json' }
        if (-not (Test-QtStagePath -Path $marker)) { throw 'REPARSE_POINT_REFUSED' }
        Assert-QtResumeEvidenceAcl -Path $marker
        Assert-QtCleanMarker -Journal $journal -Path $marker
        $candidates += $journalPath
    }
    if ($candidates.Count -gt 1) { throw 'STAGE_RESUME_AMBIGUOUS' }
    if ($candidates.Count -eq 1) { return $candidates[0] }
    if ($null -ne $liveMarker) {
        $installed = Read-QtInstallState -Path (Get-QtPaths -Root $LiveRoot).StateFile
        if ($null -eq $installed -or $installed.state -ne 'DONE') { throw 'CLEAN_RECOVERY_EVIDENCE_MISSING' }
    }
    return ''
}

function Write-QtCleanMarker {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Journal, [Parameter(Mandatory)][string] $Path)
    $marker = [ordered]@{}
    foreach ($key in @('cleanup_id', 'manifest_sha256', 'install_sid', 'live_root', 'root_created_ticks')) { $marker[$key] = $Journal.$key }
    Write-QtStageJournal -Journal $marker -Path $Path
}

function Invoke-QtCleanReinstall {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $StageRoot, [Parameter(Mandatory)][string] $LiveRoot,
        [Parameter(Mandatory)][string] $PackageVersion, [bool] $AcceptCleanReinstall = $false)
    $result = [ordered]@{ Ok = $false; Reason = ''; RequiresConsent = $false; Resumed = $false; State = $null }
    try {
        $id = Get-QtStageIdentity -StageRoot $StageRoot -LiveRoot $LiveRoot
        $acl = Set-QtInstallRootAcl -Path $id.StageRoot
        if (-not $acl.Ok) { throw 'STAGE_ACL_FAILED' }
        $manifest = Read-QtManifest -Path $id.Manifest
        if ($manifest.package_version -ne $PackageVersion) { throw 'PACKAGE_VERSION_MISMATCH' }
        $verified = Invoke-QtPayloadVerify -Manifest $manifest -StageRoot $id.StageRoot -DestinationRoot $id.LiveRoot
        if (-not $verified.Ok -or -not (Test-QtManifestCoredumpL2 -Manifest $manifest)) { throw 'PAYLOAD_CORRUPT' }
        $hash = Get-QtFileHash -Path $id.Manifest
        $sid = Get-QtCurrentUserSid
        if ($sid -notmatch '\AS-1-(?:5-21|12-1)(?:-\d+){4}\z') { throw 'INSTALL_USER_SID_INVALID' }
        $livePaths = Get-QtPaths -Root $id.LiveRoot
        $old = Read-QtInstallState -Path $livePaths.StateFile
        $journal = $null
        if (-not (Test-QtPath -Path $id.Journal)) {
            $previousJournal = Find-QtResumeStage -StageRoot $id.StageRoot -LiveRoot $id.LiveRoot -ManifestHash $hash -InstallSid $sid
            if ($previousJournal) { $id.Journal = $previousJournal }
        }
        if (Test-QtPath -Path $id.Journal) {
            $journal = Read-QtTextFile -Path $id.Journal | ConvertFrom-Json
            if ($journal.manifest_sha256 -ne $hash -or $journal.install_sid -ne $sid -or
                $journal.live_root -ne $id.LiveRoot) { throw 'STAGE_IDENTITY_MISMATCH' }
            if ($journal.phase -eq 'prepared') {
                if ($null -eq $old -or -not (Test-QtHasProperty $old 'package_manifest_sha256') -or
                    $old.package_manifest_sha256 -ne $hash -or $old.env.install_user_sid -ne $sid) {
                    throw 'RESUME_IDENTITY_MISMATCH'
                }
                if ($old.state -ne 'DONE') {
                    Get-QtCleanOwnership -LiveRoot $id.LiveRoot -InstalledState $old -InstallSid $sid | Out-Null
                    $result.Ok = $true; $result.Resumed = $true; $result.State = $old
                    return [pscustomobject]$result
                }
                $journal = $null
            }
        }
        if ($null -eq $journal) {
            $ownership = Get-QtCleanOwnership -LiveRoot $id.LiveRoot -InstalledState $old -InstallSid $sid
            if ($ownership.Exists -and -not $AcceptCleanReinstall) {
                $result.Reason = 'CLEAN_REINSTALL_CONFIRM_REQUIRED'; $result.RequiresConsent = $true
                return [pscustomobject]$result
            }
            $journal = [pscustomobject]@{ manifest_sha256 = $hash; install_sid = $sid
                live_root = $id.LiveRoot; phase = 'cleaning'; old_state = $old; accepted = $AcceptCleanReinstall
                cleanup_id = [guid]::NewGuid().ToString('N')
                root_created_ticks = $(if ($ownership.Exists) { [string](Get-Item -LiteralPath $id.LiveRoot -Force -ErrorAction Stop).CreationTimeUtc.Ticks } else { '' }) }
            Write-QtStageJournal -Journal $journal -Path $id.Journal
            Write-QtCleanMarker -Journal $journal -Path (Join-Path (Split-Path -Parent $id.Journal) 'clean-owner.json')
        }
        if ($journal.phase -eq 'cleaning') {
            # Revalidate all targets before the first effect on every retry.
            $ownership = Get-QtCleanOwnership -LiveRoot $id.LiveRoot -InstalledState $old -InstallSid $sid
            if ($ownership.Exists -and -not $journal.accepted) { throw 'CLEAN_REINSTALL_CONFIRM_REQUIRED' }
            if ($ownership.Exists) { Assert-QtCleanRootIdentity -Journal $journal }
            if ($ownership.Task) {
                foreach ($verb in @('/Change', '/Delete')) {
                    $taskArgs = @($verb, '/TN', ($ownership.Names.task_path + $ownership.Names.task_name))
                    if ($verb -eq '/Change') { $taskArgs += '/Disable' }
                    if ($verb -eq '/Delete') { $taskArgs += '/F' }
                    $r = Invoke-QtProcess -FilePath 'schtasks.exe' -ArgumentList $taskArgs -TimeoutSec 30
                    if ($r.TimedOut -or $r.ExitCode -ne 0) { throw 'TASK_REMOVE_FAILED' }
                }
            }
            if ($ownership.Service) {
                $r = Invoke-QtSc -ScArgs @('stop', $ownership.Names.service_name)
                if ($r.TimedOut -or $r.ExitCode -notin @(0, 1062)) { throw 'SERVICE_STOP_FAILED' }
                $stopped = $false
                for ($i = 0; $i -lt 30; $i++) {
                    if ((Get-QtServiceStatus -Name $ownership.Names.service_name) -eq 'Stopped') { $stopped = $true; break }
                    Start-QtSleep -Seconds 1
                }
                if (-not $stopped) { throw 'SERVICE_STOP_TIMEOUT' }
            }
            # Re-query each PID immediately before termination to avoid PID reuse.
            foreach ($p in $ownership.Processes) {
                $now = @(Get-QtCim -ClassName 'Win32_Process' -Filter ('ProcessId=' + [int]$p.ProcessId))
                if ($now.Count -eq 0) { continue }
                if ($now.Count -ne 1 -or $now[0].ExecutablePath -ne $p.ExecutablePath) { throw 'PROCESS_IDENTITY_CHANGED' }
                $r = Invoke-QtProcess -FilePath 'taskkill.exe' -ArgumentList @('/PID', [string]$p.ProcessId, '/F') -TimeoutSec 30
                if ($r.TimedOut -or $r.ExitCode -ne 0) { throw 'PROCESS_STOP_FAILED' }
            }
            foreach ($d in $ownership.Distros) {
                foreach ($verb in @('--terminate', '--unregister')) {
                    $r = Invoke-QtWsl -WslArgs @($verb, [string]$d.name) -TimeoutSec 300
                    if ($r.TimedOut -or $r.ExitCode -ne 0) { throw 'DISTRO_REMOVE_FAILED' }
                }
            }
            if ($ownership.Service) {
                $r = Invoke-QtSc -ScArgs @('delete', $ownership.Names.service_name)
                if ($r.TimedOut -or $r.ExitCode -ne 0) { throw 'SERVICE_REMOVE_FAILED' }
            }
            if ($ownership.UninstallKey) {
                Remove-QtItem -Path $ownership.UninstallKey -Recurse
                if (Test-QtRegistryKey -Path $ownership.UninstallKey) { throw 'UNINSTALL_ENTRY_REMOVE_FAILED' }
            }
            if ($ownership.Exists) {
                if ($null -ne $ownership.ConfigText) {
                    if ((Read-QtTextFile -Path $ownership.ConfigPath) -cne $ownership.ConfigOriginalText) {
                        throw 'KERNEL_CONFIG_CHANGED'
                    }
                    Write-QtUtf8NoBom -Path $ownership.ConfigPath -Text $ownership.ConfigText | Out-Null
                }
                Assert-QtCleanRootIdentity -Journal $journal
                $markerPath = Join-Path $id.LiveRoot '.qtrade-clean-owner.json'
                if (Test-QtPath -Path $markerPath) {
                    $existingMarker = Read-QtTextFile -Path $markerPath | ConvertFrom-Json -ErrorAction Stop
                    if ($existingMarker.cleanup_id -cne $journal.cleanup_id -and $null -ne $old -and $old.state -eq 'DONE') {
                        if ($existingMarker.install_sid -cne $sid -or $existingMarker.live_root -cne $id.LiveRoot -or
                            $existingMarker.root_created_ticks -cne $journal.root_created_ticks -or
                            -not (Test-QtHasProperty $old 'package_manifest_sha256') -or
                            $existingMarker.manifest_sha256 -cne $old.package_manifest_sha256) { throw 'CLEAN_MARKER_MISMATCH' }
                        # Replace only the completed installation marker after clean consent.
                        Write-QtCleanMarker -Journal $journal -Path $markerPath
                    }
                    Assert-QtCleanMarker -Journal $journal -Path $markerPath
                } else {
                    $marker = [ordered]@{}
                    foreach ($key in @('cleanup_id', 'manifest_sha256', 'install_sid', 'live_root', 'root_created_ticks')) {
                        $marker[$key] = $journal.$key
                    }
                    Write-QtStageJournal -Journal $marker -Path $markerPath
                }
                $journal.phase = 'removing'
            } else {
                $journal.phase = 'copying'
            }
            Write-QtStageJournal -Journal $journal -Path $id.Journal
        }
        if ($journal.phase -in @('removing', 'root_empty')) {
            # Only this journal and its private-root marker can replace a deleted state file.
            $ownership = Get-QtCleanOwnership -LiveRoot $id.LiveRoot -InstalledState $journal.old_state -InstallSid $sid
            if ($ownership.Service -or $ownership.Task -or $ownership.Distros.Count -or
                $ownership.Processes.Count -or $ownership.UninstallKey -or
                $null -ne $ownership.ConfigText) { throw 'CLEAN_TARGET_REAPPEARED' }
            if ($ownership.Exists) {
                if (-not $journal.accepted) { throw 'CLEAN_REINSTALL_CONFIRM_REQUIRED' }
                Assert-QtCleanRootIdentity -Journal $journal
                $markerPath = Join-Path $id.LiveRoot '.qtrade-clean-owner.json'
                if ($journal.phase -eq 'removing') {
                    Assert-QtCleanMarker -Journal $journal -Path $markerPath
                    foreach ($item in @(Get-ChildItem -LiteralPath $id.LiveRoot -Force -ErrorAction Stop)) {
                        if ($item.FullName -eq $markerPath) { continue }
                        if (-not (Test-QtStagePath -Path $item.FullName -Tree)) { throw 'REPARSE_POINT_REFUSED' }
                        Remove-QtItem -Path $item.FullName -Recurse
                        if (Test-QtPath -Path $item.FullName) { throw 'LIVE_ROOT_REMOVE_FAILED' }
                    }
                    # Persist the narrow final boundary before removing the last in-root proof.
                    $journal.phase = 'root_empty'
                    Write-QtStageJournal -Journal $journal -Path $id.Journal
                }
                $left = @(Get-ChildItem -LiteralPath $id.LiveRoot -Force -ErrorAction Stop)
                if (@($left | Where-Object { $_.FullName -ne $markerPath }).Count) { throw 'CLEAN_ROOT_NOT_EMPTY' }
                if (Test-QtPath -Path $markerPath) {
                    Assert-QtCleanMarker -Journal $journal -Path $markerPath
                    Remove-QtItem -Path $markerPath
                    if (Test-QtPath -Path $markerPath) { throw 'LIVE_ROOT_REMOVE_FAILED' }
                }
                # No recursive removal after losing the marker: new contents must block removal.
                Remove-QtItem -Path $id.LiveRoot
                if (Test-QtPath -Path $id.LiveRoot) { throw 'LIVE_ROOT_REMOVE_FAILED' }
            } elseif ($journal.phase -ne 'root_empty') { throw 'CLEAN_ROOT_IDENTITY_CHANGED' }
            $journal.phase = 'copying'
            Write-QtStageJournal -Journal $journal -Path $id.Journal
        }
        if ($journal.phase -ne 'copying') { throw 'STAGE_PHASE_INVALID' }
        $fresh = New-QtInstallState -PackageVersion $PackageVersion
        $fresh | Add-Member -NotePropertyName package_manifest_sha256 -NotePropertyValue $hash
        $fresh.env | Add-Member -NotePropertyName install_user_sid -NotePropertyValue $sid
        if (Test-QtPath -Path $id.LiveRoot) {
            $partial = Read-QtInstallState -Path $livePaths.StateFile
            if ($null -eq $partial -or -not (Test-QtHasProperty $partial 'package_manifest_sha256') -or
                $partial.package_manifest_sha256 -ne $hash -or $partial.env.install_user_sid -ne $sid -or
                $partial.state -ne 'PRECHECK') { throw 'PARTIAL_COPY_OWNERSHIP_UNKNOWN' }
            $copyOwner = Get-QtCleanOwnership -LiveRoot $id.LiveRoot -InstalledState $partial -InstallSid $sid
            if ($copyOwner.Service -or $copyOwner.Task -or $copyOwner.Distros.Count -or $copyOwner.Processes.Count) {
                throw 'PARTIAL_COPY_IN_USE'
            }
        }
        $creatingRoot = -not (Test-QtPath -Path $id.LiveRoot)
        New-QtDirectory -Path $id.LiveRoot | Out-Null
        $acl = Set-QtInstallRootAcl -Path $id.LiveRoot
        if (-not $acl.Ok) { throw 'LIVE_ACL_FAILED' }
        if ($creatingRoot) {
            $journal.root_created_ticks = [string](Get-Item -LiteralPath $id.LiveRoot -Force -ErrorAction Stop).CreationTimeUtc.Ticks
            Write-QtStageJournal -Journal $journal -Path $id.Journal
            Write-QtCleanMarker -Journal $journal -Path (Join-Path $id.LiveRoot '.qtrade-clean-owner.json')
            Write-QtCleanMarker -Journal $journal -Path (Join-Path (Split-Path -Parent $id.Journal) 'clean-owner.json')
        }
        Write-QtInstallState -State $fresh -Path $livePaths.StateFile | Out-Null
        foreach ($entry in $verified.Entries) {
            $relative = [string]$entry.path -replace '/', '\'
            $target = Join-Path $id.LiveRoot $relative
            if (-not (Test-QtStagePath -Path $target)) { throw 'REPARSE_POINT_REFUSED' }
            New-QtDirectory -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath (Join-Path $id.StageRoot $relative) -Destination $target -Force -ErrorAction Stop
        }
        Copy-Item -LiteralPath $id.Manifest -Destination $livePaths.Manifest -Force -ErrorAction Stop
        $copied = Invoke-QtPayloadVerify -Manifest $manifest -StageRoot $id.LiveRoot
        if (-not $copied.Ok) { throw 'COPIED_PAYLOAD_CORRUPT' }
        $fresh.resume.engine = $livePaths.EngineExe
        Write-QtInstallState -State $fresh -Path $livePaths.StateFile | Out-Null
        $journal.phase = 'prepared'; $journal.old_state = $null
        Write-QtStageJournal -Journal $journal -Path $id.Journal
        $result.Ok = $true; $result.State = $fresh
    } catch { $result.Reason = $_.Exception.Message }
    return [pscustomobject]$result
}

Export-ModuleMember -Function Test-QtStagePath, Get-QtStageIdentity, Get-QtCleanTask, Get-QtCleanOwnership,
    Write-QtStageJournal, Find-QtResumeStage, Invoke-QtCleanReinstall
