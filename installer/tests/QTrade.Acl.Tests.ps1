#requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0.0' }
<#
    安装根 ACL 收紧(裁决 11)的单测。

    🔴 全程**不碰真实文件系统的 ACL**:Get-QtAcl / Set-QtAcl 是 Native 接缝,这里全 Mock,
       夹具用**内存里构造的真 DirectorySecurity 对象**(不是假对象)——
       所以 SID 翻译、继承标志、权限位这些真正容易错的地方,测的是 .NET 的真实语义。
#>

BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Native.psm1') -Force -DisableNameChecking
    Import-Module (Join-Path $script:ModulesDir 'QTrade.Acl.psm1') -Force -DisableNameChecking

    $script:SidAdmins = 'S-1-5-32-544'
    $script:SidSystem = 'S-1-5-18'
    $script:SidUsers  = 'S-1-5-32-545'

    # 在内存里拼一份 DirectorySecurity
    $script:NewAcl = {
        param([bool] $Protected, [object[]] $Rules)
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($Protected, $false)
        foreach ($r in $Rules) {
            $inherit = if ($r.ContainsKey('Inherit')) { $r.Inherit } else {
                [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [Security.AccessControl.InheritanceFlags]::ObjectInherit
            }
            $type = if ($r.ContainsKey('Type')) { $r.Type } else { [Security.AccessControl.AccessControlType]::Allow }
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
                (New-Object Security.Principal.SecurityIdentifier($r.Sid)),
                $r.Rights, $inherit, [Security.AccessControl.PropagationFlags]::None, $type)))
        }
        return $acl
    }

    $script:GoodAcl = {
        & $script:NewAcl $true @(
            @{ Sid = $script:SidAdmins; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidSystem; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidUsers;  Rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute }
        )
    }

    # %ProgramData% 的默认样子:继承着、Users 还能建文件
    $script:DefaultProgramDataAcl = {
        & $script:NewAcl $false @(
            @{ Sid = $script:SidAdmins; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidSystem; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidUsers;  Rights = ([Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
                                                  [Security.AccessControl.FileSystemRights]::Write) }
        )
    }
}

Describe 'Get-QtInstallRootAclSpec —— 期望的 ACE 表' {
    It '正好三条:Administrators / SYSTEM / Users' {
        $spec = Get-QtInstallRootAclSpec
        $spec.Count | Should -Be 3
        $spec.Sid | Should -Contain 'S-1-5-32-544'
        $spec.Sid | Should -Contain 'S-1-5-18'
        $spec.Sid | Should -Contain 'S-1-5-32-545'
    }
    It '🔴 用众所周知 SID 而不是账户名(中文 Windows 上 Administrators/Users 是本地化显示名)' {
        foreach ($s in (Get-QtInstallRootAclSpec)) {
            $s.Sid | Should -Match '^S-1-'
        }
    }
    It 'Users 只有 ReadAndExecute' {
        $u = (Get-QtInstallRootAclSpec | Where-Object { $_.Sid -eq 'S-1-5-32-545' })
        $u.Rights | Should -Be ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    }
}

