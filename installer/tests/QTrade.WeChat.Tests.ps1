# Pester 5 —— 微信检测/矩阵/备份/重装红线(docs/03 §2.9.1、§2.9.2、§2.9.3)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.WeChat')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
}

Describe '随包版本与矩阵(§2.9.2;P-18 / B-1)' {

    It '随包版本 = 4.1.12.26' { Get-QtBundledWeChatVersion | Should -Be '4.1.12.26' }

    It '矩阵三行,状态与来源与文档一致' {
        $m = Get-QtWeChatMatrix
        $m.Count | Should -Be 3
        (@($m | Where-Object { $_.version -eq '4.1.12.26' })[0]).dll | Should -Be 'wx_key2.dll'
        (@($m | Where-Object { $_.version -eq '4.1.12.26' })[0]).status | Should -Be 'verified'
        (@($m | Where-Object { $_.version -eq '4.1.11.52' })[0]).status | Should -Be 'failed'
        (@($m | Where-Object { $_.version -eq '4.1.13.12' })[0]).status | Should -Be 'unknown'
    }
}

Describe '四段版本比较(§2.9.2「逐段比较」)' {

    It '比较' -ForEach @(
        @{ L = '4.1.12.26'; R = '4.1.12.26'; W = 0 }
        @{ L = '4.1.13.12'; R = '4.1.12.26'; W = 1 }
        @{ L = '4.1.11.52'; R = '4.1.12.26'; W = -1 }
        @{ L = '4.1.12.100'; R = '4.1.12.26'; W = 1 }
        @{ L = '3.9.12.51'; R = '4.1.12.26'; W = -1 }
        @{ L = '4.1.12'; R = '4.1.12.0'; W = 0 }
    ) { Compare-QtVersion -Left $L -Right $R | Should -Be $W }
}

Describe '五个结论(基线 §8.6 / §2.9.2)' {

    It '未检测到 → NOT_INSTALLED / BLOCK' {
        $r = Get-QtWeChatMatch -InstallCount 0
        $r.Match | Should -Be 'NOT_INSTALLED'
        $r.Action | Should -Be 'BLOCK'
    }

    It '去重后 ≥ 2 → MULTIPLE_INSTALLS(用户选一处后再判)' {
        (Get-QtWeChatMatch -InstallCount 2 -Version '4.1.12.26').Match | Should -Be 'MULTIPLE_INSTALLS'
    }

    It '随包版本 → SUPPORTED / KEEP,查得 wx_key2.dll(验收 M3.5-2)' {
        $r = Get-QtWeChatMatch -InstallCount 1 -Version '4.1.12.26'
        $r.Match | Should -Be 'SUPPORTED'
        $r.Action | Should -Be 'KEEP'
        $r.Dll | Should -Be 'wx_key2.dll'
    }

    It '🔴 4.1.13.12(矩阵里是 unknown)→ UNSUPPORTED_NEWER,unknown 行不改判定' {
        $r = Get-QtWeChatMatch -InstallCount 1 -Version '4.1.13.12'
        $r.Match | Should -Be 'UNSUPPORTED_NEWER'
        $r.Action | Should -Be 'REINSTALL_BUNDLED'
    }

    It '🔴 4.1.11.52(矩阵里是 failed)→ UNSUPPORTED_OLDER,failed 行不当 SUPPORTED(验收 M3.5-3/4)' {
        (Get-QtWeChatMatch -InstallCount 1 -Version '4.1.11.52').Match | Should -Be 'UNSUPPORTED_OLDER'
    }

    It '3.x → UNSUPPORTED_OLDER' {
        (Get-QtWeChatMatch -InstallCount 1 -Version '3.9.12.51').Match | Should -Be 'UNSUPPORTED_OLDER'
    }
}

