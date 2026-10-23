# QTrade —— 把签名证书导入本机信任库(**目标机**上跑;需要管理员)
# 规格:docs/03 §2.2.3(AppLocker 环境下由 IT 把发布者加白:签名后把发布者信息与证书指纹写进部署说明)
#
# 用法(管理员 PowerShell):
#   .\Import-QtCodeSigningCert.ps1 -CerPath .\QTrade-CodeSigning-<指纹>.cer -ExpectedThumbprint <指纹>
#   .\Import-QtCodeSigningCert.ps1                             # 不传路径 = 在本目录找唯一的 .cer
#   .\Import-QtCodeSigningCert.ps1 -CerPath .\xxx.cer -WhatIf      # 先看它打算做什么
#   双击版:同目录的「导入QTrade签名证书.cmd」
#
# 这个脚本会改动这台机器上的什么:
#   1) `Cert:\LocalMachine\Root`            +1 张证书(受信任的根证书颁发机构)
#   2) `Cert:\LocalMachine\TrustedPublisher`+1 张证书(受信任的发布者)
#   —— 两处都是**全机生效**,所以要管理员。别的什么都不改。
#   撤销:同目录的 `Remove-QtCodeSigningCert.ps1 -Thumbprint <指纹>`。
#
# 🔴 导进 Root 等于告诉这台机器「这张证书签过的东西都可信」。所以导入前**必须核对指纹**:
#    脚本会把指纹与主题打出来让你看,`-ExpectedThumbprint` 对不上就**拒绝导入**。
#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # 不传则在**本脚本所在目录**自动找唯一的 .cer(双击 .cmd 走的就是这条路)
    [string] $CerPath = '',
    # 安琳给出的指纹;不符即拒绝(防止有人把 .cer 换掉)
    [string] $ExpectedThumbprint = '',
    # 非交互场景(IT 批量部署)跳过人工确认;仍然做 -ExpectedThumbprint 核对
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'QTrade.Signing.psm1') -Force -DisableNameChecking

if (-not (Test-QtAdmin)) {
    Write-Host ''
    Write-Host '  [失败] 需要管理员权限。' -ForegroundColor Red
    Write-Host '         导入到 LocalMachine 的证书存储是全机生效的操作,普通权限做不了。' -ForegroundColor Red
    Write-Host '         请「以管理员身份运行」PowerShell 后重跑,或直接双击同目录的「导入QTrade签名证书.cmd」。' -ForegroundColor Red
    Write-Host ''
    exit 5
}

# 没传 -CerPath 时在本目录找(双击 .cmd 的路径)。0 个 / 多个都明确报错,不猜。
if (-not $CerPath) {
    $cers = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.cer' -File -ErrorAction SilentlyContinue)
    if ($cers.Count -eq 0) {
        Write-Host ''
        Write-Host '  [失败] 本目录下没有找到 .cer 证书文件。' -ForegroundColor Red
        Write-Host ('         请把安琳给的 QTrade-CodeSigning-<指纹>.cer 放进 {0} 再双击。' -f $PSScriptRoot) -ForegroundColor Red
        Write-Host ''
        exit 2
    }
    if ($cers.Count -gt 1) {
        Write-Host ''
        Write-Host ('  [失败] 本目录下有 {0} 个 .cer,不知道该导哪一个:' -f $cers.Count) -ForegroundColor Red
        foreach ($c in $cers) { Write-Host ('           - {0}' -f $c.Name) -ForegroundColor Red }
        Write-Host '         只保留安琳给的那一个,或改用管理员 PowerShell 明确指定:' -ForegroundColor Red
        Write-Host '           .\Import-QtCodeSigningCert.ps1 -CerPath <路径> -ExpectedThumbprint <指纹>' -ForegroundColor Red
        Write-Host ''
        exit 2
    }
    $CerPath = $cers[0].FullName
}

if (-not (Test-Path -LiteralPath $CerPath)) {
    Write-Host ('  [失败] 找不到证书文件:{0}' -f $CerPath) -ForegroundColor Red
    exit 2
}

$cert = Get-QtCertFileObject -Path (Resolve-Path -LiteralPath $CerPath).Path
Write-Host ''
Write-Host '== 请核对这张证书 ============================================' -ForegroundColor Cyan
Write-Host ('  主题   : {0}' -f $cert.Subject)
Write-Host ('  颁发者 : {0}' -f $cert.Issuer)
Write-Host ('  指纹   : {0}' -f $cert.Thumbprint) -ForegroundColor Yellow
Write-Host ('  有效期 : {0:yyyy-MM-dd} ~ {1:yyyy-MM-dd}' -f $cert.NotBefore, $cert.NotAfter)
Write-Host ''
Write-Host '  🔴 指纹必须与安琳给出的完全一致。对不上就**别导**,先找安琳核对。' -ForegroundColor Yellow
Write-Host ''

if (-not $ExpectedThumbprint -and -not $Force) {
    $answer = Read-Host '  指纹核对无误?输入证书指纹以确认(直接回车 = 取消)'
    if (-not $answer) { Write-Host '  已取消,什么都没做。' -ForegroundColor Yellow; exit 1 }
    $ExpectedThumbprint = $answer
}

try {
    $r = Import-QtCodeSigningCertCore -CerPath (Resolve-Path -LiteralPath $CerPath).Path -ExpectedThumbprint $ExpectedThumbprint
}
catch {
    Write-Host ''
    Write-Host ('  [失败] {0}' -f $_.Exception.Message) -ForegroundColor Red
    Write-Host ''
    exit 3
}

if ($r.WhatIf) {
    Write-Host '  [WhatIf] 干跑,没有真导入任何东西。' -ForegroundColor Yellow
    return $r
}
foreach ($s in $r.AlreadyThere) { Write-Host ('  [已有] {0}(幂等,跳过)' -f $s) -ForegroundColor DarkGray }
foreach ($s in $r.Imported) { Write-Host ('  [导入] {0}' -f $s) -ForegroundColor Green }
Write-Host ''
Write-Host '  完成。现在这台机器会把 QTrade 的签名认作「已知发布者」。' -ForegroundColor Green
Write-Host ('  要撤销:.\Remove-QtCodeSigningCert.ps1 -Thumbprint {0}(同样需要管理员)' -f $r.Thumbprint) -ForegroundColor DarkGray
Write-Host ''
return $r
