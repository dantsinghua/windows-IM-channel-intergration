<#
.SYNOPSIS
    验证补丁里那条「解压前空间判据」的阈值规则:max(6 GiB, 解包总大小 x 1.1)。

.DESCRIPTION
    🔴 端到端触发不了这条分支:判据是 `free < max(6GiB, unpacked*1.1)`,
       而本机 C:/D: 都有 280 GB+ 可用 —— 要触发就得造一个**声明解包大小 ~260 GiB**
       的哑载荷,不现实(见 README「已知的验证缺口」)。

    所以换一条路:**把真源码里的那个函数原样抽出来**,和一段边界断言一起用同一个
    cl.exe 编译成一个小程序跑一遍。注意是「抽出来」不是「抄一份」——
    抄一份只能证明抄的那份对,源码改了它还会继续绿。

    这里不碰 SDK 的其它任何东西:那个函数是自包含的(只有整数运算)。
#>
[CmdletBinding()]
param([string] $SrcDir, [string] $VsWherePath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'QTrade.SfxStub.psm1')

if (-not $SrcDir) { $SrcDir = Join-Path $PSScriptRoot 'src' }
$engineCpp = Join-Path $SrcDir 'CPP\7zip\Bundles\SFXSetup\ExtractEngine.cpp'
if (-not (Test-Path -LiteralPath $engineCpp)) { throw "源码不在:$engineCpp`n先跑 .\fetch-sdk.ps1" }

$probe = Find-QtMsvcToolchain -VsWherePath $VsWherePath
if (-not $probe.Found) { Write-Host (Get-QtMsvcMissingMessage -Probe $probe); exit 2 }

# ── 把 kQTradeMinFreeBytes 与 QTrade_GetRequiredBytes 原样抽出来 ──────────
$text = [IO.File]::ReadAllText($engineCpp)
$i = $text.IndexOf('static const UInt64 kQTradeMinFreeBytes')
if ($i -lt 0) { throw '源码里找不到 kQTradeMinFreeBytes —— 补丁没打?' }
# 结束锚点必须是**紧接其后**的那个函数;取更远的会把 QTrade_PropToUInt64 一起带进来,
# 那个函数依赖 NWindows::NCOM::CPropVariant,单独编译不了。
$j = $text.IndexOf('static UInt64 QTrade_PropToUInt64')
if ($j -le $i) { $j = $text.IndexOf('static HRESULT QTrade_GetUnpackSize') }
if ($j -le $i) { throw '源码结构变了,抽不出判定函数' }
$extracted = $text.Substring($i, $j - $i).Trim()
if ($extracted -notmatch 'QTrade_GetRequiredBytes') { throw '抽出来的片段里没有 QTrade_GetRequiredBytes' }
Write-Host '抽出的源码片段:' -ForegroundColor Cyan
$extracted -split "`r?`n" | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }

$work = Join-Path $env:TEMP ('qt-space-rule-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    $cpp = @"
#include <stdio.h>
typedef unsigned __int64 UInt64;

$extracted

static int g_fail = 0;
static void CHECK(const char *what, UInt64 got, UInt64 want)
{
    if (got != want) { printf("FAIL %s: got %llu want %llu\n", what, got, want); g_fail++; }
    else             { printf("ok   %-28s = %llu\n", what, got); }
}

int main(void)
{
    const UInt64 GiB = (UInt64)1 << 30;
    /* 空归档 -> 硬下限 6 GiB */
    CHECK("unpacked=0", QTrade_GetRequiredBytes(0), 6 * GiB);
    /* 5 GiB x 1.1 = 5.5 GiB < 6 GiB -> 仍取下限 */
    CHECK("unpacked=5GiB", QTrade_GetRequiredBytes(5 * GiB), 6 * GiB);
    /* 6 GiB x 1.1 = 6.6 GiB > 6 GiB -> 取余量值 */
    CHECK("unpacked=6GiB", QTrade_GetRequiredBytes(6 * GiB), 6 * GiB + (6 * GiB) / 10);
    /* 10 GiB */
    CHECK("unpacked=10GiB", QTrade_GetRequiredBytes(10 * GiB), 10 * GiB + GiB);
    /* 交叉点附近必须单调,且不塌回下限 */
    {
        UInt64 u = 6442450944ULL; /* 正好 6 GiB */
        UInt64 a = QTrade_GetRequiredBytes(u);
        UInt64 b = QTrade_GetRequiredBytes(u + 1);
        if (b < a) { printf("FAIL 单调性: %llu -> %llu\n", a, b); g_fail++; }
        else printf("ok   monotonic near 6GiB      = %llu <= %llu\n", a, b);
    }
    /* 10 TiB 不得溢出回绕 */
    {
        UInt64 huge = (UInt64)10 * 1024 * GiB;
        UInt64 r = QTrade_GetRequiredBytes(huge);
        if (r < huge) { printf("FAIL 溢出: %llu < %llu\n", r, huge); g_fail++; }
        else printf("ok   no overflow at 10TiB     = %llu\n", r);
    }
    printf(g_fail ? "\n*** %d 条不通过 ***\n" : "\n全部通过\n", g_fail);
    return g_fail;
}
"@
    [IO.File]::WriteAllText((Join-Path $work 'test.cpp'), $cpp, (New-Object Text.UTF8Encoding($true)))
    $log = Join-Path $work 'build.log'
    $line = '"{0}" && cd /d "{1}" && cl /nologo /W4 /WX /EHsc test.cpp /Fe:test.exe' -f $probe.VcVarsPath, $work
    & cmd.exe /c "$line > `"$log`" 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Get-Content $log | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        throw "编译判定函数的测试程序失败,退出码 $LASTEXITCODE"
    }
    Write-Host ''
    Write-Host '边界断言(用同一个 cl.exe 编译并真跑):' -ForegroundColor Cyan
    $outLog = Join-Path $work 'run.log'
    & cmd.exe /c "`"$(Join-Path $work 'test.exe')`" > `"$outLog`" 2>&1"
    $rc = $LASTEXITCODE
    Get-Content $outLog | ForEach-Object { Write-Host "  $_" }
    if ($rc -ne 0) { throw "空间判据的边界断言有 $rc 条不通过" }
    Write-Host '空间判据规则验证通过。' -ForegroundColor Green
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
