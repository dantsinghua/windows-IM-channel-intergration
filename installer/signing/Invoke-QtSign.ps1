# QTrade 安装器 —— 给一批文件签名并逐个复核(手工补签 / 排障用;正式出包走 build.ps1 -Sign)
# 规格:docs/03 §2.2.3(SHA-256 + RFC 3161 时间戳;exe 走 signtool,ps1 走 Set-AuthenticodeSignature)
#
# 用法:
#   .\Invoke-QtSign.ps1 -CertThumbprint <指纹> -Path a.exe,b.ps1
#   .\Invoke-QtSign.ps1 -CertThumbprint <指纹> -Path <目录> -Scope engine        # 目录内全部 ps1/psm1
#   .\Invoke-QtSign.ps1 -CertThumbprint <指纹> -Path <副本目录> -Scope winagent  # 顶层两个 winagent exe
#   .\Invoke-QtSign.ps1 -CertThumbprint <指纹> -Path a.exe -NoTimestamp          # 离线环境(见下面的警告)
#
# 这个脚本会改动这台机器上的什么:**只改 -Path 指到的那些文件**(在文件尾追加 Authenticode 签名块)。
# 🔴 别拿它去签仓库里的源文件 —— 签名块会改文件内容:污染 git、破 build.ps1 的 G1 BOM 门
#    (BOM 还在,但文件哈希变了)、破 test_spec_consistency.py 的逐字对账。要签就签**副本**。
#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $CertThumbprint,
    [Parameter(Mandatory)][string[]] $Path,
    # 给目录用:按范围算出应签清单(engine = 全部 ps1/psm1;winagent / console = 顶层我方 exe)
    [ValidateSet('', 'engine', 'winagent', 'console')][string] $Scope = '',
    [string] $TimestampUrl = '',
    [switch] $NoTimestamp,
    [string] $SignToolPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'QTrade.Signing.psm1') -Force -DisableNameChecking

$defaults = Get-QtSigningDefault
if (-not $TimestampUrl) { $TimestampUrl = $defaults.TimestampUrl }
if ($NoTimestamp) { Write-QtNoTimestampWarning }

# 展开应签清单
$targets = @()
foreach ($p in $Path) {
    if ($Scope -and (Test-Path -LiteralPath $p -PathType Container)) {
        $targets += @(Get-QtSignPlan -Root $p -Scope $Scope)
    } else {
        $targets += $p
    }
}
$targets = @($targets | Select-Object -Unique)
if ($targets.Count -eq 0) { throw '应签清单是空的 —— 检查 -Path / -Scope 是不是指错了' }

$cert = Get-QtSigningCertByThumbprint -Thumbprint $CertThumbprint
$signTool = Find-QtSignTool -Explicit $SignToolPath
if (-not $signTool) {
    # 全是脚本时不需要 signtool;有 PE 才需要
    $needPe = @($targets | Where-Object { (Get-QtSignFileKind -Path $_) -eq 'exe' })
    if ($needPe.Count -gt 0) {
        throw ('要签 {0} 个 PE 文件但找不到 signtool.exe —— 装 Windows SDK(Signing Tools),或用 -SignToolPath 指定完整路径' -f $needPe.Count)
    }
}

Write-Host ''
Write-Host ('== 签名 {0} 个文件(指纹 {1}) ==============' -f $targets.Count, $cert.Thumbprint) -ForegroundColor Cyan
if (-not $NoTimestamp) { Write-Host ('  时间戳: {0}' -f $TimestampUrl) -ForegroundColor DarkGray }

$results = Invoke-QtSignFiles -Path $targets -Thumbprint $cert.Thumbprint -Certificate $cert `
    -SignToolPath $signTool -TimestampUrl $TimestampUrl -NoTimestamp:$NoTimestamp

$untrusted = 0
foreach ($r in $results) {
    if ($r.Trusted) {
        Write-Host ('  [OK]   {0}' -f (Split-Path -Leaf $r.Path)) -ForegroundColor Green
    } else {
        $untrusted++
        Write-Host ('  [OK*]  {0}  (签名完好,本机未信任该发布者)' -f (Split-Path -Leaf $r.Path)) -ForegroundColor Green
    }
}
if ($untrusted -gt 0) {
    Write-Host ''
    Write-Host ('  * {0} 个文件的发布者在**本机**未受信任 —— 这是自签名阶段的正常态,不是失败。' -f $untrusted) -ForegroundColor Yellow
    Write-Host  '    要让本机(或目标机)信任,跑:installer\signing\Import-QtCodeSigningCert.ps1(需管理员)' -ForegroundColor Yellow
}
Write-Host ''
return $results
