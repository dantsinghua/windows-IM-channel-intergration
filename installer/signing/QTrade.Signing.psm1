# QTrade 安装器 —— 代码签名(自签名阶段)的唯一出入口
# 规格:docs/03 §2.2.3(外壳 EXE / 引擎 EXE / qtrade-winagent-svc.exe / Electron 主程序 / 全部 ps1;
#       SHA-256 + RFC 3161 时间戳;adb / scrcpy 由其上游签名**不动**)、§2.2.2(单文件签名覆盖整个
#       EXE 含归档 ⇒ 改载荷必须重签)、§2.6.7 W1(Authenticode 签名块追加在**文件尾**,BOM 仍在)。
#
# 🔴 设计约束(照 engine/modules/QTrade.Native.psm1 的规矩来):
#    **所有**对 signtool.exe / Set-AuthenticodeSignature / Get-AuthenticodeSignature /
#    证书存储(Cert:\)/ 管理员身份 的访问都必须经本模块的「接缝」函数,别处一律不直接调。
#    理由:①单测可整组 Mock,**不碰这台机器的证书库、不给任何文件真签名**(本批禁区);
#          ②signtool 的参数只拼一处;③「自签名证书在未导入信任库的机器上状态不是 Valid」
#          这条反直觉的判定只写一处(见 Test-QtSignatureVerdict)。
#requires -Version 5.1
Set-StrictMode -Version Latest

# 自签名阶段的默认公钥导出目录:**产物根之下、仓库之外**(仓库里绝不出现任何密钥材料)
$script:QtDefaultCertDir = 'C:\Users\anlin\qtrade-payload\signing'
# 默认时间戳服务(RFC 3161);离线环境用 -NoTimestamp 显式关闭
$script:QtDefaultTimestampUrl = 'http://timestamp.digicert.com'
$script:QtCertSubjectCn = 'QTrade Internal Code Signing'

#region 接缝:外部世界(测试整组 Mock)────────────────────────────────────────

function Get-QtSigningDefault {
    <#
    .SYNOPSIS
        模块级默认值(证书目录 / 时间戳 URL / 证书 CN)。集中一处,便于对账与测试。
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param()
    return @{
        CertDir      = $script:QtDefaultCertDir
        TimestampUrl = $script:QtDefaultTimestampUrl
        SubjectCn    = $script:QtCertSubjectCn
    }
}

function Find-QtSignTool {
    <#
    .SYNOPSIS
        探测 signtool.exe(Windows SDK)。风格与 build.ps1 的 Find-QtTool 一致:
        **只找、不装**;找不到就回空串,由调用方决定报错措辞。
    .NOTES
        本机实测在 10.0.19041.0\x64 下。列表按「新 SDK 在前、x64 在前」排,
        取第一个存在的;都不在就退回 PATH 里的 signtool。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string] $Explicit = '')

    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        return ''
    }
    $roots = @("${env:ProgramFiles(x86)}\Windows Kits\10\bin", "$env:ProgramFiles\Windows Kits\10\bin")
    $vers = @('10.0.26100.0', '10.0.22621.0', '10.0.22000.0', '10.0.20348.0', '10.0.19041.0', '10.0.18362.0', '10.0.17763.0')
    foreach ($r in $roots) {
        foreach ($v in $vers) {
            foreach ($arch in @('x64', 'x86')) {
                $p = [IO.Path]::Combine($r, $v, $arch, 'signtool.exe')
                if (Test-Path -LiteralPath $p) { return $p }
            }
        }
        # SDK 7/8 风格:bin\x64\signtool.exe(没有版本号那层)
        foreach ($arch in @('x64', 'x86')) {
            $p = [IO.Path]::Combine($r, $arch, 'signtool.exe')
            if (Test-Path -LiteralPath $p) { return $p }
        }
    }
    $cmd = Get-Command -Name 'signtool.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ''
}

function Invoke-QtSignToolRaw {
    <#
    .SYNOPSIS
        真正拉起 signtool.exe 的**唯一**地方。回 {ExitCode, Output}。
    .NOTES
        🔴 PowerShell 5.1 的 $ErrorActionPreference **管不到原生 exe**(WinAgent build.ps1 上
        已经踩过一次,见 commit 0dba42d),所以这里自己读 $LASTEXITCODE 并原样回传,
        由 Invoke-QtSignFile 决定 throw 与否。输出全量留着 —— 签名失败的原因常常只在 stderr 里。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $SignToolPath,
        [Parameter(Mandatory)][string[]] $Arguments
    )
    $out = & $SignToolPath @Arguments 2>&1
    $code = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = [int]$code
        Output   = (@($out) -join [Environment]::NewLine)
    }
}

