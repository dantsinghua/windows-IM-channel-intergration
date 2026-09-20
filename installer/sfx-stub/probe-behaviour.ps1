<#
.SYNOPSIS
    哑 EXE 行为实验 —— 用自编存根包一个**只含 hello.cmd 的哑载荷**,实测六件事:
    解压位置 / 留存 / RunProgram 拉起 / 参数透传 / 退出码透传 / 无 InstallPath 时的原行为。

.DESCRIPTION
    🔴 绝不含我方引擎,也绝不运行 QTrade-Setup*.exe。
    🔴 解压目标写死 `%TEMP%\qt-sfx-probe`,**跑前断言**它以 %TEMP% 开头,否则中止 ——
       这道断言是上一批那次「哑载荷被搬进真 %ProgramData%\QTrade」事故之后加的。

    实验用的存根是出货存根的**副本**,用 mt.exe 注入了 asInvoker 清单:
    出货存根声明 requireAdministrator(补丁第三处),在非提权上下文里根本起不来。
    清单只决定要不要提权,对被测的六件事没有任何影响;
    脚本结束时会复核**出货存根 sha256 未变**。
#>
[CmdletBinding()]
param([string] $StubPath, [string] $SevenZipPath, [string] $MtPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'QTrade.SfxStub.psm1')

if (-not $StubPath) { $StubPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'build\QTradeSD.sfx' }
if (-not (Test-Path -LiteralPath $StubPath)) { throw "存根不在:$StubPath`n先跑 .\build.ps1" }
if (-not $SevenZipPath) {
    $SevenZipPath = @('C:\ProgramData\chocolatey\tools\7z.exe', (Join-Path $env:ProgramFiles '7-Zip\7z.exe')) |
        Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $MtPath) {
    $MtPath = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' -Recurse -Filter 'mt.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like '*\x86\*' } | Select-Object -First 1 -ExpandProperty FullName
}
foreach ($t in @(@('7z.exe', $SevenZipPath), @('mt.exe', $MtPath))) {
    if (-not $t[1]) { throw ('找不到 {0}' -f $t[0]) }
}

$Exp   = Join-Path $env:TEMP 'qt-sfx-exp'
$Probe = Join-Path $env:TEMP 'qt-sfx-probe'
$shipHash = (Get-FileHash -LiteralPath $StubPath -Algorithm SHA256).Hash

