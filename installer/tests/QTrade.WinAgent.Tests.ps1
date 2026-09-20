# Pester 5 —— WinAgent 安装 / 健康门 / 防火墙 / 日志 / 卸载(docs/03 §2.8、§2.11、§2.14、§3.1、§6)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log',
            'QTrade.Wsl', 'QTrade.Kernel', 'QTrade.WinAgent', 'QTrade.Firewall',
            'QTrade.Console', 'QTrade.WeChat', 'QTrade.Uninstall')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe 'R-14 实名(§2.8.1:会话代理不开任何入站口)' {

    It '两个可执行体名字' {
        $n = Get-QtWinAgentNames
        $n.svc_exe | Should -Be 'qtrade-winagent-svc.exe'
        $n.user_exe | Should -Be 'qtrade-winagent-user.exe'
        $n.service_name | Should -Be 'QTradeWinAgent'
        $n.task_path | Should -Be '\QTrade\'
        $n.task_name | Should -Be 'WinAgentUser'
        $n.base_url | Should -Be 'http://127.0.0.1:17610'
    }
}

Describe 'VC++ 运行库退出码(§2.8.3 第 1 步)' {

    It '0 成功 / 1638 已装更高版本算成功 / 3010 记重启但不阻断 / 其它失败' -ForEach @(
        @{ Code = 0; Ok = $true; Reboot = $false }
        @{ Code = 1638; Ok = $true; Reboot = $false }
        @{ Code = 3010; Ok = $true; Reboot = $true }
        @{ Code = 1603; Ok = $false; Reboot = $false }
    ) {
        Mock -ModuleName QTrade.WinAgent Test-QtPath { $true }
        Mock -ModuleName QTrade.WinAgent Invoke-QtProcess { [pscustomobject]@{ ExitCode = $Code; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        $r = Install-QtVcRedist -ExePath 'X:\VC_redist.x64.exe'
        $r.Ok | Should -Be $Ok
        $r.RebootPending | Should -Be $Reboot
    }
}

Describe '服务注册用 sc.exe(§2.15:New-Service 不设恢复策略)' {

    It '四条命令:create / description / failure / failureflag,且 start= delayed-auto' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtSc { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        $r = Install-QtWinAgentService -ExePath 'C:\ProgramData\QTrade\winagent\app\qtrade-winagent-svc.exe'
        $r.Ok | Should -BeTrue
        $r.Commands.Count | Should -Be 4
        $r.Commands[0] | Should -Match 'start= delayed-auto'
        $r.Commands[0] | Should -Match 'obj= LocalSystem'
        $r.Commands[0] | Should -Match 'depend= LxssManager'
        $r.Commands[2] | Should -Match 'actions= restart/5000/restart/30000/restart/60000'
        $r.Commands[3] | Should -Match 'failureflag QTradeWinAgent 1'
    }

    It '服务已存在(1073)视为幂等成功' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtSc { [pscustomobject]@{ ExitCode = 1073; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        (Install-QtWinAgentService -ExePath 'X:\svc.exe').Ok | Should -BeTrue
    }

    It '真失败 → SERVICE_INSTALL_FAILED' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtSc { [pscustomobject]@{ ExitCode = 5; StdOut = ''; StdErr = 'denied'; TimedOut = $false; DurationMs = 1 } }
        (Install-QtWinAgentService -ExePath 'X:\svc.exe').Reason | Should -Be 'SERVICE_INSTALL_FAILED'
    }
}

Describe '🔴 R3-16 / R4-10:健康门不以 user_agent:true 为条件' {

    It 'health 200 且 user_agent=false → 仍算就绪(静默/无人值守安装属正常态)' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"user_agent":false,"vault":"ok","version":"1.0.0"}' }
        }
        Mock -ModuleName QTrade.WinAgent Start-QtSleep { }
        $r = Wait-QtWinAgentHealthy -TimeoutSec 5
        $r.Ok | Should -BeTrue
        $r.UserAgent | Should -BeFalse
    }

    It 'health 200 且 user_agent=true → 就绪' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"user_agent":true,"vault":"ok"}' }
        }
        Mock -ModuleName QTrade.WinAgent Start-QtSleep { }
        (Wait-QtWinAgentHealthy -TimeoutSec 5).UserAgent | Should -BeTrue
    }

    It '服务本身不 200 → WINAGENT_NOT_READY(退出码 82)' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{ Ok = $false; StatusCode = 0; Body = 'refused' } }
        Mock -ModuleName QTrade.WinAgent Start-QtSleep { }
        Mock -ModuleName QTrade.WinAgent Get-QtNow { (Get-Date).AddMinutes(10) }
        (Wait-QtWinAgentHealthy -TimeoutSec 1).Reason | Should -Be 'WINAGENT_NOT_READY'
    }

    It '幂等判据:服务未 Running → false' {
        Mock -ModuleName QTrade.WinAgent Get-QtServiceStatus { 'Stopped' }
        Test-QtWinAgentInstalled -Context ([pscustomobject]@{}) | Should -BeFalse
    }

    It '幂等判据:Running + health 200 → true(不看 user_agent)' {
        Mock -ModuleName QTrade.WinAgent Get-QtServiceStatus { 'Running' }
        Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"user_agent":false}' } }
        Test-QtWinAgentInstalled -Context ([pscustomobject]@{}) | Should -BeTrue
    }
}

