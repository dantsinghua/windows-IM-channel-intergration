<#
    QTrade.Acl —— 安装根的 ACL 收紧(裁决 11 / 建议 11,总控 2026-09-20 采纳)。

    【为什么要这件事】
      §2.1 要求安装根是 `%ProgramData%\QTrade`。而 `%ProgramData%` 的**默认 ACL**
      给 `BUILTIN\Users` 继承了「创建文件 / 创建文件夹」的权限(CREATOR OWNER 那一套),
      所以在默认状态下:**任何一个普通用户都能往安装根里写文件**。

      这不是理论风险,有两条现成的利用路径:
        1. 🔴 安装根是引擎进程的**当前目录**(自编存根 `SetCurrentDir(<InstallPath>)`,
           且子进程继承),而当前目录在 Windows 默认 DLL 搜索序列里 ——
           普通用户在那放一个同名 DLL,就能让**以管理员身份运行**的引擎加载它。
        2. 安装根下还有 `install\install_state.json`、`manifest.json`、引擎 EXE 本身;
           可写就意味着可篡改续跑状态、可替换将要被执行的文件。

      第四批还实打实撞过这套 ACL 的另一面:往 `%ProgramData%\QTrade` 写进去的东西,
      因为 Users 只有 `ReadAndExecute + Write`(**没有 Delete**),普通身份删不掉。

    【收紧成什么样】
      去继承(不复制继承来的 ACE),只留三条显式 ACE,全部按容器+对象继承:
        * Administrators(S-1-5-32-544) FullControl
        * SYSTEM        (S-1-5-18)     FullControl
        * Users         (S-1-5-32-545) ReadAndExecute —— 只读执行,**不含任何写/建/删**

      卸载不需要额外做什么:Administrators 有完全控制,§2.14 删 `paths.Root` 时随目录一起没。

    【幂等】
      先读回 ACL 比对(`Test-QtInstallRootAcl`),已经对了就一个字节都不写。
      改完再读回比对一次 —— 这既是幂等判据,也是「真的生效了吗」的唯一凭据
      (`Set-Acl` 不抛异常 ≠ DACL 变成了你要的样子)。
#>

Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking

# 🔴 一律用**众所周知 SID**,不用账户名。
#    "Administrators" / "Users" 在中文 Windows 上是本地化显示名,
#    按名字建 ACE 会失败、按名字比对读回结果会错判。SID 与系统语言无关。
$script:QtAclSidAdministrators = 'S-1-5-32-544'
$script:QtAclSidSystem         = 'S-1-5-18'
$script:QtAclSidUsers          = 'S-1-5-32-545'

# Users 一旦沾上这里任何一个位,收紧就等于没做。
# FileSystemRights.Write 本身就是 WriteData|AppendData|WriteExtendedAttributes|WriteAttributes 的合成位。
#
# 🔴 这里**只能放真正危险的原子位**,绝不能把 Modify / FullControl 这种**复合掩码**
#    OR 进来:`FullControl` = 0x1F01FF、`Modify` = 0x301BF,它们把 ReadData / ReadAttributes /
#    Synchronize 这些**读位也包含在内**。OR 进去之后,任何一条哪怕只有只读权限的 ACE
#    都会 `-band` 出非零 —— 于是「收紧得完全正确的 ACL」反而被判成违规。
#    (实测:Users 的 ReadAndExecute ACE 实际是 `ReadAndExecute, Synchronize` = 0x120089,
#     与 FullControl 的 Synchronize 位重叠,直接误报。)
#    Modify / FullControl 里真正危险的位(写/删/改权限/夺取所有权)下面都已单独列出,
#    所以不放复合掩码并不会放松判据。
$script:QtAclForbiddenForUsers =
    [Security.AccessControl.FileSystemRights]::Write -bor
    [Security.AccessControl.FileSystemRights]::Delete -bor
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [Security.AccessControl.FileSystemRights]::TakeOwnership

function Get-QtInstallRootAclSpec {
    <#
    .SYNOPSIS
        安装根期望的 ACE 表(纯数据,便于单测直接比对)。
    #>
    [CmdletBinding()]
    param()
    return @(
        [pscustomobject]@{ Sid = $script:QtAclSidAdministrators; Rights = [Security.AccessControl.FileSystemRights]::FullControl;     Name = 'Administrators' }
        [pscustomobject]@{ Sid = $script:QtAclSidSystem;         Rights = [Security.AccessControl.FileSystemRights]::FullControl;     Name = 'SYSTEM' }
        [pscustomobject]@{ Sid = $script:QtAclSidUsers;          Rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute;  Name = 'Users' }
    )
}

function ConvertTo-QtAclSid {
    <#  把 ACE 的 IdentityReference 归一成 SID 字符串;翻不动就回空串(调用方按「非期望身份」处理)。 #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][AllowNull()][object] $Identity)
    if ($null -eq $Identity) { return '' }
    try {
        return [string]$Identity.Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        return ''
    }
}

