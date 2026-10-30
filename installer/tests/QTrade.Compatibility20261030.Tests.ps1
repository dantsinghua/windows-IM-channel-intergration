# Developer regressions. All operating-system effects are mocked.
#requires -Version 5.1
BeforeAll {
    $modules = $env:QT_COMPAT_MODULES
    if (-not $modules) { $modules = Join-Path $PSScriptRoot '..\engine\modules' }
    foreach ($name in @('Native','State','Log','Wsl','Kernel','Preflight','Distro','Stage','Acl','Console')) {
        Import-Module (Join-Path $modules ('QTrade.' + $name + '.psm1')) -DisableNameChecking -Force
    }
    $source=Join-Path (Split-Path -Parent $modules) 'run-step.ps1'
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'RUNSTEP_PARSE_FAILED'}
    $guard=@($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.IfStatementAst] -and $_.Extent.Text -match 'FULL_INSTALLER_REQUIRED' -and $_.Extent.Text -match 'Read-QtInstallState' })
    if($guard.Count -ne 1){throw 'RESUME_GUARD_NOT_UNIQUE'}
    Set-Item Function:Invoke-CompatibilityResumeGuard ([scriptblock]::Create($guard[0].Extent.Text))
    $dispatcher=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.SwitchStatementAst] -and $n.Condition.Extent.Text -eq '$Step'},$true))[0]
    foreach($name in @('verify-kernel','DONE')) {
        $body=@($dispatcher.Clauses | Where-Object {$_.Item1.Value -eq $name})[0].Item2.Extent.Text
        Set-Item ('Function:Invoke-CompatibilityStep-' + $name) ([scriptblock]::Create($body.Substring(1,$body.Length-2)))
    }
    function Get-QtOpt {param($Name,$Default); if($Name -eq 'package_version'){return '1.0.3'}; return $Default}
    function Write-QtStepResult {param($Ok,$State,$Reason,$ExitName,$Message,$Data); $script:Result=[pscustomobject]@{Ok=$Ok;State=$State;Reason=$Reason}; throw 'CAPTURED_COMPAT_RESULT'}
}

Describe 'INST-01 verify-only entry must enforce the resume identity guard' {
    BeforeEach {
        $script:Step='verify-kernel'; $script:StageRoot=''; $script:Result=$null
        $script:paths=Get-QtPaths -Root (Join-Path $TestDrive 'guard')
        $script:Saved=[pscustomobject]@{state='WSLCONFIG_WRITTEN';package_version='1.0.3';package_manifest_sha256='fixture-hash';env=[pscustomobject]@{install_user_sid='S-1-5-21-1-2-3-1001'}}
        Mock Read-QtInstallState {$script:Saved}
        Mock Get-QtCurrentUserSid {'S-1-5-21-1-2-3-1001'}
        Mock Test-QtPath {$true}
        Mock Get-QtFileHash {'fixture-hash'}
    }
    It 'allows an unfinished same-SID same-package verify without side effects' {
        {Invoke-CompatibilityResumeGuard} | Should -Not -Throw
        $script:Result | Should -BeNullOrEmpty
    }
    It 'rejects an invalid resume <Boundary> before kernel actions' -ForEach @(@{Boundary='sid'},@{Boundary='hash'},@{Boundary='done'},@{Boundary='version'}) {
        switch($Boundary) {
            'sid' {$script:Saved.env.install_user_sid='S-1-5-21-1-2-3-1002'}
            'hash' {$script:Saved.package_manifest_sha256='wrong'}
            'done' {$script:Saved.state='DONE'}
            'version' {$script:Saved.package_version='other'}
        }
        {Invoke-CompatibilityResumeGuard} | Should -Throw '*CAPTURED_COMPAT_RESULT*'
        $script:Result.Ok | Should -BeFalse
        $script:Result.Reason | Should -BeIn @('INSTALL_OWNERSHIP_UNKNOWN','RESUME_IDENTITY_MISMATCH')
    }
}

