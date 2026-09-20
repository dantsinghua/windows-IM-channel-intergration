# Pester 5 —— PRECHECK 判定(docs/03 §2.2.2 E-18 门槛、§2.4.1、§2.4.2、§2.4.6)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl', 'QTrade.Preflight')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe '门槛常量(docs/03 §7 [install] / §2.2.2 E-18)' {

    It '硬 16 / 建议 20 / SFX 粗判 6 / 最低内存 8 / 最低构建号 19044' {
        $t = Get-QtPreflightThresholds
        $t.disk_hard_gb | Should -Be 16
        $t.disk_recommend_gb | Should -Be 20
        $t.sfx_disk_rough_gb | Should -Be 6
        $t.min_memory_gb | Should -Be 8
        $t.min_windows_build | Should -Be 19044
    }
}

Describe 'Windows 构建号(§2.4.1;A-3 砍 5.15 线后最低 19044)' {

    It '19043 被拒(19041–19043 不支持、不再恢复 5.15 线)' {
        $r = Test-QtWindowsBuild -Build 19043
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'WIN_TOO_OLD'
        $r.Message | Should -Match '19044'
    }
    It '19044 通过' { (Test-QtWindowsBuild -Build 19044).Ok | Should -BeTrue }
    It '19045 通过' { (Test-QtWindowsBuild -Build 19045).Ok | Should -BeTrue }
    It 'Win11 22631 同规则通过' { (Test-QtWindowsBuild -Build 22631).Ok | Should -BeTrue }
}

Describe '磁盘门槛(E-18;验收 M1-29)' {

    It '15 GB → 拒,差额准确' {
        $r = Test-QtDiskThreshold -FreeBytes ([long](15 * 1GB))
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'DISK_LOW'
        $r.FreeGB | Should -Be 15
        $r.DeficitGB | Should -Be 1
        $r.Message | Should -Match '需要 16 GB'
        $r.Message | Should -Match '建议 20 GB'
        $r.Message | Should -Match '当前剩余 15 GB'
    }

    It '18 GB → 放行但 warn=low_headroom(不拒)' {
        $r = Test-QtDiskThreshold -FreeBytes ([long](18 * 1GB))
        $r.Ok | Should -BeTrue
        $r.Warn | Should -Be 'low_headroom'
    }

    It '刚好 16 GB → 放行(硬门槛是「<16 即拒」)' {
        (Test-QtDiskThreshold -FreeBytes ([long](16 * 1GB))).Ok | Should -BeTrue
    }

    It '25 GB → 无警告' {
        $r = Test-QtDiskThreshold -FreeBytes ([long](25 * 1GB))
        $r.Ok | Should -BeTrue
        $r.Warn | Should -Be ''
    }
}

Describe '内存门槛(§2.4.1)' {

    It '6 GB → MEM_LOW' {
        $r = Test-QtMemoryThreshold -TotalBytes ([long](6 * 1GB))
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'MEM_LOW'
    }
    It '8 GB → 放行但警告「只能稳定跑 1 个企点账号」' {
        $r = Test-QtMemoryThreshold -TotalBytes ([long](8 * 1GB))
        $r.Ok | Should -BeTrue
        $r.Warn | Should -Be 'low_memory'
        $r.Message | Should -Match '1 个企点账号'
    }
    It '16 GB → 无警告' {
        (Test-QtMemoryThreshold -TotalBytes ([long](16 * 1GB))).Warn | Should -Be ''
    }
}