Describe 'winagent.toml —— 已存在则只补缺省键(§2.8.3 第 2 步)' {

    It '空文件 → 全量写入' {
        $r = Merge-QtWinAgentToml -ExistingText '' -Defaults ([ordered]@{
                install = [ordered]@{ package_version = '1.0.0'; disk_hard_gb = 16 }
            })
        $r.Text | Should -Match '(?m)^\[install\]\r?$'
        $r.Text | Should -Match '(?m)^package_version = "1\.0\.0"\r?$'
        $r.Text | Should -Match '(?m)^disk_hard_gb = 16\r?$'
        $r.KeysAdded | Should -Contain 'install.package_version'
    }

    It '🔴 已有键不被覆盖(用户/上一版改过的值留着)' {
        $t = "[install]`r`npackage_version = `"0.9.0`"`r`n"
        $r = Merge-QtWinAgentToml -ExistingText $t -Defaults ([ordered]@{
                install = [ordered]@{ package_version = '1.0.0'; disk_hard_gb = 16 }
            })
        $r.Text | Should -Match '(?m)^package_version = "0\.9\.0"\r?$'
        $r.Text | Should -Match '(?m)^disk_hard_gb = 16\r?$'
        $r.KeysAdded | Should -Be @('install.disk_hard_gb')
    }

    It '布尔与数组按 TOML 语法渲染' {
        $r = Merge-QtWinAgentToml -ExistingText '' -Defaults ([ordered]@{
                wsl   = [ordered]@{ verify_user_distros = $true }
                probe = [ordered]@{ precheck_targets = @('apk_url', 'mail_smtp') }
            })
        $r.Text | Should -Match '(?m)^verify_user_distros = true\r?$'
        $r.Text | Should -Match '(?m)^precheck_targets = \["apk_url", "mail_smtp"\]\r?$'
    }

    It '新段被创建,原段内容不动' {
        $t = "[wechat]`r`nenabled = true`r`n"
        $r = Merge-QtWinAgentToml -ExistingText $t -Defaults ([ordered]@{ selftest = [ordered]@{ selftest_adb_port = 16099 } })
        $r.Text | Should -Match '(?m)^\[wechat\]\r?$'
        $r.Text | Should -Match '(?m)^enabled = true\r?$'
        $r.Text | Should -Match '(?m)^\[selftest\]\r?$'
        $r.Text | Should -Match '(?m)^selftest_adb_port = 16099\r?$'
    }
}

Describe '🔴 防火墙:引擎只调 WinAgent,自己不建规则(§6 / 验收 M1-14)' {

    It '固定规则名(docs/04 §2.6.3)' {
        (Get-QtFirewallRuleNames) -join ',' | Should -Be 'QTrade-WinAgent-17610-from-WSL,QTrade-Agent-17600-LAN'
    }

    It 'ensure 返回 created/updated/unchanged' {
        Mock -ModuleName QTrade.Firewall Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"action":"created","rule_name":"QTrade-WinAgent-17610-from-WSL"}' }
        }
        $r = Invoke-QtFirewallEnsure
        $r.Action | Should -Be 'created'
        $r.Blocked | Should -BeFalse
    }

    It '🔴 blocked_by_policy 只记警告不失败(验收 M1-15:仍 DONE 退出码 0)' {
        Mock -ModuleName QTrade.Firewall Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"action":"blocked_by_policy"}' }
        }
        $r = Invoke-QtFirewallEnsure
        $r.Ok | Should -BeTrue
        $r.Blocked | Should -BeTrue
        $r.Message | Should -Match '请 IT 放行'
    }

    It '卸载先走端点;端点不可用才按固定名兜底删' {
        Mock -ModuleName QTrade.Firewall Invoke-QtHttp { [pscustomobject]@{ Ok = $false; StatusCode = 0; Body = 'down' } }
        Mock -ModuleName QTrade.Firewall Remove-QtFirewallRuleByName { }
        $r = Invoke-QtFirewallDelete
        $r.Via | Should -Be 'fallback'
        ($r.Removed) -join ',' | Should -Be 'QTrade-WinAgent-17610-from-WSL,QTrade-Agent-17600-LAN'
    }

    It '🔴 兜底只删 QTrade-* 名字的(§6)' {
        { Remove-QtFirewallRuleByName -DisplayName 'SomeoneElse-Rule' } | Should -Throw
    }
}

Describe '日志(§3.1:每行 `时间 级别 步名 消息`;不记密码/令牌)' {

    It '行格式' {
        $line = Format-QtLogLine -Level 'INFO' -Message '内核已就位' -Step 'KERNEL_STAGED' -At ([datetime]'2026-09-20T18:15:00')
        $line | Should -Be '2026-09-20 18:15:00.000 INFO  KERNEL_STAGED 内核已就位'
    }

    It '多行消息压成一行(日志按行 grep)' {
        (Format-QtLogLine -Level 'ERROR' -Message "a`nb" -Step 'X' -At ([datetime]'2026-09-20T00:00:00')) | Should -Match 'a \| b'
    }

    It '🔴 令牌/口令被抹掉' {
        Protect-QtLogText -Text 'token=abcdef123456' | Should -Be 'token=***'
        Protect-QtLogText -Text '{"api_key": "sk-xyz"}' | Should -Match '\*\*\*'
        Protect-QtLogText -Text 'Authorization: Bearer eyJhbGciOi' | Should -Be 'Authorization: Bearer ***'
    }

    It '普通文本不被误改' {
        Protect-QtLogText -Text '内核 6.6.123.2-microsoft-standard-WSL2-binder 已生效' |
            Should -Be '内核 6.6.123.2-microsoft-standard-WSL2-binder 已生效'
    }

    It '子日志与主日志同目录同前缀(§3.1)' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-log-' + [Guid]::NewGuid().ToString('N'))
        try {
            Initialize-QtLog -Directory $tmp -Prefix 'install' -Stamp '20260920-181500' | Out-Null
            (Split-Path -Leaf (Get-QtLogPath)) | Should -Be 'install-20260920-181500.log'
            (Split-Path -Leaf (Get-QtSubLogPath -Name 'msiexec')) | Should -Be 'install-20260920-181500.msiexec.log'
            (Split-Path -Parent (Get-QtSubLogPath -Name 'robocopy')) | Should -Be $tmp
        } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

Describe '卸载计划(§2.14 第 1/3 步)' {

    It '保留数据时保留 winagent.db / data-backup-* / .wslconfig.bak-*' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $plan = Get-QtUninstallPlan -State $s -KeepData $true -Root 'C:\ProgramData\QTrade'
        $plan.KeepData | Should -BeTrue
        $plan.KeepPaths | Should -Contain 'C:\ProgramData\QTrade\winagent\winagent.db'
        $plan.KeepPaths | Should -Contain 'C:\ProgramData\QTrade\wsl\data-backup-*'
        $plan.KeepPaths | Should -Contain 'C:\ProgramData\QTrade\wsl\.wslconfig.bak-*'
    }

    It '删除全部数据时不保留任何子项' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        (Get-QtUninstallPlan -State $s -KeepData $false -Root 'C:\ProgramData\QTrade').KeepPaths.Count | Should -Be 0
    }

    It '🔴 keys_added 里的 kernel 不进 RemoveKeys(kernel= 行由 RemoveKernelLine 专门处理)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $s.wslconfig.keys_added = @('kernel', 'memory', 'autoMemoryReclaim')
        $plan = Get-QtUninstallPlan -State $s -KeepData $true -Root 'C:\ProgramData\QTrade'
        ($plan.WslConfig.RemoveKeys) -join ',' | Should -Be 'memory,autoMemoryReclaim'
        $plan.WslConfig.RemoveKernelLine | Should -BeTrue
    }

    It 'changed[] 含 memory → 卸载时要问是否恢复原值(缺省恢复)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $s.wslconfig.changed = @([pscustomobject]@{ key = 'memory'; from = '6GB'; to = '11GB' })
        (Get-QtUninstallPlan -State $s -KeepData $true).WslConfig.AskRestoreMemory | Should -BeTrue
    }

    It 'replaced_kernel 非空 → 卸载时要问是否恢复那一行(验收 M1-28)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $s.wslconfig.replaced_kernel = 'D:\k\bzImage'
        $plan = Get-QtUninstallPlan -State $s -KeepData $true
        $plan.WslConfig.AskRestoreKernel | Should -BeTrue
        $plan.WslConfig.ReplacedKernel | Should -Be 'D:\k\bzImage'
    }

    It '执行顺序:防火墙与 hosts 都排在服务之前(§2.14 第 5/6 步)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $steps = (Get-QtUninstallPlan -State $s -KeepData $true).Steps
        [array]::IndexOf($steps, 'firewall') | Should -BeLessThan ([array]::IndexOf($steps, 'service'))
        [array]::IndexOf($steps, 'hosts') | Should -BeLessThan ([array]::IndexOf($steps, 'service'))
    }
}

