# Pester 5 —— `.wslconfig` 合并规则 R1~R7 / wsl_state / kernel_state / docker 选段
# 规格:docs/03 §2.4.4、§2.4.5、§2.6.2、§2.6.7(W2/W3/W4)、docs/04 §2.7.1(受管键表 10 键)、§2.7.4(候选表)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Exit.psm1') -DisableNameChecking
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Native.psm1') -DisableNameChecking
    Import-Module (Join-Path $script:ModulesDir 'QTrade.State.psm1') -DisableNameChecking
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Wsl.psm1') -DisableNameChecking

    $script:KernelPath = 'C:\ProgramData\QTrade\kernel\bzImage-6.6'
    $script:KernelLine = 'kernel=C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6'
}

Describe '.wslconfig 受管键表 10 键(docs/04 §2.7.1;裁决② 以逐字列出的键为准)' {

    It '🔴 归属 03 的键**恰好 10 个**,与 docs/04 §2.7.1 逐字列出的一致(裁决②)' {
        (Get-QtWslManagedKeys).Count | Should -Be 10
        $names = @((Get-QtWslManagedKeys) | ForEach-Object { $_.Name })
        $names -join ',' | Should -Be 'kernel,memory,processors,swap,localhostForwarding,guiApplications,maxCrashDumpCount,crashDumpFolder,autoMemoryReclaim,sparseVhd'
    }

    It '「不写、不动」集合' {
        (Get-QtWslUntouchedKeys) -join ',' | Should -Be 'networkingMode,dnsTunneling,autoProxy,firewall,vmIdleTimeout'
    }

    It 'processors 的规则是 NoWrite(04:「不写」=全部逻辑核)' {
        $keys = Get-QtWslManagedKeys
        (@($keys | Where-Object { $_.Name -eq 'processors' })[0]).Rule | Should -Be 'NoWrite'
    }
}

Describe 'memory 分档表(docs/04 §2.7.1,C-43 唯一出处)' {

    It '按物理内存查表' -ForEach @(
        @{ MB = 8192; Want = '5GB' }
        @{ MB = 12288; Want = '8GB' }
        @{ MB = 16384; Want = '11GB' }
        @{ MB = 24576; Want = '16GB' }
        @{ MB = 32768; Want = '24GB' }
    ) {
        Get-QtMemoryByPhysical -PhysicalMB $MB | Should -Be $Want
    }

    It '16G 档微信模块开 → 10GB(P-21:Windows 侧要给微信池留 1.5G)' {
        Get-QtMemoryByPhysical -PhysicalMB 16384 -WeChatOn | Should -Be '10GB'
    }

    It '> 32G 取物理的 75%' {
        Get-QtMemoryByPhysical -PhysicalMB 65536 | Should -Be '48GB'
    }

    It '内存串解析' -ForEach @(
        @{ V = '11GB'; MB = 11264 }
        @{ V = '6144MB'; MB = 6144 }
        @{ V = '8192'; MB = 8192 }
    ) { ConvertFrom-QtMemorySize -Value $V | Should -Be $MB }

    It '解析不了回 $null(交调用方当「无法比较」处理,不瞎改用户值)' {
        ConvertFrom-QtMemorySize -Value 'auto' | Should -BeNullOrEmpty
    }
}

Describe '.wslconfig 解析:保留原文行、不重排、不去注释(§2.4.5)' {

    It '解析出段与键' {
        $t = "[wsl2]`r`n# 我的注释`r`nmemory=6GB`r`nkernel=D:\\k\\bzImage`r`n`r`n[experimental]`r`nsparseVhd=true"
        $p = ConvertFrom-QtWslConfig -Text $t
        (Get-QtWslConfigValue -Parsed $p -Section 'wsl2' -Key 'memory') | Should -Be '6GB'
        (Get-QtWslConfigValue -Parsed $p -Section 'experimental' -Key 'sparseVhd') | Should -Be 'true'
        (Get-QtWslConfigValue -Parsed $p -Section 'wsl2' -Key 'swap') | Should -BeNullOrEmpty
    }

    It 'kernel= 值 `\\` 还原成真实路径(W2)' {
        ConvertFrom-QtKernelLineValue -Value 'C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6' |
            Should -Be 'C:\ProgramData\QTrade\kernel\bzImage-6.6'
    }

    It '🔴 W2:写出的 kernel 行是双反斜杠、不加引号、不用正斜杠' {
        $line = Format-QtKernelLine -KernelPath $script:KernelPath
        $line | Should -Be $script:KernelLine
        $line | Should -Not -Match '"'
        $line | Should -Not -Match '/'
    }
}

