# Match the current authenticated API response contract, never historical fake bodies.
#requires -Version 5.1
BeforeAll {
    $modules = Join-Path $PSScriptRoot '..\engine\modules'
    foreach ($name in @('Native','WinAgent','Distro','Selftest')) {
        Import-Module (Join-Path $modules ('QTrade.' + $name + '.psm1')) -DisableNameChecking
    }
    $script:Token = 'A' * 64
    $script:WaBody = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'fixtures\health-winagent-authenticated.json'))
    $script:AgentBody = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'fixtures\health-agent-authenticated.json'))
}

Describe 'Isolated installer health and selftest fixtures' {

BeforeEach {
    Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
        [pscustomobject]@{ Ok = $true; Reason = ''; Response = [pscustomobject]@{ console_token = $script:Token; winagent_token = ('B' * 64) } }
    }
    Mock -ModuleName QTrade.WinAgent Invoke-QtWsl { throw 'Real WSL is forbidden in health contract tests.' }
    Mock -ModuleName QTrade.Distro Invoke-QtWsl { throw 'Real WSL is forbidden in health contract tests.' }
    Mock -ModuleName QTrade.Selftest Invoke-QtWsl { throw 'Real WSL is forbidden in health contract tests.' }
}

Describe 'Installed readiness uses the real service authentication and health shapes' {
    It 'sends the bootstrap bearer credential to the protected WinAgent health endpoint' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            if ($Headers.Authorization -eq ('Bearer ' + $script:Token)) {
                return [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:WaBody }
            }
            [pscustomobject]@{ Ok = $false; StatusCode = 401; Body = '{"ok":false,"code":"UNAUTHORIZED"}' }
        }
        $r = Get-QtWinAgentHealth -Token $script:Token
        $r.Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtHttp -Times 1 -Exactly -ParameterFilter {
            $Headers.Authorization -eq ('Bearer ' + $script:Token)
        }
    }
    It 'reads vault readiness from modules.vault while user_agent false remains supported' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:WaBody } }
        $r = Get-QtWinAgentHealth
        $r.Ok | Should -BeTrue
        $r.Vault | Should -Be 'ok'
        $r.UserAgent | Should -BeFalse
    }
    It 'accepts a healthy authenticated Agent response using dockerd and winagent.online' {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:AgentBody } }
        $r = Test-QtSelftestAgent -Token $script:Token
        $r.Ok | Should -BeTrue -Because ($r | ConvertTo-Json -Depth 5 -Compress)
    }
    It 'can recheck a completed healthy installation with both authenticated service responses' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            $Headers.Authorization | Should -Be ('Bearer ' + $script:Token)
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:WaBody }
        }
        Mock -ModuleName QTrade.Distro Invoke-QtHttp {
            $Headers.Authorization | Should -Be ('Bearer ' + $script:Token)
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:AgentBody }
        }
        Test-QtSelftestComplete -Context ([pscustomobject]@{
            WinAgentBaseUrl = 'http://127.0.0.1:17610'; AgentBaseUrl = 'http://172.23.22.9:17600'
        }) | Should -BeTrue
    }
    It 'does not report a completed installation when trusted credentials cannot be recovered' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_PIPE_UNTRUSTED'; Response = $null }
        }
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { throw 'Untrusted health request forbidden.' }
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { throw 'Untrusted health request forbidden.' }
        Test-QtSelftestComplete -Context ([pscustomobject]@{
            WinAgentBaseUrl = 'http://127.0.0.1:17610'; AgentBaseUrl = 'http://172.23.22.9:17600'
        }) | Should -BeFalse
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtHttp -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtHttp -Times 0 -Exactly
    }
    It 'uses one trusted console token throughout orchestrated selftest with explicit token <Explicit>' -ForEach @(
        @{ Explicit = $true; BootstrapCalls = 0 }, @{ Explicit = $false; BootstrapCalls = 1 }
    ) {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            $Headers.Authorization | Should -Be ('Bearer ' + $script:Token)
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:WaBody }
        }
        Mock -ModuleName QTrade.Distro Invoke-QtHttp {
            $Headers.Authorization | Should -Be ('Bearer ' + $script:Token)
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:AgentBody }
        }
        Mock -ModuleName QTrade.Selftest Invoke-QtSelftestRedroid { [pscustomobject]@{ Ok = $true; Reason = '' } }
        Mock -ModuleName QTrade.Selftest Invoke-QtProbe { [pscustomobject]@{ Via = 'fixture'; Results = @() } }
        $token = if ($Explicit) { $script:Token } else { '' }
        $r = Invoke-QtSelftest -ConsoleToken $token -SkipNapcat
        $r.Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe -Times $BootstrapCalls -Exactly
        Should -Invoke -ModuleName QTrade.Selftest Invoke-QtProbe -Times 1 -Exactly -ParameterFilter {
            $ConsoleToken -eq $script:Token
        }
    }
    It 'does not start selftest HTTP or a container after bootstrap trust fails' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtBootstrapPipe {
            [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_PIPE_UNTRUSTED'; Response = $null }
        }
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { throw 'Untrusted HTTP forbidden.' }
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { throw 'Untrusted HTTP forbidden.' }
        Mock -ModuleName QTrade.Selftest Invoke-QtSelftestRedroid { throw 'Container before authentication forbidden.' }
        $r = Invoke-QtSelftest -SkipNapcat
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'SELFTEST_WINAGENT'
        Should -Invoke -ModuleName QTrade.Selftest Invoke-QtSelftestRedroid -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtHttp -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtHttp -Times 0 -Exactly
    }
    It 'does not treat an unknown or offline backend as healthy' -ForEach @(
        @{ Body = '{"ok":true,"agent":{"db_mb":1},"dockerd":null,"winagent":{"online":true}}' }
        @{ Body = '{"ok":true,"agent":{"db_mb":1},"dockerd":true,"winagent":{"online":false}}' }
    ) {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $Body } }
        (Test-QtSelftestAgent -Token $script:Token).Ok | Should -BeFalse
    }
}


