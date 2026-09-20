# Pester 5 —— manifest 校验(docs/03 §2.2.1、§2.1 两层校验与失败清理、§2.6.9 R6-38)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Payload')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }

    function New-QtTestStage {
        $root = Join-Path ([IO.Path]::GetTempPath()) ('qt-pl-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $root 'kernel') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $root 'wsl') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $root 'install') -Force | Out-Null
        return $root
    }
    function Set-QtTestFile {
        param([string] $Path, [string] $Content)
        New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
        [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

Describe 'manifest 逐文件 sha256(§2.1「两层都过才算 PAYLOAD_STAGED」)' {

    BeforeEach { $script:Stage = New-QtTestStage }
    AfterEach { Remove-Item -LiteralPath $script:Stage -Recurse -Force -ErrorAction SilentlyContinue }

    It '全部一致 → Ok' {
        $h1 = Set-QtTestFile -Path (Join-Path $script:Stage 'kernel\bzImage-6.6') -Content 'kernel-bytes'
        $h2 = Set-QtTestFile -Path (Join-Path $script:Stage 'wsl\rootfs.tar') -Content 'rootfs-bytes'
        $m = [pscustomobject]@{ files = @(
                [pscustomobject]@{ path = 'kernel/bzImage-6.6'; sha256 = $h1; critical = $true }
                [pscustomobject]@{ path = 'wsl/rootfs.tar'; sha256 = $h2; critical = $true }
            ) }
        $r = Invoke-QtPayloadVerify -Manifest $m -StageRoot $script:Stage
        $r.Ok | Should -BeTrue
        $r.VerifiedCount | Should -Be 2
    }

    It 'sha 不一致 → HashMismatch,critical 的进 CriticalFailed' {
        Set-QtTestFile -Path (Join-Path $script:Stage 'kernel\bzImage-6.6') -Content 'tampered' | Out-Null
        $m = [pscustomobject]@{ files = @(
                [pscustomobject]@{ path = 'kernel/bzImage-6.6'; sha256 = ('0' * 64); critical = $true }
            ) }
        $r = Invoke-QtPayloadVerify -Manifest $m -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Mismatched.Count | Should -Be 1
        $r.CriticalFailed.Count | Should -Be 1
    }

    It '文件缺失 → Missing' {
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'wsl/wsl.msi'; sha256 = ('0' * 64); critical = $true }) }
        $r = Invoke-QtPayloadVerify -Manifest $m -StageRoot $script:Stage
        $r.Missing.Count | Should -Be 1
    }

    It '通配条目(pkg/adb/*)只验目录存在,不展开逐文件' {
        New-Item -ItemType Directory -Path (Join-Path $script:Stage 'pkg\adb') -Force | Out-Null
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'pkg/adb/*' }) }
        $r = Invoke-QtPayloadVerify -Manifest $m -StageRoot $script:Stage
        $r.Entries[0].status | Should -Be 'SkippedGlob'
        $r.Ok | Should -BeTrue
    }

    It '无 sha256(或文档里的省略号占位)→ SkippedNoHash,不误判失败' {
        Set-QtTestFile -Path (Join-Path $script:Stage 'wsl\wsl.msi') -Content 'msi' | Out-Null
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'wsl/wsl.msi'; sha256 = '…' }) }
        (Invoke-QtPayloadVerify -Manifest $m -StageRoot $script:Stage).Entries[0].status | Should -Be 'SkippedNoHash'
    }

    It '🔴 §2.1 失败清理:只删 sha 不一致的,已一致的留着(重跑按 sha 跳过)' {
        $good = Set-QtTestFile -Path (Join-Path $script:Stage 'wsl\rootfs.tar') -Content 'good'
        Set-QtTestFile -Path (Join-Path $script:Stage 'kernel\bzImage-6.6') -Content 'bad' | Out-Null
        $m = [pscustomobject]@{ files = @(
                [pscustomobject]@{ path = 'wsl/rootfs.tar'; sha256 = $good }
                [pscustomobject]@{ path = 'kernel/bzImage-6.6'; sha256 = ('0' * 64) }
            ) }
        $r = Invoke-QtPayloadVerify -Manifest $m -StageRoot $script:Stage
        $removed = Clear-QtInconsistentPayload -VerifyResult $r
        ($removed) -join ',' | Should -Be 'kernel/bzImage-6.6'
        (Test-Path -LiteralPath (Join-Path $script:Stage 'wsl\rootfs.tar')) | Should -BeTrue
        (Test-Path -LiteralPath (Join-Path $script:Stage 'kernel\bzImage-6.6')) | Should -BeFalse
    }

    It 'PAYLOAD_STAGED 幂等判据 = 全部一致' {
        $h = Set-QtTestFile -Path (Join-Path $script:Stage 'wsl\rootfs.tar') -Content 'r'
        $mf = Join-Path $script:Stage 'install\manifest.json'
        [IO.File]::WriteAllText($mf, (@{ files = @(@{ path = 'wsl/rootfs.tar'; sha256 = $h }) } | ConvertTo-Json -Depth 6))
        $ctx = [pscustomobject]@{ ManifestPath = $mf; StageRoot = $script:Stage }
        Test-QtPayloadStaged -Context $ctx | Should -BeTrue
        # manifest 不存在时不得误判「已完成」
        Test-QtPayloadStaged -Context ([pscustomobject]@{ ManifestPath = (Join-Path $script:Stage 'nope.json'); StageRoot = $script:Stage }) | Should -BeFalse
    }

    It 'dest 里的 %ProgramData% 会被展开' {
        $e = [pscustomobject]@{ path = 'kernel/bzImage-6.6'; dest = '%ProgramData%\QTrade\kernel\bzImage-6.6' }
        (Expand-QtPayloadPath -File $e -StageRoot 'X:\stage') | Should -Be (Join-Path $env:ProgramData 'QTrade\kernel\bzImage-6.6')
    }

    It 'dest 缺省或是文档里的省略号 → 落回 «stage»\«path»' {
        $e = [pscustomobject]@{ path = 'wsl/rootfs.tar'; dest = '…\wsl\rootfs.tar' }
        (Expand-QtPayloadPath -File $e -StageRoot 'X:\stage') | Should -Be 'X:\stage\wsl\rootfs.tar'
    }
}