Describe '🔴🔴 微信卸载红线(§2.9.3 事实 1:/S = 卸载并清空聊天记录与登录态)' {

    It 'Assert-QtNoSilentUninstall 对 /S 抛' {
        { Assert-QtNoSilentUninstall -Arguments @('/S') } | Should -Throw
        { Assert-QtNoSilentUninstall -Arguments @('/s') } | Should -Throw
        { Assert-QtNoSilentUninstall -Arguments @(' /S ') } | Should -Throw
    }

    It '空参数与其它参数放行' {
        Assert-QtNoSilentUninstall -Arguments @() | Should -BeTrue
        Assert-QtNoSilentUninstall -Arguments @('/D=C:\x') | Should -BeTrue
    }

    It '🔴 交互式卸载启动卸载器时**一个参数都不加**' {
        Mock -ModuleName QTrade.WeChat Start-Process { }
        Mock -ModuleName QTrade.WeChat Test-QtRegistryKey { $false }
        Mock -ModuleName QTrade.WeChat Get-QtProcessByName { @() }
        Mock -ModuleName QTrade.WeChat Start-QtSleep { }
        (Start-QtWeChatInteractiveUninstall -UninstallString '"C:\Tencent\Weixin\Uninstall.exe"').Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.WeChat Start-Process -Times 1 -ParameterFilter {
            -not $PSBoundParameters.ContainsKey('ArgumentList')
        }
    }

    It '轮询超时 → UNINSTALL_TIMEOUT(卸载键还在)' {
        Mock -ModuleName QTrade.WeChat Start-Process { }
        Mock -ModuleName QTrade.WeChat Test-QtRegistryKey { $true }
        Mock -ModuleName QTrade.WeChat Get-QtProcessByName { @() }
        Mock -ModuleName QTrade.WeChat Start-QtSleep { }
        Mock -ModuleName QTrade.WeChat Get-QtNow { (Get-Date).AddHours(1) }
        (Start-QtWeChatInteractiveUninstall -UninstallString 'X:\Un.exe' -TimeoutSec 1).Reason | Should -Be 'UNINSTALL_TIMEOUT'
    }
}

Describe '/QT_WECHAT 模式解析(P-19:静默下 reinstall 降级为 check)' {

    It '静默 + reinstall → check(验收 M3.5-5)' {
        Resolve-QtWeChatMode -Requested 'reinstall' -Silent $true | Should -Be 'check'
    }
    It '交互 + reinstall → reinstall' {
        Resolve-QtWeChatMode -Requested 'reinstall' -Silent $false | Should -Be 'reinstall'
    }
    It 'skip 不受影响' {
        Resolve-QtWeChatMode -Requested 'skip' -Silent $true | Should -Be 'skip'
    }
    It '缺省 check' { Resolve-QtWeChatMode | Should -Be 'check' }
}

Describe '备份模式(§2.9.3 第 3 步;backup_copy_max_gb 默认 5)' {

    It 'auto + 小数据 → copy' {
        Select-QtWeChatBackupMode -Requested 'auto' -TotalBytes ([long](2 * 1GB)) | Should -Be 'copy'
    }
    It 'auto + 大数据 → rename(零磁盘开销、秒级)' {
        Select-QtWeChatBackupMode -Requested 'auto' -TotalBytes ([long](22 * 1GB)) | Should -Be 'rename'
    }
    It '🔴 数据根未知(读不到 ini)→ 只剩 skip' {
        Select-QtWeChatBackupMode -Requested 'auto' -TotalBytes ([long](1 * 1GB)) -DataRootKnown $false | Should -Be 'skip'
        Select-QtWeChatBackupMode -Requested 'copy' -TotalBytes ([long](1 * 1GB)) -DataRootKnown $false | Should -Be 'skip'
    }
    It '显式指定不被 auto 逻辑覆盖' {
        Select-QtWeChatBackupMode -Requested 'rename' -TotalBytes ([long](1 * 1GB)) | Should -Be 'rename'
    }

    It '估时估空间按 80 MB/s + 每万文件 10 s,空间 ×1.05' {
        $e = Get-QtWeChatBackupEstimate -Bytes ([long](22.36 * 1GB)) -Files 146954
        $e.Seconds | Should -BeGreaterThan 0
        $e.RequiredBytes | Should -BeGreaterThan ([long](22.36 * 1GB))
    }
}

