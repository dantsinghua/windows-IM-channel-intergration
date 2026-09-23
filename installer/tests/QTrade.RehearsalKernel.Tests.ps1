# Pester 5 —— 预演:WSL 起不来不得记成 KERNEL_NO_BINDER
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl', 'QTrade.Kernel')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
    $script:Ver = '6.6.123.2-microsoft-standard-WSL2-binder'
    $script:Hcs = "没有收到虚拟机或容器的回应，操作超时。`r`n错误代码: Wsl/Service/CreateInstance/HCS_E_CONNECTION_TIMEOUT"
}

Describe '预演:uname 不是版本串' {

    It 'HCS 超时文案、退出码 0 → KERNEL_BOOT_FAILED,且说明本机 WSL2 无法启动' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = $script:Hcs; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $r = Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -SkipShutdown
        $r.Reason | Should -Not -Be 'KERNEL_NO_BINDER'
        $r.Reason | Should -Be 'KERNEL_BOOT_FAILED'
        $r.Message | Should -Match '本机 WSL2 无法启动'
    }

    It '回滚时超时文案不得当成 OfficialKernel' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = $script:Hcs; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        Mock -ModuleName QTrade.Kernel Remove-QtKCheck { }
        Mock -ModuleName QTrade.Kernel Test-QtPath { $false }
        $cfg = Join-Path $TestDrive 'none.wslconfig'
        $r = Invoke-QtKernelRollback -WslConfigPath $cfg -ShutdownConfirmed $true -KCheckDirectory (Join-Path $TestDrive 'k')
        [string]$r.OfficialKernel | Should -BeNullOrEmpty
        [string]$r.OfficialKernel | Should -Not -Be $script:Hcs
    }

    It '真版本串与 manifest 不符 → 仍是 KERNEL_NO_BINDER' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            if ($WslArgs -contains '--shutdown') { return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            if ($WslArgs -contains 'uname') { return [pscustomobject]@{ ExitCode = 0; StdOut = '6.6.0-fake'; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
            return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -SkipShutdown).Reason | Should -Be 'KERNEL_NO_BINDER'
    }

    It '退出码非 0 → KERNEL_BOOT_FAILED' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            return [pscustomobject]@{ ExitCode = 1; StdOut = 'boom'; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $r = Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -SkipShutdown
        $r.Reason | Should -Be 'KERNEL_BOOT_FAILED'
        $r.Reason | Should -Not -Be 'KERNEL_NO_BINDER'
    }

    It 'TimedOut → KERNEL_BOOT_TIMEOUT' {
        Mock -ModuleName QTrade.Kernel Invoke-QtWsl {
            return [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = 1 }
        }
        (Invoke-QtKernelVerify -ManifestVersion $script:Ver -ShutdownConfirmed $true -SkipShutdown).Reason | Should -Be 'KERNEL_BOOT_TIMEOUT'
    }
}
