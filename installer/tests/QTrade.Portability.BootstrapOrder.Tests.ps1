#requires -Version 5.1
# Execute only the installer switch body with all operating-system effects mocked.
BeforeAll {
    $modules = $env:QT_REMAINING_TEST_MODULES
    if (-not $modules) { $modules = Join-Path $PSScriptRoot '..\engine\modules' }
    foreach ($name in @('Native','State','Distro','WinAgent','Firewall','Console','Acl')) {
        Import-Module (Join-Path $modules ('QTrade.' + $name + '.psm1')) -DisableNameChecking
    }
    $tokens = $null; $errors = $null
    $source = $env:QT_REMAINING_TEST_RUNSTEP
    if (-not $source) { $source = Join-Path $PSScriptRoot '..\engine\run-step.ps1' }
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        $source, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Installer source did not parse.' }
    $switches = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.SwitchStatementAst] }, $true)
    $bodies = @($switches | ForEach-Object { $_.Clauses } | Where-Object { $_.Item1.Extent.Text -eq "'WINAGENT_INSTALLED'" })
    if ($bodies.Count -ne 1) { throw 'The installer must have one WinAgent installation branch.' }
    $body = $bodies[0].Item2.Extent.Text
    Set-Item -Path Function:Invoke-FixtureWinAgentStep -Value ([scriptblock]::Create($body.Substring(1, $body.Length - 2)))
    $done = @($switches | ForEach-Object { $_.Clauses } | Where-Object { $_.Item1.Extent.Text -eq "'DONE'" })[0].Item2.Extent.Text
    Set-Item -Path Function:Invoke-FixtureDoneStep -Value ([scriptblock]::Create($done.Substring(1, $done.Length - 2)))
    function New-QtFailedState { param($Step, $Reason) return ('FAILED:' + $Step + ':' + $Reason) }
    function Write-QtStepResult {
        param($Ok, $State, $Reason, $ExitName, $Message, $Data)
        throw 'Unmocked step result.'
    }
}

Describe 'Installer starts authenticated WinAgent before binding guest credentials' {
    BeforeEach {
        $script:Events = New-Object Collections.Generic.List[string]
        $script:Result = $null
        $script:Failure = ''
        $script:paths = [pscustomobject]@{ Pkg = $TestDrive; WinAgent = $TestDrive; StateFile = (Join-Path $TestDrive 'state.json') }
        $script:state = [pscustomobject]@{ state = 'DISTRO_IMPORTED'; package_version = '1.0.2'; firewall_rules = '';
            env = [pscustomobject]@{ install_user_sid = 'S-1-5-21-1111-2222-3333-1001' };
            distro = [pscustomobject]@{ name = 'qtrade' } }
        Mock Invoke-QtProcess { throw 'Real process forbidden.' }
        Mock Invoke-QtWsl { throw 'Real WSL forbidden.' }
        Mock Install-QtVcRedist { [pscustomobject]@{ Ok = $true } }
        Mock Install-QtWinAgentService { [pscustomobject]@{ Ok = $true } }
        Mock Register-QtWinAgentUserTask { [pscustomobject]@{ Ok = $true } }
        Mock Invoke-QtSc {
            $ScArgs[0] | Should -Be 'start'
            $script:Events.Add('service-start')
            [pscustomobject]@{ ExitCode = 0; TimedOut = $false }
        }
        Mock Get-QtBootstrapTokens {
            $script:Events.Add('bootstrap')
            [pscustomobject]@{ Ok = ($script:Failure -ne 'bootstrap'); Reason = 'BOOTSTRAP_PIPE_UNTRUSTED';
                ConsoleToken = ('A' * 64); WinAgentToken = ('B' * 64) }
        }
        Mock Wait-QtWinAgentHealthy {
            $script:Events.Add('authenticated-health')
            $Token | Should -Be ('A' * 64)
            [pscustomobject]@{ Ok = ($script:Failure -ne 'health'); Reason = 'WINAGENT_NOT_READY'; UserAgent = $false }
        }
        Mock Write-QtWinAgentToken {
            $Token | Should -Be ('B' * 64)
            $script:Events.Add('guest-wa-token')
            [pscustomobject]@{ Ok = ($script:Failure -ne 'credential'); Reason = 'TOKEN_WRITE_FAILED' }
        }
        Mock Set-QtAgentConsoleToken { $script:Events.Add('guest-console-token'); return $true }
        Mock Start-QtDistroFirstboot {
            $script:Events.Add('firstboot')
            [pscustomobject]@{ Ok = ($script:Failure -ne 'firstboot'); Reason = 'FIRSTBOOT_FAILED' }
        }
        Mock Invoke-QtFirewallEnsure { [pscustomobject]@{ Ok = ($script:Failure -ne 'firewall'); Reason = 'FIREWALL_REQUEST_FAILED'; Action = 'unchanged'; Blocked = $false; Message = 'fixture' } }
        Mock Write-QtLog { }
        Mock Set-QtState { $State.state = $To }
        Mock Write-QtInstallState { }
        Mock Write-QtStepResult {
            $script:Result = [pscustomobject]@{ Ok = $Ok; State = $State; Reason = $Reason; ExitName = $ExitName; Data = $Data }
            throw 'FixtureStepFinished'
        }
    }
    It 'starts the service, gets trusted credentials, authenticates health, then binds guest and starts firstboot' {
        { Invoke-FixtureWinAgentStep } | Should -Throw 'FixtureStepFinished'
        $script:Result.Ok | Should -BeTrue
        ($script:Events -join ',') | Should -Be 'service-start,bootstrap,authenticated-health,guest-wa-token,guest-console-token,firstboot'
        $script:Result.State | Should -Be 'WINAGENT_INSTALLED'
    }
    It 'stops on <Failure> and never reports installation success' -ForEach @(
        @{ Failure = 'bootstrap'; Reason = 'BOOTSTRAP_PIPE_UNTRUSTED'; BlockedEvent = 'authenticated-health' },
        @{ Failure = 'health'; Reason = 'WINAGENT_NOT_READY'; BlockedEvent = 'guest-wa-token' },
        @{ Failure = 'credential'; Reason = 'TOKEN_BOOTSTRAP_FAILED'; BlockedEvent = 'firstboot' },
        @{ Failure = 'firstboot'; Reason = 'FIRSTBOOT_FAILED'; BlockedEvent = 'unused' },
        @{ Failure = 'firewall'; Reason = 'FIREWALL_REQUEST_FAILED'; BlockedEvent = 'unused' }
    ) {
        $script:Failure = $Failure
        { Invoke-FixtureWinAgentStep } | Should -Throw 'FixtureStepFinished'
        $script:Result.Ok | Should -BeFalse
        $script:Result.Reason | Should -Be $Reason
        $script:Result.State | Should -Not -Be 'WINAGENT_INSTALLED'
        $script:Events | Should -Not -Contain $BlockedEvent
    }
}

