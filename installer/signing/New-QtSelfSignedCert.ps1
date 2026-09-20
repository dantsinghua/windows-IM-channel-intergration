# QTrade 安装器 —— 生成自签名代码签名证书(自签名阶段;将来换公司内部 CA / 购买的 OV 证书时本脚本作废)
# 规格:docs/03 §2.2.3(证书类型定案 A-4「OV 起步」—— 自签名是**内部试用期**的过渡形态,不是交付形态)
#
# 用法:
#   .\New-QtSelfSignedCert.ps1 -WhatIf                         # 🔴 先干跑一遍,看它打算做什么
#   .\New-QtSelfSignedCert.ps1 -Organization 'QTrade'          # 真生成
#   .\New-QtSelfSignedCert.ps1 -Organization 'QTrade' -ValidYears 3 -CertDir 'C:\Users\anlin\qtrade-payload\signing'
#
# 这个脚本会改动这台机器上的什么:
#   1) 在 `Cert:\CurrentUser\My`(**当前用户**的个人证书存储)里多一张代码签名证书;
#   2) 在 -CertDir(默认 C:\Users\anlin\qtrade-payload\signing\)下多一个 **公钥** .cer 文件。
#   ——— 不碰 LocalMachine 任何存储、不需要管理员、不动任何已有证书。
#   撤销:`Remove-Item Cert:\CurrentUser\My\<指纹>`,再删掉那个 .cer。
#
# 🔴 私钥 `-KeyExportPolicy NonExportable`:**不可导出**。本脚本没有任何路径产出 .pfx,
#    密钥材料只存在于当前用户的证书存储里 —— 永远不落盘、永远不进仓库。
#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # 证书主题里的 O=;不传则只有 CN=
    [string] $Organization = '',
    # 有效期(年),默认 3 年
    [ValidateRange(1, 30)][int] $ValidYears = 3,
    # 公钥 .cer 的导出目录;默认产物根下的 signing\(**仓库之外**)
    [string] $CertDir = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'QTrade.Signing.psm1') -Force -DisableNameChecking

$defaults = Get-QtSigningDefault
if (-not $CertDir) { $CertDir = $defaults.CertDir }

Write-Host ''
Write-Host '== QTrade 自签名代码签名证书 =================================' -ForegroundColor Cyan
Write-Host ('  主题     : {0}' -f (Get-QtCertSubject -Organization $Organization))
Write-Host  '  算法     : RSA 3072 / SHA-256 / 用途 CodeSigning'
Write-Host ('  有效期   : {0} 年' -f $ValidYears)
Write-Host  '  私钥     : **不可导出**(NonExportable),存 Cert:\CurrentUser\My'
Write-Host ('  公钥导出 : {0}' -f $CertDir)
Write-Host ''

$r = New-QtCodeSigningCertCore -Organization $Organization -ValidYears $ValidYears -CertDir $CertDir

if ($r.WhatIf) {
    Write-Host '  [WhatIf] 干跑,没有真生成任何东西。' -ForegroundColor Yellow
    Write-Host ('  [WhatIf] 真跑时会在 Cert:\CurrentUser\My 生成证书,并导出公钥到 {0}' -f $r.CerPath) -ForegroundColor Yellow
    return $r
}

if ($r.Reused) {
    Write-Host '  [复用] 已有同主题且未过期的证书,**没有重复生成**。' -ForegroundColor Green
} else {
    Write-Host '  [新建] 证书已生成。' -ForegroundColor Green
}
Write-Host ('  指纹     : {0}' -f $r.Thumbprint) -ForegroundColor Green
Write-Host ('  公钥 .cer: {0}' -f $r.CerPath)
Write-Host ''
Write-Host '  下一步:带签名出包' -ForegroundColor DarkGray
Write-Host ('    cd ..\build; .\build.ps1 -Version 1.0.0 -SourceRoot <产物根> -Sign -CertThumbprint {0}' -f $r.Thumbprint) -ForegroundColor DarkGray
Write-Host '  再下一步:把上面那个 .cer + installer\signing\ 下的导入脚本随包发给目标机同事' -ForegroundColor DarkGray
Write-Host ''
return $r
