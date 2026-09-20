# QTrade —— 反向清理:把签名证书从本机信任库里删掉(需要管理员)
#
# 用法(管理员 PowerShell):
#   .\Remove-QtCodeSigningCert.ps1 -Thumbprint <指纹>
#   .\Remove-QtCodeSigningCert.ps1 -Thumbprint <指纹> -WhatIf
#
# 这个脚本会改动这台机器上的什么:
#   从 `Cert:\LocalMachine\Root` 与 `Cert:\LocalMachine\TrustedPublisher` 里各删掉**指定指纹**那一张。
#   幂等:不在就说「不在」,不报错。不碰任何其它证书,不碰 CurrentUser 的存储。
#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param([Parameter(Mandatory)][string] $Thumbprint)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'QTrade.Signing.psm1') -Force -DisableNameChecking

if (-not (Test-QtAdmin)) {
    Write-Host ''
    Write-Host '  [失败] 需要管理员权限(删除 LocalMachine 存储里的证书是全机生效的操作)。' -ForegroundColor Red
    Write-Host '         请「以管理员身份运行」PowerShell 后重跑。' -ForegroundColor Red
    Write-Host ''
    exit 5
}

$r = Remove-QtCodeSigningCertCore -Thumbprint $Thumbprint
if ($r.WhatIf) {
    Write-Host '  [WhatIf] 干跑,没有真删除任何东西。' -ForegroundColor Yellow
    return $r
}
foreach ($s in $r.NotFound) { Write-Host ('  [不在] {0}(幂等,跳过)' -f $s) -ForegroundColor DarkGray }
foreach ($s in $r.Removed) { Write-Host ('  [删除] {0}' -f $s) -ForegroundColor Green }
Write-Host ''
Write-Host '  完成。这台机器不再把 QTrade 的签名当作受信任发布者。' -ForegroundColor Green
Write-Host '  ⚠️ 已经装好的 QTrade 不受影响;只是以后再运行已签名的包会重新显示「未知发布者」。' -ForegroundColor DarkGray
Write-Host ''
return $r