Describe '合并规则 R3/R4 —— 已有值不动,无值才写(§2.6.2)' {

    It 'R4:空文件 → 按 04 表默认值写入,记 keys_added' {
        $r = Merge-QtWslConfig -Text '' -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $r.KeysAdded | Should -Contain 'kernel'
        $r.KeysAdded | Should -Contain 'memory'
        $r.KeysAdded | Should -Contain 'swap'
        $r.KeysAdded | Should -Contain 'localhostForwarding'
        $r.KeysAdded | Should -Contain 'guiApplications'
        $r.KeysAdded | Should -Contain 'autoMemoryReclaim'
        $r.KeysAdded | Should -Contain 'sparseVhd'
        $r.Text | Should -Match '(?m)^swap=2GB\r?$'
        $r.Text | Should -Match '(?m)^localhostForwarding=true\r?$'
        $r.Text | Should -Match '(?m)^guiApplications=false\r?$'
        $r.Text | Should -Match '(?m)^autoMemoryReclaim=gradual\r?$'
        $r.Text | Should -Match '(?m)^sparseVhd=true\r?$'
    }

    It '🔴 R4:processors 一律不写(04 定「不写」)' {
        $r = Merge-QtWslConfig -Text '' -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $r.Text | Should -Not -Match '(?m)^\s*processors\s*='
        $r.KeysAdded | Should -Not -Contain 'processors'
    }

    It 'R3:用户已有 swap/localhostForwarding 一律不动,只进 kept[]' {
        $t = "[wsl2]`r`nswap=8GB`r`nlocalhostForwarding=false"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $r.Text | Should -Match '(?m)^swap=8GB\r?$'
        $r.Text | Should -Match '(?m)^localhostForwarding=false\r?$'
        @($r.Kept | Where-Object { $_.key -eq 'swap' }).Count | Should -Be 1
        @($r.Kept | Where-Object { $_.key -eq 'localhostForwarding' }).Count | Should -Be 1
        $r.KeysAdded | Should -Not -Contain 'swap'
    }

    It '用户的注释与无关键逐行原样保留' {
        $t = "# 我自己的说明`r`n[wsl2]`r`n; 另一种注释`r`nnetworkingMode=mirrored`r`nswap=4GB"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $r.Text | Should -Match '(?m)^# 我自己的说明\r?$'
        $r.Text | Should -Match '(?m)^; 另一种注释\r?$'
        $r.Text | Should -Match '(?m)^networkingMode=mirrored\r?$'   # 「不写、不动」的 Win11 键原样保留
    }
}

Describe 'R3′ memory —— 安装器可直接改,但只在低于查表值时(B-5)' {

    It '已有 6GB 低于 11GB → 改,并进 changed[](验收 M1-6 ③)' {
        $t = "[wsl2]`r`nmemory=6GB"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $r.Text | Should -Match '(?m)^memory=11GB\r?$'
        $c = @($r.Changed | Where-Object { $_.key -eq 'memory' })
        $c.Count | Should -Be 1
        $c[0].from | Should -Be '6GB'
        $c[0].to | Should -Be '11GB'
        $r.KeysAdded | Should -Not -Contain 'memory'    # 是「改」不是「增」,卸载时不该删
    }

    It '🔴 已有 14GB 高于 11GB → 不动(验收 M1-6b:用户给 WSL 更多内存对本产品无害)' {
        $t = "[wsl2]`r`nmemory=14GB"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $r.Text | Should -Match '(?m)^memory=14GB\r?$'
        @($r.Changed | Where-Object { $_.key -eq 'memory' }).Count | Should -Be 0
        @($r.Kept | Where-Object { $_.key -eq 'memory' }).Count | Should -Be 1
    }

    It '🔴 MemoryAdjust=never(/QT_KEEP_WSL_MEMORY=1)退回 R3 只提示(验收 M1-6 ④)' {
        $t = "[wsl2]`r`nmemory=6GB"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT' -MemoryAdjust 'never'
        $r.Text | Should -Match '(?m)^memory=6GB\r?$'
        @($r.Changed).Count | Should -Be 0
    }
}

