<#
.SYNOPSIS
    用 MSVC 重编 QTrade 自用的 SFX 存根 QTradeSD.sfx(x86、静态 CRT)。

.DESCRIPTION
    前置:先跑 .\fetch-sdk.ps1 把源码取回 .\src\ 并打上补丁。

    🔴 本脚本**只探测工具链,绝不自行安装**。缺 C++ 工作负载时打印缺什么、
       怎么补,然后以退出码 2 结束,由人决定装不装。

.EXAMPLE
    .\build.ps1 -ProbeOnly     # 只看工具链够不够
.EXAMPLE
    .\build.ps1                # 真编,产物落到 ..\build\QTradeSD.sfx
#>
[CmdletBinding()]
param(
    [string] $SrcDir,
    [string] $OutputPath,
    [string] $VsWherePath,
    [switch] $ProbeOnly,
    [switch] $Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'QTrade.SfxStub.psm1')

$info = Get-QtSfxSdkInfo
if (-not $SrcDir)     { $SrcDir = Join-Path $PSScriptRoot 'src' }
if (-not $OutputPath) { $OutputPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'build\QTradeSD.sfx' }

# ── 1. 工具链 ─────────────────────────────────────────────────────────────
Write-Host '[1/4] 探测 MSVC C++ 工具链 ...' -ForegroundColor Cyan
$probe = Find-QtMsvcToolchain -VsWherePath $VsWherePath
if (-not $probe.Found) {
    Write-Host ''
    Write-Host (Get-QtMsvcMissingMessage -Probe $probe) -ForegroundColor Yellow
    exit 2
}
Write-Host "  VS 实例  : $($probe.InstallPath)" -ForegroundColor Green
Write-Host "  vcvars32 : $($probe.VcVarsPath)" -ForegroundColor Green
if ($ProbeOnly) { exit 0 }

# ── 2. 源码 ───────────────────────────────────────────────────────────────
Write-Host '[2/4] 检查源码树 ...' -ForegroundColor Cyan
$projDir = Join-Path $SrcDir $info.ProjectDir
if (-not (Test-Path -LiteralPath $projDir)) {
    throw "源码树不在:$projDir`n先跑:.\fetch-sdk.ps1"
}
# 补丁标记 —— 没有它说明源码是原始的,编出来会是一个官方存根(不认 InstallPath)
$mainCpp = Join-Path $projDir 'SfxSetup.cpp'
$mainText = [System.IO.File]::ReadAllText($mainCpp)
if ($mainText -notmatch 'kQTradeExitDiskLow') {
    throw "源码没打补丁(SfxSetup.cpp 里找不到 kQTradeExitDiskLow)。`n先跑:.\fetch-sdk.ps1"
}
if ($mainText -notmatch 'GetExitCodeProcess') {
    throw '源码里找不到 GetExitCodeProcess —— 补丁 (b) 没进去,退出码不会透传。'
}
Write-Host '  补丁标记齐全(InstallPath + 退出码透传)' -ForegroundColor Green

# 🔴 被改过的三个源文件必须带 UTF-8 BOM。
#    补丁往源码里加了中文注释。没有 BOM 时 cl.exe 只能按系统 ANSI 代码页解析:
#    在**非 936** 的机器上(英文 Windows、多数 CI 容器),UTF-8 续字节里的
#    0x81/0x8D/0x8F/0x90/0x9D 落在 CP1252 的未定义码位 —— 每一处触发一条 C4819,
#    而 7-Zip 的 CFLAGS 带 -WX(告警即错误)=> 必然编译失败。
#    fetch-sdk.ps1 里的应用器写出来的就是带 BOM 的;这里再验一道,
#    是为了挡住「有人改用 GNU patch / git apply 打补丁」那条路 —— 它们不加 BOM,
#    而且这个坑只在某些机器上才炸,最难查。
$patched = @('SfxSetup.cpp', 'ExtractEngine.cpp', 'ExtractEngine.h')
$noBom = @()
foreach ($f in $patched) {
    $fp = Join-Path $projDir $f
    $head = [byte[]](Get-Content -LiteralPath $fp -Encoding Byte -TotalCount 3)
    if (-not ($head.Length -eq 3 -and $head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF)) {
        $noBom += $f
    }
}
# 🔴 反过来:resource.rc 与清单**必须没有 BOM**。给 resource.rc 加了 BOM 之后,
#    rc.exe 不再展开第 1 行 #include 进来的宏,直接
#    `error RC2135: file not found: MY_VERSION_INFO_APP`(实测踩过)。
#    这两个文件本来就是纯 ASCII,加 BOM 只有坏处。
$mustNotHaveBom = @('resource.rc', 'qtrade-sfx.manifest')
$badBom = @()
foreach ($f in $mustNotHaveBom) {
    $fp = Join-Path $projDir $f
    if (-not (Test-Path -LiteralPath $fp)) { continue }
    $head = [byte[]](Get-Content -LiteralPath $fp -Encoding Byte -TotalCount 3)
    if ($head.Length -eq 3 -and $head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF) { $badBom += $f }
}
if ($badBom.Count -gt 0) {
    throw ("这些文件不该有 UTF-8 BOM:{0}`n" -f ($badBom -join ', ')) +
          "rc.exe 遇到带 BOM 的 .rc 就不再展开 #include 进来的宏(RC2135)。"
}
if ($noBom.Count -gt 0) {
    throw ("这些打过补丁的源文件缺 UTF-8 BOM:{0}`n" -f ($noBom -join ', ')) +
          "补丁里有中文注释,无 BOM 时 cl.exe 按系统 ANSI 解析,在非中文机上会触发 C4819," +
          "而 -WX 会把它变成错误。`n用 .\fetch-sdk.ps1 重新取源打补丁(它的应用器会写 BOM)," +
          "不要用 GNU patch / git apply。"
}
Write-Host '  三个源文件均带 UTF-8 BOM(cl.exe 不会按 GBK/CP1252 误读中文注释)' -ForegroundColor Green

