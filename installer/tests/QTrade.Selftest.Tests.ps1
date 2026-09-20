# Pester 5 —— 探测调用(§2.10)与自检(§2.11);验收 M1-16 / M1-17
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl',
            'QTrade.Distro', 'QTrade.WinAgent', 'QTrade.Selftest')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe '§7 [probe] install_targets(基线 §8.5 十个 probe_target)' {

    It '十个目标' {
        $t = Get-QtInstallTargets
        $t.Count | Should -Be 10
        $t -join ',' | Should -Be 'apk_url,mail_pop3,mail_imap,mail_smtp,qidian_msf,qq_servers,wechat_servers,docker_registry,winagent_from_wsl,agent_from_windows'
    }

    It 'Windows 侧只有两个(§2.10 第 2 行)' {
        (Get-QtWindowsSideTargets) -join ',' | Should -Be 'wechat_servers,agent_from_windows'
    }

    It '自检端口 16099、redroid boot 超时 180(C-43:只引用 02 [runtime] boot_timeout_s)' {
        Get-QtSelftestAdbPort | Should -Be 16099
        Get-QtRedroidBootTimeout | Should -Be 180
    }
}

Describe '§2.10 分侧(Agent 不可达时 WSL 侧只能 SKIPPED)' {

    It '按侧拆分' {
        $s = Split-QtProbeTargetsBySide -Targets (Get-QtInstallTargets)
        $s.Windows -join ',' | Should -Be 'wechat_servers,agent_from_windows'
        $s.Wsl | Should -Contain 'apk_url'
        $s.Wsl | Should -Contain 'mail_smtp'
        $s.Wsl | Should -Contain 'winagent_from_wsl'
        $s.Wsl | Should -Not -Contain 'wechat_servers'
    }
}

Describe 'Invoke-QtProbe —— 两条路径(§2.10)' {

    It 'Agent 可达 → 走主路径 POST /api/v1/system/probe,via=agent' {
        Mock -ModuleName QTrade.Selftest Get-QtAgentHealth { [pscustomobject]@{ Ok = $true } }
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '[{"target":"apk_url","result":"OK"},{"target":"mail_smtp","result":"TCP_TIMEOUT"}]' }
        }
        $r = Invoke-QtProbe -Targets @('apk_url', 'mail_smtp')
        $r.Via | Should -Be 'agent'
        $r.Results.Count | Should -Be 2
        $r.Results[0].via | Should -Be 'agent'
        $r.Results[1].result | Should -Be 'TCP_TIMEOUT'
    }

    It '🔴 Agent 不可达 → Windows 侧是真结论,WSL 侧一律 SKIPPED + detail(验收 M1-17)' {
        Mock -ModuleName QTrade.Selftest Get-QtAgentHealth { [pscustomobject]@{ Ok = $false } }
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '[{"target":"wechat_servers","result":"OK"}]' }
        }
        $r = Invoke-QtProbe -Targets @('apk_url', 'wechat_servers', 'mail_smtp')
        $r.Via | Should -Be 'winagent'
        $win = @($r.Results | Where-Object { $_.target -eq 'wechat_servers' })
        $win[0].result | Should -Be 'OK'
        foreach ($t in @('apk_url', 'mail_smtp')) {
            $row = @($r.Results | Where-Object { $_.target -eq $t })[0]
            $row.result | Should -Be 'SKIPPED'
            $row.detail | Should -Be 'agent unreachable'
            $row.via | Should -Be 'winagent'
        }
    }

    It '端点回的不是 JSON → 空结果,不抛' {
        Mock -ModuleName QTrade.Selftest Get-QtAgentHealth { [pscustomobject]@{ Ok = $true } }
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = 'not json' } }
        (Invoke-QtProbe -Targets @('apk_url')).Results.Count | Should -Be 0
    }

    It '端点把结果包在 results 字段里也认' {
        Mock -ModuleName QTrade.Selftest Get-QtAgentHealth { [pscustomobject]@{ Ok = $true } }
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"results":[{"target":"apk_url","result":"OK"}]}' }
        }
        (Invoke-QtProbe -Targets @('apk_url')).Results[0].target | Should -Be 'apk_url'
    }
}