Describe 'R5 kernel —— 唯一允许改已有值的键(§2.6.2)' {

    It 'DEFAULT:插到 [wsl2] 段首行' {
        $t = "[wsl2]`r`nswap=4GB"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'DEFAULT'
        $lines = @($r.Text -split "`r`n")
        $lines[0] | Should -Be '[wsl2]'
        $lines[1] | Should -Be $script:KernelLine
    }

    It 'OURS_STALE:替换为本次路径' {
        $t = "[wsl2]`r`nkernel=C:\\ProgramData\\QTrade\\kernel\\bzImage-old"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'OURS_STALE'
        $r.Text | Should -Match '(?m)^kernel=C:\\\\ProgramData\\\\QTrade\\\\kernel\\\\bzImage-6\.6\r?$'
        $r.KernelLineWritten | Should -Be $script:KernelLine
    }

    It '🔴 OTHER_CUSTOM 且未获确认 → 抛(对应退出码 28,验收 M1-6 ①②)' {
        $t = "[wsl2]`r`nkernel=D:\\k\\bzImage"
        { Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'OTHER_CUSTOM' } |
            Should -Throw
    }

    It 'OTHER_CUSTOM 已确认 → 替换并把旧值记进 replaced_kernel(卸载时问是否恢复)' {
        $t = "[wsl2]`r`nkernel=D:\\k\\bzImage"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB' } -KernelState 'OTHER_CUSTOM' -ReplaceOtherKernel
        $r.ReplacedKernel | Should -Be 'D:\k\bzImage'
        $r.Text | Should -Match '(?m)^kernel=C:\\\\ProgramData'
    }
}

Describe 'R7 crash dump 两键(R-10 / N-7 / R2-11)' {

    It '🔴 键缺失 → 必写我方值(缺失时系统默认 10 才是真正的坑)' {
        $r = Merge-QtWslConfig -Text '' -Desired @{ kernel = $script:KernelPath; memory = '11GB'; maxCrashDumpCount = '2'; crashDumpFolder = 'D:\\QTrade\\wsl-crashes' } -KernelState 'DEFAULT'
        $r.Text | Should -Match '(?m)^maxCrashDumpCount=2\r?$'
        $r.Text | Should -Match '(?m)^crashDumpFolder=D:\\\\QTrade\\\\wsl-crashes\r?$'
        $r.KeysAdded | Should -Contain 'maxCrashDumpCount'
    }

    It '🔴 已有用户值 → 保留不改(不并入 R3′ 的「不同则改」)' {
        $t = "[wsl2]`r`nmaxCrashDumpCount=5"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB'; maxCrashDumpCount = '2' } -KernelState 'DEFAULT'
        $r.Text | Should -Match '(?m)^maxCrashDumpCount=5\r?$'
        @($r.Changed | Where-Object { $_.key -eq 'maxCrashDumpCount' }).Count | Should -Be 0
    }

    It '已有值 > 2 时出告警(P-ENV 显示建议值 2)' {
        $t = "[wsl2]`r`nmaxCrashDumpCount=10"
        $r = Merge-QtWslConfig -Text $t -Desired @{ kernel = $script:KernelPath; memory = '11GB'; maxCrashDumpCount = '2' } -KernelState 'DEFAULT'
        @($r.Warnings).Count | Should -BeGreaterThan 0
        $r.Warnings[0] | Should -Match 'maxCrashDumpCount'
    }

    It 'crashDumpFolder 取不到数据盘(期望值空)时不写,留系统盘默认' {
        $r = Merge-QtWslConfig -Text '' -Desired @{ kernel = $script:KernelPath; memory = '11GB'; maxCrashDumpCount = '2' } -KernelState 'DEFAULT'
        $r.Text | Should -Not -Match '(?m)^\s*crashDumpFolder\s*='
    }
}

