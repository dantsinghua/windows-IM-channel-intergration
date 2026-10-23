# R6-82 behavioral regressions. All WSL calls are mocked.
#requires -Version 5.1
BeforeAll {
    if (Test-Path -LiteralPath 'C:\ProgramData\QTrade') { throw 'Protected ProgramData root exists.' }
    $script:ModulesDir = $env:QT_KERNEL_TEST_MODULES
    if (-not $script:ModulesDir) { $script:ModulesDir = Join-Path $PSScriptRoot '..\engine\modules' }
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Kernel.psm1') -DisableNameChecking -Force
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Exit.psm1') -DisableNameChecking
    $script:KernelVersion = '6.6.123.2-microsoft-standard-WSL2-binder'
    function script:New-Result([int] $Code = 0, [string] $Out = '', [string] $Err = '', [bool] $Timeout = $false) {
        [pscustomobject]@{ ExitCode = $Code; StdOut = $Out; StdErr = $Err; TimedOut = $Timeout; DurationMs = 1 }
    }
}
AfterAll { (Test-Path -LiteralPath 'C:\ProgramData\QTrade') | Should -BeFalse }

Describe 'Kernel probe result boundaries' {
    BeforeEach {
        Mock -ModuleName QTrade.Kernel Start-QtSleep { }
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { throw 'Unexpected WSL probe in fake-only test.' }
    }
    It 'rejects a nonzero shutdown before grace or later probes' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { New-Result -Code 42 -Err 'shutdown failed' }
        $result = Invoke-QtWslShutdown -Confirmed $true
        $result.Ok | Should -BeFalse
        $result.Reason | Should -Be 'KERNEL_SHUTDOWN_FAILED'
        Should -Invoke -ModuleName QTrade.Kernel Start-QtSleep -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 1 -Exactly
    }
    It 'preserves nonzero shutdown classification through verification and stops' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { New-Result -Code 42 -Err 'shutdown failed' }
        $result = Invoke-QtKernelVerify -ManifestVersion $script:KernelVersion -ShutdownConfirmed $true
        $result.Ok | Should -BeFalse
        $result.Reason | Should -Be 'KERNEL_SHUTDOWN_FAILED'
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 1 -Exactly
    }
    It 'rejects binder success text when its process returned nonzero' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains 'uname') { return (New-Result -Out $script:KernelVersion) }
            return (New-Result -Code 42 -Out "binder hwbinder vndbinder`nBINDERFS_OK")
        }
        $result = Invoke-QtKernelVerify -ManifestVersion $script:KernelVersion -ShutdownConfirmed $true -SkipShutdown
        $result.Ok | Should -BeFalse
        $result.Reason | Should -Be 'KERNEL_NO_BINDER'
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 2 -Exactly
    }
    It 'preserves the quoted diagnostic command <Command>' -ForEach @(
        @{ Command = "dmesg | grep -c 'registering driver hv_sock'" }
        @{ Command = "dmesg | grep -c 'alg: self-tests'" }
        @{ Command = "zcat /proc/config.gz | grep -E 'BINDER|VSOCKETS|CRYPTO_TEST|LOCALVERSION'" }
    ) {
        $script:SeenCommands = [Collections.Generic.List[string]]::new()
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains 'uname') { return (New-Result -Out $script:KernelVersion) }
            $script:SeenCommands.Add([string]$WslArgs[-1])
            return (New-Result -Out "binder hwbinder vndbinder`nBINDERFS_OK")
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:KernelVersion -ShutdownConfirmed $true -SkipShutdown).Ok | Should -BeTrue
        $script:SeenCommands | Should -Contain $Command
    }
    It 'rejects non-version uname output <Text>' -ForEach @(
        @{ Text = "6.6.0`nWSL_ERROR" }
        @{ Text = '6.6.0 WSL_ERROR' }
        @{ Text = '6.6.0: startup failed' }
    ) {
        Test-QtUnameLooksLikeVersion -Text $Text | Should -BeFalse
    }
    It 'accepts one complete version with normal line termination' {
        Test-QtUnameLooksLikeVersion -Text ($script:KernelVersion + "`r`n") | Should -BeTrue
    }
    It 'maps new R6-82 reason <Name> to <Code> and back' -ForEach @(
        @{ Name = 'E_INSTALL_KCHECK_BOOT_FAILED'; Code = 68 }
        @{ Name = 'E_INSTALL_KERNEL_SHUTDOWN_FAILED'; Code = 69 }
    ) {
        Get-QtExitCode -Name $Name | Should -Be $Code
        Get-QtExitName -Code $Code | Should -Be $Name
    }

    It 'accepts a valid current-kernel baseline using exactly one bounded read probe' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { New-Result -Out ($script:KernelVersion + "`r`n") }
        $result = Test-QtKCheckBaseline
        $result.Ok | Should -BeTrue
        $result.Uname | Should -Be $script:KernelVersion
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 1 -Exactly -ParameterFilter {
            ($WslArgs -join '|') -eq '-d|qtrade-kcheck|--exec|uname|-r' -and $TimeoutSec -eq 120
        }
        Should -Invoke -ModuleName QTrade.Kernel Start-QtSleep -Times 0 -Exactly
    }

    It 'rejects an invalid current-kernel baseline <Label> without shutdown' -ForEach @(
        @{ Label = 'nonzero'; Code = 42; Out = '6.6.0'; Timeout = $false }
        @{ Label = 'timeout'; Code = -1; Out = 'partial output'; Timeout = $true }
        @{ Label = 'empty'; Code = 0; Out = ''; Timeout = $false }
        @{ Label = 'numeric error prefix'; Code = 0; Out = '6.6.0 failed'; Timeout = $false }
        @{ Label = 'multiple lines'; Code = 0; Out = "6.6.0`nWSL_ERROR"; Timeout = $false }
    ) {
        $script:BaselineCode = $Code
        $script:BaselineOut = $Out
        $script:BaselineTimeout = $Timeout
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            New-Result -Code $script:BaselineCode -Out $script:BaselineOut -Err 'diagnostic detail' -Timeout $script:BaselineTimeout
        }
        $result = Test-QtKCheckBaseline
        $result.Ok | Should -BeFalse
        $result.Reason | Should -Be 'KCHECK_BOOT_FAILED'
        $result.Records | Should -Not -BeNullOrEmpty
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 1 -Exactly -ParameterFilter {
            ($WslArgs -join '|') -eq '-d|qtrade-kcheck|--exec|uname|-r'
        }
        Should -Invoke -ModuleName QTrade.Kernel Start-QtSleep -Times 0 -Exactly
    }
}