Describe '🔴 R6-38 CI 硬门:manifest.kernel.coredump_l2 恒 "D"(§2.2.1 / §2.6.9)' {

    It '"D" 通过' {
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'kernel/bzImage-6.6'; coredump_l2 = 'D' }) }
        Test-QtManifestCoredumpL2 -Manifest $m | Should -BeTrue
    }

    It '"A"(未采纳的备选)被拒 —— 它不是合法交付形态' {
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'kernel/bzImage-6.6'; coredump_l2 = 'A' }) }
        Test-QtManifestCoredumpL2 -Manifest $m | Should -BeFalse
    }

    It '字段缺失被拒' {
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'kernel/bzImage-6.6' }) }
        Test-QtManifestCoredumpL2 -Manifest $m | Should -BeFalse
    }

    It '没有内核条目被拒' {
        Test-QtManifestCoredumpL2 -Manifest ([pscustomobject]@{ files = @() }) | Should -BeFalse
    }
}

Describe 'Get-QtManifestKernel(内核线恒 6.6,A-3)' {

    It '取到 6.6 条目' {
        $m = [pscustomobject]@{ files = @([pscustomobject]@{ path = 'kernel/bzImage-6.6'; version = '6.6.123.2-microsoft-standard-WSL2-binder' }) }
        (Get-QtManifestKernel -Manifest $m).version | Should -Be '6.6.123.2-microsoft-standard-WSL2-binder'
    }

    It '没有该线抛错(不静默回退到别的线)' {
        { Get-QtManifestKernel -Manifest ([pscustomobject]@{ files = @() }) } | Should -Throw
    }
}
