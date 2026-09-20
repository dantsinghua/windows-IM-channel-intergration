# QTrade 安装器 —— 总装:引擎编译 → 载荷收集 → manifest → 7z 归档 → SFX 拼接 → 单 EXE
# 规格:docs/03 §2.1(两段式)、§2.2.2(压缩参数)、§2.2.3(签名)、§2.2.4(CI 验证门 G0)、§2.6.7 W1(ps1 BOM)
#
# 用法:
#   .\build.ps1 -Version 1.0.0 -SourceRoot <产物根>
#   .\build.ps1 -Version 1.0.0 -AllowMissing            # 轻量验证包(缺件占位,只验流程)
#   .\build.ps1 -CheckOnly                              # 只跑 BOM 与语法门,不出包
#
# 🔴 本脚本**不安装任何工具**:缺 ISCC / 7z / SFX 存根时明确报缺,并给出获取方式。
#requires -Version 5.1
[CmdletBinding()]
param(
    [string] $Version = '1.0.0',
    [string] $SourceRoot = '',
    [string] $OutDir = '',
    [string] $IsccPath = '',
    [string] $SevenZipPath = '',
    [string] $SfxStubPath = '',
    # auto = 有自编存根就用自编的,没有就回退官方存根(方案 B 搬运路径)
    [ValidateSet('auto', 'qtrade', 'official')]
    [string] $Stub = 'auto',
    [switch] $AllowMissing,
    [switch] $CheckOnly,
    [switch] $SelfCheck,
    [switch] $SkipEngine,
    # ── 代码签名(§2.2.3;操作步骤见 build\README.md 第 5 节)────────────────────
    # 🔴 **不带 -Sign 时,本脚本的行为与加签名之前逐字节一致** —— 签名相关的每一句都在
    #    `if ($Sign)` 里,`installer/tests/QTrade.Signing.Tests.ps1` 有回归守卫钉着这条。
    [switch] $Sign,
    [string] $CertThumbprint = '',
    [string] $TimestampUrl = '',
    [switch] $NoTimestamp,
    [string] $SignToolPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$BuildDir = $PSScriptRoot
$InstallerRoot = Split-Path -Parent $BuildDir
$EngineDir = Join-Path $InstallerRoot 'engine'
if (-not $OutDir) { $OutDir = Join-Path $InstallerRoot 'out' }
$StageDir = Join-Path $OutDir 'payload'
$ArchivePath = Join-Path $OutDir 'payload.7z'
$FinalExe = Join-Path $OutDir ('QTrade-Setup-{0}.exe' -f $Version)
$SigningDir = Join-Path $InstallerRoot 'signing'
# 签名只发生在 out\ 下的**副本**上 —— 仓库里的源文件一个字节都不动(见下面「为什么」)
$SignedEngineDir = Join-Path $OutDir 'engine-signed'
$PresignDir = Join-Path $OutDir 'presign'
# ISCC 的编译输入:不签名时 = 仓库 engine\;签名时 = 已签脚本的副本
$IssSourceDir = $EngineDir
# 签了哪些文件(G6 门逐个复核)
$QtSignedArtifacts = @()

# ── §2.2.3 应签对象清单 ────────────────────────────────────────────────────
# 🔴 与 docs/03 §2.2.3 逐条对账(installer/tests/test_signing_consistency.py)。
#    改这里 = 改规格口径:要么同步改文档,要么先出裁决(基线 §15g,编号续 R6-N)。
#    §2.2.3 同时写明 **adb / scrcpy 由其上游签名不动** —— 第三方件一律不重签,
#    清单在 signing\QTrade.Signing.psm1 的 Get-QtNeverSignRule。
$QtSignSpec = @(
    @{ Key = 'shell_exe'; Spec = '外壳 EXE'; Step = '步 5:拼接完成之后**最后**签(签名覆盖整个 EXE 含归档)' }
    @{ Key = 'engine_exe'; Spec = '引擎 EXE'; Step = '步 1b:ISCC 从**已签脚本副本**编译出来之后签' }
    @{ Key = 'winagent_svc'; Spec = 'qtrade-winagent-svc.exe'; Step = '步 0b:签预签副本,**先于 manifest 算 sha256**' }
    @{ Key = 'winagent_user'; Spec = 'qtrade-winagent-user.exe'; Step = '步 0b:签预签副本,**先于 manifest 算 sha256**' }
    @{ Key = 'electron_main'; Spec = 'Electron 主程序'; Step = '步 0b:签预签副本,**先于 manifest 算 sha256**' }
    @{ Key = 'ps1_all'; Spec = 'ps1'; Step = '步 1a:引擎副本内全部 .ps1/.psm1(§2.2.3 第 3 条)' }
)

function Write-Section { param([string] $T) Write-Host ''; Write-Host ('== ' + $T + ' ' + ('=' * [math]::Max(0, 60 - $T.Length))) -ForegroundColor Cyan }
function Write-Ok { param([string] $T) Write-Host ('  [OK]   ' + $T) -ForegroundColor Green }
function Write-Bad { param([string] $T) Write-Host ('  [FAIL] ' + $T) -ForegroundColor Red }
function Write-Warn2 { param([string] $T) Write-Host ('  [WARN] ' + $T) -ForegroundColor Yellow }

# ── 工具链探测(缺就报,不代装)──────────────────────────────────────────
function Find-QtTool {
    param([string] $Explicit, [string[]] $Candidates, [string] $Name, [string] $HowTo, [string] $Param = '')
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        Write-Bad ('你用 {0} 指定的路径不存在:{1}' -f $Param, $Explicit)
        return ''
    }
    foreach ($c in $Candidates) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
    $cmd = Get-Command -Name $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    Write-Warn2 ('缺工具链:{0}' -f $Name)
    Write-Host ('           怎么补:{0}' -f $HowTo) -ForegroundColor DarkGray
    if ($Param) { Write-Host ('           或用 {0} 指定完整路径' -f $Param) -ForegroundColor DarkGray }
    Write-Host  '           找过这些位置:' -ForegroundColor DarkGray
    foreach ($c in $Candidates) { if ($c) { Write-Host ('             - ' + $c) -ForegroundColor DarkGray } }
    return ''
}

