# QTrade 安装器 —— Pester 单测入口
# 需要 **Pester 5**;Windows 自带的是 Pester 3.4.0(语法不兼容 `Should -Be`)。
# 本脚本不往机器的模块路径里装东西:优先用已装的 Pester ≥5,否则从缓存目录加载,
# 缓存目录没有时(且带 -Bootstrap)从 PSGallery 下 nupkg 解到 **缓存目录**(不进 $env:PSModulePath)。
#
# 用法:
#   .\run-pester.ps1                    # 跑全部
#   .\run-pester.ps1 -Bootstrap         # 缺 Pester 5 时允许联网取到缓存目录
#   .\run-pester.ps1 -Path .\QTrade.Wsl.Tests.ps1
#requires -Version 5.1
[CmdletBinding()]
param(
    [string] $Path = '',
    [string] $CacheDir = '',
    [string] $PesterVersion = '5.5.0',
    [switch] $Bootstrap,
    [switch] $Detailed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $CacheDir) { $CacheDir = Join-Path $env:LOCALAPPDATA 'QTradeInstallerTests\pester' }

function Import-QtPester {
    # 1) 机器上已装 Pester ≥ 5
    $installed = @(Get-Module -ListAvailable -Name Pester | Where-Object { $_.Version.Major -ge 5 } | Sort-Object Version -Descending)
    if ($installed.Count -gt 0) {
        Import-Module $installed[0].Path -Force
        return $installed[0].Version.ToString()
    }
    # 2) 缓存目录
    $cached = Join-Path $CacheDir 'Pester.psd1'
    if (Test-Path -LiteralPath $cached) {
        Import-Module $cached -Force
        return (Get-Module Pester).Version.ToString()
    }
    # 3) 取到缓存目录(需显式 -Bootstrap)
    if (-not $Bootstrap) {
        throw ("没有 Pester 5(本机只有 {0})。加 -Bootstrap 从 PSGallery 取到 {1},或自行 Install-Module Pester -MinimumVersion 5.0" -f
            (@(Get-Module -ListAvailable -Name Pester | ForEach-Object { $_.Version.ToString() }) -join ','), $CacheDir)
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
    $nupkg = Join-Path $CacheDir ('pester-{0}.zip' -f $PesterVersion)
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 180 `
        -Uri ('https://www.powershellgallery.com/api/v2/package/Pester/{0}' -f $PesterVersion) -OutFile $nupkg
    Expand-Archive -Path $nupkg -DestinationPath $CacheDir -Force
    Import-Module (Join-Path $CacheDir 'Pester.psd1') -Force
    return (Get-Module Pester).Version.ToString()
}

$ver = Import-QtPester
Write-Host ('Pester {0}' -f $ver)

$target = $Path
if (-not $target) { $target = $PSScriptRoot }

$cfg = New-PesterConfiguration
$cfg.Run.Path = $target
$cfg.Run.Exit = $false
$cfg.Run.PassThru = $true
$cfg.Output.Verbosity = $(if ($Detailed) { 'Detailed' } else { 'Normal' })
$cfg.Should.ErrorAction = 'Stop'

$result = Invoke-Pester -Configuration $cfg
Write-Host ''
Write-Host ('通过 {0} / 失败 {1} / 跳过 {2} / 用时 {3:N1}s' -f
    $result.PassedCount, $result.FailedCount, $result.SkippedCount, $result.Duration.TotalSeconds)
exit $(if ($result.FailedCount -gt 0) { 1 } else { 0 })