Describe '卸载:.wslconfig 恢复(§2.14 第 3 步;验收 M1-28)' {

    It 'Set-QtIniValue 改已有键' {
        $t = "[wsl2]`r`nmemory=11GB`r`nswap=2GB"
        (Set-QtIniValue -Text $t -Section 'wsl2' -Key 'memory' -Value '6GB') | Should -Match '(?m)^memory=6GB\r?$'
    }

    It 'Set-QtIniValue 段不存在时创建' {
        (Set-QtIniValue -Text '' -Section 'wsl2' -Key 'kernel' -Value 'D:\\k\\bzImage') | Should -Match '(?m)^\[wsl2\]\r?$'
    }

    It 'Set-QtIniValue 键不存在时追加到段末' {
        $t = "[wsl2]`r`nmemory=11GB"
        (Set-QtIniValue -Text $t -Section 'wsl2' -Key 'swap' -Value '2GB') | Should -Match '(?m)^swap=2GB\r?$'
    }
}

Describe '🔴 rename 备份残留 —— 卸载只列出、不代删(§2.14 第 6 步末)' {

    It '列出 *.qtbak-«ts» 目录' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-ob-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $tmp 'xwechat_files.qtbak-20260920-181500') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $tmp 'normal') -Force | Out-Null
        try {
            $r = Get-QtOrphanQtbakDirs -SearchRoots @($tmp)
            $r.Count | Should -Be 1
            $r[0] | Should -Match 'qtbak-20260920-181500$'
            # 只列出,目录仍在
            (Test-Path -LiteralPath $r[0]) | Should -BeTrue
        } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