try {
    foreach ($d in @($Exp, $Probe)) { if (Test-Path $d) { Remove-Item $d -Recurse -Force } }
    New-Item -ItemType Directory -Path $Exp -Force | Out-Null

    # --- 实验专用副本:注入 asInvoker,好让它在非提权上下文里跑 ---
    $Stub = Join-Path $Exp 'probe-stub.sfx'
    Copy-Item -LiteralPath $StubPath -Destination $Stub -Force
    $man = Join-Path $Exp 'asinvoker.manifest'
    [IO.File]::WriteAllText($man, @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" />
  </requestedPrivileges></security></trustInfo>
</assembly>
'@, (New-Object Text.UTF8Encoding($false)))
    & $MtPath -nologo -manifest $man "-outputresource:$Stub;#1" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "mt.exe 注入清单失败,退出码 $LASTEXITCODE" }
    if ((Get-QtPeManifestLevel -Path $Stub) -ne 'asInvoker') { throw '实验副本的清单没注进去' }

    function New-Probe {
        param([string] $Name, [int] $ExitCode, [bool] $WithInstallPath)
        $pay = Join-Path $Exp "pay-$Name"
        New-Item -ItemType Directory -Path (Join-Path $pay 'install\engine') -Force | Out-Null
        $cmd = @('@echo off',
            "> `"%TEMP%\qt-out-$Name.txt`" echo CWD=%CD%",
            ">> `"%TEMP%\qt-out-$Name.txt`" echo SELF=%~f0",
            ">> `"%TEMP%\qt-out-$Name.txt`" echo ARGS=%*",
            "exit /b $ExitCode") -join "`r`n"
        [IO.File]::WriteAllText((Join-Path $pay 'install\engine\hello.cmd'), $cmd + "`r`n", (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText((Join-Path $pay 'marker.txt'), "qtrade-probe`r`n", (New-Object Text.UTF8Encoding($false)))

        $lines = @(';!@Install@!UTF-8!', 'Title="QTrade SFX 探针"', 'Progress="no"')
        if ($WithInstallPath) { $lines += 'InstallPath="%TEMP%\\qt-sfx-probe"' }
        $lines += 'RunProgram="install\\engine\\hello.cmd"'
        $lines += ';!@InstallEnd@!'
        $cfgText = ($lines -join "`r`n") + "`r`n"

        if ($WithInstallPath) {
            # 🔴 跑前断言:解压目标必须以 %TEMP% 开头
            $m = [regex]::Match($cfgText, 'InstallPath="([^"]+)"')
            if (-not $m.Success) { throw '断言失败:配置里没有 InstallPath' }
            if (-not $m.Groups[1].Value.StartsWith('%TEMP%')) {
                throw ('🔴 断言失败:解压目标不以 %TEMP% 开头,而是 ' + $m.Groups[1].Value + ' —— 中止')
            }
            Write-Host ('  [断言通过] 解压目标 = ' + $m.Groups[1].Value) -ForegroundColor DarkGray
        }
        $cfg = Join-Path $Exp "cfg-$Name.txt"
        [IO.File]::WriteAllText($cfg, $cfgText, (New-Object Text.UTF8Encoding($false)))
        $arch = Join-Path $Exp "pay-$Name.7z"
        Push-Location $pay
        & $SevenZipPath a -t7z -mx1 $arch '*' | Out-Null
        Pop-Location
        $exe = Join-Path $Exp "probe-$Name.exe"
        $fs = [IO.File]::Create($exe)
        foreach ($part in @($Stub, $cfg, $arch)) { $b = [IO.File]::ReadAllBytes($part); $fs.Write($b,0,$b.Length) }
        $fs.Close()
        return $exe
    }
    function Invoke-Probe {
        param([string] $Exe, [string] $Name)
        Remove-Item (Join-Path $env:TEMP "qt-out-$Name.txt") -Force -ErrorAction SilentlyContinue
        return (Start-Process -FilePath $Exe -ArgumentList '/QT_MODE=install', '/PROBE=1' -Wait -PassThru -NoNewWindow).ExitCode
    }
    function Get-Out { param([string] $Name)
        $f = Join-Path $env:TEMP "qt-out-$Name.txt"
        if (Test-Path $f) { return ((Get-Content $f) -join ' | ') } else { return '(没有产生)' } }

    $fail = 0
    function Check { param([string] $What, $Got, $Want)
        if ("$Got" -eq "$Want") { Write-Host ("  [OK]   {0,-22} = {1}" -f $What, $Got) -ForegroundColor Green }
        else { Write-Host ("  [FAIL] {0,-22} = {1}  (期望 {2})" -f $What, $Got, $Want) -ForegroundColor Red; $script:fail++ } }

    Write-Host '=== A. 带 InstallPath,子进程退 26 ==='
    $rc = Invoke-Probe -Exe (New-Probe -Name 'p26' -ExitCode 26 -WithInstallPath $true) -Name 'p26'
    Check '退出码透传' $rc 26
    $out = Get-Out 'p26'
    Check '解压位置' ($out -match [regex]::Escape($Probe)) $true
    Check 'RunProgram 拉起' ($out -match 'hello\.cmd') $true
    Check '参数透传' ($out -match '/QT_MODE=install /PROBE=1') $true
    Check '解压目标留存' (Test-Path $Probe) $true
    if (Test-Path $Probe) {
        $files = @(Get-ChildItem $Probe -Recurse -File | ForEach-Object { $_.FullName.Substring($Probe.Length + 1) })
        Write-Host ("         留存内容             = " + ($files -join ', '))
        Check '留存的是整棵树' ($files.Count) 2
    }
    Check '没碰 ProgramData\QTrade' (Test-Path 'C:\ProgramData\QTrade') $false

    Write-Host '=== B. 带 InstallPath,子进程退 3010 ==='
    Remove-Item $Probe -Recurse -Force -ErrorAction SilentlyContinue
    Check '退出码透传(3010)' (Invoke-Probe -Exe (New-Probe -Name 'p3010' -ExitCode 3010 -WithInstallPath $true) -Name 'p3010') 3010

    Write-Host '=== C. 不带 InstallPath —— 应逐字保持官方原行为 ==='
    Remove-Item $Probe -Recurse -Force -ErrorAction SilentlyContinue
    $before = @(Get-ChildItem $env:TEMP -Directory -Filter '7zS*' -ErrorAction SilentlyContinue).Count
    $rc = Invoke-Probe -Exe (New-Probe -Name 'noip' -ExitCode 26 -WithInstallPath $false) -Name 'noip'
    $out = Get-Out 'noip'
    Check '退出码仍透传' $rc 26
    Check '解到 %TEMP%\7zS*' ($out -match '7zS[0-9A-F]+') $true
    Check '没误建 qt-sfx-probe' (Test-Path $Probe) $false
    Check '跑完即删(7zS* 计数)' (@(Get-ChildItem $env:TEMP -Directory -Filter '7zS*' -ErrorAction SilentlyContinue).Count) $before

    Write-Host ''
    Check '出货存根未被改动' ((Get-FileHash -LiteralPath $StubPath -Algorithm SHA256).Hash) $shipHash
    if ($fail -gt 0) { throw "$fail 项不通过" }
    Write-Host '哑 EXE 行为实验全部通过。' -ForegroundColor Green
} finally {
    foreach ($d in @($Exp, $Probe)) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue }
    Get-ChildItem $env:TEMP -Filter 'qt-out-*.txt' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}
