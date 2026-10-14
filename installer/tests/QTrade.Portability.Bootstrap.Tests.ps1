# Target-machine discovery and credential bootstrap with fully fake OS boundaries.
#requires -Version 5.1
BeforeAll {
    $script:Modules = Join-Path $PSScriptRoot '..\engine\modules'
    foreach ($name in @('Native','State','Wsl','Distro','WinAgent')) {
        Import-Module (Join-Path $script:Modules ('QTrade.' + $name + '.psm1')) -DisableNameChecking
    }
}

Describe 'Guest network plan is injected as data before product startup' {
    BeforeEach {
        $script:Events = New-Object Collections.Generic.List[string]
        $script:GuestEnv = ''
        $script:GuestExit = 44
        $script:EnvPath = Join-Path $TestDrive ([guid]::NewGuid().ToString('N') + '.env')
        Mock -ModuleName QTrade.Distro Get-QtOccupiedNetworkPrefixes { [pscustomobject]@{ Ok = $true; Prefixes = @('10.213.0.0/16') } }
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            param($WslArgs, $StandardInput)
            if ($null -ne $StandardInput -and $StandardInput -ne '') {
                $script:Events.Add('stdin-env')
                $StandardInput | Should -Match 'QTRADE_PACKAGE_VERSION=1.0.2'
                $StandardInput | Should -Match 'QTRADE_DOCKER_POOL_BASE='
                ($WslArgs -join ' ') | Should -Not -Match 'ProgramData|/mnt/c'
                return [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = ''; StdErr = '' }
            }
            if (($WslArgs -join ' ') -match 'cat /etc/qtrade/install.env') {
                return [pscustomobject]@{ ExitCode = $script:GuestExit; TimedOut = $false; StdOut = $script:GuestEnv; StdErr = '' }
            }
            $script:Events.Add('os-boot')
            [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = ''; StdErr = '' }
        }
        Mock -ModuleName QTrade.Distro Wait-QtDistroSystemd {
            $script:Events.Add('systemd-ready')
            [pscustomobject]@{ Ok = $true; Status = 'running' }
        }
    }
    It 'selects a nonconflicting target-machine pool and sends canonical LF environment by stdin' {
        $r = Initialize-QtDistroBootstrap -InstallEnvPath $script:EnvPath -PackageVersion '1.0.2'
        $r.Ok | Should -BeTrue -Because $r.Reason
        $r.Cidr | Should -Be '10.231.0.0/16'
        $script:Events.IndexOf('stdin-env') | Should -BeLessThan ($script:Events.IndexOf('systemd-ready'))
        $text = [IO.File]::ReadAllText($script:EnvPath)
        $text | Should -Not -Match "`r"
        $text | Should -Match 'QTRADE_DOCKER_BIP=10.231.0.1/24'
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtWsl -Times 0 -Exactly -ParameterFilter { $WslArgs -contains 'start' }
    }
    It 'fails closed when route discovery is unavailable rather than selecting an assumed pool' {
        Mock -ModuleName QTrade.Distro Get-QtOccupiedNetworkPrefixes { [pscustomobject]@{ Ok = $false; Prefixes = @() } }
        $r = Initialize-QtDistroBootstrap -InstallEnvPath $script:EnvPath -PackageVersion '1.0.2'
        $r.Reason | Should -Be 'NETWORK_DISCOVERY_FAILED'
        $r.Ok | Should -BeFalse
        $script:Events | Should -Not -Contain 'stdin-env'
        Test-Path -LiteralPath $script:EnvPath | Should -BeFalse
    }
    It 'fails when every candidate conflicts without using the old first-candidate fallback' {
        $r = Initialize-QtDistroBootstrap -InstallEnvPath $script:EnvPath -PackageVersion '1.0.2' -Candidates @('10.213.0.0/16')
        $r.Reason | Should -Be 'DOCKER_CIDR_EXHAUSTED'
        $script:Events | Should -Not -Contain 'stdin-env'
    }
    It 'rejects an existing malformed guest environment without overwriting it' {
        $script:GuestExit = 0; $script:GuestEnv = 'docker_cidr=10.213.0.0/16'
        $r = Initialize-QtDistroBootstrap -InstallEnvPath $script:EnvPath -PackageVersion '1.0.2'
        $r.Reason | Should -Be 'INSTALL_ENV_INVALID'
        $script:Events | Should -Not -Contain 'stdin-env'
        Should -Invoke -ModuleName QTrade.Distro Get-QtOccupiedNetworkPrefixes -Times 0 -Exactly
    }
    It 'does not reselect an already matching persisted network plan' {
        Write-QtInstallEnv -Path $script:EnvPath -DockerCidr '10.244.0.0/16' -PackageVersion '1.0.2' | Out-Null
        $script:GuestExit = 0; $script:GuestEnv = [IO.File]::ReadAllText($script:EnvPath)
        $r = Initialize-QtDistroBootstrap -InstallEnvPath $script:EnvPath -PackageVersion '1.0.2'
        $r.Ok | Should -BeTrue -Because $r.Reason
        $r.Cidr | Should -Be '10.244.0.0/16'
        $script:Events | Should -Not -Contain 'stdin-env'
        Should -Invoke -ModuleName QTrade.Distro Get-QtOccupiedNetworkPrefixes -Times 0 -Exactly
    }
}