Describe '安装摘要(§2.11 第 5 项;验收 M1-19「除时间戳外逐行相同」)' {

    It '字段顺序固定、不含随机量' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-sum-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        try {
            $p = Join-Path $tmp 'install-summary.txt'
            $s = [pscustomobject]@{ package_version = '1.0.0'; kernel_version = '6.6.123.2-…-binder'; docker_cidr = '10.213.0.0/16' }
            Write-QtInstallSummary -Path $p -Summary $s | Out-Null
            $a = @([IO.File]::ReadAllLines($p))
            Write-QtInstallSummary -Path $p -Summary $s | Out-Null
            $b = @([IO.File]::ReadAllLines($p))
            $a.Count | Should -Be $b.Count
            # 除首行「生成时间」外逐行相同
            for ($i = 1; $i -lt $a.Count; $i++) { $a[$i] | Should -Be $b[$i] }
            $a[1] | Should -Be 'package_version: 1.0.0'
        } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}


Describe 'DONE 步:删暂存 rootfs.tar(A-6,不询问)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-done-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '有就删,并回报释放了多少' {
        $tar = Join-Path $script:Tmp 'rootfs.tar'
        [IO.File]::WriteAllBytes($tar, (New-Object byte[] 4096))
        $r = Remove-QtStagedRootfs -WslDir $script:Tmp
        $r.Removed | Should -BeTrue
        $r.FreedBytes | Should -Be 4096
        (Test-Path -LiteralPath $tar) | Should -BeFalse
    }

    It '没有就当没事(重跑 DONE 不该炸)' {
        $r = Remove-QtStagedRootfs -WslDir $script:Tmp
        $r.Removed | Should -BeFalse
        $r.FreedBytes | Should -Be 0
    }
}