# ── 签名辅助(只在 -Sign 时用到)────────────────────────────────────────────
function Resolve-QtPresignSource {
    <#
        与 collect-payload.ps1 的 Resolve-QtSource **同语义**:EnvVar > -SourceRoot > 相对路径。
        预签副本必须按同一套规则找源,否则签的是 A、collect 收的是 B。
    #>
    param([string] $EnvVar, [string[]] $RelPaths)
    $v = [Environment]::GetEnvironmentVariable($EnvVar)
    if ($v) { return @($v) }
    if ($SourceRoot) { return @($RelPaths | ForEach-Object { [IO.Path]::Combine($SourceRoot, ($_ -replace '/', '\')) }) }
    return @($RelPaths | ForEach-Object { $_ -replace '/', '\' })
}

function New-QtPresignCopy {
    <#
        把一组源目录**合并复制**到 out\presign\<名> 下(合并语义与 collect 的多 Sources 覆盖合并一致),
        在**副本**上签我方可执行件,再把对应的 QT_SRC_* 环境变量指到副本。

        🔴 为什么非得这么绕(这是本次改动最容易被"简化"掉、一简化就出事的地方):
           collect-payload.ps1 是「复制 → 算 sha256 → 写 manifest」一气呵成的,而 build.ps1
           **调了它两次**(第二次是为了把引擎 exe 登记进 files[]),第二次会照 PayloadMap
           **重新从源复制、覆盖 stage 里的同名文件**。所以「收完载荷再去签 stage 里的 exe」
           会被第二次 collect 覆盖回未签名版本 —— manifest 记的是未签名哈希,
           装机时按 manifest 复核…… 其实两边"一致地错",包里的 exe 压根没签名。
           把源头换成**已签名副本**,collect 跑几次都从副本拷,sha256 天然算在签名之后。
    #>
    param(
        [string] $Name, [string] $EnvVar, [string[]] $RelPaths,
        [ValidateSet('winagent', 'console')][string] $Scope,
        [string] $Thumbprint, $Certificate, [string] $SignTool, [string] $Ts, [bool] $NoTs
    )
    $srcs = @(Resolve-QtPresignSource -EnvVar $EnvVar -RelPaths $RelPaths)
    $exists = @($srcs | Where-Object { Test-Path -LiteralPath $_ })
    if ($exists.Count -eq 0) {
        Write-Warn2 ('{0}:源目录一个都不在({1}),跳过预签 —— 这一项会在收载荷时报缺件' -f $Name, ($srcs -join ' | '))
        return @()
    }
    $dst = Join-Path $PresignDir $Name
    if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    foreach ($src in $exists) {
        foreach ($c in @(Get-ChildItem -LiteralPath $src -Force -ErrorAction SilentlyContinue)) {
            Copy-Item -LiteralPath $c.FullName -Destination $dst -Recurse -Force
        }
    }
    $plan = @(Get-QtSignPlan -Root $dst -Scope $Scope)
    if ($plan.Count -eq 0) { throw ('{0}:预签副本 {1} 里找不到任何该签的可执行件 —— 源目录形态不对' -f $Name, $dst) }
    foreach ($f in $plan) {
        Invoke-QtSignFile -Path $f -Thumbprint $Thumbprint -Certificate $Certificate `
            -SignToolPath $SignTool -TimestampUrl $Ts -NoTimestamp:$NoTs | Out-Null
        Write-Ok ('已签 {0}' -f (Split-Path -Leaf $f))
    }
    # 🔴 把源指到已签名副本:collect-payload.ps1 的 Resolve-QtSource 里 EnvVar 优先级最高
    [Environment]::SetEnvironmentVariable($EnvVar, $dst, 'Process')
    return $plan
}

$Iscc = Find-QtTool -Explicit $IsccPath -Name 'ISCC.exe' -Param '-IsccPath' `
    -HowTo '安装 Inno Setup 6(https://jrsoftware.org/isdl.php),装完 ISCC.exe 在安装目录下' -Candidates @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$SevenZip = Find-QtTool -Explicit $SevenZipPath -Name '7z.exe' -Param '-SevenZipPath' `
    -HowTo '安装 7-Zip(https://www.7-zip.org/)' -Candidates @(
    "$env:ProgramFiles\7-Zip\7z.exe"
    "${env:ProgramFiles(x86)}\7-Zip\7z.exe"
    "$env:ProgramData\chocolatey\tools\7z.exe"
)
# ── SFX 存根:两条路都保留,各有各的配置、各有各的链首脚本、各有各的 G5 白名单 ──
#
#   qtrade   自编 QTradeSD.sfx(installer/sfx-stub/build.ps1 出)
#            认 InstallPath -> 直接解到 %ProgramData%\QTrade 并留存;
#            解压前判空间(不足退 26);**透传子进程退出码**。
#            链首 = run-engine.cmd(只拉引擎 + 落盘退出码)。
#   official 官方 7zSD.sfx(LZMA SDK / 7-Zip Extra)
#            只解到 %TEMP% 且跑完即删、退出码恒 0(第四批哑 EXE 实测坐实)。
#            链首 = precheck-disk.cmd(判空间 + 搬运到 %ProgramData%\QTrade + 拉引擎)。
#
# 🔴 存根、配置、链首脚本这三样必须**配套**。配错的后果不是报错,是静默跑偏:
#    官方存根遇到 InstallPath 会直接忽略它,于是载荷解到 %TEMP%、跑完即删。
#    所以下面把三者绑成一个对象,G5 按它切换白名单。
$QtStubFile     = Join-Path $BuildDir 'QTradeSD.sfx'
$OfficialStubs  = @(
    (Join-Path $BuildDir '7zSD.sfx')
    (Join-Path $BuildDir '7zS2.sfx')
    "$env:ProgramFiles\7-Zip\7zSD.sfx"
    "$env:ProgramFiles\7-Zip\7z.sfx"
)

$StubKind = 'official'
if ($SfxStubPath) {
    # 显式指定时,按文件名判类型(QTradeSD.sfx = 自编);G5 之后还会验二进制自证
    $StubKind = if ((Split-Path -Leaf $SfxStubPath) -ieq 'QTradeSD.sfx') { 'qtrade' } else { 'official' }
    if ($Stub -ne 'auto') { $StubKind = $Stub }
    $SfxStub = Find-QtTool -Explicit $SfxStubPath -Name 'QTradeSD.sfx' -Param '-SfxStubPath' `
        -HowTo '检查 -SfxStubPath 指的路径' -Candidates @()
}
elseif ($Stub -eq 'qtrade' -or ($Stub -eq 'auto' -and (Test-Path -LiteralPath $QtStubFile))) {
    $StubKind = 'qtrade'
    $SfxStub = Find-QtTool -Explicit '' -Name 'QTradeSD.sfx' -Param '-SfxStubPath' `
        -HowTo '自编存根:cd installer\sfx-stub; .\fetch-sdk.ps1 -Proxy <代理>; .\build.ps1(需要 MSVC C++ 工具集)' `
        -Candidates @($QtStubFile)
}
else {
    $SfxStub = Find-QtTool -Explicit '' -Name '7zSD.sfx' -Param '-SfxStubPath' `
        -HowTo '🔴 SFX 存根**不在 7-Zip 主安装包里**:从**官方 LZMA SDK**(lzma<ver>.7z)或 7-Zip Extra 里解出 7zSD.sfx 放进 installer\build\。⚠️ 要官方件,**不要**第三方改版 7zsfxmm(理由见 build/README §8)' `
        -Candidates $OfficialStubs
}

# 存根类型决定的那三样
if ($StubKind -eq 'qtrade') {
    $SfxConfigName = 'sfx-config-qtrade.txt'
    $ChainHeadName = 'run-engine.cmd'
    $SfxAllowedKeys = @('Title', 'BeginPrompt', 'Progress', 'Directory', 'RunProgram', 'ExecuteFile', 'ExecuteParameters', 'InstallPath')
} else {
    $SfxConfigName = 'sfx-config.txt'
    $ChainHeadName = 'precheck-disk.cmd'
    $SfxAllowedKeys = @('Title', 'BeginPrompt', 'Progress', 'Directory', 'RunProgram', 'ExecuteFile', 'ExecuteParameters')
}

# ── 门 1:ps1/psm1 必须 UTF-8 with BOM(W1;验收 M0-10)──────────────────────
Write-Section 'G1 脚本编码(W1:UTF-8 with BOM)'
# ⚠️ 不用 `-LiteralPath -Recurse -Include`:PowerShell 5.1 下 -Include 会被 -LiteralPath 吃掉,
#    结果把目录里**所有**文件都列出来(.json/.pyc 都进来)。按扩展名显式过滤。
function Get-QtScriptFile {
    param([string] $Root)
    if (-not (Test-Path -LiteralPath $Root)) { return @() }
    return @(Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Extension -in '.ps1', '.psm1' -and
            $_.FullName -notmatch '\\(\.omc|__pycache__|out|node_modules)\\'
        })
}
# signing\ 下的脚本**要随包发给目标机同事**(双击导入证书那一套),同样受 W1 约束
$scriptFiles = @(Get-QtScriptFile -Root $EngineDir) +
@(Get-QtScriptFile -Root $BuildDir) +
@(Get-QtScriptFile -Root $SigningDir) +
@(Get-QtScriptFile -Root (Join-Path $InstallerRoot 'tests'))
$bomBad = @()
foreach ($f in $scriptFiles) {
    $b = [IO.File]::ReadAllBytes($f.FullName)
    if ($b.Length -lt 3 -or $b[0] -ne 0xEF -or $b[1] -ne 0xBB -or $b[2] -ne 0xBF) { $bomBad += $f.FullName }
}
if ($bomBad.Count -gt 0) {
    foreach ($f in $bomBad) { Write-Bad ('缺 BOM:' + $f) }
    throw ('有 {0} 个脚本不是 UTF-8 with BOM —— Windows PowerShell 5.1 会按系统 ANSI(中文机=GBK)解析,中文注释会引发 ParserError(docs/03 §2.6.7 W1)' -f $bomBad.Count)
}
Write-Ok ('{0} 个脚本全部 UTF-8 with BOM' -f $scriptFiles.Count)

# ── 门 1b:.cmd 必须 **CRLF** 且**不带 BOM** ─────────────────────────────────
# 🔴 LF-only 的 .cmd 会让 cmd.exe 的多行结构(for/if/call/标签)解析错乱,
#    症状是「The system cannot find the batch label specified」「) was unexpected at this time」,
#    而且只有真跑起来才暴露。BOM 则会让第一行命令解析失败。
Write-Section 'G1b .cmd 换行与编码(CRLF、无 BOM)'
$cmdFiles = @(@(Get-ChildItem -Path $EngineDir -Recurse -File -ErrorAction SilentlyContinue) +
    @(Get-ChildItem -Path $SigningDir -Recurse -File -ErrorAction SilentlyContinue) |
    Where-Object { $_.Extension -eq '.cmd' -and $_.FullName -notmatch '\\(\.omc|__pycache__)\\' })
$cmdBad = @()
foreach ($f in $cmdFiles) {
    $b = [IO.File]::ReadAllBytes($f.FullName)
    if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
        $cmdBad += ('{0}:带 BOM' -f $f.Name); continue
    }
    $text = [Text.Encoding]::UTF8.GetString($b)
    $lf = ([regex]::Matches($text, "`n")).Count
    $crlf = ([regex]::Matches($text, "`r`n")).Count
    if ($lf -ne $crlf) { $cmdBad += ('{0}:有 {1} 个 LF-only 换行' -f $f.Name, ($lf - $crlf)) }
    # 可执行行必须纯 ASCII(SFX 在未知代码页下拉起它)
    foreach ($line in ($text -split "`r?`n")) {
        $s = $line.TrimStart()
        if ($s -eq '' -or $s.ToLowerInvariant().StartsWith('rem')) { continue }
        foreach ($ch in $line.ToCharArray()) {
            if ([int]$ch -gt 127) { $cmdBad += ('{0}:可执行行含非 ASCII -> {1}' -f $f.Name, $line.Trim()); break }
        }
    }
}
if ($cmdBad.Count -gt 0) {
    foreach ($b in ($cmdBad | Select-Object -Unique)) { Write-Bad $b }
    throw '.cmd 文件必须是 CRLF、无 BOM、可执行行纯 ASCII'
}
Write-Ok ('{0} 个 .cmd 全部 CRLF / 无 BOM / 可执行行纯 ASCII' -f $cmdFiles.Count)

# ── 门 2:PowerShell 语法解析 ───────────────────────────────────────────────
Write-Section 'G2 PowerShell 语法解析'
$synBad = @()
foreach ($f in $scriptFiles) {
    $errors = $null
    $tokens = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors)
    if ($errors -and $errors.Count -gt 0) {
        $synBad += [pscustomobject]@{ File = $f.FullName; Errors = $errors }
    }
}
if ($synBad.Count -gt 0) {
    foreach ($b in $synBad) {
        Write-Bad $b.File
        foreach ($e in $b.Errors) { Write-Host ('         ' + $e.Extent.StartLineNumber + ': ' + $e.Message) }
    }
    throw ('有 {0} 个脚本语法错误' -f $synBad.Count)
}
Write-Ok ('{0} 个脚本语法全绿' -f $scriptFiles.Count)

# ── 门 3/4 的公共前提:红线类检查只看**随包发行的代码**,且只看代码不看注释 ───────
#    build/ 与 tests/ 里故意写着被禁模式的字样——它们是守门人与用例,不是违规。
$runtimeFiles = @(Get-ChildItem -Path $EngineDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in '.ps1', '.psm1', '.iss' -and $_.FullName -notmatch '\\(\.omc|__pycache__)\\' })
function Remove-QtScriptComments {
    param([string] $Text)
    $t = [regex]::Replace($Text, '<#.*?#>', '', 'Singleline')
    $kept = @()
    foreach ($line in ($t -split "`r?`n")) {
        $s = $line.TrimStart()
        if ($s.StartsWith('#') -or $s.StartsWith(';') -or $s.StartsWith('//')) { continue }
        $kept += $line
    }
    return ($kept -join "`n")
}

# ── 门 3:禁止自建防火墙规则(验收 M1-14)──────────────────────────────────
Write-Section 'G3 防火墙红线(M1-14:零处 netsh advfirewall / New-NetFirewallRule)'
$fwHits = @()
foreach ($f in $runtimeFiles) {
    $code = Remove-QtScriptComments -Text ([IO.File]::ReadAllText($f.FullName))
    foreach ($pat in @('netsh(\.exe)?\s+advfirewall', '\bNew-NetFirewallRule\b')) {
        if ($code -match $pat) { $fwHits += ('{0} :: {1}' -f $f.Name, $pat) }
    }
}
if ($fwHits.Count -gt 0) {
    foreach ($h in $fwHits) { Write-Bad $h }
    throw '引擎不得自建防火墙规则:建规则唯一入口是 WinAgent `POST /wa/v1/firewall/ensure`(docs/03 §6 / 验收 M1-14)'
}
Write-Ok ('零处自建防火墙规则({0} 个随包脚本)' -f $runtimeFiles.Count)

# ── 门 4:微信卸载红线(§2.9.3 事实 1)────────────────────────────────────
Write-Section 'G4 微信卸载红线(禁止 Uninstall.exe /S)'
$wxHits = @()
foreach ($f in $runtimeFiles) {
    $code = Remove-QtScriptComments -Text ([IO.File]::ReadAllText($f.FullName))
    foreach ($line in ($code -split "`n")) {
        if ($line -match '(?i)(Uninstall\w*\.exe|UninstallString)[^\n]*[''"]\s*/S\s*[''"]') {
            $wxHits += ('{0} :: {1}' -f $f.Name, $line.Trim())
        }
    }
}
if ($wxHits.Count -gt 0) {
    foreach ($h in $wxHits) { Write-Bad $h }
    throw '禁止以 /S 运行微信 Uninstall.exe(= 卸载并清空聊天记录与登录态,docs/03 §2.9.3 事实 1)'
}
Write-Ok '零处 /S 卸载调用'

# ── 门 5:SFX 配置键必须是**当前这个存根认识的那些** ─────────────────────────
# 🔴 存根对不认识的键**静默忽略** —— 给官方存根写 InstallPath 不报错也不生效,
#    结果是载荷被解到 %TEMP% 然后跑完即删。这类错只有装到现场才会暴露,所以在这里卡死。
#    白名单随存根切换:官方 7 个键,自编存根多一个 InstallPath(补丁改动 a)。
Write-Section ('G5 SFX 配置键(存根 = {0},认 {1} 个键)' -f $StubKind, $SfxAllowedKeys.Count)
$sfxCfg = Join-Path $BuildDir $SfxConfigName
if (-not (Test-Path -LiteralPath $sfxCfg)) { throw ('缺配置文件:{0}' -f $sfxCfg) }
$badKeys = @()
$seenKeys = @()
$runProgram = ''
foreach ($line in [IO.File]::ReadAllLines($sfxCfg)) {
    $s = $line.Trim()
    if ($s -eq '' -or $s.StartsWith(';')) { continue }
    $m = [regex]::Match($s, '^([A-Za-z_]+)\s*=\s*"?([^"]*)"?')
    if (-not $m.Success) { continue }
    $k = $m.Groups[1].Value
    $seenKeys += $k
    if ($k -eq 'RunProgram') { $runProgram = $m.Groups[2].Value }
    if ($SfxAllowedKeys -notcontains $k) { $badKeys += $k }
}
if ($badKeys.Count -gt 0) {
    foreach ($k in $badKeys) {
        Write-Bad ('{0} 里的 "{1}" 不是当前存根({2})认识的键,存根会**静默忽略**它' -f $SfxConfigName, $k, $StubKind)
    }
    Write-Host ('           只认:{0}' -f ($SfxAllowedKeys -join ' / ')) -ForegroundColor DarkGray
    throw 'SFX 配置里有存根不认识的键 —— 它不会报错,只会不生效,然后把载荷解到 %TEMP% 并跑完即删'
}
if ($seenKeys -notcontains 'RunProgram') { throw ('{0} 缺 RunProgram —— 外壳不知道该拉起谁' -f $SfxConfigName) }
# 自编存根的价值全在 InstallPath 上,缺了它就退化成官方行为、而链首脚本又不搬运 -> 装不成
if ($StubKind -eq 'qtrade' -and $seenKeys -notcontains 'InstallPath') {
    throw ('{0} 缺 InstallPath —— 自编存根会退化成「解到 %TEMP% 跑完即删」,而 run-engine.cmd 不做搬运' -f $SfxConfigName)
}
if ($StubKind -eq 'official' -and $seenKeys -contains 'InstallPath') {
    throw 'sfx-config.txt 里出现了 InstallPath —— 官方存根不认它,会静默忽略'
}
# 🔴 裁决 12:RunProgram 必须是**相对路径**。
#    存根启动子进程时拼的是 `dirPrefix + appLaunched`(dirPrefix 缺省 ".\"),
#    而它此前已经 SetCurrentDir 到解压目标。所以写绝对路径(或用 %%T 展开成绝对路径)
#    会被拼成 `.\C:\ProgramData\QTrade\...` —— 一个不存在的路径,外壳直接起不来。
#    ⚠️ 这是**官方存根原版就有的行为**,不是自编补丁引入的,两条路都受它约束。
$runNorm = $runProgram.Replace('\\', '\')
if ($runNorm -match '^[A-Za-z]:' -or $runNorm.StartsWith('\') -or $runNorm -like '*%%T*') {
    throw ('{0} 的 RunProgram = "{1}" 是绝对路径(或用了 %%T)。存根会把它拼成 ".\<绝对路径>",起不来。必须写相对路径。' -f `
            $SfxConfigName, $runProgram)
}

# 🔴 配置里的 RunProgram 必须正是我们待会儿塞进载荷的那个链首脚本,
#    否则出来的包会去拉一个根本不存在的文件。
$expectedRun = 'install\engine\' + $ChainHeadName
if ($runNorm -ne $expectedRun) {
    throw ('{0} 的 RunProgram = "{1}",但存根 {2} 配套的链首是 "{3}"' -f `
            $SfxConfigName, $runProgram, $StubKind, $expectedRun)
}
Write-Ok ('配置键全部合法({0});链首 = {1}' -f ($seenKeys -join ', '), $ChainHeadName)

if ($CheckOnly) {
    Write-Section '仅检查模式(-CheckOnly):五道门已跑完,不出包'
    exit 0
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

# ── 签名准备(只在 -Sign 时)─────────────────────────────────────────────────
$SignCert = $null
$SignTool = ''
$SignTs = ''
if ($Sign) {
    Write-Section '签名准备(§2.2.3)'
    if (-not $CertThumbprint) {
        throw '-Sign 必须同时给 -CertThumbprint <指纹>。没有证书先跑:installer\signing\New-QtSelfSignedCert.ps1'
    }
    Import-Module (Join-Path $SigningDir 'QTrade.Signing.psm1') -Force -DisableNameChecking
    $SignCert = Get-QtSigningCertByThumbprint -Thumbprint $CertThumbprint
    $CertThumbprint = [string]$SignCert.Thumbprint     # 统一成存储里的规范写法(大写、无空格)
    $SignTool = Find-QtSignTool -Explicit $SignToolPath
    if (-not $SignTool) {
        throw ('要签 PE 文件但找不到 signtool.exe(装 Windows SDK 的 Signing Tools,' +
               '本机实测在 "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.19041.0\x64\signtool.exe"),' +
               '或用 -SignToolPath 指定完整路径')
    }
    $SignTs = $TimestampUrl
    if (-not $SignTs) { $SignTs = (Get-QtSigningDefault).TimestampUrl }
    if ($NoTimestamp) { Write-QtNoTimestampWarning }

    Write-Ok ('证书  : {0}' -f $SignCert.Subject)
    Write-Ok ('指纹  : {0}' -f $CertThumbprint)
    Write-Ok ('有效期: {0:yyyy-MM-dd} ~ {1:yyyy-MM-dd}' -f $SignCert.NotBefore, $SignCert.NotAfter)
    Write-Ok ('signtool: {0}' -f $SignTool)
    if ($NoTimestamp) { Write-Warn2 '时间戳:**关闭**(-NoTimestamp)' } else { Write-Ok ('时间戳: {0}' -f $SignTs) }
    Write-Host '  应签对象(§2.2.3):' -ForegroundColor DarkGray
    foreach ($t in $QtSignSpec) { Write-Host ('    - {0,-26} {1}' -f $t.Spec, $t.Step) -ForegroundColor DarkGray }
    Write-Host '  🔴 第三方件(微信安装包 / wsl.msi / VC_redist / chatlog / adb / scrcpy / 嵌入式 python)一律**不重签**' -ForegroundColor DarkGray
}

if ($SelfCheck) {
    # -SelfCheck:五道门 + 收载荷 + 生成 manifest。不碰 ISCC / 7z / SFX 存根,
    # 所以在**没装工具链的开发机上也能跑**,用来确认「载荷来源对不对、manifest 长什么样」。
    Write-Section '步 S 自检模式:收集载荷并生成 manifest(不编译、不归档、不拼 EXE)'
    if ($Sign) {
        # 自检模式不产出可交付的包,签名没有意义;更要紧的是别让人误以为"自检过了 = 签过了"。
        Write-Warn2 '-SelfCheck 只收载荷与生成 manifest,**不签名**;要签名请走正式出包路径(不加 -SelfCheck)'
    }
    $collect = & (Join-Path $BuildDir 'collect-payload.ps1') -Stage $StageDir -SourceRoot $SourceRoot `
        -PackageVersion $Version -AllowMissing:$AllowMissing -Clean
    Write-Section '自检完成'
    Write-Host ('  载荷 stage : {0}' -f $collect.Stage)
    Write-Host ('  manifest   : {0}' -f $collect.ManifestPath)
    Write-Host ('  已收集     : {0} 项' -f $collect.Copied)
    if ($collect.Missing.Count -gt 0) {
        Write-Host ('  缺件       : {0}' -f ($collect.Missing -join ', ')) -ForegroundColor Yellow
    }
    exit 0
}

# ── 步 0b:预签载荷里的我方可执行件(🔴 必须**先于** collect 算 sha256)────────
#    做法与理由见 New-QtPresignCopy 的注释:签副本 + 把 QT_SRC_* 指到副本,
#    这样 collect-payload.ps1 无论被调几次,拷进 stage 的都已经是签过名的文件。
if ($Sign) {
    Write-Section '步 0b 预签载荷中的我方可执行件(先于 manifest 哈希)'
    $QtSignedArtifacts += @(New-QtPresignCopy -Name 'winagent-app' -EnvVar 'QT_SRC_WA_APP' `
            -RelPaths @('winagent/dist/qtrade-winagent-svc', 'winagent/dist/qtrade-winagent-user') `
            -Scope 'winagent' -Thumbprint $CertThumbprint -Certificate $SignCert `
            -SignTool $SignTool -Ts $SignTs -NoTs ([bool]$NoTimestamp))
    $QtSignedArtifacts += @(New-QtPresignCopy -Name 'console' -EnvVar 'QT_SRC_CONSOLE' `
            -RelPaths @('console/release/win-unpacked') `
            -Scope 'console' -Thumbprint $CertThumbprint -Certificate $SignCert `
            -SignTool $SignTool -Ts $SignTs -NoTs ([bool]$NoTimestamp))
}

# ── 步 1:编译引擎(Inno Setup 6)──────────────────────────────────────────
# 步 1a(只在 -Sign 时):把 engine\ 复制到 out\engine-signed\,在**副本**上签全部 ps1/psm1。
# 🔴 为什么签副本而不是仓库里的源文件:Authenticode 签名块会追加到文件尾 ——
#    ①污染 git(每次出包都让 18 个模块变成已修改);
#    ②破 test_spec_consistency.py 的逐字对账(它按内容比对文档与实现);
#    ③G1 的 BOM 门虽然仍过(签名块在文件尾,BOM 还在),但文件哈希与内容都变了。
#    副本落在 out\ 下,而 G1/G1b 的文件枚举**本来就排除了 \out\**,不会被门扫到。
if ($Sign) {
    Write-Section '步 1a 在引擎副本上签全部 ps1/psm1(不碰仓库源文件)'
    if (Test-Path -LiteralPath $SignedEngineDir) { Remove-Item -LiteralPath $SignedEngineDir -Recurse -Force }
    New-Item -ItemType Directory -Path $SignedEngineDir -Force | Out-Null
    foreach ($c in @(Get-ChildItem -LiteralPath $EngineDir -Force)) {
        Copy-Item -LiteralPath $c.FullName -Destination $SignedEngineDir -Recurse -Force
    }
    $enginePlan = @(Get-QtSignPlan -Root $SignedEngineDir -Scope 'engine')
    if ($enginePlan.Count -eq 0) { throw ('引擎副本 {0} 里一个 ps1/psm1 都没有 —— 复制出问题了' -f $SignedEngineDir) }
    foreach ($f in $enginePlan) {
        Invoke-QtSignFile -Path $f -Thumbprint $CertThumbprint -Certificate $SignCert `
            -SignToolPath $SignTool -TimestampUrl $SignTs -NoTimestamp:$NoTimestamp | Out-Null
    }
    $QtSignedArtifacts += $enginePlan
    Write-Ok ('{0} 个 ps1/psm1 已签(副本:{1})' -f $enginePlan.Count, $SignedEngineDir)
    # 🔴 .cmd **不支持 Authenticode**,不签;链首脚本照旧从仓库原件复制进载荷。
    Write-Host '  .cmd 不支持 Authenticode,不签(链首 run-engine.cmd / precheck-disk.cmd)' -ForegroundColor DarkGray
    # ISCC 改从副本编译 ⇒ 引擎里内嵌的就是**已签名**的脚本
    $IssSourceDir = $SignedEngineDir
}

Write-Section '步 1 编译安装引擎(Inno Setup 6)'
$engineExe = ''
if ($SkipEngine) {
    Write-Warn2 '按 -SkipEngine 跳过引擎编译'
} elseif (-not $Iscc) {
    Write-Warn2 '缺 ISCC.exe,跳过引擎编译 —— 产出的包将没有引擎,只能用于流程验证'
} else {
    $issOut = Join-Path $OutDir 'engine'
    & $Iscc ('/O' + $issOut) ('/DEngineVersion=' + $Version) (Join-Path $IssSourceDir 'qtrade-setup-engine.iss')
    if ($LASTEXITCODE -ne 0) { throw ('ISCC 编译失败,退出码 {0}' -f $LASTEXITCODE) }
    $engineExe = Join-Path $issOut 'qtrade-setup-engine.exe'
    if (-not (Test-Path -LiteralPath $engineExe)) { throw '引擎编译成功但找不到产物 qtrade-setup-engine.exe' }
    Write-Ok ('引擎:{0}({1:N0} 字节)' -f $engineExe, (Get-Item -LiteralPath $engineExe).Length)
    # ── 步 1b:签引擎 exe(🔴 必须在它被复制进 stage、被 collect 算 sha256 之前)────
    if ($Sign) {
        Invoke-QtSignFile -Path $engineExe -Thumbprint $CertThumbprint -Certificate $SignCert `
            -SignToolPath $SignTool -TimestampUrl $SignTs -NoTimestamp:$NoTimestamp | Out-Null
        $QtSignedArtifacts += $engineExe
        Write-Ok ('引擎 exe 已签({0:N0} 字节,签名后)' -f (Get-Item -LiteralPath $engineExe).Length)
    }
}

# ── 步 2:收集载荷 + 生成 manifest ─────────────────────────────────────────
Write-Section '步 2 收集载荷与 manifest'
$collect = & (Join-Path $BuildDir 'collect-payload.ps1') -Stage $StageDir -SourceRoot $SourceRoot `
    -PackageVersion $Version -AllowMissing:$AllowMissing -Clean

# 引擎与 SFX 链首脚本落进载荷(§2.1:引擎本来就在解压目录里)
New-Item -ItemType Directory -Path (Join-Path $StageDir 'install\engine') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $EngineDir $ChainHeadName) -Destination (Join-Path $StageDir 'install\engine') -Force
if ($engineExe) {
    Copy-Item -LiteralPath $engineExe -Destination (Join-Path $StageDir 'install\engine') -Force
    # 引擎进了 stage 才能进 manifest,故重跑一次收集(它会把引擎登记进 files[])
    $collect = & (Join-Path $BuildDir 'collect-payload.ps1') -Stage $StageDir -SourceRoot $SourceRoot `
        -PackageVersion $Version -AllowMissing:$AllowMissing
}
Write-Ok ('载荷已就位:{0}' -f $StageDir)

# ── 步 3:7z 归档(§2.2.2 压缩参数)────────────────────────────────────────
Write-Section '步 3 7z 归档'
if (-not $SevenZip) {
    Write-Warn2 '缺 7z.exe,无法归档 —— 到此为止(载荷 stage 已生成,可人工检查)'
    exit 0
}
if (Test-Path -LiteralPath $ArchivePath) { Remove-Item -LiteralPath $ArchivePath -Force }

$packedEntries = @()
$packedFile = Join-Path $StageDir 'install\.packed-entries.txt'
if (Test-Path -LiteralPath $packedFile) {
    $packedEntries = @([IO.File]::ReadAllLines($packedFile) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

# 第一块:可压缩件 —— solid、LZMA2 ultra(§2.2.2:`-t7z -mx=9 -m0=lzma2 -ms=on -mf=BCJ2`)
$excludes = @()
foreach ($p in $packedEntries) { $excludes += ('-x!' + ($p -replace '/', '\')) }
# `.packed-entries.txt` 是**打包期中间件**(告诉本脚本哪些进第二块),不是载荷,
# 也不在 manifest 里 —— 让它进包等于给装机现场塞一个没人校验、没人用的文件。
$excludes += '-x!install\.packed-entries.txt'
$argsA = @('a', '-t7z', '-mx=9', '-m0=lzma2', '-ms=on', '-mf=BCJ2', $ArchivePath, (Join-Path $StageDir '*')) + $excludes
& $SevenZip @argsA | Out-Null
if ($LASTEXITCODE -gt 1) { throw ('7z 第一块失败,退出码 {0}' -f $LASTEXITCODE) }
Write-Ok 'solid LZMA2 块已写入'

# 第二块:已压缩件(MSI / 微信安装包 / …)—— **单独非 solid、-mx=0**(省 CI 时间且不掉体积)
$existingPacked = @($packedEntries | Where-Object { Test-Path -LiteralPath (Join-Path $StageDir ($_ -replace '/', '\')) })
if ($existingPacked.Count -gt 0) {
    # ⚠️ 必须**在 stage 目录里、用相对路径**调 7z:给绝对路径的话这些文件会被放进归档**根目录**,
    #    而不是 `pkg\vcredist\…` / `wsl\…` —— 解压出来路径全错,引擎按 manifest 一个也找不到。
    Push-Location $StageDir
    try {
        $rel = @($existingPacked | ForEach-Object { $_ -replace '/', '\' })
        $argsB = @('a', '-t7z', '-mx=0', '-ms=off', $ArchivePath) + $rel
        & $SevenZip @argsB | Out-Null
    } finally { Pop-Location }
    if ($LASTEXITCODE -gt 1) { throw ('7z 第二块失败,退出码 {0}' -f $LASTEXITCODE) }
    Write-Ok ('非 solid -mx=0 块已写入({0} 个已压缩件)' -f $existingPacked.Count)
} else {
    Write-Warn2 '没有已压缩件可放进第二块(轻量包常见)'
}

# 归档后复核:manifest 里每个非通配条目都能在归档里按**原路径**找到
# (上面那类「绝对路径 → 落到归档根」的错位是静默的,只有这一步能把它逼出来)
$listed = & $SevenZip 'l' '-ba' '-slt' $ArchivePath
$inArchive = @($listed | Where-Object { $_ -like 'Path = *' } | ForEach-Object { ($_ -replace '^Path = ', '').Trim() })
$manifestObj = (Get-Content -LiteralPath (Join-Path $StageDir 'install\manifest.json') -Raw) | ConvertFrom-Json
$lost = @()
foreach ($f in $manifestObj.files) {
    $rel = ([string]$f.path) -replace '/', '\'
    if ($rel.EndsWith('\*')) { continue }
    if ($inArchive -notcontains $rel) { $lost += $rel }
}
if ($lost.Count -gt 0) {
    foreach ($l in $lost) { Write-Bad ('归档里找不到(或路径错位):' + $l) }
    throw '归档与 manifest 对不上:解压后引擎会按 manifest 找不到文件'
}
Write-Ok ('归档 ↔ manifest 路径复核通过({0} 个条目)' -f $manifestObj.files.Count)
Write-Ok ('归档:{0}({1:N0} 字节)' -f $ArchivePath, (Get-Item -LiteralPath $ArchivePath).Length)

# ── 步 4:拼 SFX 存根 + 配置 + 归档 = 单文件 EXE ────────────────────────────
Write-Section '步 4 拼接自解压 EXE'
if (-not $SfxStub) {
    Write-Warn2 ('缺 SFX 存根({0}),无法拼单 EXE —— 归档已生成,可人工拼接' -f $StubKind)
    Write-Host ''
    Write-Host ('  copy /b "<存根>" + "{0}" + "{1}" "{2}"' -f (Join-Path $BuildDir $SfxConfigName), $ArchivePath, $FinalExe)
    exit 0
}
# 🔴 自编存根的二进制自证:防的是「把官方存根改名成 QTradeSD.sfx」这种最容易犯的错 ——
#    那样 G5 会按 8 键白名单放行 InstallPath,而存根其实不认,载荷照样解到 %TEMP% 跑完即删。
if ($StubKind -eq 'qtrade') {
    Import-Module (Join-Path $InstallerRoot 'sfx-stub\QTrade.SfxStub.psm1')
    $stubCheck = Test-QtSfxStubBinary -Path $SfxStub
    if (-not $stubCheck.Ok) {
        foreach ($pb in $stubCheck.Problems) { Write-Bad $pb }
        throw '自编存根自检不通过 —— 它不是一个打过 QTrade 补丁的 x86 静态 CRT 存根'
    }
    Write-Ok ('自编存根自检通过(x86 / 静态 CRT / 含 InstallPath,sha256 {0})' -f $stubCheck.Sha256.Substring(0, 16))
}
$cfg = Join-Path $BuildDir $SfxConfigName
# 🔴 **必须流式拼接,不能 `[IO.File]::ReadAllBytes($part)`** ——
#    PowerShell 5.1 跑在 .NET Framework 上,`ReadAllBytes` 要把整个文件塞进一个
#    `byte[]`,而 `byte[]` 的长度上限是 `Int32.MaxValue`(2 GiB)。真载荷的 payload.7z
#    是 **2.05 GB**(rootfs.tar 一项就 2.16 GB,且它已是 docker 层、LZMA2 压不动),
#    于是最后一步直接抛
#      「使用"1"个参数调用"ReadAllBytes"时发生异常:该文件太长。此操作当前仅限于支持大小小于 2 GB 的文件。」
#    ——存根和配置已经写进去了,EXE 留下一个 ~200 KB 的**残次品**,看着像出包成功。
#    2026-09-20 首次用真载荷出包时踩到;此前只出过 1.7 MB 的轻量验证包,永远碰不到这条线。
#    `CopyTo` 按块搬,与文件大小无关。
$fs = [IO.File]::Create($FinalExe)
try {
    foreach ($part in @($SfxStub, $cfg, $ArchivePath)) {
        $in = [IO.File]::OpenRead($part)
        try { $in.CopyTo($fs, 1MB) } finally { $in.Dispose() }
    }
} finally { $fs.Close() }
Write-Ok ('单文件安装包:{0}({1:N0} 字节)' -f $FinalExe, (Get-Item -LiteralPath $FinalExe).Length)

# ── 步 5:签外壳 EXE(🔴 **最后一步**;签名覆盖整个 EXE 含归档,§2.2.2)────────
if ($Sign) {
    Write-Section '步 5 签外壳 EXE(最后一步)'
    Invoke-QtSignFile -Path $FinalExe -Thumbprint $CertThumbprint -Certificate $SignCert `
        -SignToolPath $SignTool -TimestampUrl $SignTs -NoTimestamp:$NoTimestamp | Out-Null
    $QtSignedArtifacts += $FinalExe
    Write-Ok ('外壳已签:{0}({1:N0} 字节,签名后)' -f $FinalExe, (Get-Item -LiteralPath $FinalExe).Length)
    Write-Warn2 '此后**再改载荷就必须重签**(签名覆盖整个 EXE,含里面的 7z 归档)'
}

# ── 门 6:签名复核(只在 -Sign 时)──────────────────────────────────────────
# 🔴 逐个验证「应签文件**确实带签名**、且签名者指纹 = 传入的指纹」,缺一个就 throw。
#    为什么不能省:signtool 的退出码只说明"命令没报错",证明不了文件里真写进了签名;
#    而预签副本 + 两次 collect 的链条里,任何一环把文件覆盖回未签名版本都是**静默**的。
if ($Sign) {
    Write-Section 'G6 签名复核(应签文件全部带签名 + 指纹逐字相符)'

    $g6 = @()
    $g6 += [pscustomobject]@{ Path = $FinalExe; What = '外壳 EXE' }
    $g6 += [pscustomobject]@{ Path = (Join-Path $StageDir 'install\engine\qtrade-setup-engine.exe'); What = '引擎 EXE(stage 内)' }
    $g6 += [pscustomobject]@{ Path = (Join-Path $StageDir 'winagent\app\qtrade-winagent-svc.exe'); What = 'WinAgent 服务(stage 内)' }
    $g6 += [pscustomobject]@{ Path = (Join-Path $StageDir 'winagent\app\qtrade-winagent-user.exe'); What = 'WinAgent 会话代理(stage 内)' }
    foreach ($e in @(Get-ChildItem -LiteralPath (Join-Path $StageDir 'console') -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ieq '.exe' })) {
        $g6 += [pscustomobject]@{ Path = $e.FullName; What = 'Electron 主程序(stage 内)' }
    }
    foreach ($f in @(Get-QtSignPlan -Root $SignedEngineDir -Scope 'engine')) {
        $g6 += [pscustomobject]@{ Path = $f; What = 'ps1/psm1(内嵌进引擎 EXE 的那一份)' }
    }

    $g6Bad = @()
    $g6Skip = @()
    $g6Untrusted = 0
    foreach ($t in $g6) {
        if (-not (Test-Path -LiteralPath $t.Path)) {
            # 轻量验证包里本来就缺件;正式包缺一个都不行
            if ($AllowMissing) { $g6Skip += $t.Path; continue }
            $g6Bad += ('{0}:文件不在({1})' -f $t.What, $t.Path); continue
        }
        $v = Test-QtSignatureVerdict -Signature (Get-QtFileSignature -Path $t.Path) -ExpectedThumbprint $CertThumbprint
        if (-not $v.Ok) { $g6Bad += ('{0} {1}:{2}' -f $t.What, (Split-Path -Leaf $t.Path), $v.Reason); continue }
        if (-not $v.Trusted) { $g6Untrusted++ }
    }

    # 🔴 反向检查:第三方件**不该**被我们的证书签过(重签会毁掉原厂签名链;
    #    随包微信的 sha256 还是钉死的,改一个字节就是 E_INSTALL_PAYLOAD_CORRUPT)。
    $thirdParty = @(
        'pkg\wechat\weixin_4.1.12.26.exe'
        'pkg\vcredist\VC_redist.x64.exe'
        'pkg\adb\adb.exe'
        'pkg\scrcpy\scrcpy.exe'
        'winagent\python\python.exe'
    )
    foreach ($rel in $thirdParty) {
        $abs = Join-Path $StageDir $rel
        if (-not (Test-Path -LiteralPath $abs)) { continue }
        $sig = Get-QtFileSignature -Path $abs
        $th = ''
        if ($sig -and $sig.SignerCertificate) { $th = [string]$sig.SignerCertificate.Thumbprint }
        if ($th -and $th -eq $CertThumbprint) {
            $g6Bad += ('🔴 第三方件被我方证书重签了:{0} —— §2.2.3 要求 adb/scrcpy 等由其上游签名不动' -f $rel)
        }
    }

    # 🔴 「签名先于 manifest 哈希」的**真凭据**:manifest 里登记的引擎 sha256
    #    必须等于 stage 里那个**已签名**文件的实际 sha256。顺序错了这里当场红。
    $mfPath = Join-Path $StageDir 'install\manifest.json'
    $engInStage = Join-Path $StageDir 'install\engine\qtrade-setup-engine.exe'
    if ((Test-Path -LiteralPath $mfPath) -and (Test-Path -LiteralPath $engInStage)) {
        $mf = (Get-Content -LiteralPath $mfPath -Raw) | ConvertFrom-Json
        $row = @($mf.files | Where-Object { $_.path -eq 'install/engine/qtrade-setup-engine.exe' })
        if ($row.Count -gt 0) {
            $actual = (Get-FileHash -LiteralPath $engInStage -Algorithm SHA256).Hash.ToLowerInvariant()
            if ([string]$row[0].sha256 -ne $actual) {
                $g6Bad += ('manifest 里引擎的 sha256({0})与 stage 里已签名文件的实际值({1})不符 —— 签名发生在算哈希**之后**了,装机时会判 PAYLOAD_CORRUPT' -f
                    $row[0].sha256, $actual)
            }
        }
    }

    foreach ($sk in $g6Skip) { Write-Warn2 ('轻量包缺件,跳过复核:' + $sk) }
    if ($g6Bad.Count -gt 0) {
        foreach ($b in $g6Bad) { Write-Bad $b }
        throw ('G6 签名复核不通过({0} 项)' -f $g6Bad.Count)
    }
    Write-Ok ('{0} 个应签文件全部带签名,签名者指纹 = {1}' -f ($g6.Count - $g6Skip.Count), $CertThumbprint)
    if ($g6Untrusted -gt 0) {
        Write-Warn2 ('其中 {0} 个在**本机**显示"发布者未受信任" —— 自签名阶段的正常态,不是失败;' -f $g6Untrusted)
        Write-Warn2 '   目标机跑 installer\signing\导入QTrade签名证书.cmd 之后即受信任'
    }
}

Write-Section '完成'
Write-Host ('  版本      : {0}' -f $Version)
Write-Host ('  存根      : {0}({1})' -f $StubKind, (Split-Path -Leaf $SfxStub))
if ($StubKind -eq 'official') {
    Write-Host '              ⚠️ 官方存根**不透传退出码**(恒 0):验收请读 <目标>\logs\last-exit-code.txt' -ForegroundColor Yellow
} else {
    Write-Host '              退出码由存根透传,验收可直接读 EXE 退出码' -ForegroundColor DarkGray
}
Write-Host ('  产物      : {0}' -f $FinalExe)
Write-Host ('  轻量包    : {0}' -f $collect.Lightweight)
if ($collect.Missing.Count -gt 0) {
    Write-Host ('  缺件      : {0}' -f ($collect.Missing -join ', ')) -ForegroundColor Yellow
}
Write-Host ''
if ($Sign) {
    Write-Host ('  ✅ 已签名(§2.2.3):指纹 {0}' -f $CertThumbprint) -ForegroundColor Green
    if ($NoTimestamp) {
        Write-Host '     🔴 **没有时间戳**:证书一过期,已发出去的包签名当场失效。联网后请重签。' -ForegroundColor Yellow
    } else {
        Write-Host ('     时间戳:{0}(RFC 3161;证书过期后签名仍有效)' -f $SignTs) -ForegroundColor DarkGray
    }
    Write-Host '     自签名阶段:目标机需先跑 installer\signing\导入QTrade签名证书.cmd,否则仍显示"未知发布者"。' -ForegroundColor DarkGray
    Write-Host '     换公司内部 CA / 购买的 OV 证书时:流程不变,只把 -CertThumbprint 换成新证书的指纹。' -ForegroundColor DarkGray
} else {
    Write-Host '  ⚠️ 签名未做:§2.2.3 要求外壳 EXE / 引擎 EXE / 全部 ps1 用同一张代码签名证书(OV 起步,A-4)签名。'
    Write-Host '     加 -Sign -CertThumbprint <指纹> 即可在出包过程中签(见 build\README.md 第 5 节);'
    Write-Host '     没有证书先跑:installer\signing\New-QtSelfSignedCert.ps1'
}
