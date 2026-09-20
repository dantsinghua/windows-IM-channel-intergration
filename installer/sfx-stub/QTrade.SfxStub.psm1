<#
    QTrade.SfxStub —— 自编 SFX 存根的取源、打补丁、探工具链、构建、验货。

    这个模块**只在打包机上跑**,不随安装包发行(collect-payload 不收 sfx-stub/)。
    它与 engine/modules/ 下那批运行期模块是两套东西,不要互相 import。

    docs/03 §2.1 / 第五批(安琳 2026-09-20 拍板「重编 SFX 存根」)。
#>

Set-StrictMode -Version Latest

# ── 源码出处(逐字钉死)────────────────────────────────────────────────────
# 官方 LZMA SDK 23.01。sha256 由安琳在任务里给定,下载后**先校验再解压**。
$script:QtSdkFileName = 'lzma2301.7z'
$script:QtSdkUrl      = 'https://www.7-zip.org/a/lzma2301.7z'
$script:QtSdkSha256   = '317DD834D6BBFD95433488B832E823CD3D4D420101436422C03AF88507DD1370'

# 重编需要这三棵子树:CPP 是 C++ 实现,C 是 LZMA 解码核,
# Asm 是 CRC 的汇编优化 —— `Crc.mak` 会去引 `Asm/x86/7zCrcOpt.asm`,
# 少了它 nmake 直接 `U1073: 不知道如何生成…`(实测踩过)。
# CS/Java/DOC/bin 对 SFXSetup 的 nmake 目标没有用,不解出来省时间也少噪音。
$script:QtSdkSubtrees = @('C', 'CPP', 'Asm')

# SFXSetup 在 SDK 里的位置,以及 nmake 的产物名(makefile 里 PROG = 7zS.sfx)。
$script:QtSfxProjectDir = 'CPP\7zip\Bundles\SFXSetup'
$script:QtSfxMakeOutput  = '7zS.sfx'

function Get-QtSfxSdkInfo {
    <#  取源信息;fetch-sdk.ps1 与测试都从这里读,避免两处各写一份 sha256。 #>
    [CmdletBinding()]
    param()
    return [pscustomobject]@{
        FileName    = $script:QtSdkFileName
        Url         = $script:QtSdkUrl
        Sha256      = $script:QtSdkSha256
        Subtrees    = @($script:QtSdkSubtrees)
        ProjectDir  = $script:QtSfxProjectDir
        MakeOutput  = $script:QtSfxMakeOutput
    }
}

function Assert-QtSha256 {
    <#  校验文件摘要。不匹配就抛 —— 这是供应链上唯一的关卡,不给 -Force 之类的旁路。 #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Expected
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "要校验的文件不存在:$Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    $want   = $Expected.ToUpperInvariant()
    if ($actual -ne $want) {
        throw ("sha256 不符,拒绝使用:`n  文件 = {0}`n  期望 = {1}`n  实际 = {2}" -f $Path, $want, $actual)
    }
    return $actual
}

# ── 补丁应用 ──────────────────────────────────────────────────────────────
#
#  为什么自己写一个 unified diff 应用器:本机 Windows 侧**没有** git.exe,也没有
#  patch.exe(实测),而 fetch/build 全在 Windows 上跑。与其要求装一个新工具,
#  不如把这几十行写清楚 —— 而且它是严格的:上下文对不上就抛,绝不做模糊匹配、
#  绝不像 GNU patch 那样「偏移 N 行后找到了」就悄悄应用。补丁一旦和源码版本
#  对不上,我们要的是当场炸,不是一份错位打进去的源码。

function ConvertTo-QtDiffLines {
    <#  把文本切成「无行尾符」的行数组。

        🔴 行尾:SDK 源码是 CRLF,GNU diff 生成的补丁里,内容行**结尾带着那个 CR**
        (对 diff 来说 CR 是行内容的一部分)。而补丁文件本身经过 git/编辑器之后
        也可能被 CRLF 化,于是同一行可能变成 "内容\r\r"。
        C++ 源码里没有哪一行的内容真的以 CR 结尾,所以统一把**结尾所有 CR 剥掉**,
        两边都剥、再逐字比较,对两种表示都成立。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Text)

    $lines = $Text -split "`n"
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($l in $lines) { $out.Add(($l -replace "`r+$", '')) }
    # 文本以换行结尾时,split 会多出一个空元素 —— 那不是一行。
    if ($out.Count -gt 0 -and $out[$out.Count - 1] -eq '' -and $Text.EndsWith("`n")) {
        $out.RemoveAt($out.Count - 1)
    }
    return , $out.ToArray()
}