function Test-QtInstallRootAcl {
    <#
    .SYNOPSIS
        比对一份 ACL 是否已经是收紧后的样子。回 { Ok; Problems[] }。
    .NOTES
        判据四条,缺一不可:
          1. 已去继承(AreAccessRulesProtected = true)——否则 %ProgramData% 的默认放行会继承回来;
          2. Users 不带任何写/建/删/改权位;
          3. Administrators 与 SYSTEM 都有 FullControl(否则卸载和修复会删不掉自己装的东西);
          4. 没有期望之外的 Allow 身份(多出来一个 CREATOR OWNER 就前功尽弃)。
        三条 ACE 都必须带容器+对象继承,否则只收紧了根目录、子目录照旧敞着。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowNull()][object] $Acl)

    $problems = New-Object System.Collections.Generic.List[string]
    if ($null -eq $Acl) {
        $problems.Add('拿不到 ACL')
        return [pscustomobject]@{ Ok = $false; Problems = @($problems.ToArray()) }
    }

    if (-not (Test-QtHasProperty -Object $Acl -Name 'AreAccessRulesProtected') -or -not $Acl.AreAccessRulesProtected) {
        $problems.Add('继承没去掉(AreAccessRulesProtected=false)—— %ProgramData% 的默认「Users 可建文件」会继承回来')
    }

    $allow = @()
    if (Test-QtHasProperty -Object $Acl -Name 'Access') {
        $allow = @($Acl.Access | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow })
    }

    $wanted = @{}
    foreach ($spec in Get-QtInstallRootAclSpec) { $wanted[$spec.Sid] = $spec }

    $seen = @{}
    foreach ($ace in $allow) {
        $sid = ConvertTo-QtAclSid -Identity $ace.IdentityReference
        if (-not $wanted.ContainsKey($sid)) {
            $problems.Add(('多出一条期望之外的 Allow ACE:{0}' -f $ace.IdentityReference))
            continue
        }
        $rights = [int]$ace.FileSystemRights
        if (-not $seen.ContainsKey($sid)) { $seen[$sid] = 0 }
        $seen[$sid] = $seen[$sid] -bor $rights

        $ci = ($ace.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ContainerInherit) -ne 0
        $oi = ($ace.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ObjectInherit) -ne 0
        if (-not ($ci -and $oi)) {
            $problems.Add(('{0} 的 ACE 没有同时带容器+对象继承 —— 只收紧了根目录,子目录照旧敞着' -f $wanted[$sid].Name))
        }
    }

    foreach ($spec in Get-QtInstallRootAclSpec) {
        if (-not $seen.ContainsKey($spec.Sid)) {
            $problems.Add(('缺 {0} 的 Allow ACE' -f $spec.Name))
            continue
        }
        $got = $seen[$spec.Sid]
        if ($spec.Sid -eq $script:QtAclSidUsers) {
            if (($got -band [int]$script:QtAclForbiddenForUsers) -ne 0) {
                $problems.Add('Users 仍带写/建/删/改权位 —— 普通用户还能往安装根里放 DLL')
            }
            if (($got -band [int][Security.AccessControl.FileSystemRights]::ReadAndExecute) -ne [int][Security.AccessControl.FileSystemRights]::ReadAndExecute) {
                $problems.Add('Users 连读+执行都不全 —— 控制台/引擎会起不来')
            }
        } elseif (($got -band [int][Security.AccessControl.FileSystemRights]::FullControl) -ne [int][Security.AccessControl.FileSystemRights]::FullControl) {
            $problems.Add(('{0} 不是 FullControl —— 卸载/修复会删不掉自己装的东西' -f $spec.Name))
        }
    }

    return [pscustomobject]@{ Ok = ($problems.Count -eq 0); Problems = @($problems.ToArray()) }
}

function Set-QtInstallRootAcl {
    <#
    .SYNOPSIS
        把安装根收紧到 Get-QtInstallRootAclSpec 的样子。幂等:已经对了就不写。
    .OUTPUTS
        { Ok; Changed; Problems[]; Path }
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)

    if (-not (Test-QtPath -Path $Path)) {
        return [pscustomobject]@{ Ok = $false; Changed = $false; Path = $Path; Problems = @(('安装根不存在:{0}' -f $Path)) }
    }

    try {
        $acl = Get-QtAcl -Path $Path
    } catch {
        return [pscustomobject]@{ Ok = $false; Changed = $false; Path = $Path; Problems = @(('读 ACL 失败:{0}' -f $_.Exception.Message)) }
    }

    # 幂等判据:先读回比对,已经是收紧后的样子就一个字节都不写
    $before = Test-QtInstallRootAcl -Acl $acl
    if ($before.Ok) {
        return [pscustomobject]@{ Ok = $true; Changed = $false; Path = $Path; Problems = @() }
    }

    try {
        # 去继承,且**不**把继承来的 ACE 复制成显式的 —— 复制了等于什么也没收紧
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }

        $inherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                   [Security.AccessControl.InheritanceFlags]::ObjectInherit
        foreach ($spec in Get-QtInstallRootAclSpec) {
            $ace = New-Object Security.AccessControl.FileSystemAccessRule(
                (New-Object Security.Principal.SecurityIdentifier($spec.Sid)),
                $spec.Rights,
                $inherit,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow)
            $acl.AddAccessRule($ace)
        }
        Set-QtAcl -Path $Path -AclObject $acl
    } catch {
        return [pscustomobject]@{ Ok = $false; Changed = $false; Path = $Path; Problems = @(('写 ACL 失败:{0}' -f $_.Exception.Message)) }
    }

    # 🔴 读回再比一次。Set-Acl 不抛异常 ≠ DACL 真的变成了你要的样子
    #    (被组策略/权限限制挡下来时它也可能静默无效)。
    try {
        $after = Test-QtInstallRootAcl -Acl (Get-QtAcl -Path $Path)
    } catch {
        return [pscustomobject]@{ Ok = $false; Changed = $true; Path = $Path; Problems = @(('写完读回失败:{0}' -f $_.Exception.Message)) }
    }
    return [pscustomobject]@{ Ok = $after.Ok; Changed = $true; Path = $Path; Problems = @($after.Problems) }
}

Export-ModuleMember -Function Get-QtInstallRootAclSpec, ConvertTo-QtAclSid, Test-QtInstallRootAcl, Set-QtInstallRootAcl
