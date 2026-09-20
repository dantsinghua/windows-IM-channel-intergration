# Pester 5 —— install_state.json / 可重入 / 幂等判据(docs/03 §2.3、§2.12、§3.3)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Exit.psm1') -DisableNameChecking
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Native.psm1') -DisableNameChecking
    Import-Module (Join-Path $script:ModulesDir 'QTrade.State.psm1') -DisableNameChecking
}

Describe 'install_state —— schema 与原子写' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-state-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
        $script:StateFile = Join-Path $script:Tmp 'install_state.json'
    }
    AfterEach {
        if (Test-Path -LiteralPath $script:Tmp) { Remove-Item -LiteralPath $script:Tmp -Recurse -Force }
    }

    It '新建的 state 是 §2.3 schema 的形状' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $s.schema | Should -Be 1
        $s.package_version | Should -Be '1.0.0'
        $s.state | Should -Be 'PRECHECK'
        $s.substate | Should -Be ''
        $s.parked | Should -BeNullOrEmpty
        $s.resume.runonce_armed | Should -BeFalse
    }

    It '写 → 读回一致,且落盘不留 .tmp(原子写)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtState -State $s -To 'PAYLOAD_STAGED' | Out-Null
        Write-QtInstallState -State $s -Path $script:StateFile | Out-Null
        (Test-Path -LiteralPath ($script:StateFile + '.tmp')) | Should -BeFalse
        $r = Read-QtInstallState -Path $script:StateFile
        $r.state | Should -Be 'PAYLOAD_STAGED'
        $r.package_version | Should -Be '1.0.0'
    }

    It '第二次写替换而非报错(可重入重跑)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Write-QtInstallState -State $s -Path $script:StateFile | Out-Null
        Set-QtState -State $s -To 'WSL_FEATURE' | Out-Null
        Write-QtInstallState -State $s -Path $script:StateFile | Out-Null
        (Read-QtInstallState -Path $script:StateFile).state | Should -Be 'WSL_FEATURE'
    }

    It '文件不存在时读回 $null(首次运行)' {
        Read-QtInstallState -Path (Join-Path $script:Tmp 'nope.json') | Should -BeNullOrEmpty
    }

    It '每次迁状态都记一行 history,并清 substate' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtSubstate -State $s -Substate 'features_enabled' | Out-Null
        Set-QtState -State $s -To 'WSL_FEATURE' -Note 'enabled' | Out-Null
        $s.substate | Should -Be ''
        @($s.history).Count | Should -Be 1
        $s.history[0].from | Should -Be 'PRECHECK'
        $s.history[0].to | Should -Be 'WSL_FEATURE'
        $s.history[0].note | Should -Be 'enabled'
    }

    It '可以迁到 FAILED:«步»:«码»' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtState -State $s -To (New-QtFailedState -Step 'PRECHECK' -Reason 'DISK_LOW') | Out-Null
        $s.state | Should -Be 'FAILED:PRECHECK:DISK_LOW'
    }

    It '未知状态名被拒(防止写出基线 §8.2 之外的键名)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        { Set-QtState -State $s -To 'HALF_DONE' } | Should -Throw
    }
}

Describe 'parked —— 停车不是状态(§2.3 图末行,C-36)' {

    It '停车只写 parked 字段,state 不变' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtState -State $s -To 'KERNEL_STAGED' | Out-Null
        Set-QtParked -State $s -Step 'WSLCONFIG_WRITTEN' -Reason 'WAIT_SHUTDOWN_CONFIRM' | Out-Null
        $s.state | Should -Be 'KERNEL_STAGED'
        (Test-QtParked -State $s) | Should -BeTrue
        $s.parked.step | Should -Be 'WSLCONFIG_WRITTEN'
        $s.parked.reason | Should -Be 'WAIT_SHUTDOWN_CONFIRM'
    }

    It '清停车' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtParked -State $s -Step 'CLIENTS_CHECKED' -Reason 'WAIT_WECHAT_CLOSE' | Out-Null
        Clear-QtParked -State $s | Out-Null
        (Test-QtParked -State $s) | Should -BeFalse
    }
}