Describe '策略判定(§2.4.2;基线 §8.5 v1.1 四值,C-36)' {

    It 'policy_reason 只有四值' {
        (Get-QtPolicyReasons) -join ',' | Should -Be 'FEATURE_PAYLOAD_REMOVED,WSUS_BLOCKS_FOD,CUSTOM_KERNEL_FORBIDDEN,APPLOCKER'
    }

    It '🔴 AllowCustomKernelUserSetting=0 优先命中 —— 不检测它的后果是「被当成内核起不来」排查方向全错(W12)' {
        $r = Get-QtPolicyBlock -Facts ([pscustomobject]@{
                CustomKernelForbidden = $true; FeaturePayloadRemoved = $true; WsusBlocksFod = $true; AppLockerBlocked = $true
            })
        $r.blocked | Should -BeTrue
        $r.policy_reason | Should -Be 'CUSTOM_KERNEL_FORBIDDEN'
    }

    It '功能载荷移除' {
        (Get-QtPolicyBlock -Facts ([pscustomobject]@{ CustomKernelForbidden = $false; FeaturePayloadRemoved = $true; WsusBlocksFod = $false; AppLockerBlocked = $false })).policy_reason |
            Should -Be 'FEATURE_PAYLOAD_REMOVED'
    }

    It 'WSUS 挡载荷(验收 M1-8)' {
        (Get-QtPolicyBlock -Facts ([pscustomobject]@{ CustomKernelForbidden = $false; FeaturePayloadRemoved = $false; WsusBlocksFod = $true; AppLockerBlocked = $false })).policy_reason |
            Should -Be 'WSUS_BLOCKS_FOD'
    }

    It '全不命中 → 不阻断' {
        (Get-QtPolicyBlock -Facts ([pscustomobject]@{ CustomKernelForbidden = $false; FeaturePayloadRemoved = $false; WsusBlocksFod = $false; AppLockerBlocked = $false })).blocked |
            Should -BeFalse
    }
}

Describe 'net_state(§2.4.6;只识别不改)' {

    It '判定' -ForEach @(
        @{ Proxy = $false; Vpn = $false; Gw = $true; Want = 'DIRECT' }
        @{ Proxy = $true; Vpn = $false; Gw = $true; Want = 'SYSTEM_PROXY' }
        @{ Proxy = $false; Vpn = $true; Gw = $true; Want = 'VPN_ACTIVE' }
        @{ Proxy = $true; Vpn = $true; Gw = $true; Want = 'VPN_ACTIVE_WITH_PROXY' }
        @{ Proxy = $true; Vpn = $true; Gw = $false; Want = 'OFFLINE' }
    ) {
        Get-QtNetState -ProxyActive $Proxy -VpnActive $Vpn -HasDefaultGateway $Gw | Should -Be $Want
    }
}

Describe '数据盘选择(§2.4.1 E-18;🔴 只提示不自动做,P0-12 暂存恒落系统盘)' {

    It '系统盘紧张且另有大盘 → 提示那块盘' {
        $r = Get-QtDataDriveSuggestion -SystemDriveFreeBytes ([long](15 * 1GB)) -Drives @(
            [pscustomobject]@{ Letter = 'C'; FreeBytes = [long](15 * 1GB) }
            [pscustomobject]@{ Letter = 'D'; FreeBytes = [long](60 * 1GB) }
        )
        $r.Suggest | Should -BeTrue
        $r.Drive | Should -Be 'D'
        $r.FreeGB | Should -Be 60
    }

    It '系统盘够用 → 不提示' {
        (Get-QtDataDriveSuggestion -SystemDriveFreeBytes ([long](40 * 1GB)) -Drives @(
                [pscustomobject]@{ Letter = 'D'; FreeBytes = [long](60 * 1GB) })).Suggest | Should -BeFalse
    }

    It '没有别的够大的盘 → 不提示' {
        (Get-QtDataDriveSuggestion -SystemDriveFreeBytes ([long](15 * 1GB)) -Drives @(
                [pscustomobject]@{ Letter = 'D'; FreeBytes = [long](5 * 1GB) })).Suggest | Should -BeFalse
    }

    It '多块候选取最大那块' {
        (Get-QtDataDriveSuggestion -SystemDriveFreeBytes ([long](10 * 1GB)) -Drives @(
                [pscustomobject]@{ Letter = 'D'; FreeBytes = [long](30 * 1GB) }
                [pscustomobject]@{ Letter = 'E'; FreeBytes = [long](90 * 1GB) })).Drive | Should -Be 'E'
    }
}

