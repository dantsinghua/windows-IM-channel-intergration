# Pester 5 —— 内核验证/回滚(docs/03 §2.6.1、§2.6.3 红线 6、§2.6.4、§2.6.5、§2.6.7 K4/K5/W7/W9/W13/W14、§2.6.8)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl', 'QTrade.Kernel')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
    $script:Ver = '6.6.123.2-microsoft-standard-WSL2-binder'
}

Describe '超时值(docs/03 §7 [wsl] 追加键 / §2.6.8 第 7 条)' {

    It '逐项对齐文档' {
        $t = Get-QtKernelTimeouts
        $t.shutdown_timeout_s | Should -Be 60
        $t.shutdown_grace_s | Should -Be 8        # W9:统一 8 s,不是 rollback-kernel.ps1 的 3 s
        $t.kernel_boot_timeout_s | Should -Be 120
        $t.binder_check_timeout_s | Should -Be 60
        $t.user_distro_check_timeout_s | Should -Be 60
    }

    It '内核线恒 6.6(A-3)' { Get-QtKernelLine | Should -Be '6.6' }
    It 'kcheck 发行版名' { Get-QtKCheckDistroName | Should -Be 'qtrade-kcheck' }
}

Describe '🔴 W7:binder 判据命令串不含双引号' {

    It '命令串里零个双引号(PowerShell 5.1 传原生程序时会弄坏引号)' {
        (Get-QtBinderCheckCommand) | Should -Not -Match '"'
    }

    It '逐段对应 §2.6.4 第 5 步原文' {
        $c = Get-QtBinderCheckCommand
        $c | Should -Match 'zcat /proc/config\.gz \| grep -q \^CONFIG_ANDROID_BINDER_IPC=y'
        $c | Should -Match 'grep -qw binder /proc/filesystems'
        $c | Should -Match 'mount -t binder binder /run/binderfs-check'
        $c | Should -Match 'echo BINDERFS_OK'
    }

    It '🔴 K5:判据里不得出现 `ls /dev/binder`(6.x 开 binderfs 后不预建,v1 脚本据此误判好内核)' {
        (Get-QtBinderCheckCommand) | Should -Not -Match '/dev/binder'
    }
}

Describe 'binder 判据输出判定(§2.6.4 第 5 步)' {

    It '三设备齐 + BINDERFS_OK → 通过' {
        Test-QtBinderCheckOutput -Output "binder`nhwbinder`nvndbinder`nBINDERFS_OK" | Should -BeTrue
        Test-QtBinderCheckOutput -Output "binder hwbinder vndbinder`nBINDERFS_OK" | Should -BeTrue
    }

    It '缺 BINDERFS_OK → 不通过' {
        Test-QtBinderCheckOutput -Output "binder hwbinder vndbinder" | Should -BeFalse
    }

    It '缺 vndbinder → 不通过' {
        Test-QtBinderCheckOutput -Output "binder hwbinder`nBINDERFS_OK" | Should -BeFalse
    }

    It '空输出 → 不通过' { Test-QtBinderCheckOutput -Output '' | Should -BeFalse }
}

Describe '🔴 K4:uname -r 与 manifest version **逐字**相等(不再只判「含 binder」)' {

    It '逐字相等 → 通过' {
        Test-QtKernelVersionExact -UnameOutput "$script:Ver`n" -ManifestVersion $script:Ver | Should -BeTrue
    }

    It '带 `+` 后缀 → 不通过(它也含 binder,只判「含 binder」根本判不出来)' {
        Test-QtKernelVersionExact -UnameOutput ($script:Ver + '+') -ManifestVersion $script:Ver | Should -BeFalse
    }

    It '官方内核 → 不通过' {
        Test-QtKernelVersionExact -UnameOutput '6.6.87.2-microsoft-standard-WSL2' -ManifestVersion $script:Ver | Should -BeFalse
    }

    It '大小写敏感(-ceq)' {
        Test-QtKernelVersionExact -UnameOutput $script:Ver.ToUpperInvariant() -ManifestVersion $script:Ver | Should -BeFalse
    }
}