Describe 'Firewall policy warning survives completion and is not a failure' {
    BeforeEach {
        $script:paths = Get-QtPaths -Root (Join-Path $TestDrive 'done')
        $script:state = New-QtInstallState -PackageVersion '1.0.2'
        $script:state.env | Add-Member -NotePropertyName install_user_sid -NotePropertyValue 'S-1-5-21-1-2-3-1001' -Force
        $script:state.env | Add-Member -NotePropertyName docker_cidr -NotePropertyValue '10.213.0.0/16' -Force
        $script:Result = $null
        $script:Summary = $null
        Mock Clear-QtRunOnce { }
        Mock Set-QtConsoleConfigAcl {
            $InstallSid | Should -Be 'S-1-5-21-1-2-3-1001'
            $Path | Should -Be (Join-Path $script:paths.Root 'config\console')
            [pscustomobject]@{ Ok = $true; Problems = @() }
        }
        Mock Install-QtConsoleShortcuts { [pscustomobject]@{ Created = @() } }
        Mock Remove-QtStagedRootfs { [pscustomobject]@{ FreedBytes = 0 } }
        Mock Write-QtInstallSummary { $script:Summary = $Summary }
        Mock Set-QtState { $State.state = $To }
        Mock Write-QtInstallState { }
        Mock Test-QtPath { $false }
        Mock Write-QtStepResult {
            $script:Result = [pscustomobject]@{ Ok = $Ok; State = $State; Data = $Data }
            throw 'FixtureDone'
        }
        Mock Invoke-QtProcess { throw 'No native process in done fixture.' }
    }
    It 'includes an actionable warning for blocked policy at DONE' {
        $script:state.firewall_rules = 'blocked_by_policy'
        { Invoke-FixtureDoneStep } | Should -Throw 'FixtureDone'
        $script:Result.Ok | Should -BeTrue
        $script:Result.State | Should -Be 'DONE'
        $script:Result.Data.firewall_message | Should -Match '17610.*IT'
        $script:Summary.notes | Should -Match ([regex]::Escape($script:Result.Data.firewall_message))
    }
    It 'does not invent a warning for a successful firewall result' {
        $script:state.firewall_rules = 'unchanged'
        { Invoke-FixtureDoneStep } | Should -Throw 'FixtureDone'
        $script:Result.Ok | Should -BeTrue
        $script:Result.Data.firewall_message | Should -Be ''
    }
}