Describe '§2.11 第 1 项 WinAgent 健康' {

    It 'vault:ok + 200 → 通过' {
        Mock -ModuleName QTrade.Selftest Get-QtWinAgentHealth { [pscustomobject]@{ Ok = $true; Vault = 'ok'; UserAgent = $true } }
        (Test-QtSelftestWinAgent).Ok | Should -BeTrue
    }

    It '🔴 R4-10:user_agent=false **不判失败**(无人值守装到这一步永远到不了 DONE 的那个坑)' {
        Mock -ModuleName QTrade.Selftest Get-QtWinAgentHealth { [pscustomobject]@{ Ok = $true; Vault = 'ok'; UserAgent = $false } }
        $r = Test-QtSelftestWinAgent
        $r.Ok | Should -BeTrue
        $r.UserAgent | Should -BeFalse
    }

    It 'vault 不 ok → SELFTEST_WINAGENT' {
        Mock -ModuleName QTrade.Selftest Get-QtWinAgentHealth { [pscustomobject]@{ Ok = $true; Vault = 'missing'; UserAgent = $true } }
        (Test-QtSelftestWinAgent).Reason | Should -Be 'SELFTEST_WINAGENT'
    }

    It '不 200 → SELFTEST_WINAGENT' {
        Mock -ModuleName QTrade.Selftest Get-QtWinAgentHealth { [pscustomobject]@{ Ok = $false; Vault = ''; UserAgent = $false } }
        (Test-QtSelftestWinAgent).Reason | Should -Be 'SELFTEST_WINAGENT'
    }
}

Describe '§2.11 第 2 项 Agent 健康(docker:ok、db:ok、winagent_reachable:true 缺一不可)' {

    It '三项齐 → 通过' {
        Mock -ModuleName QTrade.Selftest Get-QtAgentHealth { [pscustomobject]@{ Ok = $true; Docker = 'ok'; Db = 'ok'; WinAgentReachable = $true } }
        (Test-QtSelftestAgent).Ok | Should -BeTrue
    }

    It '缺任一 → SELFTEST_AGENT' -ForEach @(
        @{ D = 'fail'; B = 'ok'; W = $true }
        @{ D = 'ok'; B = 'fail'; W = $true }
        @{ D = 'ok'; B = 'ok'; W = $false }
    ) {
        Mock -ModuleName QTrade.Selftest Get-QtAgentHealth { [pscustomobject]@{ Ok = $true; Docker = $D; Db = $B; WinAgentReachable = $W } }
        (Test-QtSelftestAgent).Reason | Should -Be 'SELFTEST_AGENT'
    }
}

Describe '§2.11 第 3 项 临时 redroid(202 + 轮询)' {

    BeforeEach { Mock -ModuleName QTrade.Selftest Start-QtSleep { } }

    It '202 → 轮询到 ok → 通过,并带回耗时' {
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp {
            if ($Method -eq 'POST') { return [pscustomobject]@{ Ok = $true; StatusCode = 202; Body = '{"run_id":"r1"}' } }
            return [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"status":"ok"}' }
        }
        $r = Invoke-QtSelftestRedroid
        $r.Ok | Should -BeTrue
        $r.RunId | Should -Be 'r1'
    }

    It '没回 202 → SELFTEST_REDROID_BOOT' {
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp { [pscustomobject]@{ Ok = $false; StatusCode = 500; Body = 'boom' } }
        (Invoke-QtSelftestRedroid).Reason | Should -Be 'SELFTEST_REDROID_BOOT'
    }

    It '202 但没带 run_id → SELFTEST_REDROID_BOOT(不静默当成功)' {
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 202; Body = '{}' } }
        (Invoke-QtSelftestRedroid).Reason | Should -Be 'SELFTEST_REDROID_BOOT'
    }

    It '🔴 轮询到 failed → 失败并**附 docker logs 前 40 行**(§2.11 第 3 项)' {
        Mock -ModuleName QTrade.Selftest Invoke-QtHttp {
            if ($Method -eq 'POST') { return [pscustomobject]@{ Ok = $true; StatusCode = 202; Body = '{"run_id":"r1"}' } }
            return [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"status":"failed"}' }
        }
        Mock -ModuleName QTrade.Selftest Get-QtContainerLogsTail { 'boot loop...' }
        $r = Invoke-QtSelftestRedroid
        $r.Ok | Should -BeFalse
        $r.Logs | Should -Be 'boot loop...'
        Should -Invoke -ModuleName QTrade.Selftest Get-QtContainerLogsTail -Times 1 -ParameterFilter { $Lines -eq 40 }
    }
}