function ConvertFrom-QtUnifiedDiff {
    <#  解析补丁文本 → 每个文件一个对象 { Path; Hunks[] },Hunk = { OldStart; Lines[] }。 #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $PatchText,
        [int] $StripComponents = 1
    )

    $lines = ConvertTo-QtDiffLines -Text $PatchText
    $files = New-Object System.Collections.Generic.List[object]
    $current = $null
    $hunk = $null
    $i = 0

    while ($i -lt $lines.Count) {
        $line = $lines[$i]

        if ($line -like '--- *') {
            $plus = if ($i + 1 -lt $lines.Count) { $lines[$i + 1] } else { '' }
            if ($plus -notlike '+++ *') { throw "补丁格式坏了:第 $($i+1) 行 '---' 后面不是 '+++'" }
            $raw = ($plus.Substring(4) -split "`t")[0].Trim()
            $parts = $raw -split '[\\/]'
            if ($parts.Count -le $StripComponents) { throw "补丁里的路径层级不够剥:$raw" }
            $rel = ($parts[$StripComponents..($parts.Count - 1)]) -join '\'
            $current = [pscustomobject]@{
                Path  = $rel
                Hunks = (New-Object System.Collections.Generic.List[object])
            }
            $files.Add($current)
            $hunk = $null
            $i += 2
            continue
        }

        if ($line -like '@@ *') {
            if ($null -eq $current) { throw "补丁格式坏了:第 $($i+1) 行的 @@ 不属于任何文件" }
            $m = [regex]::Match($line, '^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
            if (-not $m.Success) { throw "补丁格式坏了:无法解析 hunk 头 '$line'" }
            $hunk = [pscustomobject]@{
                OldStart = [int] $m.Groups[1].Value
                OldCount = if ($m.Groups[2].Success) { [int] $m.Groups[2].Value } else { 1 }
                Lines    = (New-Object System.Collections.Generic.List[string])
            }
            $current.Hunks.Add($hunk)
            $i++
            continue
        }

        if ($null -ne $hunk) {
            if ($line -like '\ No newline at end of file*') { $i++; continue }
            if ($line -eq '' -or $line[0] -eq ' ' -or $line[0] -eq '+' -or $line[0] -eq '-') {
                # 空行 = 内容为空的上下文行(有的工具会把那个前导空格裁掉)
                $hunk.Lines.Add($(if ($line -eq '') { ' ' } else { $line }))
                $i++
                continue
            }
        }
        $i++
    }

    if ($files.Count -eq 0) { throw '补丁里一个文件段都没解析到' }
    return , $files.ToArray()
}