function Set-QtFileSignature {
    <#
    .SYNOPSIS
        Set-AuthenticodeSignature 的唯一包装(给 .ps1 / .psm1 签名)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)] $Certificate,
        [string] $TimestampServer = ''
    )
    $splat = @{
        FilePath      = $Path
        Certificate   = $Certificate
        HashAlgorithm = 'SHA256'
        ErrorAction   = 'Stop'
    }
    if ($TimestampServer) { $splat['TimestampServer'] = $TimestampServer }
    return (Set-AuthenticodeSignature @splat)
}

function Get-QtFileSignature {
    <#
    .SYNOPSIS
        Get-AuthenticodeSignature 的唯一包装(签完立刻复核、G6 门复核都走它)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    return (Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop)
}

function Get-QtCertFromStore {
    <#
    .SYNOPSIS
        列出某个证书存储里的证书(默认 Cert:\CurrentUser\My)。
    #>
    [CmdletBinding()]
    param([string] $StorePath = 'Cert:\CurrentUser\My')
    return @(Get-ChildItem -Path $StorePath -ErrorAction Stop)
}

function New-QtCertInStore {
    <#
    .SYNOPSIS
        New-SelfSignedCertificate 的唯一包装。
    .NOTES
        🔴 `-KeyExportPolicy NonExportable`:私钥**不可导出**。整个模块里没有任何
        Export-PfxCertificate / -KeyExportPolicy Exportable 的调用 —— 密钥材料只存在于
        当前用户的证书存储里,永远不落盘、永远不进仓库。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Subject,
        [Parameter(Mandatory)][datetime] $NotAfter,
        [int] $KeyLength = 3072
    )
    return (New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $Subject `
            -KeyAlgorithm RSA `
            -KeyLength $KeyLength `
            -HashAlgorithm SHA256 `
            -KeyExportPolicy NonExportable `
            -KeyUsage DigitalSignature `
            -CertStoreLocation 'Cert:\CurrentUser\My' `
            -NotAfter $NotAfter `
            -ErrorAction Stop)
}

function Export-QtCertFile {
    <#
    .SYNOPSIS
        导出**公钥** .cer(DER)。只导公钥,绝不导 .pfx。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Certificate,
        [Parameter(Mandatory)][string] $Path
    )
    return (Export-Certificate -Cert $Certificate -FilePath $Path -Type CERT -Force -ErrorAction Stop)
}

function Import-QtCertFile {
    <#
    .SYNOPSIS
        把 .cer 导入某个存储(Root / TrustedPublisher)。需要管理员。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $StorePath
    )
    return (Import-Certificate -FilePath $Path -CertStoreLocation $StorePath -ErrorAction Stop)
}

function Remove-QtCertFromStore {
    <#
    .SYNOPSIS
        从某个存储里删掉一张证书(按指纹)。仅 Remove-QtCodeSigningCert.ps1 的反向清理用。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $StorePath,
        [Parameter(Mandatory)][string] $Thumbprint
    )
    $target = Join-Path $StorePath $Thumbprint
    Remove-Item -Path $target -Force -ErrorAction Stop
}

function Get-QtCertFileObject {
    <#
    .SYNOPSIS
        从 .cer 文件读出证书对象(导入前给人核对指纹与主题用)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    return (New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList $Path)
}