Describe 'RUN-01 DONE cannot succeed when preference ACL setup fails' {
    It 'preserves the ACL gate and never marks DONE or creates shortcuts after rejection' {
        $script:paths=Get-QtPaths -Root (Join-Path $TestDrive 'done')
        $script:state=New-QtInstallState -PackageVersion '1.0.3'
        $script:state.env | Add-Member NoteProperty install_user_sid 'S-1-5-21-1-2-3-1001' -Force
        $script:Result=$null
        Mock Set-QtConsoleConfigAcl {[pscustomobject]@{Ok=$false;Problems=@('fixture denied')}}
        Mock Clear-QtRunOnce {throw 'Must retain recovery after ACL failure'}
        Mock Install-QtConsoleShortcuts {throw 'Must not create shortcuts after ACL failure'}
        {Invoke-CompatibilityStep-DONE} | Should -Throw '*CAPTURED_COMPAT_RESULT*'
        $script:Result.Ok | Should -BeFalse
        $script:Result.Reason | Should -Be 'ACL_HARDEN_FAILED'
        Should -Invoke Clear-QtRunOnce -Times 0 -Exactly
        Should -Invoke Install-QtConsoleShortcuts -Times 0 -Exactly
    }
}

Describe 'INST-03 WSUS must only block when feature payload is needed' {
    BeforeEach {
        Mock -ModuleName QTrade.Preflight Get-QtWindowsFacts { [pscustomobject]@{win_build=22631;win_ubr=1;edition='Enterprise';ltsc=$false;x64=$true} }
        Mock -ModuleName QTrade.Preflight Test-QtAdmin { $true }
        Mock -ModuleName QTrade.Preflight Test-QtElevatedUserMatchesLogon { [pscustomobject]@{Ok=$true;ElevatedSid='S-1-5-21-1-2-3-1001';LogonSid='S-1-5-21-1-2-3-1001'} }
        Mock -ModuleName QTrade.Preflight Get-QtVirtualizationOk { $true }
        Mock -ModuleName QTrade.Preflight Get-QtWslPolicyFacts { [pscustomobject]@{CustomKernelForbidden=$false;WsusBlocksFod=$true} }
        # Stop after the policy verdict; never enumerate real drives or WSL.
        Mock -ModuleName QTrade.Preflight Get-QtCim { @([pscustomobject]@{TotalPhysicalMemory=0}) }
        Mock -ModuleName QTrade.Preflight Get-QtOptionalFeatureState { 'Enabled' }
        Mock -ModuleName QTrade.Preflight Invoke-QtWsl { throw 'Unexpected WSL action' }
    }
    It 'passes the WSUS gate when both features are already Enabled' {
        $r = Invoke-QtPreflight -TargetDriveLetter 'Z'
        $r.Env.policy.blocked | Should -BeFalse
        $r.Reason | Should -Be 'MEM_LOW'
    }
    It 'still refuses required payload under WSUS' -ForEach @(@{State='Disabled'},@{State='DisabledWithPayloadRemoved'}) {
        Mock -ModuleName QTrade.Preflight Get-QtOptionalFeatureState { $State }
        $r = Invoke-QtPreflight -TargetDriveLetter 'Z'
        $r.Env.policy.blocked | Should -BeTrue
        $r.Reason | Should -Be 'POLICY_BLOCKED'
    }
}

Describe 'INST-05 desktop absence and failed identity query are distinct' {
    BeforeEach {
        Mock -ModuleName QTrade.Native Get-Process {
            if ($Id) { return [pscustomobject]@{Id=$Id;SessionId=7;ProcessName='powershell'} }
            @([pscustomobject]@{Id=901;SessionId=7;ProcessName='explorer'})
        }
        Mock -ModuleName QTrade.Native Get-CimInstance { throw 'fixture CIM denied' }
        Mock -ModuleName QTrade.Native Invoke-CimMethod { throw 'unexpected query' }
    }
    It 'rejects unknown SID while explorer exists' {
        { Get-QtInteractiveUserSid } | Should -Throw '*INTERACTIVE_USER_SID_QUERY_FAILED*'
    }
    It 'rejects empty SID even with successful CIM transport' {
        Mock -ModuleName QTrade.Native Get-CimInstance { [pscustomobject]@{ProcessId=901} }
        Mock -ModuleName QTrade.Native Invoke-CimMethod { [pscustomobject]@{ReturnValue=0;Sid=''} }
        { Get-QtInteractiveUserSid } | Should -Throw '*INTERACTIVE_USER_SID_QUERY_FAILED*'
    }
    It 'preserves confirmed unattended absence' {
        Mock -ModuleName QTrade.Native Get-Process {
            if ($Id) { return [pscustomobject]@{Id=$Id;SessionId=7;ProcessName='powershell'} }
            @()
        }
        Get-QtInteractiveUserSid | Should -BeNullOrEmpty
    }
}