function Invoke-QtUnifiedDiff {
    <#  把补丁应用到 $Root 下。严格匹配:任何一行上下文对不上就抛。

        输出文件一律写成 **CRLF + UTF-8 with BOM**:
          * CRLF —— SDK 源码本来就是 CRLF,保持一致;
          * BOM  —— 🔴 补丁往源码里加了中文注释。cl.exe 读到**没有 BOM** 的 UTF-8
            源码时,会按系统 ANSI 代码页解析(中文机 = GBK),中文注释立刻变乱码;
            更糟的是 GBK 双字节里可能出现 0x5C(反斜杠),落在 `//` 注释行尾就会把
            下一行代码一起吞进注释。加 BOM 后 cl.exe 无条件按 UTF-8 解析,这条路堵死。
            (等价做法是给 cl 传 /utf-8,但那要改 makefile;BOM 是自包含的,更小。)
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $PatchPath,
        [Parameter(Mandatory)][string] $Root,
        [int] $StripComponents = 1,
        [switch] $DryRun
    )

    if (-not (Test-Path -LiteralPath $PatchPath)) { throw "补丁文件不存在:$PatchPath" }
    if (-not (Test-Path -LiteralPath $Root))      { throw "源码根目录不存在:$Root" }

    $patchText = [System.IO.File]::ReadAllText($PatchPath, [System.Text.Encoding]::UTF8)
    $files = ConvertFrom-QtUnifiedDiff -PatchText $patchText -StripComponents $StripComponents

    $utf8Bom = New-Object System.Text.UTF8Encoding($true)
    $changed = New-Object System.Collections.Generic.List[string]

    foreach ($file in $files) {
        $target = Join-Path $Root $file.Path
        if (-not (Test-Path -LiteralPath $target)) {
            throw "补丁要改的文件不在源码树里:$($file.Path)(源码版本对不上?)"
        }

        $srcText = [System.IO.File]::ReadAllText($target, [System.Text.Encoding]::UTF8)
        $src = ConvertTo-QtDiffLines -Text $srcText

        $out = New-Object System.Collections.Generic.List[string]
        $cur = 0   # 0-based,已消费到 $src 的哪一行

        foreach ($h in $file.Hunks) {
            $start = $h.OldStart - 1
            if ($start -lt $cur) {
                throw "补丁 hunk 顺序错乱:$($file.Path) @@ -$($h.OldStart)"
            }
            if ($start -gt $src.Count) {
                throw "补丁 hunk 越过文件末尾:$($file.Path) @@ -$($h.OldStart),文件只有 $($src.Count) 行"
            }
            for ($k = $cur; $k -lt $start; $k++) { $out.Add($src[$k]) }
            $cur = $start

            foreach ($pl in $h.Lines) {
                $tag = $pl[0]
                $content = $pl.Substring(1)
                if ($tag -eq '+') {
                    $out.Add($content)
                    continue
                }
                if ($cur -ge $src.Count) {
                    throw "补丁在 $($file.Path) 第 $($cur+1) 行越过了文件末尾"
                }
                if ($src[$cur] -cne $content) {
                    throw ("补丁上下文对不上:{0}:{1}`n  期望 = <{2}>`n  实际 = <{3}>" -f `
                            $file.Path, ($cur + 1), $content, $src[$cur])
                }
                if ($tag -eq ' ') { $out.Add($content) }
                $cur++
            }
        }
        for ($k = $cur; $k -lt $src.Count; $k++) { $out.Add($src[$k]) }

        if (-not $DryRun) {
            # 🔴 LZMA SDK 归档里的源文件**带只读属性**(7z 会把属性一起还原),
            #    直接 WriteAllText 会 UnauthorizedAccessException。先摘掉只读位。
            $fi = New-Object System.IO.FileInfo($target)
            if ($fi.IsReadOnly) { $fi.IsReadOnly = $false }
            $text = ($out -join "`r`n") + "`r`n"
            [System.IO.File]::WriteAllText($target, $text, $utf8Bom)
        }
        $changed.Add($file.Path)
    }

    return , $changed.ToArray()
}

# ── 工具链探测 ────────────────────────────────────────────────────────────

function Find-QtMsvcToolchain {
    <#  用 vswhere 找带 C++ 工具集的 VS/Build Tools 实例,回一个结论对象。

        🔴 本函数**只探测、不安装**。缺件时把「缺什么、装什么」讲清楚,由人去决定。
    #>
    [CmdletBinding()]
    param([string] $VsWherePath)

    $missing = New-Object System.Collections.Generic.List[string]

    if (-not $VsWherePath) {
        $VsWherePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    }
    if (-not (Test-Path -LiteralPath $VsWherePath)) {
        $missing.Add('vswhere.exe(Visual Studio Installer 都没有,说明本机从未装过 VS/Build Tools)')
        return [pscustomobject]@{
            Found = $false; VcVarsPath = $null; InstallPath = $null
            VsWherePath = $VsWherePath; Missing = @($missing.ToArray())
        }
    }

    # -products * 才能匹配 BuildTools(它不是 Community/Professional/Enterprise)
    $installPath = & $VsWherePath -all -prerelease -products '*' `
        -requires 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64' `
        -property installationPath 2>$null | Select-Object -First 1

    if (-not $installPath) {
        $anyInstance = & $VsWherePath -all -prerelease -products '*' -property installationPath 2>$null |
            Select-Object -First 1
        if ($anyInstance) {
            $missing.Add("VS 实例在($anyInstance),但缺 C++ 工具集组件 Microsoft.VisualStudio.Component.VC.Tools.x86.x64")
        } else {
            $missing.Add('任何 Visual Studio / Build Tools 实例(vswhere 返回空)')
        }
        return [pscustomobject]@{
            Found = $false; VcVarsPath = $null; InstallPath = $anyInstance
            VsWherePath = $VsWherePath; Missing = @($missing.ToArray())
        }
    }

    # x86 静态 CRT 的产物用 vcvars32.bat(32 位目标)
    $vcvars = Join-Path $installPath 'VC\Auxiliary\Build\vcvars32.bat'
    if (-not (Test-Path -LiteralPath $vcvars)) {
        $missing.Add("vcvars32.bat(期望在 $vcvars)")
        return [pscustomobject]@{
            Found = $false; VcVarsPath = $null; InstallPath = $installPath
            VsWherePath = $VsWherePath; Missing = @($missing.ToArray())
        }
    }

    return [pscustomobject]@{
        Found = $true; VcVarsPath = $vcvars; InstallPath = $installPath
        VsWherePath = $VsWherePath; Missing = @()
    }
}

function Get-QtMsvcMissingMessage {
    <#  缺工具链时给人看的中文说明 —— 讲清楚缺什么、怎么补,并且**明确不自行安装**。 #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][object] $Probe)

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('本机没有可用的 MSVC C++ 工具链,无法重编 SFX 存根。缺:')
    foreach ($m in $Probe.Missing) { [void]$sb.AppendLine("  - $m") }
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('补齐办法(二选一,需要管理员;🔴 本脚本不会自行安装):')
    [void]$sb.AppendLine('  A) choco install visualstudio2019buildtools visualstudio2019-workload-vctools -y')
    [void]$sb.AppendLine('  B) 下 VS Build Tools 安装器,勾选「使用 C++ 的桌面开发」工作负载')
    [void]$sb.AppendLine('     (最小组件集:Microsoft.VisualStudio.Component.VC.Tools.x86.x64 + Windows 10/11 SDK)')
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('在工具链补齐之前,build/build.ps1 会回退到官方存根 + precheck-disk.cmd 搬运路径(方案 B),')
    [void]$sb.AppendLine('那条路能出包,但退出码不透传 —— 验收请读 <目标>\logs\last-exit-code.txt,别读 EXE 退出码。')
    return $sb.ToString()
}

# ── 产物验货 ──────────────────────────────────────────────────────────────

function Test-QtSfxStubBinary {
    <#  对重编出来的存根做三项自证,任何一项不过就不该拿去出包:
          1. PE 机器类型 = x86(0x014C)—— §2.4.1 要求 32 位外壳,任何 Windows 都能跑;
          2. 二进制里出现字符串 `InstallPath` —— 证明补丁 (a) 真的编进去了
             (这正是第四批打哑 EXE 才发现官方存根**没有**的那个键);
          3. 没有对 msvcr*/vcruntime* DLL 的导入 —— 证明是静态 CRT,
             目标机上没装 VC++ 运行库也能跑。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) { throw "存根不存在:$Path" }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $problems = New-Object System.Collections.Generic.List[string]

    # --- PE 头
    $machine = 0
    if ($bytes.Length -gt 0x40 -and $bytes[0] -eq 0x4D -and $bytes[1] -eq 0x5A) {
        $peOff = [System.BitConverter]::ToInt32($bytes, 0x3C)
        if ($peOff -gt 0 -and ($peOff + 6) -lt $bytes.Length -and
            $bytes[$peOff] -eq 0x50 -and $bytes[$peOff + 1] -eq 0x45) {
            $machine = [System.BitConverter]::ToUInt16($bytes, $peOff + 4)
        }
    }
    if ($machine -ne 0x014C) {
        $problems.Add(('PE 机器类型 = 0x{0:X4},不是 x86(0x014C)' -f $machine))
    }

    # --- 字符串(存根里的配置键名是窄字符,按 latin1 扫最省事)
    $text = [System.Text.Encoding]::GetEncoding('iso-8859-1').GetString($bytes)
    if ($text -notmatch 'InstallPath') {
        $problems.Add('二进制里找不到 InstallPath —— 补丁没编进去,这就是一个官方存根')
    }
    foreach ($crt in @('msvcr', 'vcruntime', 'api-ms-win-crt')) {
        if ($text -match ([regex]::Escape($crt) + '\w*\.dll')) {
            $problems.Add("发现动态 CRT 导入($crt*.dll)—— 不是静态 CRT(-MT)构建")
        }
    }

    return [pscustomobject]@{
        Path     = $Path
        Ok       = ($problems.Count -eq 0)
        Machine  = $machine
        Problems = @($problems.ToArray())
        Size     = $bytes.Length
        Sha256   = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    }
}

Export-ModuleMember -Function `
    Get-QtSfxSdkInfo, Assert-QtSha256, ConvertTo-QtDiffLines, ConvertFrom-QtUnifiedDiff, `
    Invoke-QtUnifiedDiff, Find-QtMsvcToolchain, Get-QtMsvcMissingMessage, Test-QtSfxStubBinary