function Test-QtAdmin {
    <#
    .SYNOPSIS
        当前进程是否已提权。导入/删除 LocalMachine 存储必须提权。
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-QtNow2 {
    <#
    .SYNOPSIS
        时钟接缝(证书有效期判定用),测试可 Mock。
    #>
    [CmdletBinding()]
    [OutputType([datetime])]
    param()
    return (Get-Date)
}

#endregion

#region 纯逻辑:应签清单、文件分类、验证判定 ────────────────────────────────

function Get-QtSignSpecTarget {
    <#
    .SYNOPSIS
        docs/03 §2.2.3 逐条列出的**应签对象**。本清单是实现侧的唯一出处,
        `installer/tests/test_signing_consistency.py` 拿它跟文档逐条对账。
    .NOTES
        `Spec` 的字样**逐字抄自 §2.2.3**,改动等于改规格口径 —— 要么同步改文档,要么先出裁决。
    #>
    [CmdletBinding()]
    param()
    return @(
        [pscustomobject]@{ Key = 'shell_exe'; Spec = '外壳 EXE'; Kind = 'exe'
            Desc = 'QTrade-Setup-<ver>.exe;拼接完成后**最后**签(签名覆盖整个 EXE 含归档)' }
        [pscustomobject]@{ Key = 'engine_exe'; Spec = '引擎 EXE'; Kind = 'exe'
            Desc = 'qtrade-setup-engine.exe;ISCC 从**已签脚本副本**编译出来之后签' }
        [pscustomobject]@{ Key = 'winagent_svc'; Spec = 'qtrade-winagent-svc.exe'; Kind = 'exe'
            Desc = 'WinAgent 服务;签在预签副本上,**先于 manifest 算 sha256**' }
        [pscustomobject]@{ Key = 'winagent_user'; Spec = 'qtrade-winagent-user.exe'; Kind = 'exe'
            Desc = 'WinAgent 会话代理(R-14 实名);同上' }
        [pscustomobject]@{ Key = 'electron_main'; Spec = 'Electron 主程序'; Kind = 'exe'
            Desc = 'console\release\win-unpacked 顶层的主 exe;同上' }
        [pscustomobject]@{ Key = 'ps1_all'; Spec = 'ps1'; Kind = 'script'
            Desc = '引擎副本内**全部** .ps1/.psm1(§2.2.3 第 3 条:AllSigned 策略的企业机也能跑)' }
    )
}

function Get-QtNeverSignRule {
    <#
    .SYNOPSIS
        **一律不重签**的件(§2.2.3:adb / scrcpy 由其上游签名不动)。
    .NOTES
        🔴 重签第三方件有两个后果,都很硬:
           ①毁掉原厂签名链,上游可验证性没了;
           ②`pkg/wechat/weixin_4.1.12.26.exe` 的 sha256 是**钉死**的(R2-6),
             改一个字节就 `E_INSTALL_PAYLOAD_CORRUPT`。
        规则按**载荷内相对路径前缀**(正斜杠)写,与 manifest.files[].path 同一套写法。
    #>
    [CmdletBinding()]
    param()
    return @(
        [pscustomobject]@{ Prefix = 'pkg/adb/'; Why = 'platform-tools,Google 上游签名(§2.2.3:不动)' }
        [pscustomobject]@{ Prefix = 'pkg/scrcpy/'; Why = 'scrcpy,上游签名(§2.2.3:不动)' }
        [pscustomobject]@{ Prefix = 'pkg/chatlog/'; Why = 'chatlog / wx_key DLL,第三方件' }
        [pscustomobject]@{ Prefix = 'pkg/wechat/'; Why = '随包微信安装包,sha256 已钉死(R2-6),改一字节即 PAYLOAD_CORRUPT' }
        [pscustomobject]@{ Prefix = 'pkg/vcredist/'; Why = '微软 VC++ 运行库,微软签名' }
        [pscustomobject]@{ Prefix = 'wsl/'; Why = 'wsl.msi(微软签名)/ rootfs.tar / bzImage / wheel:非 PE 或第三方' }
        [pscustomobject]@{ Prefix = 'kernel/'; Why = '自编内核 bzImage,非 PE,不可 Authenticode' }
        [pscustomobject]@{ Prefix = 'winagent/python/'; Why = '嵌入式 Python 运行时,python.org 上游签名' }
        [pscustomobject]@{ Prefix = 'winagent/app/_internal/'; Why = 'PyInstaller 自带的 CPython / 依赖 DLL,上游签名' }
        [pscustomobject]@{ Prefix = 'console/locales/'; Why = 'Electron 资源' }
        [pscustomobject]@{ Prefix = 'console/resources/'; Why = 'Electron 资源(app.asar 等)' }
    )
}

function Test-QtNeverSign {
    <#
    .SYNOPSIS
        这条载荷内相对路径是不是「一律不重签」的件。
    .OUTPUTS
        $null = 可以签;否则回命中的规则对象(带 Why,报错/日志直接用)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $RelativePath)
    $rel = ($RelativePath -replace '\\', '/').TrimStart('/')
    foreach ($r in (Get-QtNeverSignRule)) {
        if ($rel.StartsWith($r.Prefix, [StringComparison]::OrdinalIgnoreCase)) { return $r }
    }
    # Electron 顶层的 .dll / .pak / .bin 等同样不签(只有顶层那个 .exe 是主程序)
    if ($rel -match '(?i)^console/[^/]+\.(dll|pak|dat|bin|json|html|txt)$') {
        return [pscustomobject]@{ Prefix = 'console/*.dll|pak|…'; Why = 'Electron 自带二进制/资源,上游签名' }
    }
    return $null
}