Describe 'Installer credential and dynamic guest endpoint boundaries' {
    BeforeEach {
        $script:Reply = [pscustomobject]@{ Ok = $true; Reason = ''; Response = [pscustomobject]@{ console_token = ('A' * 64); winagent_token = ('B' * 64) } }
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe { $script:Reply }
        Mock -ModuleName QTrade.WinAgent Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; TimedOut = $false } }
    }
    It 'returns two tokens only after the authenticated reply validates' {
        $r = Get-QtBootstrapTokens
        $r.Ok | Should -BeTrue
        $r.ConsoleToken | Should -Be ('A' * 64)
        $r.WinAgentToken | Should -Be ('B' * 64)
    }
    It 'does not return any token on <Failure>' -ForEach @(
        @{ Failure = 'untrusted pipe' }, @{ Failure = 'forbidden' }, @{ Failure = 'short token' }, @{ Failure = 'non-string token' }
    ) {
        switch ($Failure) {
            'untrusted pipe' { $script:Reply.Ok = $false; $script:Reply.Reason = 'BOOTSTRAP_PIPE_UNTRUSTED' }
            'forbidden' { $script:Reply.Response | Add-Member -NotePropertyName error -NotePropertyValue 'FORBIDDEN' }
            'short token' { $script:Reply.Response.console_token = 'secret-short' }
            'non-string token' { $script:Reply.Response.console_token = 123 }
        }
        $r = Get-QtBootstrapTokens
        $r.Ok | Should -BeFalse
        $r.ConsoleToken | Should -Be ''
        $r.WinAgentToken | Should -Be ''
    }
    It 'bootstraps the Agent token only over stdin, never command-line arguments' {
        Set-QtAgentConsoleToken -Token ('A' * 64) | Should -BeTrue
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtWsl -Times 1 -Exactly -ParameterFilter {
            $StandardInput -eq ('A' * 64) -and $WslArgs -contains '--bootstrap-console-token-stdin' -and
            ($WslArgs -join ' ') -notmatch ('A' * 64)
        }
    }
    It 'rejects credential bootstrap into a foreign distro without a WSL command' {
        Set-QtAgentConsoleToken -Token ('A' * 64) -DistroName 'customer-distro' | Should -BeFalse
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtWsl -Times 0 -Exactly
    }
    It 'uses the route source rather than its Windows gateway as the Agent address' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; TimedOut = $false;
            StdOut = '[{"gateway":"172.23.16.1","prefsrc":"172.23.22.9"}]' } }
        Get-QtAgentBaseUrl | Should -Be 'http://172.23.22.9:17600'
    }
    It 'returns no guessed Agent address for <Payload>' -ForEach @(
        @{ Payload = '[{"gateway":"172.23.16.1"}]' }, @{ Payload = '[{"prefsrc":"8.8.8.8"}]' }, @{ Payload = 'invalid' }
    ) {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = $Payload } }
        Get-QtAgentBaseUrl | Should -Be ''
    }
}