Describe '§2.11 第 4 项 napcat —— 可选,失败只警告' {

    It '没给镜像 ref → 跳过,算通过' {
        (Invoke-QtSelftestNapcat).Skipped | Should -BeTrue
    }

    It '跑失败 → Ok=false 但只是一条警告文案' {
        Mock -ModuleName QTrade.Selftest Invoke-QtWsl { [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        $r = Invoke-QtSelftestNapcat -ImageRef 'x:y'
        $r.Ok | Should -BeFalse
        $r.Message | Should -Match '只警告'
    }
}

Describe '§2.11 编排:三项全过才 SELFTEST_OK' {

    BeforeEach {
        Mock -ModuleName QTrade.Selftest Start-QtSleep { }
        Mock -ModuleName QTrade.Selftest Invoke-QtProbe { [pscustomobject]@{ Via = 'agent'; Results = @() } }
    }

    It 'WinAgent 不过 → 立刻停在第 1 项,不去跑后面两项' {
        Mock -ModuleName QTrade.Selftest Test-QtSelftestWinAgent { [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_WINAGENT'; UserAgent = $false; Vault = '' } }
        Mock -ModuleName QTrade.Selftest Test-QtSelftestAgent { throw '不该被调用' }
        (Invoke-QtSelftest).Reason | Should -Be 'SELFTEST_WINAGENT'
    }

    It '🔴 Agent 不过 → 仍给一份网络汇总(§2.10 第 3 行)' {
        Mock -ModuleName QTrade.Selftest Test-QtSelftestWinAgent { [pscustomobject]@{ Ok = $true; Reason = ''; UserAgent = $true; Vault = 'ok' } }
        Mock -ModuleName QTrade.Selftest Test-QtSelftestAgent { [pscustomobject]@{ Ok = $false; Reason = 'SELFTEST_AGENT'; Health = $null } }
        $r = Invoke-QtSelftest
        $r.Reason | Should -Be 'SELFTEST_AGENT'
        $r.Probes | Should -Not -BeNullOrEmpty
    }

    It '🔴 user_agent=false 只进 Warnings,不影响通过' {
        Mock -ModuleName QTrade.Selftest Test-QtSelftestWinAgent { [pscustomobject]@{ Ok = $true; Reason = ''; UserAgent = $false; Vault = 'ok' } }
        Mock -ModuleName QTrade.Selftest Test-QtSelftestAgent { [pscustomobject]@{ Ok = $true; Reason = ''; Health = $null } }
        Mock -ModuleName QTrade.Selftest Invoke-QtSelftestRedroid { [pscustomobject]@{ Ok = $true; Reason = ''; RunId = 'r'; ElapsedSec = 42; Logs = '' } }
        $r = Invoke-QtSelftest -SkipNapcat
        $r.Ok | Should -BeTrue
        ($r.Warnings -join ' ') | Should -Match '登录 Windows 桌面'
    }

    It 'napcat 失败只进 Warnings,不影响通过' {
        Mock -ModuleName QTrade.Selftest Test-QtSelftestWinAgent { [pscustomobject]@{ Ok = $true; Reason = ''; UserAgent = $true; Vault = 'ok' } }
        Mock -ModuleName QTrade.Selftest Test-QtSelftestAgent { [pscustomobject]@{ Ok = $true; Reason = ''; Health = $null } }
        Mock -ModuleName QTrade.Selftest Invoke-QtSelftestRedroid { [pscustomobject]@{ Ok = $true; Reason = ''; RunId = 'r'; ElapsedSec = 12; Logs = '' } }
        Mock -ModuleName QTrade.Selftest Invoke-QtSelftestNapcat { [pscustomobject]@{ Ok = $false; Skipped = $false; Message = 'napcat 自检未通过(只警告,不阻断)' } }
        $r = Invoke-QtSelftest -NapcatImageRef 'x:y'
        $r.Ok | Should -BeTrue
        ($r.Warnings -join ' ') | Should -Match 'napcat'
    }
}

Describe '🔴 SELFTEST_OK 幂等判据不含「临时容器还在不在」' {

    It '只复跑前两项(容器是一次性动作,跑完就 rm -f,拿它判会把「清理干净」误判成「没做过」)' {
        Mock -ModuleName QTrade.Selftest Test-QtSelftestWinAgent { [pscustomobject]@{ Ok = $true } }
        Mock -ModuleName QTrade.Selftest Test-QtSelftestAgent { [pscustomobject]@{ Ok = $true } }
        Mock -ModuleName QTrade.Selftest Invoke-QtSelftestRedroid { throw '幂等判据不该起容器' }
        Test-QtSelftestComplete -Context ([pscustomobject]@{ WinAgentBaseUrl = 'http://x'; AgentBaseUrl = 'http://y' }) | Should -BeTrue
    }
}