function Get-QtSignFileKind {
    <#
    .SYNOPSIS
        按扩展名决定用哪条签名路径。
    .OUTPUTS
        'exe'         -> signtool sign
        'script'      -> Set-AuthenticodeSignature
        'unsignable'  -> 不签(.cmd 不支持 Authenticode;其它非 PE 同理)
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $Path)
    switch ([IO.Path]::GetExtension($Path).ToLowerInvariant()) {
        '.exe' { return 'exe' }
        '.dll' { return 'exe' }
        '.sys' { return 'exe' }
        '.ps1' { return 'script' }
        '.psm1' { return 'script' }
        '.psd1' { return 'script' }
        default { return 'unsignable' }
    }
}

function Get-QtSignToolArgument {
    <#
    .SYNOPSIS
        拼 signtool 的参数(§2.2.3:SHA-256 + RFC 3161 时间戳)。拼参数只写这一处。
    .NOTES
        `/sha1 <指纹>` 按**指纹**选证书,而不是 `/a`(自动挑)—— 机器上可能不止一张
        代码签名证书,`/a` 挑中哪张全看运气,而 G6 门要求签名者指纹**逐字等于**传入的指纹。
        `-NoTimestamp` 时不带 /tr:🔴 没有时间戳,**证书一过期,已发出去的包签名当场失效**。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Thumbprint,
        [Parameter(Mandatory)][string] $FilePath,
        [string] $TimestampUrl = ''
    )
    $a = @('sign', '/fd', 'sha256', '/sha1', $Thumbprint)
    if ($TimestampUrl) { $a += @('/tr', $TimestampUrl, '/td', 'sha256') }
    $a += @('/v', $FilePath)
    return $a
}

