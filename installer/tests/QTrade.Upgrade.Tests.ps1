# Pester 5 —— 升级(§2.13)与修复(§5.2);验收 M1-22 ~ M1-25
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl',
            'QTrade.Distro', 'QTrade.WinAgent', 'QTrade.Console', 'QTrade.Upgrade')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe '§2.13 版本闸(G-01 兼容矩阵末行)' {

    It '1.0.0 → 1.1.0 是升级' {
        (Compare-QtPackageVersion -Installed '1.0.0' -Package '1.1.0').Kind | Should -Be 'UPGRADE'
    }
    It '同版本 → SAME(走修复而不是升级)' {
        (Compare-QtPackageVersion -Installed '1.0.0' -Package '1.0.0').Kind | Should -Be 'SAME'
    }
    It '🔴 1.1.0 机上跑 1.0.0 包 → DOWNGRADE(验收 M1-24:退 122,文案指向卸载再装)' {
        $r = Compare-QtPackageVersion -Installed '1.1.0' -Package '1.0.0'
        $r.Kind | Should -Be 'DOWNGRADE'
        $r.Reason | Should -Be 'DOWNGRADE_REFUSED'
    }
    It '没有已装版本(首装残留)→ 当升级处理,不拦' {
        (Compare-QtPackageVersion -Installed '' -Package '1.0.0').Kind | Should -Be 'UPGRADE'
    }
    It '版本串解析不了 → SAME(宁可不动,不误降级)' {
        (Compare-QtPackageVersion -Installed 'dev' -Package '1.0.0').Kind | Should -Be 'SAME'
    }
}

