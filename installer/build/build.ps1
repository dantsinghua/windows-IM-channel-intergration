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
    [switch] $AllowMissing,
    [switch] $CheckOnly,
    [switch] $SelfCheck,
    [switch] $SkipEngine
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
# 🔴 SFX 存根(7zSD.sfx / 7zS2.sfx)不在 7-Zip 主安装包里,要单独取 7-Zip Extra 或 LZMA SDK
$SfxStub = Find-QtTool -Explicit $SfxStubPath -Name '7zSD.sfx' -Param '-SfxStubPath' `
    -HowTo '🔴 SFX 存根**不在 7-Zip 主安装包里**:去 https://www.7-zip.org/download.html 下 “7-Zip Extra”(7z<版本>-extra.7z),解出里面的 7zSD.sfx,放进 installer\build\' -Candidates @(
    (Join-Path $BuildDir '7zSD.sfx')
    (Join-Path $BuildDir '7zS2.sfx')
    "$env:ProgramFiles\7-Zip\7zSD.sfx"
    "$env:ProgramFiles\7-Zip\7z.sfx"
)

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
$scriptFiles = @(Get-QtScriptFile -Root $EngineDir) +
@(Get-QtScriptFile -Root $BuildDir) +
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

if ($CheckOnly) {
    Write-Section '仅检查模式(-CheckOnly):四道门已跑完,不出包'
    exit 0
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

if ($SelfCheck) {
    # -SelfCheck:四道门 + 收载荷 + 生成 manifest。不碰 ISCC / 7z / SFX 存根,
    # 所以在**没装工具链的开发机上也能跑**,用来确认「载荷来源对不对、manifest 长什么样」。
    Write-Section '步 S 自检模式:收集载荷并生成 manifest(不编译、不归档、不拼 EXE)'
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

# ── 步 1:编译引擎(Inno Setup 6)──────────────────────────────────────────
Write-Section '步 1 编译安装引擎(Inno Setup 6)'
$engineExe = ''
if ($SkipEngine) {
    Write-Warn2 '按 -SkipEngine 跳过引擎编译'
} elseif (-not $Iscc) {
    Write-Warn2 '缺 ISCC.exe,跳过引擎编译 —— 产出的包将没有引擎,只能用于流程验证'
} else {
    $issOut = Join-Path $OutDir 'engine'
    & $Iscc ('/O' + $issOut) ('/DEngineVersion=' + $Version) (Join-Path $EngineDir 'qtrade-setup-engine.iss')
    if ($LASTEXITCODE -ne 0) { throw ('ISCC 编译失败,退出码 {0}' -f $LASTEXITCODE) }
    $engineExe = Join-Path $issOut 'qtrade-setup-engine.exe'
    if (-not (Test-Path -LiteralPath $engineExe)) { throw '引擎编译成功但找不到产物 qtrade-setup-engine.exe' }
    Write-Ok ('引擎:{0}({1:N0} 字节)' -f $engineExe, (Get-Item -LiteralPath $engineExe).Length)
}

# ── 步 2:收集载荷 + 生成 manifest ─────────────────────────────────────────
Write-Section '步 2 收集载荷与 manifest'
$collect = & (Join-Path $BuildDir 'collect-payload.ps1') -Stage $StageDir -SourceRoot $SourceRoot `
    -PackageVersion $Version -AllowMissing:$AllowMissing -Clean

# 引擎与 SFX 链首脚本落进载荷(§2.1:引擎本来就在解压目录里)
New-Item -ItemType Directory -Path (Join-Path $StageDir 'install\engine') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $EngineDir 'precheck-disk.cmd') -Destination (Join-Path $StageDir 'install\engine') -Force
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
    Write-Warn2 '缺 SFX 存根(7zSD.sfx),无法拼单 EXE —— 归档已生成,可人工拼接'
    Write-Host ''
    Write-Host ('  copy /b "<7zSD.sfx>" + "{0}" + "{1}" "{2}"' -f (Join-Path $BuildDir 'sfx-config.txt'), $ArchivePath, $FinalExe)
    exit 0
}
$cfg = Join-Path $BuildDir 'sfx-config.txt'
$fs = [IO.File]::Create($FinalExe)
try {
    foreach ($part in @($SfxStub, $cfg, $ArchivePath)) {
        $bytes = [IO.File]::ReadAllBytes($part)
        $fs.Write($bytes, 0, $bytes.Length)
    }
} finally { $fs.Close() }
Write-Ok ('单文件安装包:{0}({1:N0} 字节)' -f $FinalExe, (Get-Item -LiteralPath $FinalExe).Length)

Write-Section '完成'
Write-Host ('  版本      : {0}' -f $Version)
Write-Host ('  产物      : {0}' -f $FinalExe)
Write-Host ('  轻量包    : {0}' -f $collect.Lightweight)
if ($collect.Missing.Count -gt 0) {
    Write-Host ('  缺件      : {0}' -f ($collect.Missing -join ', ')) -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  ⚠️ 签名未做:§2.2.3 要求外壳 EXE / 引擎 EXE / 全部 ps1 用同一张代码签名证书(OV 起步,A-4)签名。'
Write-Host '     本脚本不持有证书,签名由 CI 在本步之后做(signtool sign /fd sha256 /tr <时间戳> /td sha256 …)。'