Describe 'INST-04 local offline route discovery' {
    BeforeEach {
        $script:Addresses = '[{"ifname":"eth0","addr_info":[{"family":"inet","scope":"global","local":"172.24.1.2"}]}]'
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            if (($WslArgs -join ' ') -ceq '-d qtrade --user root --exec /usr/bin/wslinfo --networking-mode') {
                return [pscustomobject]@{ExitCode=0;TimedOut=$false;StdOut='nat';StdErr=''}
            }
            if ($WslArgs -contains 'route') { return [pscustomobject]@{ExitCode=2;TimedOut=$false;StdOut='';StdErr='unreachable'} }
            if (($WslArgs -contains 'addr' -or $WslArgs -contains 'address') -and $WslArgs -contains 'eth0') {
                return [pscustomobject]@{ExitCode=0;TimedOut=$false;StdOut=$script:Addresses;StdErr=''}
            }
            throw ('Unexpected discovery command: ' + ($WslArgs -join ' '))
        }
    }
    It 'finds the unique private eth0 address without any external route' {
        Get-QtAgentBaseUrl | Should -Be 'http://172.24.1.2:17600'
    }
    It 'rejects ambiguous eth0 addresses' {
        $script:Addresses = '[{"ifname":"eth0","addr_info":[{"family":"inet","scope":"global","local":"172.24.1.2"},{"family":"inet","scope":"global","local":"172.24.1.3"}]}]'
        Get-QtAgentBaseUrl | Should -BeNullOrEmpty
    }
    It 'rejects public addresses and unrelated distributions' {
        $script:Addresses = '[{"ifname":"eth0","addr_info":[{"family":"inet","scope":"global","local":"8.8.8.8"}]}]'
        Get-QtAgentBaseUrl | Should -BeNullOrEmpty
        Get-QtAgentBaseUrl -DistroName 'unrelated' | Should -BeNullOrEmpty
    }
}

Describe 'RUN-02 scheduled user agent has no default 72-hour deadline' {
    It 'passes unlimited lifetime and preserves restart and battery policy to registration' {
        Mock -ModuleName QTrade.Native Register-ScheduledTask {
            $script:CapturedSettings = $Settings
            $script:CapturedPrincipal = $Principal
        }
        Register-QtScheduledTask -TaskPath '\FixtureOnly\' -TaskName 'NeverRegistered' -Execute 'C:\fixture\never.exe' -UserId 'S-1-5-21-1-2-3-1001'
        [System.Xml.XmlConvert]::ToTimeSpan($script:CapturedSettings.ExecutionTimeLimit).TotalSeconds | Should -Be 0
        $script:CapturedSettings.RestartCount | Should -Be 3
        $script:CapturedSettings.RestartInterval | Should -Be 'PT1M'
        $script:CapturedSettings.DisallowStartIfOnBatteries | Should -BeFalse
        $script:CapturedSettings.StopIfGoingOnBatteries | Should -BeFalse
        $script:CapturedPrincipal.RunLevel.ToString() | Should -Be 'Limited'
        Should -Invoke -ModuleName QTrade.Native Register-ScheduledTask -Times 1 -Exactly
    }
}