Describe 'Test-QtInstallRootAcl —— 判据' {
    It '收紧后的 ACL 判为合规' {
        (Test-QtInstallRootAcl -Acl (& $script:GoodAcl)).Ok | Should -BeTrue
    }

    It '🔴 %ProgramData% 默认样子判为不合规(继承没去 + Users 可写)' {
        $r = Test-QtInstallRootAcl -Acl (& $script:DefaultProgramDataAcl)
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*继承没去掉*'
        ($r.Problems -join ' ') | Should -BeLike '*Users 仍带写*'
    }

    It '🔴 Users 只要沾上写位就不合规(DLL 植入面)' {
        $acl = & $script:NewAcl $true @(
            @{ Sid = $script:SidAdmins; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidSystem; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidUsers;  Rights = ([Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
                                                  [Security.AccessControl.FileSystemRights]::CreateFiles) }
        )
        (Test-QtInstallRootAcl -Acl $acl).Ok | Should -BeFalse
    }

    It '🔴 多出一条期望之外的 Allow ACE 就不合规(比如 CREATOR OWNER 漏网)' {
        $acl = & $script:GoodAcl
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier('S-1-3-0')),
            [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)))
        $r = Test-QtInstallRootAcl -Acl $acl
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*期望之外*'
    }

    It '🔴 ACE 不带继承标志就不合规(只收紧了根目录,子目录照旧敞着)' {
        $acl = & $script:NewAcl $true @(
            @{ Sid = $script:SidAdmins; Rights = [Security.AccessControl.FileSystemRights]::FullControl; Inherit = [Security.AccessControl.InheritanceFlags]::None }
            @{ Sid = $script:SidSystem; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidUsers;  Rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute }
        )
        $r = Test-QtInstallRootAcl -Acl $acl
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*继承*'
    }

    It '缺 SYSTEM 就不合规(卸载/修复会删不掉自己装的东西)' {
        $acl = & $script:NewAcl $true @(
            @{ Sid = $script:SidAdmins; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidUsers;  Rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute }
        )
        (Test-QtInstallRootAcl -Acl $acl).Ok | Should -BeFalse
    }

    It 'Users 连读都没有也不合规(控制台/引擎会起不来)' {
        $acl = & $script:NewAcl $true @(
            @{ Sid = $script:SidAdmins; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidSystem; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
            @{ Sid = $script:SidUsers;  Rights = [Security.AccessControl.FileSystemRights]::ReadData }
        )
        (Test-QtInstallRootAcl -Acl $acl).Ok | Should -BeFalse
    }

    It '🔴 位掩码坑:FullControl 含读位,禁止位掩码里不能放复合掩码' {
        # FullControl = 0x1F01FF、Modify = 0x301BF,都把 ReadData/Synchronize 这些读位包在内。
        # 一旦把它们 OR 进「Users 禁止位」,连一条纯只读 ACE 都会被 -band 出非零而误报违规。
        # 这条测试盯的就是那个误报:收紧得完全正确的 ACL 必须判合规。
        ([int][Security.AccessControl.FileSystemRights]::FullControl -band
         [int][Security.AccessControl.FileSystemRights]::ReadData) | Should -Not -Be 0
        $acl = & $script:GoodAcl
        # 实际落地的 Users ACE 是 `ReadAndExecute, Synchronize`,与 FullControl 的 Synchronize 位重叠
        $usersAce = @($acl.Access | Where-Object { (ConvertTo-QtAclSid -Identity $_.IdentityReference) -eq $script:SidUsers })[0]
        ([int]$usersAce.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Synchronize) | Should -Not -Be 0
        (Test-QtInstallRootAcl -Acl $acl).Ok | Should -BeTrue
    }

    It 'ACL 为 null 不崩,报「拿不到 ACL」' {
        $r = Test-QtInstallRootAcl -Acl $null
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*拿不到 ACL*'
    }
}

