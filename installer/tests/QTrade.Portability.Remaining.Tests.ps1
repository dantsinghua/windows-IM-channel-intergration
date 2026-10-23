#requires -Version 5.1
# TestDrive and mocked operating-system boundaries only.
BeforeAll {
    $modules = $env:QT_REMAINING_TEST_MODULES
    if (-not $modules) { $modules = Join-Path $PSScriptRoot '..\engine\modules' }
    foreach ($name in @('Native','State','Preflight','Payload','Wsl','Distro','Kernel','Firewall')) {
        Import-Module (Join-Path $modules ('QTrade.' + $name + '.psm1')) -DisableNameChecking
    }
    function New-FixtureProcessResult {
        param([int] $Code = 0, [bool] $TimedOut = $false)
        [pscustomobject]@{ ExitCode = $Code; TimedOut = $TimedOut; StdOut = ''; StdErr = ''; DurationMs = 1 }
    }
}

Describe 'Kcheck cleanup proves current installation ownership on every call' {
    BeforeEach {
        $script:Live = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:KCheck = Join-Path $script:Live 'wsl\kcheck'
        New-Item -ItemType Directory -Path $script:KCheck -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Live 'install') -Force | Out-Null
        $script:Sid = 'S-1-5-21-1111-2222-3333-1001'
        [IO.File]::WriteAllText((Join-Path $script:Live 'install\manifest.json'), '{"package_version":"1.0.2"}')
        $script:Owner = [pscustomobject]@{ state = 'KERNEL_STAGED'; package_version = '1.0.2';
            env = [pscustomobject]@{ install_user_sid = $script:Sid };
            package_manifest_sha256 = (Get-FileHash -LiteralPath (Join-Path $script:Live 'install\manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant() }
        $script:Registered = @([pscustomobject]@{ name = 'qtrade-kcheck'; base_path = $script:KCheck })
        [IO.File]::WriteAllText((Join-Path $script:Live 'install\install_state.json'), ($script:Owner | ConvertTo-Json -Depth 8))
        Mock -ModuleName QTrade.Kernel Get-QtCurrentUserSid { $script:Sid }
        Mock -ModuleName QTrade.Kernel Get-QtRegisteredDistro { return , $script:Registered }
        Mock -ModuleName QTrade.Kernel Unregister-QtDistro { $script:Registered = @(); New-FixtureProcessResult }
        Mock -ModuleName QTrade.Kernel Remove-QtItem { if ($Path -ne $script:KCheck) { throw 'Escaped TestDrive kcheck.' }; Move-Item -LiteralPath $Path -Destination ($Path + '.removed') }
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { New-FixtureProcessResult }
        Mock -ModuleName QTrade.Native Invoke-QtProcess { throw 'Real native process forbidden.' }
    }
    It 'cleans an owned registration and only its exact directory' {
        (Remove-QtKCheck -Directory $script:KCheck).Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Kernel Unregister-QtDistro -Times 1 -Exactly -ParameterFilter { $Name -eq 'qtrade-kcheck' }
        Should -Invoke -ModuleName QTrade.Kernel Remove-QtItem -Times 1 -Exactly -ParameterFilter { $Path -eq $script:KCheck }
    }
    It 'rejects changed <Boundary> before unregister, delete or import' -ForEach @(
        @{ Boundary = 'sid' }, @{ Boundary = 'base-path' }, @{ Boundary = 'missing-state' },
        @{ Boundary = 'manifest' }, @{ Boundary = 'duplicate-registration' }, @{ Boundary = 'wrong-directory' }
    ) {
        switch ($Boundary) {
            'sid' { $script:Sid = 'S-1-5-21-1111-2222-3333-2002' }
            'base-path' { $script:Registered[0].base_path = Join-Path $TestDrive 'foreign' }
            'missing-state' { Move-Item -LiteralPath (Join-Path $script:Live 'install\install_state.json') -Destination (Join-Path $script:Live 'moved-state.fixture') }
            'manifest' { [IO.File]::AppendAllText((Join-Path $script:Live 'install\manifest.json'), ' ') }
            'duplicate-registration' { $script:Registered += $script:Registered[0] }
            'wrong-directory' { $script:KCheck = Join-Path $script:Live 'foreign' }
        }
        $r = Import-QtKCheck -Directory $script:KCheck -TarPath (Join-Path $TestDrive 'fixture.tar')
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'KCHECK_OWNERSHIP_UNKNOWN'
        Should -Invoke -ModuleName QTrade.Kernel Unregister-QtDistro -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Kernel Remove-QtItem -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 0 -Exactly
    }
    It 'does not delete or reimport after unregister failure' {
        Mock -ModuleName QTrade.Kernel Unregister-QtDistro { New-FixtureProcessResult -Code 1 }
        $r = Import-QtKCheck -Directory $script:KCheck -TarPath (Join-Path $TestDrive 'fixture.tar')
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'KCHECK_REMOVE_FAILED'
        Should -Invoke -ModuleName QTrade.Kernel Remove-QtItem -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 0 -Exactly
    }
    It 'imports fresh kcheck without unregistering a nonexistent name' {
        $script:Registered = @()
        $r = Import-QtKCheck -Directory $script:KCheck -TarPath (Join-Path $TestDrive 'fixture.tar')
        $r.Ok | Should -BeTrue -Because $r.Reason
        Should -Invoke -ModuleName QTrade.Kernel Unregister-QtDistro -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 1 -Exactly -ParameterFilter { $WslArgs -contains '--import' }
    }
}

Describe 'Firewall consumes the authenticated production result contract' {
    BeforeEach {
        $script:Body = '{"result":"created","rule_name":"QTrade-WinAgent-17610-from-WSL"}'
        $script:HttpOk = $true
        $script:Status = 200
        Mock -ModuleName QTrade.Firewall Get-QtBootstrapTokens { throw 'Explicit token must be used.' }
        Mock -ModuleName QTrade.Firewall Invoke-QtHttp { [pscustomobject]@{ Ok = $script:HttpOk; StatusCode = $script:Status; Body = $script:Body } }
    }
    It 'parses result <Result> without a legacy boolean or action field' -ForEach @(
        @{ Result = 'created' }, @{ Result = 'updated' }, @{ Result = 'unchanged' }, @{ Result = 'blocked_by_policy' }
    ) {
        $script:Body = @{ result = $Result; rule_name = 'QTrade-WinAgent-17610-from-WSL' } | ConvertTo-Json -Compress
        $r = Invoke-QtFirewallEnsure -Token ('A' * 64)
        $r.Ok | Should -BeTrue
        $r.Action | Should -Be $Result
        $r.Blocked | Should -Be ($Result -eq 'blocked_by_policy')
        if ($r.Blocked) { $r.Message | Should -Not -BeNullOrEmpty }
        Should -Invoke -ModuleName QTrade.Firewall Invoke-QtHttp -Times 1 -Exactly -ParameterFilter {
            $Headers.Authorization -eq ('Bearer ' + ('A' * 64)) -and $Method -eq 'POST'
        }
    }
    It 'rejects invalid successful body <Body>' -ForEach @(
        @{ Body = '<html>bad gateway</html>' }, @{ Body = '{}' }, @{ Body = 'null' },
        @{ Body = '{"action":"created"}' }, @{ Body = '{"created":true}' },
        @{ Body = '{"result":"other"}' }, @{ Body = '{"result":true}' },
        @{ Body = '[{"result":"created"}]' }
    ) {
        $script:Body = $Body
        $r = Invoke-QtFirewallEnsure -Token ('A' * 64)
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'FIREWALL_RESPONSE_INVALID'
        $r.Action | Should -Be ''
        $r.Blocked | Should -BeFalse
    }
    It 'preserves request failure for status <Status>' -ForEach @(@{ Status = 401 }, @{ Status = 0 }, @{ Status = 500 }) {
        $script:Status = $Status; $script:HttpOk = $false; $script:Body = 'fixture request failure'
        $r = Invoke-QtFirewallEnsure -Token ('A' * 64)
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'FIREWALL_REQUEST_FAILED'
        $r.Action | Should -Be ''
        $r.Blocked | Should -BeFalse
    }
}