Describe '可重入 —— Get-QtResumeStep(§2.3 可重入总则 / §2.12)' {

    It 'FAILED:«步» → 重做该步' {
        Get-QtResumeStep -State 'FAILED:IMAGES_LOADED:DISK_FULL' | Should -Be 'IMAGES_LOADED'
        Get-QtResumeStep -State 'FAILED:PRECHECK:DISK_LOW' | Should -Be 'PRECHECK'
    }

    It '🔴 KERNEL_ROLLED_BACK → 回到 WSLCONFIG_WRITTEN(用户点【重试切换】走 §2.6.3 确认页)' {
        Get-QtResumeStep -State 'KERNEL_ROLLED_BACK' | Should -Be 'WSLCONFIG_WRITTEN'
    }

    It '🔴 REBOOT_PENDING → 回 WSL_FEATURE 复核(§2.5.1 续跑入口先复核两功能)' {
        Get-QtResumeStep -State 'REBOOT_PENDING' | Should -Be 'WSL_FEATURE'
    }

    It 'DONE → 没有下一步' {
        Get-QtResumeStep -State 'DONE' | Should -BeNullOrEmpty
    }

    It '正常状态沿链前进' -ForEach @(
        @{ From = 'PRECHECK'; To = 'PAYLOAD_STAGED' }
        @{ From = 'PAYLOAD_STAGED'; To = 'WSL_FEATURE' }
        @{ From = 'WSL_FEATURE'; To = 'WSL_MSI' }
        @{ From = 'WSL_MSI'; To = 'KERNEL_STAGED' }
        @{ From = 'KERNEL_STAGED'; To = 'WSLCONFIG_WRITTEN' }
        @{ From = 'WSLCONFIG_WRITTEN'; To = 'KERNEL_VERIFIED' }
        @{ From = 'KERNEL_VERIFIED'; To = 'DISTRO_IMPORTED' }
        @{ From = 'DISTRO_IMPORTED'; To = 'IMAGES_LOADED' }
        @{ From = 'IMAGES_LOADED'; To = 'WINAGENT_INSTALLED' }
        @{ From = 'WINAGENT_INSTALLED'; To = 'CLIENTS_CHECKED' }
        @{ From = 'CLIENTS_CHECKED'; To = 'SELFTEST_OK' }
        @{ From = 'SELFTEST_OK'; To = 'DONE' }
    ) {
        Get-QtResumeStep -State $From | Should -Be $To
    }

    It 'Split-QtFailedState 只认 FAILED 形态' {
        (Split-QtFailedState -State 'DONE') | Should -BeNullOrEmpty
        (Split-QtFailedState -State 'FAILED:WSL_MSI:WSL_BROKEN').Step | Should -Be 'WSL_MSI'
        (Split-QtFailedState -State 'FAILED:WSL_MSI:WSL_BROKEN').Reason | Should -Be 'WSL_BROKEN'
    }
}

Describe '幂等判据注册表(§2.3「已完成判据」列)' {

    BeforeEach { Clear-QtStepChecks }
    AfterEach { Clear-QtStepChecks }

    It '🔴 PRECHECK 无判据,恒回 false(§2.3:「无(每次都重跑,便宜)」)' {
        Register-QtStepCheck -Step 'PRECHECK' -Check { param($c) $true }   # 即便有人误注册
        Test-QtStepComplete -Step 'PRECHECK' -Context ([pscustomobject]@{}) | Should -BeFalse
    }

    It '未注册判据的步回 false(宁可重做,不可误 skip)' {
        Test-QtStepComplete -Step 'SELFTEST_OK' -Context ([pscustomobject]@{}) | Should -BeFalse
    }

    It '注册的判据被调用,且拿到 context' {
        Register-QtStepCheck -Step 'DISTRO_IMPORTED' -Check { param($c) return $c.Imported }
        Test-QtStepComplete -Step 'DISTRO_IMPORTED' -Context ([pscustomobject]@{ Imported = $true }) | Should -BeTrue
        Test-QtStepComplete -Step 'DISTRO_IMPORTED' -Context ([pscustomobject]@{ Imported = $false }) | Should -BeFalse
    }

    It '注册未知步名被拒' {
        { Register-QtStepCheck -Step 'NOPE' -Check { $true } } | Should -Throw
    }
}

