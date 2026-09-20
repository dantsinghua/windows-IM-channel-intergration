<#
.SYNOPSIS
    取官方 LZMA SDK 源码(按 sha256 校验)并应用 QTrade 补丁。

.DESCRIPTION
    仓库里**只存补丁与构建脚本**,不存第三方源码(见 README「许可与出处」)。
    本脚本负责把源码取回来放到 .\src\,再把 qtrade-sfx.patch 打上去。

    🔴 sha256 不符就中止,没有旁路开关 —— 这是供应链上唯一的关卡。

.EXAMPLE
    # 联网取(WSL2 环境下走 Windows 侧代理)
    .\fetch-sdk.ps1 -Proxy http://172.19.176.1:7890

.EXAMPLE
    # 已经有归档了,不联网
    .\fetch-sdk.ps1 -ArchivePath D:\dl\lzma2301.7z
#>
[CmdletBinding()]
param(
    [string] $ArchivePath,
    [string] $Url,
    [string] $Proxy,
    [string] $SevenZipPath,
    [string] $DestDir,
    [switch] $Force,
    [switch] $SkipPatch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'QTrade.SfxStub.psm1')

$info = Get-QtSfxSdkInfo
if (-not $Url)     { $Url = $info.Url }
if (-not $DestDir) { $DestDir = Join-Path $PSScriptRoot 'src' }
$workDir = Join-Path $PSScriptRoot 'work'

if (-not $SevenZipPath) {
    $candidates = @(
        'C:\ProgramData\chocolatey\tools\7z.exe',
        (Join-Path $env:ProgramFiles '7-Zip\7z.exe'),
        (Join-Path ${env:ProgramFiles(x86)} '7-Zip\7z.exe')
    )
    $SevenZipPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $SevenZipPath -or -not (Test-Path -LiteralPath $SevenZipPath)) {
    throw "找不到 7z.exe。用 -SevenZipPath 指定,或 choco install 7zip。"
}

if ((Test-Path -LiteralPath $DestDir) -and -not $Force) {
    Write-Host "源码目录已存在,跳过取源(要重来加 -Force):$DestDir" -ForegroundColor Yellow
    return
}

New-Item -ItemType Directory -Path $workDir -Force | Out-Null

# ── 1. 取归档 ─────────────────────────────────────────────────────────────
if (-not $ArchivePath) {
    $ArchivePath = Join-Path $workDir $info.FileName
    if (-not (Test-Path -LiteralPath $ArchivePath)) {
        Write-Host "下载 $Url ..." -ForegroundColor Cyan
        $params = @{ Uri = $Url; OutFile = $ArchivePath; UseBasicParsing = $true }
        if ($Proxy) { $params['Proxy'] = $Proxy }
        Invoke-WebRequest @params
    }
}

# ── 2. 校验(先校验,再解压)───────────────────────────────────────────────
$hash = Assert-QtSha256 -Path $ArchivePath -Expected $info.Sha256
Write-Host "sha256 校验通过:$hash" -ForegroundColor Green

# ── 3. 解出需要的子树 ─────────────────────────────────────────────────────
if (Test-Path -LiteralPath $DestDir) { Remove-Item -LiteralPath $DestDir -Recurse -Force }
New-Item -ItemType Directory -Path $DestDir -Force | Out-Null

$args7z = @('x', $ArchivePath, "-o$DestDir", '-y')
foreach ($sub in $info.Subtrees) { $args7z += "$sub\*" }
$args7z += '-r'
& $SevenZipPath @args7z | Out-Null
if ($LASTEXITCODE -ne 0) { throw "7z 解压失败,退出码 $LASTEXITCODE" }

$projDir = Join-Path $DestDir $info.ProjectDir
if (-not (Test-Path -LiteralPath $projDir)) {
    throw "解压后找不到 SFXSetup 工程目录:$projDir(归档结构变了?)"
}
Write-Host "已解出:$DestDir" -ForegroundColor Green

# ── 4. 打补丁 ─────────────────────────────────────────────────────────────
if ($SkipPatch) {
    Write-Host '按要求跳过打补丁(-SkipPatch)。' -ForegroundColor Yellow
    return
}
$patch = Join-Path $PSScriptRoot 'qtrade-sfx.patch'
$changed = Invoke-QtUnifiedDiff -PatchPath $patch -Root $DestDir
Write-Host "补丁已应用($($changed.Count) 个文件):" -ForegroundColor Green
foreach ($c in $changed) { Write-Host "  $c" }
Write-Host ''
Write-Host '下一步:.\build.ps1' -ForegroundColor Cyan