Describe '🔴 W4 回滚基线 = 当前文件去掉所有 kernel= 行,不是旧备份文件' {

    It '去掉 kernel= 行,其它行逐字保留' {
        $t = "[wsl2]`r`nkernel=C:\\a\\b`r`nmemory=11GB`r`n# 注释`r`nswap=2GB"
        $b = Get-QtWslConfigRollbackBaseline -Text $t
        $b | Should -Not -Match '(?m)^\s*kernel\s*='
        $b | Should -Match '(?m)^memory=11GB\r?$'
        $b | Should -Match '(?m)^# 注释\r?$'
        $b | Should -Match '(?m)^swap=2GB\r?$'
    }

    It '🔴 基线保留 R3′ 改过的 memory(内核回滚不连带撤销内存调整)' {
        $t = "[wsl2]`r`nkernel=C:\\a\\b`r`nmemory=11GB"
        (Get-QtWslConfigRollbackBaseline -Text $t) | Should -Match 'memory=11GB'
    }

    It '坏备份识别:备份里含 kernel= 行即视为过期(v2 脚本第 66~69 行)' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-bak-' + [Guid]::NewGuid().ToString('N') + '.bak')
        try {
            [IO.File]::WriteAllText($tmp, "[wsl2]`r`nkernel=C:\\x`r`n")
            Test-QtWslConfigBackupPoisoned -Path $tmp | Should -BeTrue
            [IO.File]::WriteAllText($tmp, "[wsl2]`r`nmemory=8GB`r`n")
            Test-QtWslConfigBackupPoisoned -Path $tmp | Should -BeFalse
        } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

Describe '🔴 W3 .wslconfig 可解析性(写配置前三道前置校验之二)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-wc-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
        $script:F = Join-Path $script:Tmp '.wslconfig'
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '文件不存在算可解析(首装)' {
        (Test-QtWslConfigParsable -Path (Join-Path $script:Tmp 'nope')).Ok | Should -BeTrue
    }

    It '正常 UTF-8 无 BOM 通过' {
        [IO.File]::WriteAllText($script:F, "[wsl2]`r`nmemory=8GB`r`n", (New-Object Text.UTF8Encoding($false)))
        (Test-QtWslConfigParsable -Path $script:F).Ok | Should -BeTrue
    }

    It '🔴 带 BOM 被拒(WSL 会整段忽略配置、静默用默认内核)' {
        [IO.File]::WriteAllText($script:F, "[wsl2]`r`nmemory=8GB`r`n", (New-Object Text.UTF8Encoding($true)))
        $r = Test-QtWslConfigParsable -Path $script:F
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'BOM'
    }

    It '🔴 UTF-16(含 \0)被拒' {
        [IO.File]::WriteAllBytes($script:F, [Text.Encoding]::Unicode.GetBytes("[wsl2]`r`nmemory=8GB"))
        (Test-QtWslConfigParsable -Path $script:F).Reason | Should -Be 'UTF16'
    }

    It '语法坏行被拒(段/键正则未全命中)' {
        [IO.File]::WriteAllText($script:F, "[wsl2]`r`nthis is not ini`r`n", (New-Object Text.UTF8Encoding($false)))
        (Test-QtWslConfigParsable -Path $script:F).Reason | Should -Match '^BadLine:'
    }
}

Describe 'Write-QtUtf8NoBom —— .wslconfig 唯一允许的写法(W3)' {

    It '写出无 BOM、CRLF' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-w-' + [Guid]::NewGuid().ToString('N'))
        try {
            Write-QtUtf8NoBom -Path $tmp -Text "[wsl2]`nmemory=8GB" | Out-Null
            $b = [IO.File]::ReadAllBytes($tmp)
            ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) | Should -BeFalse
            [IO.File]::ReadAllText($tmp) | Should -Be "[wsl2]`r`nmemory=8GB"
        } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

