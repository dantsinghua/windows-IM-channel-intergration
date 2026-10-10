# Pester 5 —— 评审 B6(内核切换失败文案看回滚结果)+ 预演 #16(注销 kcheck 前抓 dmesg、诊断包带上)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl',
            'QTrade.Kernel', 'QTrade.Diag')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
    $script:RunStep = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\run-step.ps1'
    $script:Ver = '6.6.123.2-microsoft-standard-WSL2-binder'
    $script:Official = '6.6.87.2-microsoft-standard-WSL2'
    $script:Hcs = "没有收到虚拟机或容器的回应，操作超时。`r`n错误代码: Wsl/Service/CreateInstance/HCS_E_CONNECTION_TIMEOUT"
    function script:New-R([int] $Code = 0, [string] $Out = '', [bool] $TimedOut = $false) {
        [pscustomobject]@{ ExitCode = $Code; StdOut = $Out; StdErr = ''; TimedOut = $TimedOut; DurationMs = 1 }
    }
}

Describe 'R6-82: rollback diagnostics preserve uncertainty about other distributions' {

    It '回滚成功 + KERNEL_BOOT_FAILED → 说 QTrade 内核未能启动、已恢复原内核,保留原因码' {
        $ver = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_FAILED'; Message = 'QTrade 内核下 WSL2 未能正常启动(uname 输出不是版本串)' }
        $rb = [pscustomobject]@{ Ok = $true; Reason = ''; OfficialKernel = $script:Official; Stage = '' }
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_BOOT_FAILED'
        $f.ExitName | Should -Be 'E_INSTALL_KERNEL_BOOT_FAILED'
        $f.Message | Should -Match 'QTrade 内核未能启动,已恢复原内核'
        $f.Message | Should -Match 'KERNEL_BOOT_FAILED'
        $f.Message | Should -Not -Match '与 QTrade 内核无关'
        $f.Message | Should -Not -Match '本机 WSL2 无法启动'
    }

    It '回滚成功 + KERNEL_BOOT_TIMEOUT → 同样归到 QTrade 内核,原因码是 KERNEL_BOOT_TIMEOUT' {
        $ver = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_TIMEOUT'; Message = '' }
        $rb = [pscustomobject]@{ Ok = $true; Reason = ''; OfficialKernel = $script:Official; Stage = '' }
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_BOOT_TIMEOUT'
        $f.ExitName | Should -Be 'E_INSTALL_KERNEL_BOOT_TIMEOUT'
        $f.Message | Should -Match 'QTrade 内核未能启动,已恢复原内核'
        $f.Message | Should -Not -Match '与 QTrade 内核无关'
    }

    It '回滚成功 + KERNEL_NO_BINDER → 仍是 docs/03 的「切换失败(«原因»),已恢复原内核」' {
        $ver = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_NO_BINDER'; Message = '' }
        $rb = [pscustomobject]@{ Ok = $true; Reason = ''; OfficialKernel = $script:Official; Stage = '' }
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_NO_BINDER'
        $f.Message | Should -Match '^切换失败\(KERNEL_NO_BINDER\),已恢复原内核'
    }

    It 'failed rollback probe reports unverified recovery without generalizing to other distributions' {
        $ver = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_FAILED'; Message = 'x' }
        $rb = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_ROLLBACK_FAILED'; OfficialKernel = ''; Stage = 'verify' }
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_ROLLBACK_FAILED'
        $f.ExitName | Should -Be 'E_INSTALL_KERNEL_ROLLBACK_FAILED'
        $f.Message | Should -Match 'Kernel recovery is unverified'
        $f.Message | Should -Match 'does not establish whether other WSL distributions can start'
        $f.Message | Should -Match 'KERNEL_BOOT_FAILED'
    }

    It '回滚那次 shutdown 挂住(Stage=shutdown)→ 分不清原装内核好坏,不得说与 QTrade 内核无关' {
        $ver = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_BOOT_FAILED'; Message = 'x' }
        $rb = [pscustomobject]@{ Ok = $false; Reason = 'KERNEL_ROLLBACK_FAILED'; OfficialKernel = ''; Stage = 'shutdown' }
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_ROLLBACK_FAILED'
        $f.Message | Should -Not -Match '与 QTrade 内核无关'
        $f.Message | Should -Match 'kernel= 行'
    }
}