function Test-QtSignatureVerdict {
    <#
    .SYNOPSIS
        把 Get-AuthenticodeSignature 的结果翻译成**三态**判定。
    .NOTES
        🔴 这是整套签名里最容易搞错的一条:**自签名证书在没导入信任库的机器上,
        Get-AuthenticodeSignature 回的不是 `Valid`,而是 `UnknownError` / `NotTrusted`**
        (链终止于一个不受信任的根)。如果照直写 `Status -eq 'Valid'` 当判据,
        打包机上每一个文件都会"验证失败",然后就有人来把验证整个关掉 —— 那才是真事故。
        所以这里分三态:
          Ok=$true,  Trusted=$true   签名完好且发布者受信任(证书已导入,或换成 CA 证书之后)
          Ok=$true,  Trusted=$false  **签名完好但发布者未受信任** —— 自签名阶段的正常态,可接受,
                                     提示去跑 Import-QtCodeSigningCert.ps1
          Ok=$false                  哈希不符 / 根本没签 / 格式不支持 —— 真失败
        另外带指纹核对:`-ExpectedThumbprint` 给了就必须逐字相等(不区分大小写),
        否则 Ok=$false —— 防的是"签是签了,但签成了另一张证书"。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowNull()] $Signature,
        [string] $ExpectedThumbprint = ''
    )
    if ($null -eq $Signature) {
        return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = 'NoResult'
            Thumbprint = ''; Reason = '拿不到签名信息(Get-AuthenticodeSignature 回了 $null)' }
    }
    $status = [string]$Signature.Status
    $thumb = ''
    if ($Signature.PSObject.Properties.Name -contains 'SignerCertificate' -and $Signature.SignerCertificate) {
        $thumb = [string]$Signature.SignerCertificate.Thumbprint
    }

    # ① 明确的失败态:没签 / 哈希不符 / 格式不支持
    if ($status -eq 'NotSigned') {
        return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = $status; Thumbprint = $thumb
            Reason = '文件没有签名' }
    }
    if ($status -eq 'HashMismatch') {
        return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = $status; Thumbprint = $thumb
            Reason = '签名与文件内容不符(哈希不匹配)—— 文件在签名之后被改过' }
    }
    if ($status -eq 'NotSupportedFileFormat' -or $status -eq 'Incompatible') {
        return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = $status; Thumbprint = $thumb
            Reason = '这个文件格式不支持 Authenticode' }
    }
    # ② 没有签名者证书 = 签名压根没写进去,不管 Status 说什么都算失败
    if (-not $thumb) {
        return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = $status; Thumbprint = ''
            Reason = ('签名里没有签名者证书(Status={0})' -f $status) }
    }
    # ③ 指纹核对(给了就必须对得上)
    if ($ExpectedThumbprint -and ($thumb -ne $ExpectedThumbprint.Replace(' ', '').ToUpperInvariant())) {
        return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = $status; Thumbprint = $thumb
            Reason = ('签名者指纹不符:期望 {0},实得 {1}' -f $ExpectedThumbprint, $thumb) }
    }
    # ④ 通过:Valid = 受信任;UnknownError / NotTrusted = 签名完好但发布者未受信任(自签名常态)
    if ($status -eq 'Valid') {
        return [pscustomobject]@{ Ok = $true; Trusted = $true; Status = $status; Thumbprint = $thumb
            Reason = '签名有效且发布者受信任' }
    }
    if ($status -eq 'UnknownError' -or $status -eq 'NotTrusted') {
        return [pscustomobject]@{ Ok = $true; Trusted = $false; Status = $status; Thumbprint = $thumb
            Reason = '签名完好,但**本机未信任该发布者**(自签名阶段的正常态);目标机跑 installer\signing\导入QTrade签名证书.cmd 即可' }
    }
    return [pscustomobject]@{ Ok = $false; Trusted = $false; Status = $status; Thumbprint = $thumb
        Reason = ('未知的签名状态:{0}' -f $status) }
}