Describe 'Firstboot starts only after configuration and credentials exist' {
    BeforeEach {
        $script:Ready = $true; $script:Started = $false; $script:Complete = $false
        Mock -ModuleName QTrade.Distro Read-QtImportedMarker { $null }
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            param($WslArgs, $StandardInput)
            if ($WslArgs -contains 'show') {
                $out = if ($script:Complete -or $script:Started) { "ActiveState=active`nSubState=exited`nResult=success" } else { "ActiveState=inactive`nSubState=dead`nResult=success" }
                return [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = $out }
            }
            if ($WslArgs -contains 'start') { $script:Started = $true }
            $code = if (($WslArgs -join ' ') -match 'test -s' -and -not $script:Ready) { 1 } else { 0 }
            [pscustomobject]@{ ExitCode = $code; TimedOut = $false; StdOut = '' }
        }
    }
    It 'does not start or reset any unit when bootstrap files are absent' {
        $script:Ready = $false
        $r = Start-QtDistroFirstboot
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'BOOTSTRAP_NOT_READY'
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtWsl -Times 0 -Exactly -ParameterFilter { $WslArgs -contains 'start' -or $WslArgs -contains 'reset-failed' }
    }
    It 'starts explicitly and verifies the unit terminal result' {
        $r = Start-QtDistroFirstboot
        $r.Ok | Should -BeTrue -Because $r.Reason
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtWsl -Times 1 -Exactly -ParameterFilter { $WslArgs -contains 'start' -and $WslArgs -contains 'qtrade-firstboot.service' }
    }
    It 'does not rerun a completed firstboot unit' {
        $script:Complete = $true
        (Start-QtDistroFirstboot).Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtWsl -Times 0 -Exactly -ParameterFilter { $WslArgs -contains 'start' }
    }
}


Describe 'Bootstrap and authenticated health ordering have bounded failure behavior' {
    BeforeEach {
        $script:Now = [DateTime]::Parse('2026-10-13T00:00:00Z').ToUniversalTime()
        $script:Events = New-Object Collections.Generic.List[string]
        $script:PipeCalls = 0
        $script:PipeFailure = ''
        $script:WaBody = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'fixtures\health-winagent-authenticated.json'))
        Mock -ModuleName QTrade.WinAgent Get-QtNow { $script:Now }
        Mock -ModuleName QTrade.WinAgent Start-QtSleep { $script:Now = $script:Now.AddSeconds($Seconds) }
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            $script:Events.Add('trusted-bootstrap')
            $script:PipeCalls++
            [pscustomobject]@{ Ok = $true; Reason = ''; Response = [pscustomobject]@{ console_token = ('A' * 64); winagent_token = ('B' * 64) } }
        }
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            $script:Events.Add('authenticated-health')
            $Headers.Authorization | Should -Be ('Bearer ' + ('A' * 64))
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:WaBody }
        }
        Mock -ModuleName QTrade.Native Invoke-QtProcess { throw 'Real process forbidden.' }
    }
    It 'acquires trusted credentials before the first protected health request' {
        $r = Wait-QtWinAgentHealthy -TimeoutSec 5
        $r.Ok | Should -BeTrue
        $r.UserAgent | Should -BeFalse
        ($script:Events -join ',') | Should -Be 'trusted-bootstrap,authenticated-health'
    }
    It 'uses explicit bootstrap credentials without reacquiring them' {
        (Wait-QtWinAgentHealthy -TimeoutSec 5 -Token ('A' * 64)).Ok | Should -BeTrue
        $script:Events | Should -Not -Contain 'trusted-bootstrap'
    }
    It 'does not send any HTTP request if the pipe peer is untrusted' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_PIPE_UNTRUSTED'; Response = $null }
        }
        (Get-QtWinAgentHealth -TimeoutSec 5).Ok | Should -BeFalse
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe -Times 1 -Exactly
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtHttp -Times 0 -Exactly
    }
    It 'retries only transient bootstrap failure <Failure> until credentials are available' -ForEach @(
        @{ Failure = 'WINAGENT_NOT_READY' }, @{ Failure = 'BOOTSTRAP_PIPE_FAILED' },
        @{ Failure = 'BOOTSTRAP_PIPE_TIMEOUT' }, @{ Failure = 'NOT_READY' }
    ) {
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            $script:PipeCalls++
            if ($script:PipeCalls -eq 1) {
                if ($Failure -eq 'NOT_READY') { return [pscustomobject]@{ Ok = $true; Reason = ''; Response = [pscustomobject]@{ error = 'NOT_READY' } } }
                return [pscustomobject]@{ Ok = $false; Reason = $Failure; Response = $null }
            }
            [pscustomobject]@{ Ok = $true; Reason = ''; Response = [pscustomobject]@{ console_token = ('A' * 64); winagent_token = ('B' * 64) } }
        }
        (Get-QtBootstrapTokens -TimeoutSec 5).Ok | Should -BeTrue
        $script:PipeCalls | Should -Be 2
    }
    It 'does not retry permanent bootstrap refusal <Failure>' -ForEach @(
        @{ Failure = 'FORBIDDEN' }, @{ Failure = 'INVALID_ARGS' }, @{ Failure = 'BOOTSTRAP_PIPE_UNTRUSTED' },
        @{ Failure = 'BOOTSTRAP_PIPE_INVALID' }
    ) {
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            if ($Failure -in @('FORBIDDEN', 'INVALID_ARGS')) {
                return [pscustomobject]@{ Ok = $true; Reason = ''; Response = [pscustomobject]@{ error = $Failure } }
            }
            [pscustomobject]@{ Ok = $false; Reason = $Failure; Response = $null }
        }
        $r = Get-QtBootstrapTokens -TimeoutSec 5
        $r.Ok | Should -BeFalse
        $r.ConsoleToken | Should -Be ''
        $r.WinAgentToken | Should -Be ''
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe -Times 1 -Exactly
        Should -Invoke -ModuleName QTrade.WinAgent Start-QtSleep -Times 0 -Exactly
    }
    It 'does not authenticate after a pipe attempt consumes the entire readiness budget' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            $script:Now = $script:Now.AddSeconds($TimeoutSec)
            [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_PIPE_TIMEOUT'; Response = $null }
        }
        $r = Wait-QtWinAgentHealthy -TimeoutSec 5
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'WINAGENT_NOT_READY'
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtHttp -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe -Times 1 -Exactly -ParameterFilter { $TimeoutSec -eq 5 }
    }
    It 'ends readiness polling at the deadline when authenticated HTTP never succeeds' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            $script:Now = $script:Now.AddSeconds($TimeoutSec)
            [pscustomobject]@{ Ok = $false; StatusCode = 0; Body = '' }
        }
        (Wait-QtWinAgentHealthy -TimeoutSec 7 -Token ('A' * 64)).Ok | Should -BeFalse
        $script:Now | Should -Be ([DateTime]::Parse('2026-10-13T00:00:07Z').ToUniversalTime())
    }
}