Describe 'B6 端到端(验证 → 回滚 → 文案),Invoke-QtWsl 打桩' {

    BeforeEach {
        Mock -ModuleName QTrade.Kernel Start-QtSleep { }
        Mock -ModuleName QTrade.Kernel Remove-QtKCheck { }
        Mock -ModuleName QTrade.Kernel Test-QtPath { $false }
        $script:Cfg = Join-Path $TestDrive 'e2e.wslconfig'
    }

    It 'QTrade 内核下 uname 不像版本串、回滚后原装内核正常 → 文案指向 QTrade 内核' {
        $script:Phase = 'qtrade'
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { $script:Phase = 'official'; return (New-R) }
            if ($script:Phase -eq 'qtrade') { return (New-R -Out $script:Hcs) }
            return (New-R -Out $script:Official)
        }
        $ver = Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -SkipShutdown
        $ver.Reason | Should -Be 'KERNEL_BOOT_FAILED'
        $rb = Invoke-QtKernelRollback -WslConfigPath $script:Cfg -ShutdownConfirmed $true
        $rb.Ok | Should -BeTrue
        $rb.Stage | Should -Be ''
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_BOOT_FAILED'
        $f.Message | Should -Match 'QTrade 内核未能启动,已恢复原内核'
        $f.Message | Should -Not -Match '与 QTrade 内核无关'
    }

    It 'invalid uname after rollback keeps verify failure and does not claim the kernel is unrelated' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return (New-R) }
            return (New-R -Out $script:Hcs)
        }
        $ver = Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -SkipShutdown
        $rb = Invoke-QtKernelRollback -WslConfigPath $script:Cfg -ShutdownConfirmed $true
        $rb.Ok | Should -BeFalse
        $rb.Stage | Should -Be 'verify'
        $f = Resolve-QtKernelSwitchFailure -Verify $ver -Rollback $rb
        $f.Reason | Should -Be 'KERNEL_ROLLBACK_FAILED'
        $f.Message | Should -Match 'Kernel recovery is unverified'
        $f.Message | Should -Match 'does not establish whether other WSL distributions can start'
    }

    It '回滚 shutdown 超时 → Stage=shutdown' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { New-R -Code -1 -TimedOut $true }
        (Invoke-QtKernelRollback -WslConfigPath $script:Cfg -ShutdownConfirmed $true).Stage | Should -Be 'shutdown'
    }
}

Describe 'run-step.ps1 KERNEL_SWITCH 段的接线(静态)' {

    BeforeAll {
        $src = [IO.File]::ReadAllText($script:RunStep)
        $start = $src.IndexOf("'KERNEL_SWITCH' {")
        $end = $src.IndexOf("'verify-kernel' {")
        $script:KsBlock = $src.Substring($start, $end - $start)
    }

    It '文案走 Resolve-QtKernelSwitchFailure,不再直接拿验证段的 Message 当结论' {
        $script:KsBlock | Should -Match 'Resolve-QtKernelSwitchFailure -Verify \$ver -Rollback \$rb'
        $script:KsBlock | Should -Not -Match '\$human\s*=\s*\[string\]\$ver\.Message'
    }

    It 'dmesg 在回滚(回滚内部会注销 kcheck)之前抓,且写进 $paths.Logs' {
        $iSave = $script:KsBlock.IndexOf('Save-QtKCheckDmesg -Directory $paths.Logs')
        $iRb = $script:KsBlock.IndexOf('Invoke-QtKernelRollback')
        $iSave | Should -BeGreaterThan -1
        $iSave | Should -BeLessThan $iRb
    }
}

