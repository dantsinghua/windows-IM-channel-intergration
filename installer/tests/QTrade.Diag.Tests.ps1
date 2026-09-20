# Pester 5 —— 诊断包(docs/03 §5.3;排除清单是红线 2 的执行点)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl',
            'QTrade.Kernel', 'QTrade.Diag')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe '§5.3 收集清单' {

    It '文档点名的东西都在清单里' {
        $names = @((Get-QtDiagItemList) | ForEach-Object { $_.name })
        foreach ($n in @('install_state.json', 'engine-logs', 'wslconfig-current', 'wslconfig-backups',
                'wsl-version', 'wsl-status', 'wsl-list', 'optional-features', 'policy-keys',
                'kernel-current.json', 'kcheck-dmesg', 'docker-logs', 'wsl-eventlog')) {
            $names | Should -Contain $n
        }
    }
}

Describe '🔴 §5.3「不含」+ §6 + 红线 2:排除清单' {

    It 'vault blob 与熵文件被挡' {
        $r = Test-QtDiagExclusions -Paths @(
            'C:\ProgramData\QTrade\winagent\vault\blobs\abc.bin'
            'C:\ProgramData\QTrade\winagent\vault\entropy.bin'
        )
        $r.Ok | Should -BeFalse
        $r.Blocked.Count | Should -Be 2
    }

    It '微信数据被挡' {
        (Test-QtDiagExclusions -Paths @('D:\Tencent\Saved Files\xwechat_files\msg.db')).Ok | Should -BeFalse
        (Test-QtDiagExclusions -Paths @('C:\Users\a\AppData\Roaming\Tencent\xwechat\log\x.log')).Ok | Should -BeFalse
    }

    It 'Agent → WinAgent 令牌被挡' {
        (Test-QtDiagExclusions -Paths @('C:\ProgramData\QTrade\wsl\winagent.token')).Ok | Should -BeFalse
    }

    It '微信备份目录被挡' {
        (Test-QtDiagExclusions -Paths @('D:\QTrade-WeChat-Backup\20260920-1900\x.dat')).Ok | Should -BeFalse
    }

    It '该收的不被误挡' {
        $r = Test-QtDiagExclusions -Paths @(
            'C:\ProgramData\QTrade\install\install_state.json'
            'C:\ProgramData\QTrade\logs\install-20260920-190000.log'
            'C:\ProgramData\QTrade\wsl\.wslconfig.bak-20260920-190000'
            'C:\ProgramData\QTrade\kernel\current.json'
        )
        $r.Ok | Should -BeTrue
    }

    It '空集合 → Ok' {
        (Test-QtDiagExclusions -Paths @()).Ok | Should -BeTrue
    }
}

Describe 'Copy-QtDiagFile —— 命中排除清单就**拒绝拷贝**' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-diag-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp 'src\winagent\vault') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp 'stage') -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $script:Tmp 'src\ok.json'), '{}')
        [IO.File]::WriteAllText((Join-Path $script:Tmp 'src\winagent\vault\secret.bin'), 'S3CR3T')
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '正常文件被拷进去' {
        $d = Copy-QtDiagFile -StagingDir (Join-Path $script:Tmp 'stage') -Source (Join-Path $script:Tmp 'src\ok.json')
        $d | Should -Not -Be ''
        (Test-Path -LiteralPath $d) | Should -BeTrue
    }

    It '🔴 vault 目录下的文件被拒(回空串,让调用方记账,而不是静默跳过)' {
        $d = Copy-QtDiagFile -StagingDir (Join-Path $script:Tmp 'stage') -Source (Join-Path $script:Tmp 'src\winagent\vault\secret.bin')
        $d | Should -Be ''
        (Test-Path -LiteralPath (Join-Path $script:Tmp 'stage\secret.bin')) | Should -BeFalse
    }

    It '源不存在 → 回空串,不抛' {
        Copy-QtDiagFile -StagingDir (Join-Path $script:Tmp 'stage') -Source (Join-Path $script:Tmp 'src\nope') | Should -Be ''
    }
}

Describe '🔴 Add-QtDiagText 先抹令牌再落盘(§3.1 / §6:日志不含密码、令牌)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-dg-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '令牌被抹成 ***' {
        $p = Add-QtDiagText -StagingDir $script:Tmp -Name 'x' -Text 'token=abcdef123456 其余正常'
        $text = [IO.File]::ReadAllText($p)
        $text | Should -Not -Match 'abcdef123456'
        $text | Should -Match '\*\*\*'
        $text | Should -Match '其余正常'
    }

    It '空文本也能落盘(不抛)' {
        (Test-Path -LiteralPath (Add-QtDiagText -StagingDir $script:Tmp -Name 'empty' -Text '')) | Should -BeTrue
    }
}

Describe '诊断包端到端(不碰 WSL:-SkipWslCommands)' {

    BeforeEach {
        $script:Root = Join-Path ([IO.Path]::GetTempPath()) ('qt-diagroot-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'install') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'logs') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'kernel') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'wsl') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Root 'winagent\vault') -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $script:Root 'install\install_state.json'), '{"state":"DONE"}')
        [IO.File]::WriteAllText((Join-Path $script:Root 'kernel\current.json'), '{"line":"6.6"}')
        [IO.File]::WriteAllText((Join-Path $script:Root 'logs\install-20260920-190000.log'), 'hello token=secret123')
        [IO.File]::WriteAllText((Join-Path $script:Root 'wsl\.wslconfig.bak-20260920-190000'), "[wsl2]`r`n")
        [IO.File]::WriteAllText((Join-Path $script:Root 'winagent\vault\entropy.bin'), 'S3CR3T')
    }
    AfterEach { Remove-Item -LiteralPath $script:Root -Recurse -Force -ErrorAction SilentlyContinue }

    It '出 zip,收进了该收的,且 zip 里**没有** vault' {
        Mock -ModuleName QTrade.Diag Get-QtOptionalFeatureState { 'Enabled' }
        Mock -ModuleName QTrade.Diag Get-QtRegistryValue { $null }
        Mock -ModuleName QTrade.Diag Get-QtEnvironmentPath { $script:Root }
        $r = New-QtDiagBundle -Root $script:Root -Stamp '20260920-191500' -SkipWslCommands
        $r.Ok | Should -BeTrue
        (Test-Path -LiteralPath $r.ZipPath) | Should -BeTrue
        $r.Included | Should -Contain 'install_state.json'
        $r.Included | Should -Contain 'kernel-current.json'
        $r.Included | Should -Contain 'optional-features'
        $r.Included | Should -Contain 'policy-keys'
        $r.Included | Should -Contain 'wsl-eventlog'

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($r.ZipPath)
        try {
            $names = @($zip.Entries | ForEach-Object { $_.FullName })
            ($names -join ';') | Should -Not -Match 'entropy\.bin'
            ($names -join ';') | Should -Not -Match 'vault'
            ($names -join ';') | Should -Match 'install_state\.json'
        } finally { $zip.Dispose() }
    }

    It '策略键取不到值时写「<未设置>」而不是崩' {
        Mock -ModuleName QTrade.Diag Get-QtOptionalFeatureState { 'Enabled' }
        Mock -ModuleName QTrade.Diag Get-QtRegistryValue { $null }
        Mock -ModuleName QTrade.Diag Get-QtEnvironmentPath { $script:Root }
        (New-QtDiagBundle -Root $script:Root -Stamp '20260920-191600' -SkipWslCommands).Ok | Should -BeTrue
    }
}