function Get-QtSignPlan {
    <#
    .SYNOPSIS
        给定一个**副本目录**,算出「该签哪些文件」。纯目录枚举,不碰签名。
    .PARAMETER Scope
        winagent —— 只签**顶层**的 qtrade-winagent-*.exe;`_internal\` 下一个都不签
                    (那是 PyInstaller 打进来的 CPython 与依赖 DLL,上游件)
        console  —— 只签**顶层**的 .exe(Electron 主程序);dll / pak / locales / resources 一律不签
        engine   —— 副本里**全部** .ps1 / .psm1(§2.2.3 第 3 条);.cmd 不支持 Authenticode,不签
    .OUTPUTS
        文件全路径数组(找不到就回空数组,由调用方决定是不是缺件)
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [Parameter(Mandatory)][ValidateSet('winagent', 'console', 'engine')][string] $Scope
    )
    if (-not (Test-Path -LiteralPath $Root)) { return @() }
    switch ($Scope) {
        'winagent' {
            return @(Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -ieq '.exe' -and $_.Name -like 'qtrade-winagent-*' } |
                ForEach-Object { $_.FullName } | Sort-Object)
        }
        'console' {
            return @(Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -ieq '.exe' } |
                ForEach-Object { $_.FullName } | Sort-Object)
        }
        default {
            return @(Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -in '.ps1', '.psm1', '.psd1' } |
                ForEach-Object { $_.FullName } | Sort-Object)
        }
    }
}

#endregion

#region 编排:签名 ──────────────────────────────────────────────────────────

function Invoke-QtSignFile {
    <#
    .SYNOPSIS
        签**一个**文件并立刻复核。失败即 throw,错误信息带文件名。
    .PARAMETER NoTimestamp
        离线环境用。🔴 不带时间戳的签名在**证书过期那天全部失效**(已发出去的包也一样)。
    .OUTPUTS
        {Path, Kind, Verdict, Trusted, Skipped}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Thumbprint,
        [Parameter(Mandatory)] $Certificate,
        [string] $SignToolPath = '',
        [string] $TimestampUrl = '',
        [switch] $NoTimestamp
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw ('要签的文件不存在:{0}' -f $Path)
    }
    $kind = Get-QtSignFileKind -Path $Path
    if ($kind -eq 'unsignable') {
        # .cmd 走到这里只可能是调用方算错了清单,明确报出来而不是默默跳过
        throw ('{0}:这个文件格式不支持 Authenticode(.cmd 等),不该出现在应签清单里' -f (Split-Path -Leaf $Path))
    }
    $ts = ''
    if (-not $NoTimestamp) { $ts = $TimestampUrl }

    if ($kind -eq 'exe') {
        if (-not $SignToolPath) {
            throw ('{0}:要给 PE 文件签名但没有 signtool.exe(装 Windows SDK,或用 -SignToolPath 指定)' -f (Split-Path -Leaf $Path))
        }
        $args0 = Get-QtSignToolArgument -Thumbprint $Thumbprint -FilePath $Path -TimestampUrl $ts
        $r = Invoke-QtSignToolRaw -SignToolPath $SignToolPath -Arguments $args0
        if ($r.ExitCode -ne 0) {
            throw ('{0}:signtool 签名失败(退出码 {1}){2}{3}' -f (Split-Path -Leaf $Path), $r.ExitCode, [Environment]::NewLine, $r.Output)
        }
    }
    else {
        $sr = Set-QtFileSignature -Path $Path -Certificate $Certificate -TimestampServer $ts
        # Set-AuthenticodeSignature 自己就回一个 Signature 对象,状态不对时下面的复核会拦住
        if ($null -eq $sr) {
            throw ('{0}:Set-AuthenticodeSignature 没有回结果' -f (Split-Path -Leaf $Path))
        }
    }

    # 🔴 签完**立刻**复核 —— 不信任 signtool 的退出码,以文件实际状态为准
    $v = Test-QtSignatureVerdict -Signature (Get-QtFileSignature -Path $Path) -ExpectedThumbprint $Thumbprint
    if (-not $v.Ok) {
        throw ('{0}:签名后复核不通过 —— {1}' -f (Split-Path -Leaf $Path), $v.Reason)
    }
    return [pscustomobject]@{
        Path    = $Path
        Kind    = $kind
        Verdict = $v
        Trusted = $v.Trusted
        Skipped = $false
    }
}

function Invoke-QtSignFiles {
    <#
    .SYNOPSIS
        签**一批**文件。任何一个失败即 throw(不吞异常、不继续)。
    .OUTPUTS
        每个文件一条 Invoke-QtSignFile 的结果。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Path,
        [Parameter(Mandatory)][string] $Thumbprint,
        [Parameter(Mandatory)] $Certificate,
        [string] $SignToolPath = '',
        [string] $TimestampUrl = '',
        [switch] $NoTimestamp
    )
    $results = @()
    foreach ($p in $Path) {
        $results += (Invoke-QtSignFile -Path $p -Thumbprint $Thumbprint -Certificate $Certificate `
                -SignToolPath $SignToolPath -TimestampUrl $TimestampUrl -NoTimestamp:$NoTimestamp)
    }
    return $results
}

function Get-QtSigningCertByThumbprint {
    <#
    .SYNOPSIS
        按指纹从 Cert:\CurrentUser\My 取签名用证书(ps1 签名需要证书对象本身)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Thumbprint,
        [string] $StorePath = 'Cert:\CurrentUser\My'
    )
    $want = $Thumbprint.Replace(' ', '').ToUpperInvariant()
    $hit = @(Get-QtCertFromStore -StorePath $StorePath | Where-Object { $_.Thumbprint -eq $want })
    if ($hit.Count -eq 0) {
        throw ('在 {0} 里找不到指纹 {1} 的证书 —— 先跑 installer\signing\New-QtSelfSignedCert.ps1 生成,或确认指纹抄对了' -f $StorePath, $Thumbprint)
    }
    return $hit[0]
}

function Write-QtNoTimestampWarning {
    <#
    .SYNOPSIS
        -NoTimestamp 的醒目警告。单独成函数,便于测试断言"确实告警了"。
    #>
    [CmdletBinding()]
    param()
    Write-Warning '🔴 本次签名**不带 RFC 3161 时间戳**(-NoTimestamp)。'
    Write-Warning '   后果:证书一旦过期,**已经发出去的包签名当场失效**(带时间戳的签名则在证书有效期内永久有效)。'
    Write-Warning '   只在确实连不上时间戳服务时这么做;联网后应重新签一遍。'
}