Describe '§2.13 第 3~6 步「变了才做」' {

    It '全没变 → 只有固定四步' {
        $f = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i1' }
        $p = Get-QtUpgradePlan -Current $f -Next $f
        $p.Kernel | Should -BeFalse
        $p.Rootfs | Should -BeFalse
        $p.AgentCode | Should -BeFalse
        $p.Images | Should -BeFalse
        $p.Steps -join ',' | Should -Be 'drain,stage,triad,selftest'
    }

    It '🔴 内核 sha 变了才走 §2.6 全段(验收 M1-23:没变就不 shutdown、不打断用户发行版)' {
        $c = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i1' }
        $n = [pscustomobject]@{ kernel_sha256 = 'b'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i1' }
        (Get-QtUpgradePlan -Current $c -Next $n).Steps | Should -Contain 'kernel'
    }

    It '🔴 rootfs 变了 → 走 rootfs;此时**不**再单独覆盖 Agent(新 rootfs 已带新 Agent)' {
        $c = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i1' }
        $n = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r2'; agent_version = 'v2'; image_tar_sha256 = 'i1' }
        $p = Get-QtUpgradePlan -Current $c -Next $n
        $p.Rootfs | Should -BeTrue
        $p.AgentCode | Should -BeFalse
        $p.Steps | Should -Not -Contain 'agent_code'
    }

    It 'rootfs 没变但 Agent 版本变 → 只覆盖 /opt/qtrade/agent(§2.13 第 5 步)' {
        $c = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i1' }
        $n = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v2'; image_tar_sha256 = 'i1' }
        $p = Get-QtUpgradePlan -Current $c -Next $n
        $p.AgentCode | Should -BeTrue
        $p.Steps | Should -Contain 'agent_code'
    }

    It '镜像 tar 变了 → docker load' {
        $c = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i1' }
        $n = [pscustomobject]@{ kernel_sha256 = 'a'; rootfs_version = 'r1'; agent_version = 'v1'; image_tar_sha256 = 'i2' }
        (Get-QtUpgradePlan -Current $c -Next $n).Steps | Should -Contain 'images'
    }

    It '指纹字段缺失 → 当成「没变」,不误触发换件' {
        $p = Get-QtUpgradePlan -Current ([pscustomobject]@{}) -Next ([pscustomobject]@{})
        $p.Kernel | Should -BeFalse
        $p.Rootfs | Should -BeFalse
    }

    It 'schema_breaking 透传' {
        (Get-QtUpgradePlan -Current ([pscustomobject]@{}) -Next ([pscustomobject]@{ schema_breaking = $true })).SchemaBreaking | Should -BeTrue
    }
}

Describe '🔴 §2.13 schema 迁移边界(A-5;验收 M1-22 / M1-22b)' {

    It 'schema_breaking=false → 向导**不得出现任何「清数据」字样**' {
        $p = Get-QtSchemaBreakingPrompt -SchemaBreaking $false
        $p.RequireConfirm | Should -BeFalse
        $p.Text | Should -Be ''
    }
    It 'schema_breaking=true → 明示 + 二次确认 + 先备份' {
        $p = Get-QtSchemaBreakingPrompt -SchemaBreaking $true
        $p.RequireConfirm | Should -BeTrue
        $p.Text | Should -Match '重建数据'
        $p.Text | Should -Match '备份'
    }
}

Describe '🔴 G-01 三件套停/起顺序(验收 M1-25 抓时间序)' {

    It '停:控制台 → 会话代理 → 服务 → Agent' {
        (Get-QtTriadStopOrder) -join ',' | Should -Be 'console,winagent_user,winagent_svc,agent'
    }
    It '起:服务 → 会话代理 → Agent → 控制台(控制台**最后**)' {
        (Get-QtTriadStartOrder) -join ',' | Should -Be 'winagent_svc,winagent_user,agent,console'
    }
    It '🔴 真正的不变量:控制台**第一个停、最后一个起**(它依赖另外三个)' {
        # ⚠️ 起顺序**不是**停顺序的严格逆序 —— Agent 是在 §2.13 第 4/5 步换的,
        #    不参与第 7 步三件套的停/起舞步。规格只约束依赖方向,别硬套「逆序」。
        $stop = ((Get-QtTriadStopOrder) -join ',').Split(',')
        $start = ((Get-QtTriadStartOrder) -join ',').Split(',')
        $stop[0] | Should -Be 'console'
        $start[$start.Count - 1] | Should -Be 'console'
    }

    It '🔴 服务先于会话代理起(会话代理依赖服务),服务后于会话代理停' {
        $stop = ((Get-QtTriadStopOrder) -join ',').Split(',')
        $start = ((Get-QtTriadStartOrder) -join ',').Split(',')
        [array]::IndexOf($start, 'winagent_svc') | Should -BeLessThan ([array]::IndexOf($start, 'winagent_user'))
        [array]::IndexOf($stop, 'winagent_user') | Should -BeLessThan ([array]::IndexOf($stop, 'winagent_svc'))
    }

    It '两张表元素相同(只是次序不同)' {
        (((Get-QtTriadStopOrder) -join ',').Split(',') | Sort-Object) -join ',' |
            Should -Be ((((Get-QtTriadStartOrder) -join ',').Split(',') | Sort-Object) -join ',')
    }

    It 'Stop-QtTriad 先停控制台、再停计划任务、最后停服务' {
        $script:Seq = @()
        Mock -ModuleName QTrade.Upgrade Stop-QtConsole { $script:Seq += 'console'; [pscustomobject]@{ Ok = $true; Killed = $false } }
        Mock -ModuleName QTrade.Upgrade Disable-ScheduledTask { $script:Seq += 'task' }
        Mock -ModuleName QTrade.Upgrade Invoke-QtSc { $script:Seq += ('sc:' + ($ScArgs -join ' ')); [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Stop-QtTriad | Out-Null
        $script:Seq[0] | Should -Be 'console'
        $script:Seq[1] | Should -Be 'task'
        $script:Seq[2] | Should -Match '^sc:stop'
    }

    It 'Start-QtTriad **最后**才拉起控制台' {
        $script:Seq2 = @()
        Mock -ModuleName QTrade.Upgrade Invoke-QtSc { $script:Seq2 += 'sc:start'; [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Upgrade Enable-ScheduledTask { $script:Seq2 += 'task-enable' }
        Mock -ModuleName QTrade.Upgrade Start-QtScheduledTask { $script:Seq2 += 'task-start' }
        Mock -ModuleName QTrade.Upgrade Wait-QtWinAgentHealthy { $script:Seq2 += 'health'; [pscustomobject]@{ Ok = $true; UserAgent = $true; Reason = '' } }
        Mock -ModuleName QTrade.Upgrade Test-QtPath { $true }
        Mock -ModuleName QTrade.Upgrade Start-Process { $script:Seq2 += 'console' }
        Start-QtTriad -ConsoleExePath 'X:\QTrade.exe' | Out-Null
        $script:Seq2[-1] | Should -Be 'console'
        $script:Seq2 | Should -Contain 'health'
    }
}

Describe '🔴 §2.13 第 1 步 drain 要用户确认(红线 6:停容器会中断账号)' {

    It '未确认 → 抛,且**一次 HTTP 都不发**' {
        Mock -ModuleName QTrade.Upgrade Invoke-QtHttp { throw '不该被调用' }
        { Invoke-QtDrain -Confirmed $false } | Should -Throw
        Should -Invoke -ModuleName QTrade.Upgrade Invoke-QtHttp -Times 0
    }
    It '已确认 → 调 POST /api/v1/system/drain' {
        Mock -ModuleName QTrade.Upgrade Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 202; Body = '{}' } }
        (Invoke-QtDrain -Confirmed $true).Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Upgrade Invoke-QtHttp -Times 1 -ParameterFilter { $Uri -like '*/api/v1/system/drain' }
    }
}

Describe '🔴 §2.13 第 4 步:没备份成功绝不 --unregister' {

    It 'tar 失败 → UPGRADE_DATA_BACKUP_FAILED(120),不回 tar 路径' {
        Mock -ModuleName QTrade.Upgrade New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Upgrade Invoke-QtWsl { [pscustomobject]@{ ExitCode = 2; StdOut = ''; StdErr = 'no space'; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Upgrade Test-QtPath { $false }
        $r = Backup-QtAgentData -WslDir 'X:\wsl'
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'UPGRADE_DATA_BACKUP_FAILED'
        $r.TarPath | Should -Be ''
    }

    It 'tar 退出 0 但文件没落盘 → 同样判失败' {
        Mock -ModuleName QTrade.Upgrade New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Upgrade Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Upgrade Test-QtPath { $false }
        (Backup-QtAgentData -WslDir 'X:\wsl').Ok | Should -BeFalse
    }

    It 'tar 成功 → 回 data-backup-<ts>.tar' {
        Mock -ModuleName QTrade.Upgrade New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Upgrade Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Upgrade Test-QtPath { $true }
        (Backup-QtAgentData -WslDir 'X:\wsl' -Stamp '20260920-191500').TarPath | Should -Match 'data-backup-20260920-191500\.tar$'
    }
}

Describe 'Windows 路径 → WSL 路径(盘符不写死 c)' {

    It '按盘符换算' -ForEach @(
        @{ W = 'C:\ProgramData\QTrade\wsl\x.tar'; L = '/mnt/c/ProgramData/QTrade/wsl/x.tar' }
        @{ W = 'D:\QTrade\y.tar'; L = '/mnt/d/QTrade/y.tar' }
        @{ W = 'E:\a b\c.tar'; L = '/mnt/e/a b/c.tar' }
    ) { ConvertTo-QtWslPath -WindowsPath $W | Should -Be $L }

    It '已经是 POSIX 路径就只换分隔符' {
        ConvertTo-QtWslPath -WindowsPath '/var/lib/qtrade' | Should -Be '/var/lib/qtrade'
    }
}

Describe '§5.2 修复' {

    It '导入 + 首启 + 从最近备份恢复' {
        Mock -ModuleName QTrade.Upgrade Import-QtDistro { [pscustomobject]@{ Ok = $true; Reason = ''; ExitCode = 0 } }
        Mock -ModuleName QTrade.Upgrade Wait-QtDistroSystemd { [pscustomobject]@{ Ok = $true; Status = 'running'; Degraded = $false } }
        Mock -ModuleName QTrade.Upgrade Get-QtLatestDataBackup { 'X:\wsl\data-backup-1.tar' }
        Mock -ModuleName QTrade.Upgrade Restore-QtAgentData { [pscustomobject]@{ Ok = $true; Reason = '' } }
        $r = Invoke-QtRepair -DistroDir 'X:\d' -RootfsTar 'X:\r.tar' -WslDir 'X:\wsl'
        $r.Ok | Should -BeTrue
        $r.Restored | Should -BeTrue
        $r.BackupUsed | Should -Be 'X:\wsl\data-backup-1.tar'
    }

    It '🔴 没有备份不算失败(§5.2「若有」)—— 修复出一个干净发行版' {
        Mock -ModuleName QTrade.Upgrade Import-QtDistro { [pscustomobject]@{ Ok = $true; Reason = ''; ExitCode = 0 } }
        Mock -ModuleName QTrade.Upgrade Wait-QtDistroSystemd { [pscustomobject]@{ Ok = $true; Status = 'running'; Degraded = $false } }
        Mock -ModuleName QTrade.Upgrade Get-QtLatestDataBackup { '' }
        $r = Invoke-QtRepair -DistroDir 'X:\d' -RootfsTar 'X:\r.tar' -WslDir 'X:\wsl'
        $r.Ok | Should -BeTrue
        $r.Restored | Should -BeFalse
    }

    It '导入失败 → 带回原因码' {
        Mock -ModuleName QTrade.Upgrade Import-QtDistro { [pscustomobject]@{ Ok = $false; Reason = 'DISK_FULL'; ExitCode = 1 } }
        (Invoke-QtRepair -DistroDir 'X:\d' -RootfsTar 'X:\r.tar' -WslDir 'X:\wsl').Reason | Should -Be 'DISK_FULL'
    }

    It 'systemd 没起 → SYSTEMD_NOT_READY' {
        Mock -ModuleName QTrade.Upgrade Import-QtDistro { [pscustomobject]@{ Ok = $true; Reason = ''; ExitCode = 0 } }
        Mock -ModuleName QTrade.Upgrade Wait-QtDistroSystemd { [pscustomobject]@{ Ok = $false; Status = 'timeout'; Degraded = $false } }
        (Invoke-QtRepair -DistroDir 'X:\d' -RootfsTar 'X:\r.tar' -WslDir 'X:\wsl').Reason | Should -Be 'SYSTEMD_NOT_READY'
    }

    It '恢复指定备份 → 用它,不去找最近的' {
        Mock -ModuleName QTrade.Upgrade Import-QtDistro { [pscustomobject]@{ Ok = $true; Reason = ''; ExitCode = 0 } }
        Mock -ModuleName QTrade.Upgrade Wait-QtDistroSystemd { [pscustomobject]@{ Ok = $true; Status = 'running'; Degraded = $false } }
        Mock -ModuleName QTrade.Upgrade Get-QtLatestDataBackup { throw '不该被调用' }
        Mock -ModuleName QTrade.Upgrade Restore-QtAgentData { [pscustomobject]@{ Ok = $true; Reason = '' } }
        (Invoke-QtRepair -DistroDir 'X:\d' -RootfsTar 'X:\r.tar' -WslDir 'X:\wsl' -RestoreDataTar 'X:\pick.tar').BackupUsed | Should -Be 'X:\pick.tar'
    }
}

Describe '🔴 §2.13 升级保留清单(A-5;任一被清空都是缺陷)' {

    It '七项都在,且各自写明了「怎么保」' {
        $l = Get-QtUpgradeRetainList
        $l.Count | Should -Be 7
        foreach ($row in $l) {
            $row.item | Should -Not -BeNullOrEmpty
            $row.how | Should -Not -BeNullOrEmpty
            $row.where | Should -BeIn @('distro', 'windows')
        }
        ($l | ForEach-Object { $_.item }) -join ' ' | Should -Match '/var/lib/qtrade'
        ($l | ForEach-Object { $_.item }) -join ' ' | Should -Match 'Vault'
        ($l | ForEach-Object { $_.item }) -join ' ' | Should -Match 'winagent\.db'
        ($l | ForEach-Object { $_.item }) -join ' ' | Should -Match '\.wslconfig'
    }

    It '🔴 微信那条明写「升级从不碰、也不运行 Uninstall.exe」' {
        $wx = @((Get-QtUpgradeRetainList) | Where-Object { $_.item -match '微信' })
        $wx.Count | Should -Be 1
        $wx[0].how | Should -Match 'Uninstall\.exe'
    }
}