Describe 'RunOnce —— 恒指向落盘引擎(§2.12)' {

    It '值写成 "«engine»" /QT_MODE=resume' {
        Mock -ModuleName QTrade.State Set-QtRegistryValue { }
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $v = Set-QtRunOnce -State $s -EnginePath 'C:\ProgramData\QTrade\install\engine\qtrade-setup-engine.exe'
        $v | Should -Be '"C:\ProgramData\QTrade\install\engine\qtrade-setup-engine.exe" /QT_MODE=resume'
        $s.resume.runonce_armed | Should -BeTrue
    }

    It '断电保险模式写 /QT_MODE=verify-kernel(§2.6.3)' {
        Mock -ModuleName QTrade.State Set-QtRegistryValue { }
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $v = Set-QtRunOnce -State $s -EnginePath 'E:\e.exe' -Mode 'verify-kernel'
        $v | Should -Be '"E:\e.exe" /QT_MODE=verify-kernel'
    }

    It '清 RunOnce 后 runonce_armed=false' {
        Mock -ModuleName QTrade.State Set-QtRegistryValue { }
        Mock -ModuleName QTrade.State Remove-QtRegistryValue { }
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtRunOnce -State $s -EnginePath 'E:\e.exe' | Out-Null
        Clear-QtRunOnce -State $s | Out-Null
        $s.resume.runonce_armed | Should -BeFalse
    }
}

Describe '镜像进 winagent.db(§3.3,C-36:单行 JSON 列,整体覆盖)' {

    It '列名与 §3.3 一一对应' {
        $s = New-QtInstallState -PackageVersion '1.2.3'
        Set-QtState -State $s -To 'KERNEL_VERIFIED' | Out-Null
        $row = ConvertTo-QtWinAgentRow -State $s
        @($row.Keys) | Should -Be @('key', 'state', 'substate', 'parked_json', 'package_version',
            'env_json', 'wslconfig_json', 'distro_json', 'clients_json', 'resume_json', 'updated_ms')
        $row['key'] | Should -Be 'current'
        $row['state'] | Should -Be 'KERNEL_VERIFIED'
        $row['package_version'] | Should -Be '1.2.3'
        $row['parked_json'] | Should -Be 'null'
    }

    It 'parked 非空时序列化成 JSON 而不是丢掉' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtParked -State $s -Step 'WSLCONFIG_WRITTEN' -Reason 'WAIT_SHUTDOWN_CONFIRM' | Out-Null
        $row = ConvertTo-QtWinAgentRow -State $s
        $row['parked_json'] | Should -Match 'WAIT_SHUTDOWN_CONFIRM'
    }
}

Describe '目录布局(基线 §4)' {

    It 'Get-QtPaths 给出 §4 的固定子目录' {
        $p = Get-QtPaths -Root 'C:\ProgramData\QTrade'
        $p.Install | Should -Be 'C:\ProgramData\QTrade\install'
        $p.Engine | Should -Be 'C:\ProgramData\QTrade\install\engine'
        $p.Kernel | Should -Be 'C:\ProgramData\QTrade\kernel'
        $p.Wsl | Should -Be 'C:\ProgramData\QTrade\wsl'
        $p.KCheck | Should -Be 'C:\ProgramData\QTrade\wsl\kcheck'
        $p.StateFile | Should -Be 'C:\ProgramData\QTrade\install\install_state.json'
        $p.Manifest | Should -Be 'C:\ProgramData\QTrade\install\manifest.json'
        $p.KernelPtr | Should -Be 'C:\ProgramData\QTrade\kernel\current.json'
    }
}