Describe 'INST-02 a new stage resumes only a unique trusted previous transaction' {
    BeforeEach {
        $script:Base = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:Old = Join-Path $script:Base ([guid]::NewGuid().ToString('N'))
        $script:New = Join-Path $script:Base ([guid]::NewGuid().ToString('N'))
        $script:Live = Join-Path $script:Base 'live'
        foreach ($d in @((Join-Path $script:Old 'install'),(Join-Path $script:New 'install'),$script:Live)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        $script:Sid = 'S-1-5-21-1-2-3-1001'
        [IO.File]::WriteAllText((Join-Path $script:Old 'install\manifest.json'), '{"package_version":"1.0.3"}')
        Copy-Item -LiteralPath (Join-Path $script:Old 'install\manifest.json') -Destination (Join-Path $script:New 'install\manifest.json')
        $script:Hash = (Get-FileHash -LiteralPath (Join-Path $script:Old 'install\manifest.json')).Hash.ToLowerInvariant()
        $script:Journal = [pscustomobject]@{manifest_sha256=$script:Hash; install_sid=$script:Sid;live_root=$script:Live;
            phase='removing';accepted=$true;cleanup_id=[guid]::NewGuid().ToString('N');
            root_created_ticks=[string](Get-Item -LiteralPath $script:Live).CreationTimeUtc.Ticks;
            old_state=[pscustomobject]@{env=[pscustomobject]@{install_user_sid=$script:Sid}}}
        $script:JournalPath = Join-Path $script:Old 'install\clean-reinstall.json'
        [IO.File]::WriteAllText($script:JournalPath, ($script:Journal | ConvertTo-Json -Depth 6))
        [IO.File]::WriteAllText((Join-Path $script:Live '.qtrade-clean-owner.json'), ($script:Journal | ConvertTo-Json -Depth 6))
        # ACL is an injected fact. No real ACL is changed by these recovery tests.
        Mock -ModuleName QTrade.Stage Get-QtAcl { [pscustomobject]@{Access=@()} }
        Mock -ModuleName QTrade.Stage Test-QtInstallRootAcl { [pscustomobject]@{Ok=$true} }
    }
    It 'finds the old journal after install_state was removed and a new GUID stage arrived' {
        Find-QtResumeStage -StageRoot $script:New -LiveRoot $script:Live -ManifestHash $script:Hash -InstallSid $script:Sid | Should -Be $script:JournalPath
    }
    It 'rejects mismatched <Field>' -ForEach @(@{Field='install_sid'},@{Field='manifest_sha256'},@{Field='cleanup_id'}) {
        $script:Journal.$Field = 'mismatch'
        [IO.File]::WriteAllText($script:JournalPath, ($script:Journal | ConvertTo-Json -Depth 6))
        $expected = if ($Field -eq 'cleanup_id') { '*CLEAN_*' } else { '*STAGE_IDENTITY_MISMATCH*' }
        { Find-QtResumeStage -StageRoot $script:New -LiveRoot $script:Live -ManifestHash $script:Hash -InstallSid $script:Sid } | Should -Throw $expected
    }
    It 'rejects multiple matching old stages rather than guessing' {
        $other = Join-Path $script:Base ([guid]::NewGuid().ToString('N'))
        Copy-Item -LiteralPath $script:Old -Destination $other -Recurse
        { Find-QtResumeStage -StageRoot $script:New -LiveRoot $script:Live -ManifestHash $script:Hash -InstallSid $script:Sid } | Should -Throw '*STAGE_RESUME_AMBIGUOUS*'
    }
    It 'rejects writable old stage evidence' {
        Mock -ModuleName QTrade.Stage Test-QtInstallRootAcl { [pscustomobject]@{Ok=$false} }
        { Find-QtResumeStage -StageRoot $script:New -LiveRoot $script:Live -ManifestHash $script:Hash -InstallSid $script:Sid } | Should -Throw '*STAGE_ACL_FAILED*'
    }
    It 'ignores historical journals from another cleanup_id when the live marker identifies this transaction' {
        $history = Join-Path $script:Base ([guid]::NewGuid().ToString('N'))
        Copy-Item -LiteralPath $script:Old -Destination $history -Recurse
        $old = $script:Journal | ConvertTo-Json -Depth 6 | ConvertFrom-Json
        $old.cleanup_id = [guid]::NewGuid().ToString('N')
        $old.phase = 'prepared'
        [IO.File]::WriteAllText((Join-Path $history 'install\clean-reinstall.json'),($old | ConvertTo-Json -Depth 6))
        Find-QtResumeStage -StageRoot $script:New -LiveRoot $script:Live -ManifestHash $script:Hash -InstallSid $script:Sid | Should -Be $script:JournalPath
    }
    It 'does not silently start fresh when a live marker has no matching journal' {
        $script:Journal.cleanup_id = [guid]::NewGuid().ToString('N')
        [IO.File]::WriteAllText($script:JournalPath,($script:Journal | ConvertTo-Json -Depth 6))
        { Find-QtResumeStage -StageRoot $script:New -LiveRoot $script:Live -ManifestHash $script:Hash -InstallSid $script:Sid } | Should -Throw '*CLEAN_*'
    }
    It 'new-stage public entry resumes deletion after install_state disappeared' {
        Mock -ModuleName QTrade.Stage Set-QtInstallRootAcl { [pscustomobject]@{Ok=$true} }
        Mock -ModuleName QTrade.Stage Invoke-QtPayloadVerify { [pscustomobject]@{Ok=$true;Entries=@()} }
        Mock -ModuleName QTrade.Stage Test-QtManifestCoredumpL2 { $true }
        Mock -ModuleName QTrade.Stage Get-QtCurrentUserSid { $script:Sid }
        Mock -ModuleName QTrade.Stage Invoke-QtSc { [pscustomobject]@{ExitCode=1060;TimedOut=$false} }
        Mock -ModuleName QTrade.Stage Get-QtCleanTask { [pscustomobject]@{present=$false} }
        Mock -ModuleName QTrade.Stage Get-QtRegisteredDistro { return ,@() }
        Mock -ModuleName QTrade.Stage Get-QtCim { @() }
        Mock -ModuleName QTrade.Stage Test-QtRegistryKey { $false }
        Mock -ModuleName QTrade.Stage Get-QtEnvironmentPath { Join-Path $script:Base 'absent-user' }
        Mock -ModuleName QTrade.Stage Invoke-QtProcess { throw 'No native process permitted' }
        Mock -ModuleName QTrade.Stage Invoke-QtWsl { throw 'No WSL operation permitted' }
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            if ($Path -ne $script:Live -and (Split-Path -Parent $Path) -ne $script:Live) { throw 'Fixture scope escape' }
            Move-Item -LiteralPath $Path -Destination (Join-Path $script:Base ('preserved-' + [guid]::NewGuid().ToString('N')))
        }
        $r = Invoke-QtCleanReinstall -StageRoot $script:New -LiveRoot $script:Live -PackageVersion '1.0.3'
        $r.Ok | Should -BeTrue -Because $r.Reason
        $r.State.state | Should -Be 'PRECHECK'
        $r.State.env.install_user_sid | Should -Be $script:Sid
        (Get-Content -LiteralPath $script:JournalPath -Raw | ConvertFrom-Json).phase | Should -Be 'prepared'
    }
}

