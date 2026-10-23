# Pester 5: run the real collector against tiny fixture payloads only.
BeforeAll {
    $script:Collector = Join-Path (Split-Path -Parent $PSScriptRoot) 'build\collect-payload.ps1'
    function Write-CollectorFixture {
        param([string] $Root, [string] $Relative, [string] $Content)
        $path = Join-Path $Root $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
        [IO.File]::WriteAllText($path, $Content)
    }
}

Describe 'Collector exact per-file manifest' {
    BeforeAll {
        $script:Source = Join-Path $TestDrive 'source'
        $script:Collected = Join-Path $TestDrive 'stage'
        $script:SavedEnv = @{}
        Get-ChildItem Env: | Where-Object Name -like 'QT_SRC_*' | ForEach-Object {
            $script:SavedEnv[$_.Name] = $_.Value
            [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process')
        }
        Write-CollectorFixture $script:Source 'kernel/out/bzImage' '6.6.123.2-microsoft-standard-WSL2-binder'
        Write-CollectorFixture $script:Source 'console/release/win-unpacked/QTrade.exe' 'electron'
        Write-CollectorFixture $script:Source 'console/release/win-unpacked/resources/app.asar' 'renderer'
        Write-CollectorFixture $script:Source 'console/release/win-unpacked/resources/locales/en.pak' 'locale'
        Write-CollectorFixture $script:Source 'third_party/chatlog/wx_key2.dll' 'key-library'
        Write-CollectorFixture $script:Source 'winagent/dist/qtrade-winagent-svc/service.exe' 'service'
        Write-CollectorFixture $script:Source 'winagent/dist/qtrade-winagent-svc/_internal/python312.dll' 'shared'
        Write-CollectorFixture $script:Source 'winagent/dist/qtrade-winagent-user/user.exe' 'user'
        Write-CollectorFixture $script:Source 'winagent/dist/qtrade-winagent-user/_internal/python312.dll' 'shared'
        Write-CollectorFixture $script:Source 'dist/qtrade_agent-1.0.2-py3-none-any.whl' 'wheel'
        Write-CollectorFixture $script:Source 'dist/not-a-wheel.txt' 'excluded'
        Write-CollectorFixture $script:Source 'rootfs/out/rootfs.tar' 'rootfs-fixture'
        Write-CollectorFixture $script:Source 'rootfs/out/contents.json' '{"agent_wheel":{"path":"/opt/qtrade/agent.whl","sha256":"fixture"}}'
        $null = & $script:Collector -SourceRoot $script:Source -Stage $script:Collected -PackageVersion '1.0.2' -AllowMissing
        $script:CollectedManifest = [IO.File]::ReadAllText((Join-Path $script:Collected 'install\manifest.json')) | ConvertFrom-Json
    }

    AfterAll {
        foreach ($name in $script:SavedEnv.Keys) {
            [Environment]::SetEnvironmentVariable($name, $script:SavedEnv[$name], 'Process')
        }
    }

    It 'emits exact relative paths for recursive trees and matching wheel files' {
        $script:CollectedManifest.files.path | Sort-Object | Should -Be (@(
            'kernel/bzImage-6.6'
            'wsl/rootfs.tar'
            'wsl/agent/qtrade_agent-1.0.2-py3-none-any.whl'
            'pkg/chatlog/wx_key2.dll'
            'winagent/app/service.exe'
            'winagent/app/user.exe'
            'winagent/app/_internal/python312.dll'
            'console/QTrade.exe'
            'console/resources/app.asar'
            'console/resources/locales/en.pak'
        ) | Sort-Object)
    }

    It 'hashes every manifest entry against the actual collected file' {
        foreach ($file in $script:CollectedManifest.files) {
            $file.path | Should -Not -Match '[*?]'
            $path = Join-Path $script:Collected ($file.path -replace '/', '\')
            Test-Path -LiteralPath $path -PathType Leaf | Should -BeTrue
            $file.sha256 | Should -Match '^[0-9a-f]{64}$'
            $file.sha256 | Should -Be (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            $file.size | Should -Be ([long](Get-Item -LiteralPath $path).Length)
            $file.dest | Should -Be ('%ProgramData%\QTrade\' + ($file.path -replace '/', '\'))
        }
    }

    It 'retains kernel version and coredump metadata alongside its exact hash' {
        $kernel = @($script:CollectedManifest.files | Where-Object path -eq 'kernel/bzImage-6.6')
        $kernel.Count | Should -Be 1
        $kernel[0].version | Should -Be '6.6.123.2-microsoft-standard-WSL2-binder'
        $kernel[0].coredump_l2 | Should -Be 'D'
        $kernel[0].critical | Should -BeTrue
    }

    It 'does not silently upgrade noncritical files or lose a critical rootfs flag' {
        ($script:CollectedManifest.files | Where-Object path -eq 'wsl/rootfs.tar').critical | Should -BeTrue
        $asar = $script:CollectedManifest.files | Where-Object path -eq 'console/resources/app.asar'
        $asar | Should -Not -BeNullOrEmpty
        [bool]$asar.critical | Should -BeFalse
    }

    It 'preserves the rootfs content inventory and marks incomplete fixtures lightweight' {
        $script:CollectedManifest.rootfs_contents.agent_wheel.path | Should -Be '/opt/qtrade/agent.whl'
        $script:CollectedManifest.lightweight | Should -BeTrue
        $script:CollectedManifest.missing | Should -Contain 'wsl/kcheck-rootfs.tar'
    }

    It 'reports an empty nested console tree as missing rather than a valid payload' {
        $source = Join-Path $TestDrive 'empty-source'
        $stage = Join-Path $TestDrive 'empty-stage'
        New-Item -ItemType Directory -Path (Join-Path $source 'console\release\win-unpacked\resources\empty') -Force | Out-Null
        $null = & $script:Collector -SourceRoot $source -Stage $stage -PackageVersion '1.0.2' -AllowMissing
        $manifest = [IO.File]::ReadAllText((Join-Path $stage 'install\manifest.json')) | ConvertFrom-Json
        $manifest.missing | Should -Contain 'console'
        @($manifest.files | Where-Object path -like 'console*').Count | Should -Be 0
    }
}