Describe '备份 R1 —— 永不覆盖旧备份(§2.6.2 R1 / R6-14 后缀口径)' {

    It '同一时间戳再备份一次会另起文件名' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-bk-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        try {
            $src = Join-Path $tmp '.wslconfig'
            [IO.File]::WriteAllText($src, "[wsl2]`r`n")
            $dir = Join-Path $tmp 'wsl'
            $a = Backup-QtWslConfig -Path $src -BackupDir $dir -Stamp '20260920-181500'
            $b = Backup-QtWslConfig -Path $src -BackupDir $dir -Stamp '20260920-181500'
            $a | Should -Not -Be $b
            (Split-Path -Leaf $a) | Should -Be '.wslconfig.bak-20260920-181500'
            (Test-Path -LiteralPath $a) | Should -BeTrue
            (Test-Path -LiteralPath $b) | Should -BeTrue
        } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }

    It '源文件不存在时回空串(不抛)' {
        Backup-QtWslConfig -Path 'Z:\no\such\.wslconfig' -BackupDir ([IO.Path]::GetTempPath()) | Should -Be ''
    }
}

Describe '卸载:只删我们新增的键与我们的 kernel= 行(§2.14 第 3 步)' {

    It '删 keys_added 的键与 QTrade 的 kernel 行,其它行逐字不变' {
        $t = "[wsl2]`r`nkernel=C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6`r`nmemory=11GB`r`nswap=8GB`r`n# 用户注释`r`n[experimental]`r`nautoMemoryReclaim=gradual"
        $r = Remove-QtWslConfigKeys -Text $t -KeysAdded @('kernel', 'memory', 'autoMemoryReclaim') -RemoveKernelLine
        $r | Should -Not -Match '(?m)^\s*kernel\s*='
        $r | Should -Not -Match '(?m)^memory='
        $r | Should -Not -Match '(?m)^autoMemoryReclaim='
        $r | Should -Match '(?m)^swap=8GB\r?$'          # 用户自己的键一个不删
        $r | Should -Match '(?m)^# 用户注释\r?$'
    }

    It '不在 keys_added 里的 memory(= R3′ 改过值的)不被删' {
        $t = "[wsl2]`r`nmemory=11GB`r`nswap=2GB"
        $r = Remove-QtWslConfigKeys -Text $t -KeysAdded @('swap')
        $r | Should -Match '(?m)^memory=11GB\r?$'
        $r | Should -Not -Match '(?m)^swap='
    }
}

Describe 'wsl_state 判定顺序(§2.4.4)' {

    It '先排除不可用,再分版本' -ForEach @(
        @{ P = @{ VirtOk = $false; PolicyBlocked = $false; FeatureVmp = 'Enabled'; FeatureWsl = 'Enabled'; WslExeExists = $true; VersionOk = $true; StatusOk = $true; AllDistrosV1 = $false; DefaultVersion = 2 }; Want = 'VIRT_DISABLED' }
        @{ P = @{ VirtOk = $true; PolicyBlocked = $true; FeatureVmp = 'Enabled'; FeatureWsl = 'Enabled'; WslExeExists = $true; VersionOk = $true; StatusOk = $true; AllDistrosV1 = $false; DefaultVersion = 2 }; Want = 'POLICY_BLOCKED' }
        @{ P = @{ VirtOk = $true; PolicyBlocked = $false; FeatureVmp = 'Disabled'; FeatureWsl = 'Disabled'; WslExeExists = $false; VersionOk = $false; StatusOk = $false; AllDistrosV1 = $false; DefaultVersion = 2 }; Want = 'NONE' }
        @{ P = @{ VirtOk = $true; PolicyBlocked = $false; FeatureVmp = 'Disabled'; FeatureWsl = 'Enabled'; WslExeExists = $true; VersionOk = $false; StatusOk = $true; AllDistrosV1 = $false; DefaultVersion = 2 }; Want = 'FEATURE_OFF' }
        @{ P = @{ VirtOk = $true; PolicyBlocked = $false; FeatureVmp = 'Enabled'; FeatureWsl = 'Enabled'; WslExeExists = $true; VersionOk = $true; StatusOk = $true; AllDistrosV1 = $false; DefaultVersion = 2 }; Want = 'WSL2_STORE' }
        @{ P = @{ VirtOk = $true; PolicyBlocked = $false; FeatureVmp = 'Enabled'; FeatureWsl = 'Enabled'; WslExeExists = $true; VersionOk = $false; StatusOk = $true; AllDistrosV1 = $false; DefaultVersion = 2 }; Want = 'WSL2_INBOX' }
        @{ P = @{ VirtOk = $true; PolicyBlocked = $false; FeatureVmp = 'Enabled'; FeatureWsl = 'Enabled'; WslExeExists = $true; VersionOk = $true; StatusOk = $true; AllDistrosV1 = $true; DefaultVersion = 1 }; Want = 'WSL1_ONLY' }
    ) {
        Get-QtWslStateFromProbe -Probe ([pscustomobject]$P) | Should -Be $Want
    }

    It '版本串解析(中英文首行都认)' {
        Get-QtWslVersionString -Output "WSL 版本: 2.5.9.0`nWSLg 版本: 1.0" | Should -Be '2.5.9'
        Get-QtWslVersionString -Output "WSL version: 2.6.1" | Should -Be '2.6.1'
        Get-QtWslVersionString -Output "用法: wsl.exe [参数]" | Should -Be ''
    }

    It '🔴 6.6 内核线的前提:wsl --version ≥ 2.4.0(K7)' {
        Test-QtWslVersionAtLeast -Version '2.5.9' | Should -BeTrue
        Test-QtWslVersionAtLeast -Version '2.4.0' | Should -BeTrue
        Test-QtWslVersionAtLeast -Version '2.3.26' | Should -BeFalse
        Test-QtWslVersionAtLeast -Version '' | Should -BeFalse
    }

    It '解析 wsl -l -v' {
        $out = "  NAME            STATE           VERSION`r`n* Ubuntu-24.04    Running         2`r`n  docker-desktop  Stopped         2`r`n  old             Stopped         1"
        $rows = ConvertFrom-QtWslListVerbose -Output $out
        $rows.Count | Should -Be 3
        $rows[0].Name | Should -Be 'Ubuntu-24.04'
        $rows[0].Default | Should -BeTrue
        $rows[2].Version | Should -Be 1
    }
}