Describe 'Set-QtInstallRootAcl —— 收紧动作' {
    BeforeEach {
        $script:SetCalls = 0
        Mock -ModuleName QTrade.Acl Test-QtPath { return $true }
    }

    It '🔴 幂等:已经收紧过就一个字节都不写' {
        Mock -ModuleName QTrade.Acl Get-QtAcl { return (& $script:GoodAcl) }
        Mock -ModuleName QTrade.Acl Set-QtAcl { $script:SetCalls++ }
        $r = Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade'
        $r.Ok | Should -BeTrue
        $r.Changed | Should -BeFalse
        Should -Invoke -ModuleName QTrade.Acl Set-QtAcl -Times 0 -Exactly
    }

    It '默认 ACL → 真的改,且改完读回是合规的' {
        # 第一次读回默认样子,Set 之后再读回收紧后的样子
        Mock -ModuleName QTrade.Acl Get-QtAcl {
            if ($script:SetCalls -eq 0) { return (& $script:DefaultProgramDataAcl) }
            return (& $script:GoodAcl)
        }
        Mock -ModuleName QTrade.Acl Set-QtAcl { $script:SetCalls++ }
        $r = Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade'
        $r.Changed | Should -BeTrue
        $r.Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Acl Set-QtAcl -Times 1 -Exactly
    }

    It '🔴 Set-Acl 不抛 ≠ 真的生效:读回仍不合规就判失败' {
        # 模拟被组策略挡下来 —— Set-Acl 静默无效,DACL 没变
        Mock -ModuleName QTrade.Acl Get-QtAcl { return (& $script:DefaultProgramDataAcl) }
        Mock -ModuleName QTrade.Acl Set-QtAcl { $script:SetCalls++ }
        $r = Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade'
        $r.Ok | Should -BeFalse
        $r.Problems.Count | Should -BeGreaterThan 0
    }

    It '写 ACL 抛异常 → 回失败而不是把异常抛给调用方(ACL 不该阻断安装)' {
        Mock -ModuleName QTrade.Acl Get-QtAcl { return (& $script:DefaultProgramDataAcl) }
        Mock -ModuleName QTrade.Acl Set-QtAcl { throw '拒绝访问' }
        $r = Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade'
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*写 ACL 失败*'
    }

    It '读 ACL 抛异常 → 同样回失败,不抛' {
        Mock -ModuleName QTrade.Acl Get-QtAcl { throw '路径不可达' }
        Mock -ModuleName QTrade.Acl Set-QtAcl { $script:SetCalls++ }
        $r = Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade'
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*读 ACL 失败*'
    }

    It '安装根不存在 → 明确报出来,不去碰 ACL' {
        Mock -ModuleName QTrade.Acl Test-QtPath { return $false }
        Mock -ModuleName QTrade.Acl Get-QtAcl { throw '不该被调用' }
        $r = Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade'
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -BeLike '*安装根不存在*'
    }

    It '🔴 去继承时不把继承来的 ACE 复制成显式的(复制了等于没收紧)' {
        $captured = $null
        Mock -ModuleName QTrade.Acl Get-QtAcl { return (& $script:DefaultProgramDataAcl) }
        Mock -ModuleName QTrade.Acl Set-QtAcl { $script:SetCalls++; $script:Captured = $AclObject }
        Set-QtInstallRootAcl -Path 'C:\ProgramData\QTrade' | Out-Null
        $captured = $script:Captured
        $captured | Should -Not -BeNullOrEmpty
        $captured.AreAccessRulesProtected | Should -BeTrue
        # 交上去的那份必须正好是三条期望 ACE
        (Test-QtInstallRootAcl -Acl $captured).Ok | Should -BeTrue
    }
}

Describe 'Set-QtAclHardened —— 结果落进 install_state' {
    BeforeAll {
        Import-Module (Join-Path $script:ModulesDir 'QTrade.State.psm1') -Force -DisableNameChecking
    }
    It '新建的 state 自带 acl_hardened / acl_problems 字段' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        $s.acl_hardened | Should -BeFalse
        $s.acl_problems | Should -BeNullOrEmpty
    }
    It '成功结果写进去' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtAclHardened -State $s -Result ([pscustomobject]@{ Ok = $true; Changed = $true; Problems = @() }) | Out-Null
        $s.acl_hardened | Should -BeTrue
    }
    It '失败结果连同问题清单一起写进去(不阻断安装,但必须留痕)' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        Set-QtAclHardened -State $s -Result ([pscustomobject]@{ Ok = $false; Changed = $true; Problems = @('Users 仍带写') }) | Out-Null
        $s.acl_hardened | Should -BeFalse
        $s.acl_problems | Should -Contain 'Users 仍带写'
    }
    It '🔴 旧版本落盘的 state 没有这两个字段 —— 必须补上而不是抛' {
        # StrictMode 下给 PSCustomObject 赋一个不存在的属性会直接抛,续跑正好会走到这条路
        $legacy = [pscustomobject]@{ schema = 1; state = 'PRECHECK' }
        { Set-QtAclHardened -State $legacy -Result ([pscustomobject]@{ Ok = $true; Problems = @() }) } | Should -Not -Throw
        $legacy.acl_hardened | Should -BeTrue
    }
    It 'Result 为 null 也不崩' {
        $s = New-QtInstallState -PackageVersion '1.0.0'
        { Set-QtAclHardened -State $s -Result $null } | Should -Not -Throw
        $s.acl_hardened | Should -BeFalse
    }
}