Describe 'robocopy 备份 —— 退出码 < 8 算成功(退出码是位标志,§2.15)' {

    It '退出码判定' -ForEach @(
        @{ Code = 0; Ok = $true }
        @{ Code = 1; Ok = $true }
        @{ Code = 3; Ok = $true }
        @{ Code = 7; Ok = $true }
        @{ Code = 8; Ok = $false }
        @{ Code = 16; Ok = $false }
    ) {
        Mock -ModuleName QTrade.WeChat Test-QtPath { $true }
        Mock -ModuleName QTrade.WeChat New-QtDirectory { $Path }
        Mock -ModuleName QTrade.WeChat Invoke-QtProcess { [pscustomobject]@{ ExitCode = $Code; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        (Invoke-QtWeChatBackupCopy -Source 'X:\a' -Destination 'Y:\b' -LogPath 'Z:\l.log').Ok | Should -Be $Ok
    }

    It '源不存在 → 直接失败,不跑 robocopy' {
        Mock -ModuleName QTrade.WeChat Test-QtPath { $false }
        Mock -ModuleName QTrade.WeChat Invoke-QtProcess { throw '不该被调用' }
        (Invoke-QtWeChatBackupCopy -Source 'X:\none' -Destination 'Y:\b' -LogPath 'Z:\l.log').Ok | Should -BeFalse
    }
}

Describe 'rename 备份 —— 🔴 中途失败先改回,.qtbak-* 不许残留(§2.9.3 第 3 步)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-wx-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp 'xwechat_files') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp 'xwechat') -Force | Out-Null
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '两个目录各改一次名' {
        $r = Invoke-QtWeChatBackupRename -Directories @(
            (Join-Path $script:Tmp 'xwechat_files'), (Join-Path $script:Tmp 'xwechat')
        ) -Stamp '20260920-181500'
        $r.Ok | Should -BeTrue
        $r.Renamed.Count | Should -Be 2
        (Test-Path -LiteralPath (Join-Path $script:Tmp 'xwechat_files.qtbak-20260920-181500')) | Should -BeTrue
        (Test-Path -LiteralPath (Join-Path $script:Tmp 'xwechat.qtbak-20260920-181500')) | Should -BeTrue
    }

    It '装完改回原名' {
        $r = Invoke-QtWeChatBackupRename -Directories @((Join-Path $script:Tmp 'xwechat_files')) -Stamp '20260920-181500'
        Restore-QtWeChatBackupRename -Renamed $r.Renamed | Out-Null
        (Test-Path -LiteralPath (Join-Path $script:Tmp 'xwechat_files')) | Should -BeTrue
        (Test-Path -LiteralPath (Join-Path $script:Tmp 'xwechat_files.qtbak-20260920-181500')) | Should -BeFalse
    }

    It '不存在的目录被跳过,不算失败' {
        (Invoke-QtWeChatBackupRename -Directories @((Join-Path $script:Tmp 'nope'))).Ok | Should -BeTrue
    }
}

Describe 'hosts 屏蔽 —— 引擎只调端点,不直接改 hosts(§2.9.3 第 5 步 B 层 / docs/04 §2.5.4)' {

    It '域名清单为空 → skipped_no_domains(验收 M3.5-8)' {
        Mock -ModuleName QTrade.WeChat Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"ok":true,"action":"unchanged","domains":[]}' }
        }
        (Invoke-QtWeChatUpdateBlock -Enable $true).HostsBlock | Should -Be 'skipped_no_domains'
    }

    It '写入成功 → written' {
        Mock -ModuleName QTrade.WeChat Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"ok":true,"action":"added","domains":["dldir1.qq.com","dldir1v6.qq.com"],"backup":"C:\\x\\hosts.bak-1"}' }
        }
        $r = Invoke-QtWeChatUpdateBlock -Enable $true
        $r.HostsBlock | Should -Be 'written'
        $r.Domains.Count | Should -Be 2
        $r.Action | Should -Be 'added'
    }

    It 'hosts 只读 → degraded_readonly,**安装仍继续**(验收 M3.5-7 ④)' {
        Mock -ModuleName QTrade.WeChat Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"ok":false,"action":"unchanged","domains":["dldir1.qq.com"],"failed_reason":"readonly"}' }
        }
        $r = Invoke-QtWeChatUpdateBlock -Enable $true
        $r.HostsBlock | Should -Be 'degraded_readonly'
        $r.Ok | Should -BeFalse
    }

    It '端点不可达 → degraded_edr(仍不阻断,C 层版本守卫兜底)' {
        Mock -ModuleName QTrade.WeChat Invoke-QtHttp { [pscustomobject]@{ Ok = $false; StatusCode = 0; Body = 'refused' } }
        (Invoke-QtWeChatUpdateBlock -Enable $true).HostsBlock | Should -Be 'degraded_edr'
    }
}

