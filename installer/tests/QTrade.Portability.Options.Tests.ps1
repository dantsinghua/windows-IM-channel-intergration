# Isolated options boundary tests. Never dispatch a real installation step.
#requires -Version 5.1
BeforeAll {
    $script:Engine = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\run-step.ps1'
    $script:PowerShellExe = Join-Path $PSHOME 'powershell.exe'
    function Invoke-OptionsProbe {
        param([string] $Payload, [switch] $Missing, [switch] $Directory)
        $root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $options = Join-Path $TestDrive (([guid]::NewGuid().ToString('N')) + '.json')
        if ($Directory) { New-Item -ItemType Directory -Path $options | Out-Null }
        elseif (-not $Missing) { [IO.File]::WriteAllText($options, $Payload, (New-Object Text.UTF8Encoding($false))) }
        $output = @(& $script:PowerShellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $script:Engine -Step '__options_probe__' -Root $root -OptionsPath $options 2>&1)
        $code = $LASTEXITCODE
        $text = ($output | ForEach-Object { [string]$_ }) -join "`n"
        $line = @($text -split "`r?`n" | Where-Object { $_ -match '^\{' }) | Select-Object -Last 1
        [pscustomobject]@{ Exit = $code; Text = $text; Result = ($line | ConvertFrom-Json); Root = $root }
    }
}

Describe 'Options fail closed before installation state is created' {
    It 'rejects <Name> rather than accepting defaults' -ForEach @(
        @{ Name = 'malformed JSON'; Payload = '{"secret":"DO_NOT_PRINT_SENTINEL",' }
        @{ Name = 'raw Windows path'; Payload = '{"source_exe":"D:\New Folder\setup.exe"}' }
        @{ Name = 'null'; Payload = 'null' }
        @{ Name = 'array'; Payload = '[]' }
        @{ Name = 'scalar string'; Payload = '"literal"' }
        @{ Name = 'boolean'; Payload = 'true' }
    ) {
        $r = Invoke-OptionsProbe -Payload $Payload
        $r.Exit | Should -Be 200
        $r.Result.ok | Should -BeFalse
        $r.Result.reason | Should -Be 'OPTIONS_INVALID'
        Test-Path -LiteralPath (Join-Path $r.Root 'install\install_state.json') | Should -BeFalse
        $r.Text | Should -Not -Match 'DO_NOT_PRINT_SENTINEL'
    }

    It 'rejects a supplied missing options file' {
        $r = Invoke-OptionsProbe -Missing
        $r.Result.reason | Should -Be 'OPTIONS_INVALID'
        $r.Exit | Should -Be 200
        Test-Path -LiteralPath (Join-Path $r.Root 'install\install_state.json') | Should -BeFalse
    }

    It 'rejects an unreadable options path with the same structured error' {
        $r = Invoke-OptionsProbe -Directory
        $r.Result.reason | Should -Be 'OPTIONS_INVALID'
        $r.Exit | Should -Be 200
        Test-Path -LiteralPath (Join-Path $r.Root 'install\install_state.json') | Should -BeFalse
    }

    It 'accepts escaped Windows paths and preserves a caller package version' {
        $payload = @{ source_exe = 'D:\New Folder\setup.exe'; package_version = '1.2.3' } | ConvertTo-Json -Compress
        $r = Invoke-OptionsProbe -Payload $payload
        $r.Result.reason | Should -Not -Be 'OPTIONS_INVALID'
        $state = Get-Content -LiteralPath (Join-Path $r.Root 'install\install_state.json') -Raw | ConvertFrom-Json
        $state.package_version | Should -Be '1.2.3'
    }
}