Describe '写配置前三道前置校验(§2.6.8 第 9 条:任一不过就不写、不 shutdown)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-k-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
        $script:Bz = Join-Path $script:Tmp 'bzImage-6.6'
        [IO.File]::WriteAllText($script:Bz, 'kernel-bytes')
        $script:Sha = (Get-FileHash -LiteralPath $script:Bz -Algorithm SHA256).Hash.ToLowerInvariant()
        $script:Wc = Join-Path $script:Tmp '.wslconfig'
        [IO.File]::WriteAllText($script:Wc, "[wsl2]`r`nmemory=8GB`r`n", (New-Object Text.UTF8Encoding($false)))
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '三道全过' {
        (Test-QtKernelPreconditions -KernelPath $script:Bz -ExpectedSha256 $script:Sha -WslConfigPath $script:Wc).Ok | Should -BeTrue
    }

    It '①sha 不符 → KERNEL_SHA_MISMATCH' {
        (Test-QtKernelPreconditions -KernelPath $script:Bz -ExpectedSha256 ('0' * 64) -WslConfigPath $script:Wc).Reason | Should -Be 'KERNEL_SHA_MISMATCH'
    }

    It '内核文件不存在 → KERNEL_SHA_MISMATCH' {
        (Test-QtKernelPreconditions -KernelPath (Join-Path $script:Tmp 'nope') -ExpectedSha256 $script:Sha -WslConfigPath $script:Wc).Reason | Should -Be 'KERNEL_SHA_MISMATCH'
    }

    It '②.wslconfig 带 BOM → WSLCONFIG_PARSE_FAILED(W3)' {
        [IO.File]::WriteAllText($script:Wc, "[wsl2]`r`n", (New-Object Text.UTF8Encoding($true)))
        (Test-QtKernelPreconditions -KernelPath $script:Bz -ExpectedSha256 $script:Sha -WslConfigPath $script:Wc).Reason | Should -Be 'WSLCONFIG_PARSE_FAILED'
    }

    It '🔴 ③策略禁自定义内核 → POLICY_BLOCKED(W12:否则验证永远是官方内核、被当成「内核起不来」)' {
        (Test-QtKernelPreconditions -KernelPath $script:Bz -ExpectedSha256 $script:Sha -WslConfigPath $script:Wc -CustomKernelForbidden $true).Reason | Should -Be 'POLICY_BLOCKED'
    }
}

Describe '🔴 红线 6:没有用户确认就不许 wsl --shutdown' {

    It '未确认 → 抛,且**一次 wsl 调用都不发**' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { throw '不该被调用' }
        { Invoke-QtWslShutdown -Confirmed $false } | Should -Throw
        Should -Invoke -ModuleName QTrade.Kernel Invoke-QtWsl -Times 0
    }

    It '已确认 → 发 --shutdown 并按 W9 睡 8 秒' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Kernel Start-QtSleep { }
        (Invoke-QtWslShutdown -Confirmed $true).Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Kernel Start-QtSleep -Times 1 -ParameterFilter { $Seconds -eq 8 }
    }

    It 'shutdown 超时 → KERNEL_SHUTDOWN_TIMEOUT' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 60000 } }
        Mock -ModuleName QTrade.Kernel Start-QtSleep { }
        (Invoke-QtWslShutdown -Confirmed $true).Reason | Should -Be 'KERNEL_SHUTDOWN_TIMEOUT'
    }
}