Describe '微信数据根 ini 前缀展开(§2.9.1;🔴 不读 FileSavePath、不假设 Documents)' {

    It '裸路径原样' {
        Expand-QtWeChatPathPrefix -Value 'D:\Program Files\Tencent\Saved Files' | Should -Be 'D:\Program Files\Tencent\Saved Files'
    }
    It 'Appdata: 前缀展开' {
        Expand-QtWeChatPathPrefix -Value 'Appdata:Tencent\xwechat' | Should -Be (Join-Path $env:APPDATA 'Tencent\xwechat')
    }
    It 'MyDocument: 前缀展开' {
        Expand-QtWeChatPathPrefix -Value 'MyDocument:WeChat Files' | Should -Be (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'WeChat Files')
    }
}

Describe '三来源版本(§2.9.1;🔴 不用卸载键 DisplayVersion)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-wv-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp '4.1.12.26') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp 'data\xwechat_files\all_users') -Force | Out-Null
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '三者一致 → 不 ambiguous' {
        [IO.File]::WriteAllText((Join-Path $script:Tmp 'data\xwechat_files\all_users\whatsnew.dat'), '4.1.12.26')
        Mock -ModuleName QTrade.WeChat Get-QtFileVersion { '4.1.12.26' }
        $v = Get-QtWeChatVersion -InstallPath $script:Tmp -DataRoot (Join-Path $script:Tmp 'data')
        $v.Version | Should -Be '4.1.12.26'
        $v.VersionSubdir | Should -Be '4.1.12.26'
        $v.WhatsNew | Should -Be '4.1.12.26'
        $v.Ambiguous | Should -BeFalse
    }

    It '🔴 三者不一致 → 以 ①exe 文件版本为准并标 ambiguous(WECHAT_VERSION_AMBIGUOUS)' {
        [IO.File]::WriteAllText((Join-Path $script:Tmp 'data\xwechat_files\all_users\whatsnew.dat'), '4.1.13.12')
        Mock -ModuleName QTrade.WeChat Get-QtFileVersion { '4.1.12.26' }
        $v = Get-QtWeChatVersion -InstallPath $script:Tmp -DataRoot (Join-Path $script:Tmp 'data')
        $v.Version | Should -Be '4.1.12.26'
        $v.Ambiguous | Should -BeTrue
    }

    It '多个版本子目录取最大' {
        New-Item -ItemType Directory -Path (Join-Path $script:Tmp '4.1.13.12') -Force | Out-Null
        Mock -ModuleName QTrade.WeChat Get-QtFileVersion { '4.1.13.12' }
        (Get-QtWeChatVersion -InstallPath $script:Tmp).VersionSubdir | Should -Be '4.1.13.12'
    }
}


Describe 'CLIENTS_CHECKED 决策表 Get-QtWeChatPlan(§2.9.2 × /QT_WECHAT × 是否交互)' {

    It 'mode=skip → 一律 BLOCK,不碰微信' -ForEach @(
        @{ M = 'SUPPORTED' }, @{ M = 'UNSUPPORTED_OLDER' }, @{ M = 'NOT_INSTALLED' }
    ) {
        $p = Get-QtWeChatPlan -Mode 'skip' -Match $M
        $p.Action | Should -Be 'BLOCK'
        $p.NeedReinstall | Should -BeFalse
    }

    It 'SUPPORTED → KEEP(不动微信、不备份)' {
        $p = Get-QtWeChatPlan -Mode 'check' -Match 'SUPPORTED'
        $p.Action | Should -Be 'KEEP'
        $p.NeedBackup | Should -BeFalse
        $p.NeedReinstall | Should -BeFalse
    }

    It '🔴 UNSUPPORTED_* 且**无人确认** → BLOCK(红线 8:用户确认后才动)' -ForEach @(
        @{ M = 'UNSUPPORTED_OLDER' }, @{ M = 'UNSUPPORTED_NEWER' }
    ) {
        $p = Get-QtWeChatPlan -Mode 'check' -Match $M -UserApproved $false
        $p.Action | Should -Be 'BLOCK'
        $p.Reason | Should -Be 'declined_reinstall'
        $p.NeedReinstall | Should -BeFalse
    }

    It 'UNSUPPORTED_* + 用户确认 → REINSTALL_BUNDLED 并要备份' {
        $p = Get-QtWeChatPlan -Mode 'check' -Match 'UNSUPPORTED_OLDER' -UserApproved $true
        $p.Action | Should -Be 'REINSTALL_BUNDLED'
        $p.NeedReinstall | Should -BeTrue
        $p.NeedBackup | Should -BeTrue
    }

    It 'backup_mode=skip 时不要备份(用户自己选的)' {
        (Get-QtWeChatPlan -Mode 'check' -Match 'UNSUPPORTED_OLDER' -UserApproved $true -BackupMode 'skip').NeedBackup | Should -BeFalse
    }

    It 'NOT_INSTALLED + 确认 → 装随包,**首装无需备份**(§2.9.3 方案①)' {
        $p = Get-QtWeChatPlan -Mode 'check' -Match 'NOT_INSTALLED' -UserApproved $true
        $p.Action | Should -Be 'REINSTALL_BUNDLED'
        $p.NeedBackup | Should -BeFalse
        $p.Reason | Should -Be 'fresh_install'
    }

    It 'NOT_INSTALLED 且用户说否 → BLOCK,安装继续(验收 M3.5-1)' {
        (Get-QtWeChatPlan -Mode 'check' -Match 'NOT_INSTALLED' -UserApproved $false).Action | Should -Be 'BLOCK'
    }

    It 'MULTIPLE_INSTALLS 没人选 → BLOCK,但标出 NeedSelect(其余一处不动)' {
        $p = Get-QtWeChatPlan -Mode 'check' -Match 'MULTIPLE_INSTALLS' -UserApproved $false
        $p.Action | Should -Be 'BLOCK'
        $p.NeedSelect | Should -BeTrue
    }

    It '🔴 静默模式:mode 先经 Resolve 降级为 check,再无人确认 ⇒ BLOCK(验收 M3.5-5)' {
        $mode = Resolve-QtWeChatMode -Requested 'reinstall' -Silent $true
        $mode | Should -Be 'check'
        (Get-QtWeChatPlan -Mode $mode -Match 'UNSUPPORTED_OLDER' -UserApproved $false).Action | Should -Be 'BLOCK'
    }
}