Describe '预检基础探测(§2.4.6 / 99b ①)' {

    It '目标表 = docs/03 §7 [probe] precheck_targets' {
        (Get-QtPrecheckTargets) -join ',' | Should -Be 'apk_url,mail_pop3,mail_imap,mail_smtp,qidian_msf,qq_servers,wechat_servers'
    }

    It '🔴 结论只出四值之一,且形状固定(§2.4.6:TLS/HTTP/代理判定不在这层做)' {
        # ⚠️ 不断言具体是哪一值 —— 有些网络会把不存在的域名劫持到广告页、有些机器 9 端口有人听,
        #    那是环境差异不是缺陷;本层的规格约束是「只有 DNS+TCP 两层、结论只有四值、都不阻断」。
        $r = Invoke-QtPrecheckProbe -Targets @(
            [pscustomobject]@{ target = 'mail_smtp'; host = 'no-such-host.qtrade.invalid'; port = 465 }
            [pscustomobject]@{ target = 'qidian_msf'; host = '127.0.0.1'; port = 9 }
        ) -DnsTimeoutSec 1 -TcpTimeoutSec 1
        $r.Count | Should -Be 2
        $r[0].target | Should -Be 'mail_smtp'
        $r[1].target | Should -Be 'qidian_msf'
        foreach ($row in $r) {
            $row.result | Should -BeIn @('OK', 'DNS_FAIL', 'TCP_TIMEOUT', 'TCP_REFUSED')
            $row.host | Should -Not -BeNullOrEmpty
            $row.port | Should -BeGreaterThan 0
            $row.at | Should -Not -BeNullOrEmpty
        }
    }

    It '🔴 不合语法的主机名必然 DNS_FAIL(这一条与网络环境无关)' {
        $r = Invoke-QtPrecheckProbe -Targets @(
            [pscustomobject]@{ target = 'apk_url'; host = '..'; port = 443 }
        ) -DnsTimeoutSec 1 -TcpTimeoutSec 1
        $r[0].result | Should -Be 'DNS_FAIL'
    }
}

Describe '提权账号 ≠ 登录账号(§2.4.1;退出码 23)' {

    It '两个 SID 相同 → Ok' {
        Mock -ModuleName QTrade.Preflight Get-QtCurrentUserSid { 'S-1-5-21-1-1-1-1001' }
        Mock -ModuleName QTrade.Preflight Get-QtInteractiveUserSid { 'S-1-5-21-1-1-1-1001' }
        (Test-QtElevatedUserMatchesLogon).Ok | Should -BeTrue
    }

    It '🔴 不同 → 停(WSL 发行版与 .wslconfig 都是按 Windows 用户登记的)' {
        Mock -ModuleName QTrade.Preflight Get-QtCurrentUserSid { 'S-1-5-21-1-1-1-500' }
        Mock -ModuleName QTrade.Preflight Get-QtInteractiveUserSid { 'S-1-5-21-1-1-1-1001' }
        $r = Test-QtElevatedUserMatchesLogon
        $r.Ok | Should -BeFalse
        $r.ElevatedSid | Should -Be 'S-1-5-21-1-1-1-500'
        $r.LogonSid | Should -Be 'S-1-5-21-1-1-1-1001'
    }

    It '无交互会话(无人值守静默安装)视为一致,不误拦' {
        Mock -ModuleName QTrade.Preflight Get-QtCurrentUserSid { 'S-1-5-21-1-1-1-500' }
        Mock -ModuleName QTrade.Preflight Get-QtInteractiveUserSid { $null }
        (Test-QtElevatedUserMatchesLogon).Ok | Should -BeTrue
    }
}

Describe '虚拟化 OR 判(§2.4.2 / §2.15:Hyper-V 已在跑时 VirtualizationFirmwareEnabled 读 false)' {

    It '固件位 true → OK' {
        Mock -ModuleName QTrade.Preflight Get-QtCim {
            if ($ClassName -eq 'Win32_Processor') { return @([pscustomobject]@{ VirtualizationFirmwareEnabled = $true }) }
            return @([pscustomobject]@{ HypervisorPresent = $false })
        }
        Get-QtVirtualizationOk | Should -BeTrue
    }

    It '🔴 固件位 false 但 HypervisorPresent true → 仍 OK(不能只看前者)' {
        Mock -ModuleName QTrade.Preflight Get-QtCim {
            if ($ClassName -eq 'Win32_Processor') { return @([pscustomobject]@{ VirtualizationFirmwareEnabled = $false }) }
            return @([pscustomobject]@{ HypervisorPresent = $true })
        }
        Get-QtVirtualizationOk | Should -BeTrue
    }

    It '两者都 false → VIRT_DISABLED' {
        Mock -ModuleName QTrade.Preflight Get-QtCim {
            if ($ClassName -eq 'Win32_Processor') { return @([pscustomobject]@{ VirtualizationFirmwareEnabled = $false }) }
            return @([pscustomobject]@{ HypervisorPresent = $false })
        }
        Get-QtVirtualizationOk | Should -BeFalse
    }
}
