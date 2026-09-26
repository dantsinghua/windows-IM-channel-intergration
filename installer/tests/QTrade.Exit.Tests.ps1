# Pester 5 —— 退出码与状态机键名(docs/03 §3.4 / §2.3)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Exit.psm1') -DisableNameChecking
}

Describe 'QTrade.Exit —— 退出码表' {

    It '码互不重复,且覆盖两端边界' {
        # 不再钉「条目数」这个魔法数字:它随规格增长而变,写死之后每加一个码就要手工改,
        # 而且用例名(45)和断言(46)早就对不上了 —— 这种数字不保护任何东西。
        # 「与 docs/03 §3.4 逐条一致」由 python 对账测试负责(它是逐名逐码比的);
        # 这里只守模块自己的不变量:码不重复、两端边界在表里。
        $t = Get-QtExitTable
        $codes = @($t.Values)
        $codes.Count | Should -BeGreaterThan 40
        ($codes | Select-Object -Unique).Count | Should -Be $codes.Count
        $t['OK'] | Should -Be 0
        $t['E_INSTALL_REBOOT_REQUIRED'] | Should -Be 3010
    }

    It 'docs/03 §3.4 的关键码逐条对齐' -ForEach @(
        @{ Name = 'OK'; Code = 0 }
        @{ Name = 'E_INSTALL_WAIT_USER'; Code = 10 }
        @{ Name = 'E_INSTALL_CANCELLED'; Code = 11 }
        @{ Name = 'E_INSTALL_REBOOT_REQUIRED'; Code = 3010 }
        @{ Name = 'E_INSTALL_DISK_LOW'; Code = 26 }
        @{ Name = 'E_INSTALL_ALREADY_RUNNING'; Code = 29 }
        @{ Name = 'E_INSTALL_KERNEL_NO_BINDER'; Code = 64 }
        @{ Name = 'E_INSTALL_KERNEL_ROLLBACK_FAILED'; Code = 65 }
        @{ Name = 'E_INSTALL_KERNEL_SHUTDOWN_TIMEOUT'; Code = 66 }
        @{ Name = 'E_INSTALL_KCHECK_IMPORT_FAILED'; Code = 67 }
        @{ Name = 'E_INSTALL_DOCKER_CIDR_EXHAUSTED'; Code = 76 }
        @{ Name = 'E_INSTALL_DOWNGRADE_REFUSED'; Code = 122 }
        @{ Name = 'E_INSTALL_DISK_FULL'; Code = 123 }
        @{ Name = 'E_INSTALL_INTERNAL'; Code = 200 }
    ) {
        Get-QtExitCode -Name $Name | Should -Be $Code
        Get-QtExitName -Code $Code | Should -Be $Name
    }

    It '原因码 = 退出码名去 E_INSTALL_ 前缀(§3.4 首句)' {
        Get-QtReasonCode -Name 'E_INSTALL_DISK_LOW' | Should -Be 'DISK_LOW'
        Get-QtReasonCode -Name 'DISK_LOW' | Should -Be 'DISK_LOW'
        Get-QtReasonCode -Name 'OK' | Should -Be 'OK'
    }

    It '原因码可以反查回码(两向一致)' {
        Get-QtExitCode -Name 'DISK_FULL' | Should -Be 123
        Get-QtExitCode -Name 'KERNEL_SHUTDOWN_TIMEOUT' | Should -Be 66
    }

    It '未知名抛错,不静默回 0' {
        { Get-QtExitCode -Name 'E_INSTALL_NOT_A_REAL_CODE' } | Should -Throw
    }

    It 'R6-73:表含 11 = E_INSTALL_CANCELLED,且与 10 WAIT_USER 是两个码' {
        (Get-QtExitTable)['E_INSTALL_CANCELLED'] | Should -Be 11
        Get-QtReasonCode -Name 'E_INSTALL_CANCELLED' | Should -Be 'CANCELLED'
        (Get-QtExitCode -Name 'CANCELLED') | Should -Not -Be (Get-QtExitCode -Name 'WAIT_USER')
    }

    It 'R6-73:.iss 的【取消】路径(RunStep 里 if Cancelled 分支)退 11,不再借 10' {
        $iss = Get-Content -Raw -Encoding UTF8 (Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\qtrade-setup-engine.iss')
        $iss | Should -Match '(?m)^\s*E_INSTALL_CANCELLED\s*=\s*11;'
        $i = $iss.IndexOf('if Cancelled then')
        $i | Should -BeGreaterThan 0
        $branch = $iss.Substring($i, $iss.IndexOf('Exit;', $i) - $i)
        $branch | Should -Match 'LastStepExit := E_INSTALL_CANCELLED;'
        $branch | Should -Match 'FailWith\(LastStepExit'
        $branch | Should -Not -Match 'LastStepExit := E_INSTALL_WAIT_USER'
    }

    It '🔴 26 DISK_LOW 与 123 DISK_FULL 是两个量,不得互相顶替(§3.4 第 123 行)' {
        (Get-QtExitCode -Name 'E_INSTALL_DISK_LOW') | Should -Not -Be (Get-QtExitCode -Name 'E_INSTALL_DISK_FULL')
    }
}

Describe 'QTrade.Exit —— 状态机键名(基线 §8.2 / docs/03 §2.3)' {

    It '15 个状态键名' {
        (Get-QtStateNames).Count | Should -Be 15
    }

    It 'KERNEL_ROLLED_BACK 是终态之一、不在顺序链上(§2.3 图)' {
        (Get-QtStateNames) | Should -Contain 'KERNEL_ROLLED_BACK'
        (Get-QtStateChain) | Should -Not -Contain 'KERNEL_ROLLED_BACK'
    }

    It 'REBOOT_PENDING 是状态但不在顺序链上(它是 WSL_FEATURE 的分支)' {
        (Get-QtStateNames) | Should -Contain 'REBOOT_PENDING'
        (Get-QtStateChain) | Should -Not -Contain 'REBOOT_PENDING'
    }

    It '链的首尾' {
        (Get-QtStateChain)[0] | Should -Be 'PRECHECK'
        (Get-QtStateChain)[-1] | Should -Be 'DONE'
    }

    It 'FAILED:«步名»:«原因码» 组装格式' {
        New-QtFailedState -Step 'KERNEL_VERIFIED' -Reason 'E_INSTALL_KERNEL_NO_BINDER' | Should -Be 'FAILED:KERNEL_VERIFIED:KERNEL_NO_BINDER'
        New-QtFailedState -Step 'IMAGES_LOADED' -Reason 'DISK_FULL' | Should -Be 'FAILED:IMAGES_LOADED:DISK_FULL'
    }

    It '未知步名抛错' {
        { New-QtFailedState -Step 'NOT_A_STEP' -Reason 'DISK_LOW' } | Should -Throw
    }

    It '/QT_MODE 六个取值(§3.4)' {
        (Get-QtModes) -join ',' | Should -Be 'install,resume,upgrade,repair,uninstall,verify-kernel'
    }
}