# ── 3. nmake ──────────────────────────────────────────────────────────────
Write-Host '[3/4] 调 vcvars32 + nmake ...' -ForegroundColor Cyan
$cleanCmd = if ($Clean) { 'nmake -f makefile clean & ' } else { '' }
# vcvars32 会把 Platform=x86 塞进环境,于是 Build.mak 的 $O 变成 x86\ ——
# 产物落在 x86\7zS.sfx;没设 Platform 时落在 o\7zS.sfx。两处都找。
# 🔴 输出落文件再读回,不要 `& cmd.exe ... 2>&1`:
#    PowerShell 会把原生程序的 stderr 当成错误记录,于是 cl.exe 的**每一条告警/错误**
#    都变成一个 NativeCommandError,真正的编译输出反而看不见(实测踩过)。
$logFile = Join-Path $env:TEMP ('qt-sfx-nmake-{0}.log' -f ([guid]::NewGuid().ToString('N')))
$cmdLine = '"{0}" && cd /d "{1}" && {2}nmake -f makefile' -f $probe.VcVarsPath, $projDir, $cleanCmd
& cmd.exe /c "$cmdLine > `"$logFile`" 2>&1"
$rc = $LASTEXITCODE
$output = @()
if (Test-Path -LiteralPath $logFile) {
    $output = @([IO.File]::ReadAllLines($logFile))
    Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue
}
$output | ForEach-Object { Write-Host "  $_" }
if ($rc -ne 0) {
    throw "nmake 失败,退出码 $rc(上面是完整输出;注意 7-Zip 用 -Wall -WX,任何告警都是错误)"
}

$built = @('x86', 'o', 'x64') |
    ForEach-Object { Join-Path $projDir (Join-Path $_ $info.MakeOutput) } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
if (-not $built) {
    throw "nmake 报成功,但找不到产物 $($info.MakeOutput)(找过 x86\ o\ x64\)"
}

# ── 4. 验货 + 落地 ────────────────────────────────────────────────────────
Write-Host '[4/4] 验货 ...' -ForegroundColor Cyan
$verdict = Test-QtSfxStubBinary -Path $built
if (-not $verdict.Ok) {
    Write-Host '产物自检不通过:' -ForegroundColor Red
    foreach ($p in $verdict.Problems) { Write-Host "  - $p" -ForegroundColor Red }
    throw '存根自检失败,不落地。'
}

$outDir = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
Copy-Item -LiteralPath $built -Destination $OutputPath -Force

$final = Test-QtSfxStubBinary -Path $OutputPath
Write-Host ''
Write-Host '存根已生成:' -ForegroundColor Green
Write-Host "  路径   : $OutputPath"
Write-Host "  大小   : $($final.Size) 字节"
Write-Host "  sha256 : $($final.Sha256)"
Write-Host ("  机器   : x86(0x{0:X4})" -f $final.Machine) -ForegroundColor Green
Write-Host ''
Write-Host '下一步:..\build\build.ps1(会自动优先用 QTradeSD.sfx)' -ForegroundColor Cyan
