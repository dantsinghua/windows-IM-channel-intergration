# Native process behavior regressions. No WSL, service, or installer execution.
#requires -Version 5.1
BeforeAll {
    $script:ProtectedRoot = 'C:\ProgramData\QTrade'
    if (Test-Path -LiteralPath $script:ProtectedRoot) {
        throw 'Protected ProgramData root already exists; stop and investigate.'
    }
    $script:NativeModule = $env:QT_NATIVE_TEST_MODULE
    if (-not $script:NativeModule) {
        $script:NativeModule = Join-Path $PSScriptRoot '..\engine\modules\QTrade.Native.psm1'
    }
    Import-Module $script:NativeModule -Force
    $script:PowerShellExe = Join-Path $PSHOME 'powershell.exe'
    $script:EchoScript = Join-Path $TestDrive 'echo-args.ps1'
    [IO.File]::WriteAllText($script:EchoScript, '[Console]::Out.Write((ConvertTo-Json -InputObject @($args) -Compress))', [Text.Encoding]::ASCII)

    function Invoke-ArgumentEcho {
        param([AllowEmptyCollection()][string[]] $Values)
        $r = Invoke-QtProcess -FilePath $script:PowerShellExe -ArgumentList (@('-NoProfile', '-NonInteractive', '-File', $script:EchoScript) + $Values) -TimeoutSec 20
        $r.TimedOut | Should -BeFalse
        $r.ExitCode | Should -Be 0
        return $r.StdOut
    }
}

AfterAll {
    (Test-Path -LiteralPath $script:ProtectedRoot) | Should -BeFalse
}

Describe 'Invoke-QtProcess real PS5.1 child contract' {
    It 'returns a real zero exit code' {
        $r = Invoke-QtProcess -FilePath $env:ComSpec -ArgumentList @('/d', '/c', 'exit 0') -TimeoutSec 10
        $r.TimedOut | Should -BeFalse
        $r.ExitCode | Should -Be 0
    }

    It 'preserves a real nonzero exit code' {
        $r = Invoke-QtProcess -FilePath $env:ComSpec -ArgumentList @('/d', '/c', 'exit 42') -TimeoutSec 10
        $r.TimedOut | Should -BeFalse
        $r.ExitCode | Should -Be 42
    }

    It 'preserves an argument containing spaces' {
        Invoke-ArgumentEcho -Values @('first value', 'last') | Should -Be '["first value","last"]'
    }

    It 'preserves an empty argument between values' {
        Invoke-ArgumentEcho -Values @('first', '', 'last') | Should -Be '["first","","last"]'
    }

    It 'preserves a double quote inside an argument' {
        Invoke-ArgumentEcho -Values @('a"b', 'last') | Should -Be '["a\"b","last"]'
    }

    It 'preserves a trailing backslash in a spaced argument' {
        Invoke-ArgumentEcho -Values @('C:\path with space\', 'last') | Should -Be '["C:\\path with space\\","last"]'
    }

    It 'preserves an entire sh-c script as one argument without executing it' {
        $scriptText = 'printf "%s\n" "$HOME"; exit 17'
        $expected = ConvertTo-Json -InputObject @('-c', $scriptText) -Compress
        Invoke-ArgumentEcho -Values @('-c', $scriptText) | Should -Be $expected
    }

    It 'retains stdout and stderr already produced before timeout and reaps that child' {
        $pidFile = Join-Path $TestDrive 'timeout-child.pid'
        $escapedPidFile = $pidFile.Replace("'", "''")
        $code = "[IO.File]::WriteAllText('$escapedPidFile', [string]`$PID); [Console]::Out.WriteLine('before-timeout-out'); [Console]::Out.Flush(); [Console]::Error.WriteLine('before-timeout-err'); [Console]::Error.Flush(); Start-Sleep -Seconds 8"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
        $r = Invoke-QtProcess -FilePath $script:PowerShellExe -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -TimeoutSec 2
        $r.TimedOut | Should -BeTrue
        $r.ExitCode | Should -Be -1
        $r.StdOut | Should -Match 'before-timeout-out'
        $r.StdErr | Should -Match 'before-timeout-err'
        $childId = [int]([IO.File]::ReadAllText($pidFile))
        @(Get-Process -Id $childId -ErrorAction SilentlyContinue).Count | Should -Be 0
    }

    It 'forwards stdin as exact UTF8 bytes without a BOM or added newline' {
        $code = '$buffer = New-Object IO.MemoryStream; [Console]::OpenStandardInput().CopyTo($buffer); [Console]::Out.Write([Convert]::ToBase64String($buffer.ToArray()))'
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
        $inputText = "token value`nquoted`"value" + [char]0x4E2D
        $expected = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($inputText))
        $r = Invoke-QtProcess -FilePath $script:PowerShellExe -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -StandardInput $inputText -TimeoutSec 10
        $r.TimedOut | Should -BeFalse
        $r.ExitCode | Should -Be 0
        $r.StdOut | Should -Be $expected
    }

    It 'closes an explicitly empty stdin so the child can finish' {
        $code = '[Console]::Out.Write([Console]::In.ReadToEnd().Length)'
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
        $r = Invoke-QtProcess -FilePath $script:PowerShellExe -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -StandardInput '' -TimeoutSec 10
        $r.TimedOut | Should -BeFalse
        $r.ExitCode | Should -Be 0
        $r.StdOut | Should -Be '0'
    }
}

Describe 'Invoke-QtWsl forwarding without real WSL' {
    It 'sets UTF8 on the child environment and preserves the caller while forwarding stdin' {
        $previous = $env:WSL_UTF8
        try {
            $env:WSL_UTF8 = 'parent-sentinel'
            Mock -ModuleName QTrade.Native Invoke-QtProcess {
                $env:WSL_UTF8 | Should -Be 'parent-sentinel'
                New-QtProcessResult -StdOut 'fake output'
            }
            $r = Invoke-QtWsl -WslArgs @('-d', 'fake-distro', '--exec', 'cat') -StandardInput 'fake token'
            $r.StdOut | Should -Be 'fake output'
            $env:WSL_UTF8 | Should -Be 'parent-sentinel'
            Should -Invoke -ModuleName QTrade.Native Invoke-QtProcess -Times 1 -Exactly -ParameterFilter {
                $FilePath -eq 'wsl.exe' -and $StandardInput -eq 'fake token' -and $Environment.WSL_UTF8 -eq '1'
            }
        } finally {
            $env:WSL_UTF8 = $previous
        }
    }
}