Describe '§2.9.3 两方案的选择 Resolve-QtWeChatReinstallMode' {

    It '默认走方案①(覆盖安装,不卸载)' {
        (Resolve-QtWeChatReinstallMode -CurrentVersion '4.1.13.12').Mode | Should -Be 'overwrite'
    }
    It '🔴 方案①失败 → 转方案②' {
        $r = Resolve-QtWeChatReinstallMode -CurrentVersion '4.1.13.12' -OverwriteFailed $true
        $r.Mode | Should -Be 'interactive_uninstall'
        $r.Reason | Should -Be 'overwrite_failed'
    }
    It '🔴 3.x 升 4.x → 方案②(两套安装布局,覆盖装不过去)' {
        (Resolve-QtWeChatReinstallMode -CurrentVersion '3.9.12.51').Reason | Should -Be 'major_3_to_4'
    }
    It 'MULTIPLE_INSTALLS 用户要清掉一处 → 方案②' {
        (Resolve-QtWeChatReinstallMode -CurrentVersion '4.1.12.26' -UserAskedRemoveOne $true).Reason | Should -Be 'user_asked_remove_one'
    }
    It '🔴 静默模式 → none(P-19:重装恒需交互)' {
        (Resolve-QtWeChatReinstallMode -CurrentVersion '3.9.12.51' -InteractiveAllowed $false).Mode | Should -Be 'none'
    }
}