Describe 'kernel_state 判定(§2.4.5)' {

    It 'kernel= 不存在 → DEFAULT' {
        Get-QtKernelState -KernelValue $null | Should -Be 'DEFAULT'
        Get-QtKernelState -KernelValue '' | Should -Be 'DEFAULT'
    }

    It '我们的路径且 sha 一致 → OURS' {
        Get-QtKernelState -KernelValue 'C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6' -OurKernelSha256 'abc' -ActualSha256 'ABC' | Should -Be 'OURS'
    }

    It '我们的路径但 sha 不一致 → OURS_STALE(C-36)' {
        Get-QtKernelState -KernelValue 'C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6' -OurKernelSha256 'abc' -ActualSha256 'def' | Should -Be 'OURS_STALE'
    }

    It '旧版路径 %USERPROFILE%\.qtrade-redroid\bzImage 也认作我们的' {
        Get-QtKernelState -KernelValue 'C:\\Users\\a\\.qtrade-redroid\\bzImage' -OurKernelSha256 'abc' -ActualSha256 'abc' | Should -Be 'OURS'
    }

    It '🔴 其它任何值(含指向不存在的文件)→ OTHER_CUSTOM,必须停下问用户' {
        Get-QtKernelState -KernelValue 'D:\\k\\bzImage' -OurKernelSha256 'abc' -ActualSha256 '' | Should -Be 'OTHER_CUSTOM'
    }
}