Describe '验证段 Invoke-QtKernelVerify(§2.6.4 第 2/4/5 步定成败)' {

    BeforeEach {
        Mock -ModuleName QTrade.Kernel Start-QtSleep { }
    }

    It '全绿 → Ok,uname 与 binder 输出都带回来' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'uname') { return [pscustomobject]@{ ExitCode = 0; StdOut = $script:Ver; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'sh') { return [pscustomobject]@{ ExitCode = 0; StdOut = "binder hwbinder vndbinder`nBINDERFS_OK"; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $r = Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true
        $r.Ok | Should -BeTrue
        $r.Uname | Should -Be $script:Ver
        $r.BinderOutput | Should -Match 'BINDERFS_OK'
    }

    It '第 2 步 shutdown 超时 → KERNEL_SHUTDOWN_TIMEOUT(退出码 66)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 1 } }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true).Reason | Should -Be 'KERNEL_SHUTDOWN_TIMEOUT'
    }

    It '第 4 步 uname 超时 → KERNEL_BOOT_TIMEOUT(退出码 62;验收 M0-4)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true).Reason | Should -Be 'KERNEL_BOOT_TIMEOUT'
    }

    It '第 4 步退出码非 0 → KERNEL_BOOT_FAILED(退出码 63)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 1; StdOut = 'boom'; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true).Reason | Should -Be 'KERNEL_BOOT_FAILED'
    }

    It '版本不等(官方内核冒充)→ KERNEL_NO_BINDER(退出码 64;验收 M0-3)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'uname') { return [pscustomobject]@{ ExitCode = 0; StdOut = '6.6.87.2-microsoft-standard-WSL2'; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true).Reason | Should -Be 'KERNEL_NO_BINDER'
    }

    It '🔴 W12:版本不等 **且**策略键存在 → 原因码改 POLICY_BLOCKED(排查方向才不会全错)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'uname') { return [pscustomobject]@{ ExitCode = 0; StdOut = '6.6.87.2-microsoft-standard-WSL2'; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -CustomKernelPolicyPresent $true).Reason | Should -Be 'POLICY_BLOCKED'
    }

    It 'binder 判据不过 → KERNEL_NO_BINDER' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'uname') { return [pscustomobject]@{ ExitCode = 0; StdOut = $script:Ver; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = 'nope'; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true).Reason | Should -Be 'KERNEL_NO_BINDER'
    }

    It '🔴 B-4:用户发行版起不来 —— 仍判 Ok,只把失败列进 UserDistroFailures(不自动回滚)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if (($WslArgs -contains 'uname') -and ($WslArgs -contains 'broken-distro')) {
                return [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = 'no init'; TimedOut = $false; DurationMs = 1 }
            }
            if ($WslArgs -contains 'uname') { return [pscustomobject]@{ ExitCode = 0; StdOut = $script:Ver; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'sh') { return [pscustomobject]@{ ExitCode = 0; StdOut = "binder hwbinder vndbinder BINDERFS_OK"; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $r = Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -UserDistros @('Ubuntu-24.04', 'broken-distro')
        $r.Ok | Should -BeTrue
        $r.UserDistroFailures.Count | Should -Be 1
        $r.UserDistroFailures[0].name | Should -Be 'broken-distro'
    }

    It '未确认 shutdown 时整段拒绝执行(红线 6 钉在代码里)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { throw '不该被调用' }
        { Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $false } | Should -Throw
    }
}

Describe '回滚段 Invoke-QtKernelRollback(§2.6.5)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-rb-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
        $script:Wc = Join-Path $script:Tmp '.wslconfig'
        [IO.File]::WriteAllText($script:Wc, "[wsl2]`r`nkernel=C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6`r`nmemory=11GB`r`nswap=2GB`r`n", (New-Object Text.UTF8Encoding($false)))
        Mock -ModuleName QTrade.Kernel Start-QtSleep { }
        Mock -ModuleName QTrade.Kernel Remove-QtKCheck { }
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '🔴 用回滚基线覆写:kernel= 行消失,用户其它行逐字保留(W4;验收 M0-3)' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = '6.6.87.2-microsoft-standard-WSL2'; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $r = Invoke-QtKernelRollback -WslConfigPath $script:Wc -ShutdownConfirmed $true
        $r.Ok | Should -BeTrue
        $r.OfficialKernel | Should -Be '6.6.87.2-microsoft-standard-WSL2'
        $txt = [IO.File]::ReadAllText($script:Wc)
        $txt | Should -Not -Match '(?m)^\s*kernel\s*='
        $txt | Should -Match '(?m)^memory=11GB\r?$'    # 内核回滚不连带撤销 R3′ 的内存调整
        $txt | Should -Match '(?m)^swap=2GB\r?$'
    }

    It '回滚后 kcheck 起不来 → KERNEL_ROLLBACK_FAILED(退出码 65),但文件已是无 kernel= 的基线' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 1 }
        }
        $r = Invoke-QtKernelRollback -WslConfigPath $script:Wc -ShutdownConfirmed $true
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'KERNEL_ROLLBACK_FAILED'
        [IO.File]::ReadAllText($script:Wc) | Should -Not -Match '(?m)^\s*kernel\s*='
    }

    It '回滚阶段 shutdown 挂起也报 KERNEL_ROLLBACK_FAILED' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 1 } }
        (Invoke-QtKernelRollback -WslConfigPath $script:Wc -ShutdownConfirmed $true).Reason | Should -Be 'KERNEL_ROLLBACK_FAILED'
    }

    It '🔴 §2.6.6 不变量②:任何路径结束时 kcheck 都被注销' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 1 } }
        Invoke-QtKernelRollback -WslConfigPath $script:Wc -ShutdownConfirmed $true | Out-Null
        Should -Invoke -ModuleName QTrade.Kernel Remove-QtKCheck -Times 1
    }
}