Describe 'PowerShell 5 route discovery preserves exactly one private source address' {
    It 'accepts a single-element ip JSON array for <Source>' -ForEach @(
        @{ Source = '10.20.30.40'; Key = 'prefsrc' }, @{ Source = '172.16.2.3'; Key = 'prefsrc' },
        @{ Source = '172.31.2.3'; Key = 'src' }, @{ Source = '192.168.2.3'; Key = 'src' }
    ) {
        $script:RouteBody = '[{"gateway":"172.23.16.1","' + $Key + '":"' + $Source + '"}]'
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = $script:RouteBody } }
        Get-QtAgentBaseUrl | Should -Be ('http://' + $Source + ':17600')
    }
    It 'refuses ambiguous or non-private route <Body>' -ForEach @(
        @{ Body = '[]' }, @{ Body = '[{"prefsrc":"10.1.2.3"},{"prefsrc":"10.1.2.4"}]' },
        @{ Body = '[{"prefsrc":"172.15.1.1"}]' }, @{ Body = '[{"prefsrc":"172.32.1.1"}]' },
        @{ Body = '[{"prefsrc":"127.0.0.1"}]' }, @{ Body = '[{"prefsrc":"::1"}]' },
        @{ Body = '[{"prefsrc":"10.1"}]' }, @{ Body = '[{"prefsrc":null}]' }
    ) {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = $Body } }
        Get-QtAgentBaseUrl | Should -Be ''
    }
    It 'does not query a foreign distro' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { throw 'Foreign distro must not be queried.' }
        Get-QtAgentBaseUrl -DistroName 'customer-distro' | Should -Be ''
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtWsl -Times 0 -Exactly
    }
}