Describe 'docker 地址池选段(docs/04 §2.7.4 候选表,03 只引用)' {

    It 'CIDR 前缀相交判定' {
        Test-QtCidrOverlap -A '10.213.0.0/16' -B '10.213.5.0/24' | Should -BeTrue
        Test-QtCidrOverlap -A '10.213.0.0/16' -B '10.231.0.0/16' | Should -BeFalse
        Test-QtCidrOverlap -A '172.17.0.0/16' -B '172.16.0.0/12' | Should -BeTrue
        Test-QtCidrOverlap -A '10.213.0.0/16' -B '0.0.0.0/0' | Should -BeTrue
        Test-QtCidrOverlap -A '10.213.0.0/16' -B 'not-a-cidr' | Should -BeFalse
    }

    It '取第一个不冲突的候选' {
        $c = @('10.213.0.0/16', '10.231.0.0/16', '10.247.0.0/16', '10.199.0.0/16')
        $r = Select-QtDockerPool -Candidates $c -OccupiedPrefixes @('10.213.0.0/16', '192.168.1.0/24')
        $r.Cidr | Should -Be '10.231.0.0/16'
        $r.AllConflict | Should -BeFalse
    }

    It '🔴 全冲突 → 取第一个 + warn,**不拒装**(R2-7 起 76 只用于显式 /QT_DOCKER_CIDR)' {
        $c = @('10.213.0.0/16', '10.231.0.0/16')
        $r = Select-QtDockerPool -Candidates $c -OccupiedPrefixes @('10.0.0.0/8')
        $r.Cidr | Should -Be '10.213.0.0/16'
        $r.AllConflict | Should -BeTrue
        $r.Warn | Should -Be 'DOCKER_POOL_ALL_CONFLICT'
    }

    It '无占用时取第一个' {
        (Select-QtDockerPool -Candidates @('10.213.0.0/16') -OccupiedPrefixes @()).Cidr | Should -Be '10.213.0.0/16'
    }
}

Describe 'WSL_MSI 退出码映射(§2.5.2 / §2.15)' {

    It '0 成功 / 1638 已装更高版本视为成功 / 3010 要重启 / 其它失败' -ForEach @(
        @{ Code = 0; Ok = $true; Reboot = $false }
        @{ Code = 1638; Ok = $true; Reboot = $false }
        @{ Code = 3010; Ok = $true; Reboot = $true }
        @{ Code = 1603; Ok = $false; Reboot = $false }
        @{ Code = 1602; Ok = $false; Reboot = $false }
    ) {
        Mock -ModuleName QTrade.Wsl Invoke-QtProcess { [pscustomobject]@{ ExitCode = $Code; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        $r = Install-QtWslMsi -MsiPath 'X:\wsl.msi' -LogPath 'X:\msi.log'
        $r.Ok | Should -Be $Ok
        $r.RebootRequired | Should -Be $Reboot
    }

    It 'wsl 错误码翻译' {
        Get-QtWslBrokenReason -Output 'Error 0x80370102' | Should -Be 'VIRT_DISABLED_IN_BIOS'
        Get-QtWslBrokenReason -Output 'Error 0x800701bc' | Should -Be 'INBOX_WSL_NO_KERNEL'
        Get-QtWslBrokenReason -Output 'Error 0x80370114' | Should -Be 'VMP_NOT_ENABLED'
        Get-QtWslBrokenReason -Output 'whatever' | Should -Be ''
    }
}

Describe '发行版导入 —— 0x80070070 映射成 DISK_FULL 而不是 IMPORT_FAILED(§2.7.2)' {

    It '磁盘满' {
        Mock -ModuleName QTrade.Wsl New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Wsl Invoke-QtWsl { [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = 'Error code: Wsl/Service/CreateInstance/CreateVm/0x80070070'; TimedOut = $false; DurationMs = 1 } }
        (Import-QtDistro -Name 'qtrade' -Directory 'X:\d' -TarPath 'X:\r.tar').Reason | Should -Be 'DISK_FULL'
    }

    It '其它失败 → IMPORT_FAILED' {
        Mock -ModuleName QTrade.Wsl New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Wsl Invoke-QtWsl { [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = 'something else'; TimedOut = $false; DurationMs = 1 } }
        (Import-QtDistro -Name 'qtrade' -Directory 'X:\d' -TarPath 'X:\r.tar').Reason | Should -Be 'IMPORT_FAILED'
    }

    It '成功' {
        Mock -ModuleName QTrade.Wsl New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Wsl Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        (Import-QtDistro -Name 'qtrade' -Directory 'X:\d' -TarPath 'X:\r.tar').Ok | Should -BeTrue
    }

    It 'systemd 就绪:running / degraded 都算起,degraded 记警告(§2.15)' {
        Mock -ModuleName QTrade.Wsl Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = "degraded`n"; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        $r = Wait-QtDistroSystemd -Name 'qtrade'
        $r.Ok | Should -BeTrue
        $r.Degraded | Should -BeTrue
    }
}