#endregion

#region 编排:证书生成 / 导入 / 清理 ─────────────────────────────────────────

function Get-QtCertSubject {
    <#
    .SYNOPSIS
        拼证书主题:`CN=QTrade Internal Code Signing, O=<组织>`。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string] $Organization = '')
    if ($Organization) { return ('CN={0}, O={1}' -f $script:QtCertSubjectCn, $Organization) }
    return ('CN={0}' -f $script:QtCertSubjectCn)
}

function Find-QtExistingCert {
    <#
    .SYNOPSIS
        幂等的依据:Cert:\CurrentUser\My 里**同 Subject 且未过期**的代码签名证书。
    .NOTES
        过期的不算(于是过期后重跑会生成新的),多张时取有效期最晚的那张。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Subject,
        [string] $StorePath = 'Cert:\CurrentUser\My'
    )
    $now = Get-QtNow2
    $hit = @(Get-QtCertFromStore -StorePath $StorePath | Where-Object {
            $_.Subject -eq $Subject -and $_.NotAfter -gt $now -and $_.NotBefore -le $now
        })
    if ($hit.Count -eq 0) { return $null }
    return (@($hit | Sort-Object NotAfter -Descending)[0])
}

function New-QtCodeSigningCertCore {
    <#
    .SYNOPSIS
        生成(或复用)自签名代码签名证书,并导出**公钥** .cer。幂等。
    .NOTES
        🔴 私钥 `NonExportable`、只存 `Cert:\CurrentUser\My`;本函数**只导出 .cer(公钥)**,
        没有任何路径会产出 .pfx —— 密钥材料永远不落盘、不进仓库。
    .OUTPUTS
        {Certificate, Thumbprint, Subject, CerPath, Reused, WhatIf}
    #>
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [string] $Organization = '',
        [int] $ValidYears = 3,
        [string] $CertDir = '',
        [string] $StorePath = 'Cert:\CurrentUser\My'
    )
    if (-not $CertDir) { $CertDir = $script:QtDefaultCertDir }
    $subject = Get-QtCertSubject -Organization $Organization
    $existing = Find-QtExistingCert -Subject $subject -StorePath $StorePath

    if ($existing) {
        # 幂等:同 Subject 且未过期 ⇒ 复用,不重复生成
        $cerPath = Join-Path $CertDir ('QTrade-CodeSigning-{0}.cer' -f $existing.Thumbprint)
        $result = [pscustomobject]@{
            Certificate = $existing; Thumbprint = $existing.Thumbprint; Subject = $subject
            CerPath     = $cerPath; Reused = $true; WhatIf = $false
        }
        if ($PSCmdlet.ShouldProcess($cerPath, '导出已有证书的公钥 .cer(证书本身复用,不重新生成)')) {
            if (-not (Test-Path -LiteralPath $CertDir)) { New-Item -ItemType Directory -Path $CertDir -Force | Out-Null }
            Export-QtCertFile -Certificate $existing -Path $cerPath | Out-Null
        }
        else { $result.WhatIf = $true }
        return $result
    }

    $notAfter = (Get-QtNow2).AddYears($ValidYears)
    if (-not $PSCmdlet.ShouldProcess($subject, ('生成自签名代码签名证书(RSA 3072 / SHA256 / 私钥不可导出 / 有效期至 {0:yyyy-MM-dd})' -f $notAfter))) {
        return [pscustomobject]@{
            Certificate = $null; Thumbprint = ''; Subject = $subject
            CerPath     = (Join-Path $CertDir 'QTrade-CodeSigning-<指纹>.cer'); Reused = $false; WhatIf = $true
        }
    }
    $cert = New-QtCertInStore -Subject $subject -NotAfter $notAfter -KeyLength 3072
    $cerPath = Join-Path $CertDir ('QTrade-CodeSigning-{0}.cer' -f $cert.Thumbprint)
    if (-not (Test-Path -LiteralPath $CertDir)) { New-Item -ItemType Directory -Path $CertDir -Force | Out-Null }
    Export-QtCertFile -Certificate $cert -Path $cerPath | Out-Null
    return [pscustomobject]@{
        Certificate = $cert; Thumbprint = $cert.Thumbprint; Subject = $subject
        CerPath     = $cerPath; Reused = $false; WhatIf = $false
    }
}