Describe '🔴 完成页探测两列并列:SKIPPED 标「未探测」而不是「不通」(§2.10 / 验收 M1-17)' {

    It 'SKIPPED 的行文案' {
        $pre = @([pscustomobject]@{ target = 'mail_smtp'; result = 'TCP_TIMEOUT' })
        $self = @([pscustomobject]@{ target = 'mail_smtp'; result = 'SKIPPED' })
        $rows = Format-QtProbeSummary -PrecheckProbes $pre -SelftestProbes $self
        $rows.Count | Should -Be 1
        $rows[0] | Should -Match 'mail_smtp'
        $rows[0] | Should -Match 'TCP_TIMEOUT'
        $rows[0] | Should -Match '未探测'
        $rows[0] | Should -Not -Match '不通'
    }

    It 'OK 显示「通」' {
        $rows = Format-QtProbeSummary -SelftestProbes @([pscustomobject]@{ target = 'apk_url'; result = 'OK' })
        $rows[0] | Should -Match '通'
    }

    It '某一列没有该目标 → 显示「—」' {
        $rows = Format-QtProbeSummary -PrecheckProbes @([pscustomobject]@{ target = 'qq_servers'; result = 'OK' }) -SelftestProbes @()
        $rows[0] | Should -Match '—'
    }

    It '两列的目标取并集,顺序稳定(预检列在前)' {
        $rows = Format-QtProbeSummary `
            -PrecheckProbes @([pscustomobject]@{ target = 'a'; result = 'OK' }) `
            -SelftestProbes @([pscustomobject]@{ target = 'b'; result = 'OK' })
        $rows.Count | Should -Be 2
        $rows[0] | Should -Match '^a '
        $rows[1] | Should -Match '^b '
    }

    It '两列都空 → 空表,不抛' {
        (Format-QtProbeSummary).Count | Should -Be 0
    }
}