Describe 'kcheck 预导入(§2.6.1 / §2.6.8 第 5 条,W14)' {

    It '导入前先注销残留(§2.6.6 不变量②)' {
        Mock -ModuleName QTrade.Kernel Remove-QtKCheck { }
        Mock -ModuleName QTrade.Kernel New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        (Import-QtKCheck -Directory 'X:\kcheck' -TarPath 'X:\kcheck.tar').Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Kernel Remove-QtKCheck -Times 1
    }

    It '导入失败 → KCHECK_IMPORT_FAILED(退出码 67;此时还没写配置、没 shutdown)' {
        Mock -ModuleName QTrade.Kernel Remove-QtKCheck { }
        Mock -ModuleName QTrade.Kernel New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = 'nope'; TimedOut = $false; DurationMs = 1 } }
        (Import-QtKCheck -Directory 'X:\kcheck' -TarPath 'X:\kcheck.tar').Reason | Should -Be 'KCHECK_IMPORT_FAILED'
    }
}

Describe '🔴 W13:dmesg 噪声不参与成败判定' {

    It '白名单里的行被认成噪声' {
        $noise = "dxgk: dxgkio_query_adapter_info: Ioctl failed: -2`nCheckConnection getaddrinfo -5`nmodprobe: FATAL: Module kvm_intel not found"
        Test-QtDmesgNoiseOnly -Text $noise | Should -BeTrue
    }

    It '出现白名单外的行就不算纯噪声' {
        Test-QtDmesgNoiseOnly -Text "dxgk: Ioctl failed: -2`nKernel panic - not syncing" | Should -BeFalse
    }

    It '白名单包含 §2.6.7 W13 列的四类' {
        $a = Get-QtDmesgNoiseAllowlist
        $a.Count | Should -Be 4
    }
}

Describe '内核指针 current.json(§2.6.1:后续所有地方只认这个指针)' {

    It '写出 line/path/sha256/version,验证后补 verified_at' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-cp-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        try {
            $p = Join-Path $tmp 'current.json'
            Write-QtKernelPointer -Path $p -KernelPath 'C:\a\bzImage-6.6' -Sha256 'abc' -Version $script:Ver | Out-Null
            $j = [IO.File]::ReadAllText($p) | ConvertFrom-Json
            $j.line | Should -Be '6.6'
            $j.version | Should -Be $script:Ver
            $j.PSObject.Properties.Name | Should -Not -Contain 'verified_at'

            Write-QtKernelPointer -Path $p -KernelPath 'C:\a\bzImage-6.6' -Sha256 'abc' -Version $script:Ver -VerifiedAt '2026-09-20T18:00:00+08:00' | Out-Null
            ([IO.File]::ReadAllText($p) | ConvertFrom-Json).verified_at | Should -Be '2026-09-20T18:00:00+08:00'
        } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

Describe 'WSLCONFIG_WRITTEN 幂等判据(§2.3:文件含我们的 kernel= 行、值与 kernel_line_written 一致)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-wcw-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
        $script:Wc = Join-Path $script:Tmp '.wslconfig'
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '行在 → true' {
        $line = 'kernel=C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6'
        [IO.File]::WriteAllText($script:Wc, "[wsl2]`r`n$line`r`n")
        Test-QtWslConfigWritten -Context ([pscustomobject]@{ WslConfigPath = $script:Wc; KernelLineWritten = $line }) | Should -BeTrue
    }

    It '行不在 → false(会重做本步,不会误 skip)' {
        [IO.File]::WriteAllText($script:Wc, "[wsl2]`r`nmemory=8GB`r`n")
        Test-QtWslConfigWritten -Context ([pscustomobject]@{ WslConfigPath = $script:Wc; KernelLineWritten = 'kernel=C:\\a' }) | Should -BeFalse
    }

    It 'kernel_line_written 为空 → false' {
        [IO.File]::WriteAllText($script:Wc, "[wsl2]`r`n")
        Test-QtWslConfigWritten -Context ([pscustomobject]@{ WslConfigPath = $script:Wc; KernelLineWritten = '' }) | Should -BeFalse
    }
}