Describe 'Health failures and incomplete bodies never satisfy installation readiness' {
    It 'rejects WinAgent response with <Case>' -ForEach @(
        @{ Case = 'ok missing' }, @{ Case = 'ok false' }, @{ Case = 'ok string' },
        @{ Case = 'modules missing' }, @{ Case = 'vault missing' }, @{ Case = 'vault failed' },
        @{ Case = 'user_agent missing' }, @{ Case = 'user_agent null' }, @{ Case = 'user_agent string' }
    ) {
        $body = $script:WaBody | ConvertFrom-Json
        switch ($Case) {
            'ok missing' { $body.PSObject.Properties.Remove('ok') }
            'ok false' { $body.ok = $false }
            'ok string' { $body.ok = 'true' }
            'modules missing' { $body.PSObject.Properties.Remove('modules') }
            'vault missing' { $body.modules.PSObject.Properties.Remove('vault') }
            'vault failed' { $body.modules.vault = 'failed' }
            'user_agent missing' { $body.PSObject.Properties.Remove('user_agent') }
            'user_agent null' { $body.user_agent = $null }
            'user_agent string' { $body.user_agent = 'false' }
        }
        $script:ResponseBody = $body | ConvertTo-Json -Depth 20 -Compress
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:ResponseBody } }
        (Get-QtWinAgentHealth -Token $script:Token).Ok | Should -BeFalse
    }
    It 'rejects the authenticated Agent readiness gate with <Case>' -ForEach @(
        @{ Case = 'ok missing' }, @{ Case = 'ok false' }, @{ Case = 'ok string' },
        @{ Case = 'docker missing' }, @{ Case = 'docker false' }, @{ Case = 'docker string' },
        @{ Case = 'winagent missing' }, @{ Case = 'online missing' }, @{ Case = 'online string' },
        @{ Case = 'agent missing' }, @{ Case = 'database size missing' }, @{ Case = 'database size negative' },
        @{ Case = 'database size bool' }, @{ Case = 'database size string' }, @{ Case = 'database size null' },
        @{ Case = 'wal missing' }, @{ Case = 'wal negative' }, @{ Case = 'accounts missing' },
        @{ Case = 'running missing' }, @{ Case = 'count missing' }, @{ Case = 'count negative' },
        @{ Case = 'count string' }, @{ Case = 'count fractional' }, @{ Case = 'running exceeds total' }
    ) {
        $body = $script:AgentBody | ConvertFrom-Json
        switch ($Case) {
            'ok missing' { $body.PSObject.Properties.Remove('ok') }
            'ok false' { $body.ok = $false }
            'ok string' { $body.ok = 'true' }
            'docker missing' { $body.PSObject.Properties.Remove('dockerd') }
            'docker false' { $body.dockerd = $false }
            'docker string' { $body.dockerd = 'true' }
            'winagent missing' { $body.PSObject.Properties.Remove('winagent') }
            'online missing' { $body.winagent.PSObject.Properties.Remove('online') }
            'online string' { $body.winagent.online = 'true' }
            'agent missing' { $body.PSObject.Properties.Remove('agent') }
            'database size missing' { $body.agent.PSObject.Properties.Remove('db_mb') }
            'database size negative' { $body.agent.db_mb = -1 }
            'database size bool' { $body.agent.db_mb = $true }
            'database size string' { $body.agent.db_mb = '1' }
            'database size null' { $body.agent.db_mb = $null }
            'wal missing' { $body.agent.PSObject.Properties.Remove('wal_mb') }
            'wal negative' { $body.agent.wal_mb = -1 }
            'accounts missing' { $body.PSObject.Properties.Remove('accounts') }
            'running missing' { $body.accounts.PSObject.Properties.Remove('running') }
            'count missing' { $body.accounts.PSObject.Properties.Remove('n') }
            'count negative' { $body.accounts.n = -1 }
            'count string' { $body.accounts.n = '1' }
            'count fractional' { $body.accounts.n = 1.5 }
            'running exceeds total' { $body.accounts.running = 1; $body.accounts.n = 0 }
        }
        $script:ResponseBody = $body | ConvertTo-Json -Depth 20 -Compress
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:ResponseBody } }
        (Test-QtSelftestAgent -Token $script:Token).Ok | Should -BeFalse
    }
    It 'does not mistake the real anonymous Agent summary for a database-ready full response' {
        $script:ResponseBody = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'fixtures\health-agent-anonymous.json'))
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:ResponseBody } }
        (Test-QtSelftestAgent -Token $script:Token).Ok | Should -BeFalse
    }
    It 'fails without requesting an anonymous Agent summary when its token is absent' {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { throw 'Anonymous health must not be requested.' }
        (Get-QtAgentHealth).Ok | Should -BeFalse
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtHttp -Times 0 -Exactly
    }
    It 'rejects a transport failure for <Service> with <Case>' -ForEach @(
        @{ Service = 'WinAgent'; Case = '401'; Code = 401; TransportOk = $false },
        @{ Service = 'Agent'; Case = '401'; Code = 401; TransportOk = $false },
        @{ Service = 'WinAgent'; Case = 'timeout'; Code = 0; TransportOk = $false },
        @{ Service = 'Agent'; Case = 'timeout'; Code = 0; TransportOk = $false },
        @{ Service = 'WinAgent'; Case = 'server error'; Code = 500; TransportOk = $false },
        @{ Service = 'Agent'; Case = 'server error'; Code = 500; TransportOk = $false },
        @{ Service = 'WinAgent'; Case = 'invalid JSON'; Code = 200; TransportOk = $true },
        @{ Service = 'Agent'; Case = 'invalid JSON'; Code = 200; TransportOk = $true }
    ) {
        $script:TransportReply = [pscustomobject]@{ Ok = $TransportOk; StatusCode = $Code; Body = 'invalid' }
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { $script:TransportReply }
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { $script:TransportReply }
        if ($Service -eq 'WinAgent') { (Get-QtWinAgentHealth -Token $script:Token).Ok | Should -BeFalse }
        else { (Test-QtSelftestAgent -Token $script:Token).Ok | Should -BeFalse }
    }
    It 'derives Agent version and database readiness from the actual full response' {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = $script:AgentBody } }
        $r = Get-QtAgentHealth -Token $script:Token
        $r.Version | Should -Be (($script:AgentBody | ConvertFrom-Json).agent.version)
        $r.Db | Should -Be 'ok'
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtHttp -Times 1 -Exactly -ParameterFilter {
            $Headers.Authorization -eq ('Bearer ' + $script:Token)
        }
    }
}

}