function Get-QtImportStorePath {
    <#
    .SYNOPSIS
        目标机要导入的两个存储。
    .NOTES
        Root            —— 让链能验到一个受信任的根(否则 Authenticode 恒 UnknownError)
        TrustedPublisher—— 让 AllSigned / AppLocker 发布者规则认这个发布者
    #>
    [CmdletBinding()]
    param()
    return @(
        [pscustomobject]@{ Path = 'Cert:\LocalMachine\Root'; Why = '受信任的根证书颁发机构:自签名证书自己就是根,不导这里签名链验不过' }
        [pscustomobject]@{ Path = 'Cert:\LocalMachine\TrustedPublisher'; Why = '受信任的发布者:AllSigned 执行策略与 AppLocker 发布者规则认它' }
    )
}

function Import-QtCodeSigningCertCore {
    <#
    .SYNOPSIS
        把 .cer 导入 LocalMachine\Root 与 LocalMachine\TrustedPublisher。幂等 + 指纹核对。
    .NOTES
        🔴 导入前**必须核对指纹与主题**:导进 Root 等于告诉这台机器「这张证书签什么都可信」,
        被人掉包一张 .cer 就是一条后门。`-ExpectedThumbprint` 不符即拒绝、不做任何导入。
    .OUTPUTS
        {Thumbprint, Subject, Imported[], AlreadyThere[], WhatIf}
    #>
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory)][string] $CerPath,
        [string] $ExpectedThumbprint = ''
    )
    if (-not (Test-Path -LiteralPath $CerPath)) {
        throw ('找不到证书文件:{0}' -f $CerPath)
    }
    $cert = Get-QtCertFileObject -Path $CerPath
    $thumb = [string]$cert.Thumbprint

    if ($ExpectedThumbprint) {
        $want = $ExpectedThumbprint.Replace(' ', '').ToUpperInvariant()
        if ($thumb.ToUpperInvariant() -ne $want) {
            throw ('🔴 证书指纹不符,拒绝导入!{0}   期望:{1}{2}   实际:{3}{4}   这张 .cer 可能被人换过 —— 别导,先找安琳核对。' -f
                [Environment]::NewLine, $want, [Environment]::NewLine, $thumb, [Environment]::NewLine)
        }
    }

    $imported = @()
    $already = @()
    $whatIf = $false
    foreach ($s in (Get-QtImportStorePath)) {
        $have = @(Get-QtCertFromStore -StorePath $s.Path | Where-Object { $_.Thumbprint -eq $thumb })
        if ($have.Count -gt 0) { $already += $s.Path; continue }   # 幂等
        if ($PSCmdlet.ShouldProcess($s.Path, ('导入证书 {0}' -f $thumb))) {
            Import-QtCertFile -Path $CerPath -StorePath $s.Path | Out-Null
            $imported += $s.Path
        }
        else { $whatIf = $true }
    }
    return [pscustomobject]@{
        Thumbprint = $thumb; Subject = [string]$cert.Subject
        Imported   = $imported; AlreadyThere = $already; WhatIf = $whatIf
    }
}

function Remove-QtCodeSigningCertCore {
    <#
    .SYNOPSIS
        反向清理:把指纹对应的证书从 LocalMachine\Root 与 TrustedPublisher 里删掉。幂等。
    .OUTPUTS
        {Thumbprint, Removed[], NotFound[], WhatIf}
    #>
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][string] $Thumbprint)
    $thumb = $Thumbprint.Replace(' ', '').ToUpperInvariant()
    $removed = @()
    $notFound = @()
    $whatIf = $false
    foreach ($s in (Get-QtImportStorePath)) {
        $have = @(Get-QtCertFromStore -StorePath $s.Path | Where-Object { $_.Thumbprint -eq $thumb })
        if ($have.Count -eq 0) { $notFound += $s.Path; continue }
        if ($PSCmdlet.ShouldProcess($s.Path, ('删除证书 {0}' -f $thumb))) {
            Remove-QtCertFromStore -StorePath $s.Path -Thumbprint $thumb
            $removed += $s.Path
        }
        else { $whatIf = $true }
    }
    return [pscustomobject]@{ Thumbprint = $thumb; Removed = $removed; NotFound = $notFound; WhatIf = $whatIf }
}

#endregion

Export-ModuleMember -Function *-Qt*
