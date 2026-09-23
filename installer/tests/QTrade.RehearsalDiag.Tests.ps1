# Pester 5 —— 预演:kcheck 已注销时的诊断包,以及 Hyper-V 事件日志
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl',
            'QTrade.Kernel', 'QTrade.Diag')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe '预演诊断包' {

    It '清单含 Hyper-V-Compute 与 Hyper-V-Worker' {
        $names = @((Get-QtDiagItemList) | ForEach-Object { $_.name }) -join ','
        $names | Should -Match 'Hyper-V-Compute'
        $names | Should -Match 'Hyper-V-Worker'
    }

    It 'kcheck 不在时 dmesg 文本说明发行版已注销,且不是空串' {
        $script:Root = Join-Path ([IO.Path]::GetTempPath()) ('qt-rehearsal-diag-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'install') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'logs') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'kernel') -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $script:Root 'install\install_state.json'), '{"state":"FAILED"}')
        [IO.File]::WriteAllText((Join-Path $script:Root 'kernel\current.json'), '{"line":"6.6"}')
        Mock -ModuleName QTrade.Diag Invoke-QtWsl {
            [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        Mock -ModuleName QTrade.Diag Get-WinEvent { throw 'log missing' }
        Mock -ModuleName QTrade.Diag Get-QtOptionalFeatureState { 'Enabled' }
        Mock -ModuleName QTrade.Diag Get-QtRegistryValue { $null }
        Mock -ModuleName QTrade.Diag Get-QtEnvironmentPath { $script:Root }
        try {
            $r = New-QtDiagBundle -Root $script:Root -Stamp 'rehearsal' -IncludeDmesg $true
            $r.Included | Should -Contain 'kcheck-dmesg'
            $r.Included -join ',' | Should -Match 'Hyper-V-Compute'
            $r.Included -join ',' | Should -Match 'Hyper-V-Worker'
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($r.ZipPath)
            try {
                $entry = $zip.Entries | Where-Object { $_.Name -eq 'kcheck-dmesg.txt' } | Select-Object -First 1
                $entry | Should -Not -BeNullOrEmpty
                $reader = New-Object IO.StreamReader($entry.Open())
                try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
                $text | Should -Match '采集时发行版已注销'
                $text.Trim().Length | Should -BeGreaterThan 0
            } finally { $zip.Dispose() }
        } finally {
            Remove-Item -LiteralPath $script:Root -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