Describe '预演 #16:Save-QtKCheckDmesg' {

    It '以 root 在 kcheck 里跑 dmesg | tail -200,落成 kcheck-dmesg-«ts».txt' {
        $script:Seen = $null
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { $script:Seen = $WslArgs; New-R -Out "[    0.000000] Linux version 6.6`n[ 1.0] binder: loaded" }
        $dir = Join-Path $TestDrive 'logs1'
        $p = Save-QtKCheckDmesg -Directory $dir -Phase 'KERNEL_SWITCH KERNEL_NO_BINDER' -Stamp '20260926-120000'
        $p | Should -Be (Join-Path $dir 'kcheck-dmesg-20260926-120000.txt')
        ($script:Seen -join ' ') | Should -Match '^-d qtrade-kcheck --user root --exec sh -c dmesg \| tail -200$'
        $text = [IO.File]::ReadAllText($p)
        $text | Should -Match 'binder: loaded'
        $text | Should -Match 'KERNEL_SWITCH KERNEL_NO_BINDER'
    }

    It '超时不抛,文件里写明抓取超时' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { New-R -Code -1 -TimedOut $true }
        $p = Save-QtKCheckDmesg -Directory (Join-Path $TestDrive 'logs2') -Stamp 't'
        [IO.File]::ReadAllText($p) | Should -Match 'collection timed out after 30 s'
        [IO.File]::ReadAllText($p) | Should -Match 'timed_out=True'
    }

    It 'wsl 调用直接抛异常 → 回空串,不打断失败路径' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl { throw 'wsl.exe 不存在' }
        { Save-QtKCheckDmesg -Directory (Join-Path $TestDrive 'logs3') } | Should -Not -Throw
        Save-QtKCheckDmesg -Directory (Join-Path $TestDrive 'logs3') | Should -Be ''
    }
}

Describe '预演 #16:诊断包带上注销前抓的 dmesg,不再只写占位句' {

    It 'kcheck 已注销、日志目录有 kcheck-dmesg-*.txt → 包里有原文件,kcheck-dmesg.txt 含其内容' {
        $root = Join-Path ([IO.Path]::GetTempPath()) ('qt-kfail-diag-' + [Guid]::NewGuid().ToString('N'))
        foreach ($d in @('install', 'logs', 'kernel')) { New-Item -ItemType Directory -Path (Join-Path $root $d) -Force | Out-Null }
        [IO.File]::WriteAllText((Join-Path $root 'install\install_state.json'), '{"state":"FAILED"}')
        [IO.File]::WriteAllText((Join-Path $root 'logs\kcheck-dmesg-20260926-120000.txt'), "# head`nMARK-DMESG-LINE hv_vmbus: probe failed")
        Mock -ModuleName QTrade.Diag Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Diag Get-WinEvent { throw 'log missing' }
        Mock -ModuleName QTrade.Diag Get-QtOptionalFeatureState { 'Enabled' }
        Mock -ModuleName QTrade.Diag Get-QtRegistryValue { $null }
        Mock -ModuleName QTrade.Diag Get-QtEnvironmentPath { $root }
        try {
            $r = New-QtDiagBundle -Root $root -Stamp 'kfail' -IncludeDmesg $true
            $r.Included | Should -Contain 'logs/kcheck-dmesg-20260926-120000.txt'
            $r.Included | Should -Contain 'kcheck-dmesg'
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($r.ZipPath)
            try {
                $names = @($zip.Entries | ForEach-Object { $_.FullName -replace '\\', '/' })
                $names | Should -Contain 'logs/kcheck-dmesg-20260926-120000.txt'
                $entry = $zip.Entries | Where-Object { $_.Name -eq 'kcheck-dmesg.txt' } | Select-Object -First 1
                $reader = New-Object IO.StreamReader($entry.Open())
                try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
                $text | Should -Match '采集时发行版已注销'
                $text | Should -Match 'MARK-DMESG-LINE'
            } finally { $zip.Dispose() }
        } finally {
            Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