Describe 'RUN-01 dedicated console ACL preserves parent protection' {
    It 'grants Modify only to the installation SID and keeps Users read-only' {
        $path = Join-Path $TestDrive 'config\console'
        $script:AclTarget = $path
        $sid = 'S-1-5-21-1-2-3-1001'
        $script:AclObject = New-Object Security.AccessControl.DirectorySecurity
        Mock -ModuleName QTrade.Acl Set-QtInstallRootAcl { [pscustomobject]@{Ok=$true} }
        Mock -ModuleName QTrade.Acl Get-QtAcl { $script:AclObject }
        Mock -ModuleName QTrade.Acl Set-QtAcl { $script:AclObject = $AclObject }
        $r = Set-QtConsoleConfigAcl -Path $path -InstallSid $sid
        $r.Ok | Should -BeTrue -Because ($r.Problems -join ',')
        $rules = @($script:AclObject.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
        $owner = @($rules | Where-Object {$_.IdentityReference.Value -eq $sid})
        $owner.Count | Should -Be 1
        ([int]$owner[0].FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Modify) | Should -Be ([int][Security.AccessControl.FileSystemRights]::Modify)
        $users = @($rules | Where-Object {$_.IdentityReference.Value -eq 'S-1-5-32-545'})
        ([int]$users[0].FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Write) | Should -Be 0
        Should -Invoke -ModuleName QTrade.Acl Set-QtInstallRootAcl -Times 1 -Exactly -ParameterFilter {$Path -eq (Split-Path -Parent $script:AclTarget)}
        Should -Invoke -ModuleName QTrade.Acl Set-QtAcl -Times 1 -Exactly -ParameterFilter {$Path -eq $script:AclTarget}
    }
}