Describe '§2.9.3 第 4 步换版本编排 Invoke-QtWeChatReinstall' {

    It '方案①成功 → 不碰卸载器' {
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup { [pscustomobject]@{ Ok = $true; Reason = ''; FinalVersion = '4.1.12.26' } }
        Mock -ModuleName QTrade.WeChat Start-QtWeChatInteractiveUninstall { throw '不该被调用' }
        $r = Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '4.1.13.12'
        $r.Ok | Should -BeTrue
        $r.Mode | Should -Be 'overwrite'
        $r.FinalVersion | Should -Be '4.1.12.26'
    }

    It '🔴 方案①失败但**用户没确认卸载** → 不卸载,回 WECHAT_REINSTALL_FAILED' {
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup { [pscustomobject]@{ Ok = $false; Reason = 'WECHAT_REINSTALL_FAILED'; FinalVersion = '' } }
        Mock -ModuleName QTrade.WeChat Start-QtWeChatInteractiveUninstall { throw '没有用户确认就不许碰卸载器(红线 8)' }
        $r = Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '4.1.13.12' -UninstallString 'C:\wx\Uninstall.exe'
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'WECHAT_REINSTALL_FAILED'
    }

    It '方案①失败 + 用户确认 → 卸载后再装' {
        $script:SetupCalls = 0
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup {
            $script:SetupCalls++
            if ($script:SetupCalls -eq 1) { return [pscustomobject]@{ Ok = $false; Reason = 'WECHAT_REINSTALL_FAILED'; FinalVersion = '' } }
            return [pscustomobject]@{ Ok = $true; Reason = ''; FinalVersion = '4.1.12.26' }
        }
        Mock -ModuleName QTrade.WeChat Start-QtWeChatInteractiveUninstall { [pscustomobject]@{ Ok = $true; Reason = '' } }
        $r = Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '4.1.13.12' `
            -UninstallString 'C:\wx\Uninstall.exe' -UserConfirmedUninstall $true
        $r.Ok | Should -BeTrue
        $r.Mode | Should -Be 'interactive_uninstall'
        $script:SetupCalls | Should -Be 2
    }

    It '🔴 3.x 一上来就要卸载确认,没确认就不动(红线 8)' {
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup { throw '3.x 不该走覆盖安装' }
        Mock -ModuleName QTrade.WeChat Start-QtWeChatInteractiveUninstall { throw '没确认不许动' }
        (Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '3.9.12.51').Reason |
            Should -Be 'need_uninstall_confirm'
    }

    It '🔴 卸载轮询超时 → UninstallTimedOut=true(交给向导问【我已卸载,继续】/【跳过】,不自己决定)' {
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup { [pscustomobject]@{ Ok = $false; Reason = 'WECHAT_REINSTALL_FAILED'; FinalVersion = '' } }
        Mock -ModuleName QTrade.WeChat Start-QtWeChatInteractiveUninstall { [pscustomobject]@{ Ok = $false; Reason = 'UNINSTALL_TIMEOUT' } }
        $r = Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '4.1.13.12' `
            -UninstallString 'C:\wx\Uninstall.exe' -UserConfirmedUninstall $true
        $r.Ok | Should -BeFalse
        $r.UninstallTimedOut | Should -BeTrue
    }

    It '没有 UninstallString(卸载键读不到)→ 明确回 no_uninstall_string' {
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup { [pscustomobject]@{ Ok = $false; Reason = 'WECHAT_REINSTALL_FAILED'; FinalVersion = '' } }
        (Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '4.1.13.12' -UserConfirmedUninstall $true).Reason |
            Should -Be 'no_uninstall_string'
    }

    It '🔴 静默模式下整条重装路不可用(P-19),且**绝不穿透到方案②**' {
        Mock -ModuleName QTrade.WeChat Start-QtWeChatSetup { throw '静默模式不该重装' }
        Mock -ModuleName QTrade.WeChat Start-QtWeChatInteractiveUninstall { throw '无人值守的机器上绝不许开卸载器' }
        $r = Invoke-QtWeChatReinstall -SetupExe 'X:\s.exe' -InstallPath 'C:\wx' -CurrentVersion '4.1.13.12' `
            -UninstallString 'C:\wx\Uninstall.exe' -UserConfirmedUninstall $true -AllowInteractiveUninstall $false
        $r.Ok | Should -BeFalse
        $r.Mode | Should -Be 'none'
        $r.Reason | Should -Be 'silent_not_supported'
    }
}

Describe '§2.9.3 末 MULTIPLE_INSTALLS 的列表(路径 / 版本 / 是否正在运行)' {

    It '逐处补上版本与运行状态' {
        Mock -ModuleName QTrade.WeChat Get-QtWeChatVersion {
            [pscustomobject]@{ Version = '4.1.12.26'; ExeVersion = '4.1.12.26'; VersionSubdir = ''; WhatsNew = ''; Ambiguous = $false }
        }
        Mock -ModuleName QTrade.WeChat Get-QtProcessByName { @([pscustomobject]@{ Path = 'D:\Tencent\Weixin\Weixin.exe' }) }
        $d = Get-QtWeChatInstallDetails -Installs @(
            [pscustomobject]@{ path = 'C:\Program Files\Tencent\Weixin'; uninstall_string = 'C:\u.exe'; source = 'uninstall_4x' }
            [pscustomobject]@{ path = 'D:\Tencent\Weixin'; uninstall_string = ''; source = 'running' }
        )
        $d.Count | Should -Be 2
        $d[0].version | Should -Be '4.1.12.26'
        $d[0].running | Should -BeFalse
        $d[1].running | Should -BeTrue
        $d[0].uninstall_string | Should -Be 'C:\u.exe'
    }

    It '空列表不抛' {
        (Get-QtWeChatInstallDetails -Installs @()).Count | Should -Be 0
    }
}
