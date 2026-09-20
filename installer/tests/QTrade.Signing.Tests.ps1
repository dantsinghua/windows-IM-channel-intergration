# Pester 5 —— 代码签名(自签名阶段)
# 规格:docs/03 §2.2.3(应签对象 / SHA-256 / RFC 3161 时间戳 / adb·scrcpy 由上游签名不动)、
#       §2.2.2(签名覆盖整个 EXE 含归档 ⇒ 改载荷必须重签)、§2.6.7 W1(签名后 BOM 仍在)。
#
# 🔴 全程 **Mock 到底**:不生成证书、不碰任何证书存储、不给任何文件真签名、不跑 signtool。
#    外部世界的每个出口都在 QTrade.Signing.psm1 的「接缝」region 里,这组用例把它们整组挡住。
#    (同 engine/modules/QTrade.Native.psm1 的规矩:禁区 = 开发机绝不真装、真签、真改证书库)
BeforeAll {
    $script:InstallerRoot = Split-Path -Parent $PSScriptRoot
    $script:SigningDir = Join-Path $script:InstallerRoot 'signing'
    $script:BuildPs1 = Join-Path $script:InstallerRoot 'build\build.ps1'
    $script:CollectPs1 = Join-Path $script:InstallerRoot 'build\collect-payload.ps1'
    Import-Module (Join-Path $script:SigningDir 'QTrade.Signing.psm1') -Force -DisableNameChecking

    $script:BuildText = [IO.File]::ReadAllText($script:BuildPs1)
    $script:BuildLines = [IO.File]::ReadAllLines($script:BuildPs1)

    # build.ps1 里某个字样第一次出现在第几行(1 基)。找不到回 -1。
    function Get-QtLineNo {
        param([Parameter(Mandatory)][string] $Pattern)
        for ($i = 0; $i -lt $script:BuildLines.Count; $i++) {
            if ($script:BuildLines[$i] -match $Pattern) { return ($i + 1) }
        }
        return -1
    }

    function Get-QtAstOf {
        param([Parameter(Mandatory)][string] $Path)
        $err = $null; $tok = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tok, [ref]$err)
        if ($err -and $err.Count -gt 0) { throw ($Path + ' 解析失败:' + ($err[0].Message)) }
        return $ast
    }

    # 造一张"假证书",只带判定要用到的属性
    function New-QtFakeCert {
        param([string] $Thumbprint = 'AABBCCDDEEFF00112233445566778899AABBCCDD',
            [string] $Subject = 'CN=QTrade Internal Code Signing, O=QTrade',
            [datetime] $NotAfter = ([datetime]'2030-01-01'),
            [datetime] $NotBefore = ([datetime]'2020-01-01'))
        return [pscustomobject]@{
            Thumbprint = $Thumbprint; Subject = $Subject
            NotAfter   = $NotAfter; NotBefore = $NotBefore
            Issuer     = $Subject
        }
    }

    function New-QtFakeSignature {
        param([string] $Status, $Cert = $null)
        return [pscustomobject]@{ Status = $Status; SignerCertificate = $Cert; StatusMessage = $Status }
    }
}

# ============================================================================
Describe '证书生成:幂等 + 私钥不可导出(自签名阶段)' {

    It '同 Subject 且未过期的证书已存在 → 复用,**不重复生成**' {
        $existing = New-QtFakeCert
        Mock -ModuleName QTrade.Signing Get-QtNow2 { [datetime]'2026-09-21' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @($existing) }
        Mock -ModuleName QTrade.Signing New-QtCertInStore { throw '不该被调用:已有同主题未过期的证书' }
        Mock -ModuleName QTrade.Signing Export-QtCertFile { }

        $r = New-QtCodeSigningCertCore -Organization 'QTrade' -CertDir (Join-Path $TestDrive 'certs')
        $r.Reused | Should -BeTrue
        $r.Thumbprint | Should -Be $existing.Thumbprint
        Should -Invoke -ModuleName QTrade.Signing New-QtCertInStore -Times 0 -Exactly
    }

    It '已有证书**已过期** → 不复用,重新生成(过期证书签出来的包没人认)' {
        $expired = New-QtFakeCert -NotAfter ([datetime]'2020-06-01')
        $fresh = New-QtFakeCert -Thumbprint '1111222233334444555566667777888899990000'
        Mock -ModuleName QTrade.Signing Get-QtNow2 { [datetime]'2026-09-21' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @($expired) }
        Mock -ModuleName QTrade.Signing New-QtCertInStore { $fresh }
        Mock -ModuleName QTrade.Signing Export-QtCertFile { }

        $r = New-QtCodeSigningCertCore -Organization 'QTrade' -CertDir (Join-Path $TestDrive 'certs')
        $r.Reused | Should -BeFalse
        $r.Thumbprint | Should -Be $fresh.Thumbprint
        Should -Invoke -ModuleName QTrade.Signing New-QtCertInStore -Times 1 -Exactly
    }

    It '存储里一张都没有 → 生成,.cer 文件名带指纹' {
        $fresh = New-QtFakeCert
        Mock -ModuleName QTrade.Signing Get-QtNow2 { [datetime]'2026-09-21' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @() }
        Mock -ModuleName QTrade.Signing New-QtCertInStore { $fresh }
        Mock -ModuleName QTrade.Signing Export-QtCertFile { }

        $r = New-QtCodeSigningCertCore -Organization 'QTrade' -ValidYears 3 -CertDir (Join-Path $TestDrive 'certs')
        $r.Reused | Should -BeFalse
        $r.CerPath | Should -Match ([regex]::Escape($fresh.Thumbprint))
    }

    It '-WhatIf 干跑:一个证书都不生成、一个文件都不导出' {
        Mock -ModuleName QTrade.Signing Get-QtNow2 { [datetime]'2026-09-21' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @() }
        Mock -ModuleName QTrade.Signing New-QtCertInStore { throw '不该被调用:-WhatIf' }
        Mock -ModuleName QTrade.Signing Export-QtCertFile { throw '不该被调用:-WhatIf' }

        $r = New-QtCodeSigningCertCore -Organization 'QTrade' -CertDir (Join-Path $TestDrive 'certs') -WhatIf
        $r.WhatIf | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Signing New-QtCertInStore -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Signing Export-QtCertFile -Times 0 -Exactly
    }

    It '🔴 私钥不可导出:模块里零处 Export-PfxCertificate / Exportable / .pfx' {
        # 一旦有人为了"方便 CI"加一句 Export-PfxCertificate,密钥材料就会落盘、
        # 然后极可能被顺手 commit 进仓库。这条用例就是那道闸。
        $src = [IO.File]::ReadAllText((Join-Path $script:SigningDir 'QTrade.Signing.psm1'))
        # 先剥 <# #> 块注释,再剥单行注释 —— 模块的文档注释里**点名**了这些禁忌写法,
        # 不剥的话这条用例会被自己的说明文字绊倒(build.ps1 的 G3/G4 门也是这么做的)。
        $noBlock = [regex]::Replace($src, '<#.*?#>', '', 'Singleline')
        $code = ($noBlock -split "`r?`n" | Where-Object { -not $_.TrimStart().StartsWith('#') }) -join "`n"
        $code | Should -Not -Match 'Export-PfxCertificate'
        $code | Should -Not -Match 'KeyExportPolicy\s+Exportable'
        $code | Should -Not -Match '\.pfx'
        $code | Should -Match 'KeyExportPolicy\s+NonExportable'
    }

    It '证书主题:CN 固定,O 可传参' {
        Get-QtCertSubject -Organization 'QTrade' | Should -Be 'CN=QTrade Internal Code Signing, O=QTrade'
        Get-QtCertSubject | Should -Be 'CN=QTrade Internal Code Signing'
    }

    It 'New-QtCertInStore 用的是 RSA 3072 / SHA256 / CodeSigningCert / CurrentUser\My' {
        $src = [IO.File]::ReadAllText((Join-Path $script:SigningDir 'QTrade.Signing.psm1'))
        $src | Should -Match '-Type\s+CodeSigningCert'
        $src | Should -Match '-HashAlgorithm\s+SHA256'
        $src | Should -Match '\$KeyLength\s*=\s*3072'
        $src | Should -Match 'Cert:\\CurrentUser\\My'
    }
}

# ============================================================================
Describe '🔴 验证逻辑:自签名证书在未导入信任库的机器上不是 Valid' {

    It 'Valid → 通过且受信任' {
        $v = Test-QtSignatureVerdict -Signature (New-QtFakeSignature -Status 'Valid' -Cert (New-QtFakeCert))
        $v.Ok | Should -BeTrue
        $v.Trusted | Should -BeTrue
    }

    It 'UnknownError / NotTrusted 且有签名者证书 → **可接受**(签名完好,只是发布者未受信任)' -ForEach @(
        @{ S = 'UnknownError' }
        @{ S = 'NotTrusted' }
    ) {
        # 这是自签名阶段打包机上的**正常态**。照直写 Status -eq 'Valid' 当判据的话,
        # 每个文件都会"验证失败",接着就会有人把验证整个关掉 —— 那才是真事故。
        $v = Test-QtSignatureVerdict -Signature (New-QtFakeSignature -Status $S -Cert (New-QtFakeCert))
        $v.Ok | Should -BeTrue
        $v.Trusted | Should -BeFalse
        $v.Reason | Should -Match '未信任'
    }

    It 'HashMismatch → 失败(文件在签名之后被改过)' {
        $v = Test-QtSignatureVerdict -Signature (New-QtFakeSignature -Status 'HashMismatch' -Cert (New-QtFakeCert))
        $v.Ok | Should -BeFalse
        $v.Reason | Should -Match '哈希'
    }

    It 'NotSigned → 失败' {
        $v = Test-QtSignatureVerdict -Signature (New-QtFakeSignature -Status 'NotSigned')
        $v.Ok | Should -BeFalse
        $v.Reason | Should -Match '没有签名'
    }

    It 'NotSupportedFileFormat → 失败' {
        (Test-QtSignatureVerdict -Signature (New-QtFakeSignature -Status 'NotSupportedFileFormat')).Ok | Should -BeFalse
    }

    It 'UnknownError 但**没有签名者证书** → 失败(签名压根没写进去)' {
        (Test-QtSignatureVerdict -Signature (New-QtFakeSignature -Status 'UnknownError' -Cert $null)).Ok | Should -BeFalse
    }

    It '$null → 失败,不当成通过' {
        (Test-QtSignatureVerdict -Signature $null).Ok | Should -BeFalse
    }

    It '指纹不符 → 失败(签是签了,但签成了另一张证书)' {
        $sig = New-QtFakeSignature -Status 'Valid' -Cert (New-QtFakeCert -Thumbprint 'AAAA1111')
        $v = Test-QtSignatureVerdict -Signature $sig -ExpectedThumbprint 'BBBB2222'
        $v.Ok | Should -BeFalse
        $v.Reason | Should -Match '指纹不符'
    }

    It '指纹相符(带空格 / 小写也认)→ 通过' {
        $sig = New-QtFakeSignature -Status 'Valid' -Cert (New-QtFakeCert -Thumbprint 'AABB1122')
        (Test-QtSignatureVerdict -Signature $sig -ExpectedThumbprint 'aa bb 11 22').Ok | Should -BeTrue
    }
}

# ============================================================================
Describe 'signtool 参数(§2.2.3:SHA-256 + RFC 3161)与 -NoTimestamp 告警' {

    It '默认带 /fd sha256、/sha1 指纹、/tr 时间戳 URL、/td sha256' {
        $a = Get-QtSignToolArgument -Thumbprint 'ABC123' -FilePath 'X:\a.exe' -TimestampUrl 'http://ts.example/'
        ($a -join ' ') | Should -Match '^sign /fd sha256 /sha1 ABC123 /tr http://ts\.example/ /td sha256'
        $a | Should -Contain 'X:\a.exe'
    }

    It '🔴 用 /sha1 指纹选证书,**不用 /a** —— 机器上不止一张证书时 /a 挑中哪张全看运气' {
        $a = Get-QtSignToolArgument -Thumbprint 'ABC123' -FilePath 'X:\a.exe'
        $a | Should -Not -Contain '/a'
        $a | Should -Contain '/sha1'
    }

    It '不带时间戳 URL → 不带 /tr /td' {
        $a = Get-QtSignToolArgument -Thumbprint 'ABC123' -FilePath 'X:\a.exe' -TimestampUrl ''
        $a | Should -Not -Contain '/tr'
        $a | Should -Not -Contain '/td'
    }

    It '-NoTimestamp 必须打醒目警告:证书过期后签名即失效' {
        $warnings = @()
        Write-QtNoTimestampWarning -WarningVariable warnings -WarningAction SilentlyContinue
        ($warnings -join ' ') | Should -Match '时间戳'
        ($warnings -join ' ') | Should -Match '过期'
        ($warnings -join ' ') | Should -Match '失效'
    }

    It 'Invoke-QtSignFile 带 -NoTimestamp 时,传给 signtool 的参数里没有 /tr' {
        $script:CapturedArgs = $null
        Mock -ModuleName QTrade.Signing Invoke-QtSignToolRaw {
            $script:CapturedArgs = $Arguments
            [pscustomobject]@{ ExitCode = 0; Output = '' }
        }
        Mock -ModuleName QTrade.Signing Get-QtFileSignature {
            [pscustomobject]@{ Status = 'UnknownError'; SignerCertificate = [pscustomobject]@{ Thumbprint = 'ABC123' } }
        }
        $f = Join-Path $TestDrive 'a.exe'
        Set-Content -LiteralPath $f -Value 'x'
        $r = Invoke-QtSignFile -Path $f -Thumbprint 'ABC123' -Certificate (New-QtFakeCert) `
            -SignToolPath 'X:\signtool.exe' -TimestampUrl 'http://ts.example/' -NoTimestamp
        $r.Verdict.Ok | Should -BeTrue
        $script:CapturedArgs | Should -Not -Contain '/tr'
    }
}

# ============================================================================
Describe '签名执行:失败即 throw,错误信息带文件名' {

    BeforeEach {
        $script:SigFile = Join-Path $TestDrive 'engine.exe'
        Set-Content -LiteralPath $script:SigFile -Value 'x'
    }

    It 'signtool 退出码非 0 → throw,消息里有文件名' {
        Mock -ModuleName QTrade.Signing Invoke-QtSignToolRaw { [pscustomobject]@{ ExitCode = 1; Output = 'SignTool Error' } }
        Mock -ModuleName QTrade.Signing Get-QtFileSignature { throw '不该走到复核' }
        { Invoke-QtSignFile -Path $script:SigFile -Thumbprint 'ABC' -Certificate (New-QtFakeCert) -SignToolPath 'X:\signtool.exe' } |
            Should -Throw -ExpectedMessage '*engine.exe*'
    }

    It '签完复核发现哈希不符 → throw(不信 signtool 的退出码,以文件实际状态为准)' {
        Mock -ModuleName QTrade.Signing Invoke-QtSignToolRaw { [pscustomobject]@{ ExitCode = 0; Output = '' } }
        Mock -ModuleName QTrade.Signing Get-QtFileSignature {
            [pscustomobject]@{ Status = 'HashMismatch'; SignerCertificate = [pscustomobject]@{ Thumbprint = 'ABC' } }
        }
        { Invoke-QtSignFile -Path $script:SigFile -Thumbprint 'ABC' -Certificate (New-QtFakeCert) -SignToolPath 'X:\signtool.exe' } |
            Should -Throw -ExpectedMessage '*engine.exe*'
    }

    It '签出来的是**别的证书** → throw(指纹不符)' {
        Mock -ModuleName QTrade.Signing Invoke-QtSignToolRaw { [pscustomobject]@{ ExitCode = 0; Output = '' } }
        Mock -ModuleName QTrade.Signing Get-QtFileSignature {
            [pscustomobject]@{ Status = 'Valid'; SignerCertificate = [pscustomobject]@{ Thumbprint = 'OTHER' } }
        }
        { Invoke-QtSignFile -Path $script:SigFile -Thumbprint 'ABC' -Certificate (New-QtFakeCert) -SignToolPath 'X:\signtool.exe' } |
            Should -Throw -ExpectedMessage '*指纹不符*'
    }

    It '文件不存在 → throw' {
        { Invoke-QtSignFile -Path (Join-Path $TestDrive 'nope.exe') -Thumbprint 'ABC' -Certificate (New-QtFakeCert) -SignToolPath 'X:\st.exe' } |
            Should -Throw -ExpectedMessage '*不存在*'
    }

    It '.cmd 不支持 Authenticode → throw,而不是默默跳过' {
        $c = Join-Path $TestDrive 'run-engine.cmd'
        Set-Content -LiteralPath $c -Value 'x'
        { Invoke-QtSignFile -Path $c -Thumbprint 'ABC' -Certificate (New-QtFakeCert) -SignToolPath 'X:\st.exe' } |
            Should -Throw -ExpectedMessage '*Authenticode*'
    }

    It 'ps1 走 Set-AuthenticodeSignature(不是 signtool)' {
        Mock -ModuleName QTrade.Signing Set-QtFileSignature { [pscustomobject]@{ Status = 'UnknownError' } }
        Mock -ModuleName QTrade.Signing Invoke-QtSignToolRaw { throw 'ps1 不该走 signtool' }
        Mock -ModuleName QTrade.Signing Get-QtFileSignature {
            [pscustomobject]@{ Status = 'UnknownError'; SignerCertificate = [pscustomobject]@{ Thumbprint = 'ABC' } }
        }
        $f = Join-Path $TestDrive 'run-step.ps1'
        Set-Content -LiteralPath $f -Value 'x'
        (Invoke-QtSignFile -Path $f -Thumbprint 'ABC' -Certificate (New-QtFakeCert)).Kind | Should -Be 'script'
        Should -Invoke -ModuleName QTrade.Signing Set-QtFileSignature -Times 1 -Exactly
    }

    It '文件类型判定:exe/dll -> exe;ps1/psm1/psd1 -> script;cmd/tar/json -> unsignable' -ForEach @(
        @{ P = 'a.exe'; K = 'exe' }
        @{ P = 'a.dll'; K = 'exe' }
        @{ P = 'a.ps1'; K = 'script' }
        @{ P = 'a.psm1'; K = 'script' }
        @{ P = 'a.psd1'; K = 'script' }
        @{ P = 'a.cmd'; K = 'unsignable' }
        @{ P = 'rootfs.tar'; K = 'unsignable' }
        @{ P = 'manifest.json'; K = 'unsignable' }
    ) {
        Get-QtSignFileKind -Path $P | Should -Be $K
    }
}

# ============================================================================
Describe '🔴 第三方件一律不重签(§2.2.3:adb / scrcpy 由其上游签名不动)' {

    It '这些载荷内路径一律不签' -ForEach @(
        @{ P = 'pkg/adb/adb.exe' }
        @{ P = 'pkg/scrcpy/scrcpy.exe' }
        @{ P = 'pkg/chatlog/wx_key2.dll' }
        @{ P = 'pkg/wechat/weixin_4.1.12.26.exe' }
        @{ P = 'pkg/vcredist/VC_redist.x64.exe' }
        @{ P = 'wsl/wsl.msi' }
        @{ P = 'wsl/rootfs.tar' }
        @{ P = 'kernel/bzImage-6.6' }
        @{ P = 'winagent/python/python.exe' }
        @{ P = 'winagent/app/_internal/python312.dll' }
        @{ P = 'console/ffmpeg.dll' }
        @{ P = 'console/resources/app.asar' }
        @{ P = 'console/locales/zh-CN.pak' }
    ) {
        $r = Test-QtNeverSign -RelativePath $P
        $r | Should -Not -BeNullOrEmpty -Because ($P + ' 是第三方/非 PE 件,不该重签')
        $r.Why | Should -Not -BeNullOrEmpty
    }

    It '我方件不在禁签清单里' -ForEach @(
        @{ P = 'winagent/app/qtrade-winagent-svc.exe' }
        @{ P = 'winagent/app/qtrade-winagent-user.exe' }
        @{ P = 'install/engine/qtrade-setup-engine.exe' }
    ) {
        Test-QtNeverSign -RelativePath $P | Should -BeNullOrEmpty
    }

    It '反斜杠写法同样认(manifest 用 /,Windows 路径用 \)' {
        Test-QtNeverSign -RelativePath 'pkg\wechat\weixin_4.1.12.26.exe' | Should -Not -BeNullOrEmpty
    }

    It '随包微信在禁签清单里,理由点名 sha256 钉死(改一字节就 PAYLOAD_CORRUPT)' {
        (Test-QtNeverSign -RelativePath 'pkg/wechat/x.exe').Why | Should -Match 'sha256'
    }
}

# ============================================================================
Describe '应签清单(Get-QtSignPlan):只签我方件,不碰上游二进制' {

    BeforeAll {
        # winagent:PyInstaller onedir —— 顶层两个我方 exe,_internal 下全是上游件
        $script:WaRoot = Join-Path $TestDrive 'wa'
        New-Item -ItemType Directory -Path (Join-Path $script:WaRoot '_internal') -Force | Out-Null
        foreach ($n in @('qtrade-winagent-svc.exe', 'qtrade-winagent-user.exe')) {
            Set-Content -LiteralPath (Join-Path $script:WaRoot $n) -Value 'x'
        }
        foreach ($n in @('python312.dll', 'libcrypto.dll', 'helper.exe')) {
            Set-Content -LiteralPath (Join-Path $script:WaRoot ('_internal\' + $n)) -Value 'x'
        }

        # console:electron-builder --dir 产物
        $script:CoRoot = Join-Path $TestDrive 'co'
        New-Item -ItemType Directory -Path (Join-Path $script:CoRoot 'resources') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:CoRoot 'locales') -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $script:CoRoot 'QTrade 控制台.exe') -Value 'x'
        foreach ($n in @('ffmpeg.dll', 'libEGL.dll', 'resources.pak', 'icudtl.dat')) {
            Set-Content -LiteralPath (Join-Path $script:CoRoot $n) -Value 'x'
        }
        Set-Content -LiteralPath (Join-Path $script:CoRoot 'resources\app.asar') -Value 'x'
        Set-Content -LiteralPath (Join-Path $script:CoRoot 'locales\zh-CN.pak') -Value 'x'

        # engine 副本
        $script:EnRoot = Join-Path $TestDrive 'en'
        New-Item -ItemType Directory -Path (Join-Path $script:EnRoot 'modules') -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $script:EnRoot 'run-step.ps1') -Value 'x'
        Set-Content -LiteralPath (Join-Path $script:EnRoot 'run-engine.cmd') -Value 'x'
        Set-Content -LiteralPath (Join-Path $script:EnRoot 'qtrade-setup-engine.iss') -Value 'x'
        Set-Content -LiteralPath (Join-Path $script:EnRoot 'modules\QTrade.Wsl.psm1') -Value 'x'
        Set-Content -LiteralPath (Join-Path $script:EnRoot 'modules\QTrade.Exit.psm1') -Value 'x'
    }

    It 'winagent:只有顶层那两个 qtrade-winagent-*.exe' {
        $plan = @(Get-QtSignPlan -Root $script:WaRoot -Scope 'winagent')
        $plan.Count | Should -Be 2
        @($plan | ForEach-Object { Split-Path -Leaf $_ }) | Should -Be @('qtrade-winagent-svc.exe', 'qtrade-winagent-user.exe')
    }

    It '🔴 winagent:_internal 下的上游件一个都不在计划里(PyInstaller 打进来的 CPython 与依赖 DLL)' {
        $plan = @(Get-QtSignPlan -Root $script:WaRoot -Scope 'winagent')
        @($plan | Where-Object { $_ -match '_internal' }).Count | Should -Be 0
    }

    It 'console:只有顶层的 Electron 主程序,dll / pak / resources / locales 一个都不签' {
        $plan = @(Get-QtSignPlan -Root $script:CoRoot -Scope 'console')
        $plan.Count | Should -Be 1
        (Split-Path -Leaf $plan[0]) | Should -Be 'QTrade 控制台.exe'
    }

    It 'engine:全部 ps1/psm1,**不含 .cmd / .iss**(.cmd 不支持 Authenticode)' {
        $plan = @(Get-QtSignPlan -Root $script:EnRoot -Scope 'engine')
        $plan.Count | Should -Be 3
        @($plan | Where-Object { $_ -match '\.cmd$' }).Count | Should -Be 0
        @($plan | Where-Object { $_ -match '\.iss$' }).Count | Should -Be 0
    }

    It '目录不存在 → 回空数组,不 throw(轻量包缺件时由调用方判断)' {
        @(Get-QtSignPlan -Root (Join-Path $TestDrive 'nope') -Scope 'engine').Count | Should -Be 0
    }
}

# ============================================================================
Describe '证书导入脚本:指纹核对 + 幂等 + 未提权拒绝' {

    BeforeEach {
        $script:CerPath = Join-Path $TestDrive 'QTrade.cer'
        Set-Content -LiteralPath $script:CerPath -Value 'fake-cer'
    }

    It '🔴 -ExpectedThumbprint 不符 → 拒绝导入,一个存储都不碰' {
        Mock -ModuleName QTrade.Signing Get-QtCertFileObject { New-QtFakeCert -Thumbprint 'REALREALREAL' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @() }
        Mock -ModuleName QTrade.Signing Import-QtCertFile { throw '不该被调用:指纹不符' }

        { Import-QtCodeSigningCertCore -CerPath $script:CerPath -ExpectedThumbprint 'FAKEFAKEFAKE' } |
            Should -Throw -ExpectedMessage '*指纹不符*'
        Should -Invoke -ModuleName QTrade.Signing Import-QtCertFile -Times 0 -Exactly
    }

    It '指纹相符 → 导入 Root 与 TrustedPublisher 两个存储' {
        Mock -ModuleName QTrade.Signing Get-QtCertFileObject { New-QtFakeCert -Thumbprint 'REALREALREAL' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @() }
        Mock -ModuleName QTrade.Signing Import-QtCertFile { }

        $r = Import-QtCodeSigningCertCore -CerPath $script:CerPath -ExpectedThumbprint 'REALREALREAL'
        $r.Imported.Count | Should -Be 2
        $r.Imported | Should -Contain 'Cert:\LocalMachine\Root'
        $r.Imported | Should -Contain 'Cert:\LocalMachine\TrustedPublisher'
        Should -Invoke -ModuleName QTrade.Signing Import-QtCertFile -Times 2 -Exactly
    }

    It '幂等:两个存储里都已经有了 → 一次都不导入' {
        $cert = New-QtFakeCert -Thumbprint 'REALREALREAL'
        Mock -ModuleName QTrade.Signing Get-QtCertFileObject { $cert }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @($cert) }
        Mock -ModuleName QTrade.Signing Import-QtCertFile { throw '不该被调用:已经有了' }

        $r = Import-QtCodeSigningCertCore -CerPath $script:CerPath -ExpectedThumbprint 'REALREALREAL'
        $r.AlreadyThere.Count | Should -Be 2
        $r.Imported.Count | Should -Be 0
        Should -Invoke -ModuleName QTrade.Signing Import-QtCertFile -Times 0 -Exactly
    }

    It '-WhatIf 干跑:一个证书都不导入' {
        Mock -ModuleName QTrade.Signing Get-QtCertFileObject { New-QtFakeCert -Thumbprint 'REALREALREAL' }
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @() }
        Mock -ModuleName QTrade.Signing Import-QtCertFile { throw '不该被调用:-WhatIf' }

        (Import-QtCodeSigningCertCore -CerPath $script:CerPath -WhatIf).WhatIf | Should -BeTrue
        Should -Invoke -ModuleName QTrade.Signing Import-QtCertFile -Times 0 -Exactly
    }

    It '.cer 不存在 → throw' {
        { Import-QtCodeSigningCertCore -CerPath (Join-Path $TestDrive 'nope.cer') } |
            Should -Throw -ExpectedMessage '*找不到证书文件*'
    }

    It '反向清理:幂等,不在就说不在' {
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @() }
        Mock -ModuleName QTrade.Signing Remove-QtCertFromStore { throw '不该被调用:不在' }
        $r = Remove-QtCodeSigningCertCore -Thumbprint 'REALREALREAL'
        $r.NotFound.Count | Should -Be 2
        $r.Removed.Count | Should -Be 0
    }

    It '反向清理:在就删掉两处' {
        $cert = New-QtFakeCert -Thumbprint 'REALREALREAL'
        Mock -ModuleName QTrade.Signing Get-QtCertFromStore { @($cert) }
        Mock -ModuleName QTrade.Signing Remove-QtCertFromStore { }
        (Remove-QtCodeSigningCertCore -Thumbprint 'REALREALREAL').Removed.Count | Should -Be 2
    }

    It '导入的是 LocalMachine\Root 与 LocalMachine\TrustedPublisher,各有中文理由' {
        $stores = @(Get-QtImportStorePath)
        $stores.Count | Should -Be 2
        @($stores | ForEach-Object { $_.Path }) | Should -Be @('Cert:\LocalMachine\Root', 'Cert:\LocalMachine\TrustedPublisher')
        foreach ($s in $stores) { $s.Why | Should -Not -BeNullOrEmpty }
    }

    It '🔴 Import-QtCodeSigningCert.ps1 未提权时给中文提示并**退出非零**' {
        # 不执行这个脚本(它会 exit,在 Pester 里会打断整场);验它的结构。
        $src = [IO.File]::ReadAllText((Join-Path $script:SigningDir 'Import-QtCodeSigningCert.ps1'))
        $src | Should -Match 'if\s*\(-not\s*\(Test-QtAdmin\)\)'
        $src | Should -Match '需要管理员权限'
        $src | Should -Match 'exit 5'
    }

    It '🔴 Remove-QtCodeSigningCert.ps1 同样有未提权自检' {
        $src = [IO.File]::ReadAllText((Join-Path $script:SigningDir 'Remove-QtCodeSigningCert.ps1'))
        $src | Should -Match 'if\s*\(-not\s*\(Test-QtAdmin\)\)'
        $src | Should -Match 'exit 5'
    }

    It '双击用的 .cmd:CRLF、无 BOM、可执行行纯 ASCII、会自提权' {
        $p = Join-Path $script:SigningDir '导入QTrade签名证书.cmd'
        Test-Path -LiteralPath $p | Should -BeTrue
        $b = [IO.File]::ReadAllBytes($p)
        ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) | Should -BeFalse -Because 'BOM 会让 cmd 第一行解析失败'
        $t = [Text.Encoding]::UTF8.GetString($b)
        ([regex]::Matches($t, "`n")).Count | Should -Be ([regex]::Matches($t, "`r`n")).Count -Because 'LF-only 会让 cmd 的 for/if/call 解析错乱'
        foreach ($line in ($t -split "`r?`n")) {
            $s2 = $line.TrimStart()
            if ($s2 -eq '' -or $s2.ToLowerInvariant().StartsWith('rem')) { continue }
            foreach ($ch in $line.ToCharArray()) {
                [int]$ch | Should -BeLessOrEqual 127 -Because ('可执行行必须纯 ASCII:' + $line.Trim())
            }
        }
        $t | Should -Match 'RunAs'
        $t | Should -Match 'ExecutionPolicy Bypass'
    }
}

# ============================================================================
Describe '🔴 build.ps1 签名顺序(内层先于外壳、签名先于 manifest 哈希)' {

    It '预签载荷副本发生在**第一次** collect-payload 之前' {
        # 顺序错了 = manifest 记的是签名前的哈希 = 装机时判 E_INSTALL_PAYLOAD_CORRUPT
        $presign = Get-QtLineNo -Pattern "New-QtPresignCopy -Name 'winagent-app'"
        # 🔴 注意别对上 -SelfCheck 分支里那次 collect:那条路只收载荷、不出包、不签名
        # (它在预签之前就 exit 0 了),正式出包路径上的收载荷是「步 2」。
        $collect = Get-QtLineNo -Pattern "Write-Section '步 2 收集载荷与 manifest'" 
        $presign | Should -BeGreaterThan 0
        $collect | Should -BeGreaterThan 0
        $presign | Should -BeLessThan $collect
    }

    It '-SelfCheck 明说不签名(别让人以为"自检过了 = 签过了")' {
        $script:BuildText | Should -Match '-SelfCheck 只收载荷与生成 manifest,\*\*不签名\*\*'
    }

    It '引擎脚本(ps1/psm1)在**副本**上签,且发生在 ISCC 编译之前' {
        $signPs1 = Get-QtLineNo -Pattern '步 1a 在引擎副本上签'
        $iscc = Get-QtLineNo -Pattern '& \$Iscc '
        $signPs1 | Should -BeGreaterThan 0
        $iscc | Should -BeGreaterThan 0
        $signPs1 | Should -BeLessThan $iscc -Because 'ISCC 必须把**已签名**的脚本编进引擎'
    }

    It '引擎 exe 签名发生在它被复制进 stage 之前' {
        $signEngine = Get-QtLineNo -Pattern '步 1b:签引擎 exe'
        $copyEngine = Get-QtLineNo -Pattern 'Copy-Item -LiteralPath \$engineExe -Destination'
        $signEngine | Should -BeGreaterThan 0
        $copyEngine | Should -BeGreaterThan 0
        $signEngine | Should -BeLessThan $copyEngine
    }

    It '外壳 EXE **最后**签:在拼接之后、在引擎 exe 签名之后' {
        $concat = Get-QtLineNo -Pattern '\$fs = \[IO\.File\]::Create\(\$FinalExe\)'
        $signShell = Get-QtLineNo -Pattern '步 5 签外壳 EXE'
        $signEngine = Get-QtLineNo -Pattern '步 1b:签引擎 exe'
        $signShell | Should -BeGreaterThan $concat
        $signShell | Should -BeGreaterThan $signEngine -Because '内层先签、外壳后签(外壳签名覆盖整个归档)'
    }

    It 'G6 门在所有签名动作之后' {
        $g6 = Get-QtLineNo -Pattern "Write-Section 'G6 签名复核"
        $signShell = Get-QtLineNo -Pattern '步 5 签外壳 EXE'
        $g6 | Should -BeGreaterThan $signShell
    }

    It 'G6 复核 manifest 里引擎的 sha256 = stage 里已签名文件的实际 sha256(签名先于哈希的真凭据)' {
        $script:BuildText | Should -Match 'PAYLOAD_CORRUPT'
        $script:BuildText | Should -Match 'Get-FileHash -LiteralPath \$engInStage'
    }

    It 'G6 反向检查:第三方件不得被我方证书签过' {
        $script:BuildText | Should -Match '第三方件被我方证书重签了'
        foreach ($p in @('weixin_4\.1\.12\.26\.exe', 'VC_redist\.x64\.exe', 'adb\.exe', 'scrcpy\.exe')) {
            $script:BuildText | Should -Match $p
        }
    }

    It '🔴 签名签的是 out\ 下的副本,不是仓库里的源文件' {
        # 签仓库源文件会:污染 git、让规格对账的逐字比对失效、把签名块混进版本库
        $script:BuildText | Should -Match 'SignedEngineDir = Join-Path \$OutDir'
        $script:BuildText | Should -Match 'PresignDir = Join-Path \$OutDir'
        # ISCC 的输入是 $IssSourceDir:默认 = $EngineDir,签名时才换成副本
        $script:BuildText | Should -Match '\$IssSourceDir = \$EngineDir'
        $script:BuildText | Should -Match '\$IssSourceDir = \$SignedEngineDir'
        $script:BuildText | Should -Match 'Join-Path \$IssSourceDir'
    }

    It '.cmd 不签(Authenticode 不支持),链首脚本仍从仓库原件复制' {
        $script:BuildText | Should -Match 'Copy-Item -LiteralPath \(Join-Path \$EngineDir \$ChainHeadName\)'
    }
}

# ============================================================================
Describe '🔴 不带 -Sign 时零行为变化(回归守卫)' {

    BeforeAll {
        $script:BuildAst = Get-QtAstOf -Path $script:BuildPs1
        # 所有签名相关的命令名 —— 每一个都必须在 if ($Sign) 的作用域里
        $script:SignCmds = @(
            'Invoke-QtSignFile', 'Invoke-QtSignFiles', 'New-QtPresignCopy', 'Get-QtSignPlan',
            'Get-QtFileSignature', 'Test-QtSignatureVerdict', 'Get-QtSigningCertByThumbprint',
            'Find-QtSignTool', 'Write-QtNoTimestampWarning', 'Get-QtSigningDefault'
        )
        # 这些函数本身就是签名专用的(它们的每一处调用都已被 if ($Sign) 守住,
        # 由下面「签名模块只在 -Sign 时导入」与逐调用检查共同保证),
        # 故其函数体内部的签名调用同样算被守住。
        $script:SignOnlyFuncs = @('New-QtPresignCopy')
        function Test-QtGuardedBySign {
            param($Node)
            $n = $Node.Parent
            while ($null -ne $n) {
                if ($n -is [System.Management.Automation.Language.IfStatementAst]) {
                    foreach ($c in $n.Clauses) {
                        if ($c.Item1.Extent.Text -match '\$Sign\b') { return $true }
                    }
                }
                if ($n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                    ($script:SignOnlyFuncs -contains $n.Name)) { return $true }
                $n = $n.Parent
            }
            return $false
        }
    }

    It '每一处签名调用都在 if ($Sign) 里 —— 不带 -Sign 时一句都不执行' {
        $cmds = $script:SignCmds
        $calls = @($script:BuildAst.FindAll({
                    param($n)
                    $n -is [System.Management.Automation.Language.CommandAst] -and
                    $n.GetCommandName() -and
                    ($cmds -contains $n.GetCommandName())
                }.GetNewClosure(), $true))
        $calls.Count | Should -BeGreaterThan 0 -Because 'build.ps1 里应该有签名调用'
        $unguarded = @($calls | Where-Object { -not (Test-QtGuardedBySign -Node $_) } |
                ForEach-Object { ('第 {0} 行:{1}' -f $_.Extent.StartLineNumber, $_.GetCommandName()) })
        $unguarded.Count | Should -Be 0 -Because ('这些签名调用没有被 if ($Sign) 守住:' + ($unguarded -join ' / '))
    }

    It '🔴 New-QtPresignCopy 的每一处**调用**都在 if ($Sign) 里(上一条放行它的函数体,这条堵住它的调用点)' {
        $calls = @($script:BuildAst.FindAll({
                    param($n)
                    $n -is [System.Management.Automation.Language.CommandAst] -and
                    $n.GetCommandName() -eq 'New-QtPresignCopy'
                }, $true))
        $calls.Count | Should -Be 2 -Because 'winagent-app 与 console 各一次'
        foreach ($c in $calls) { (Test-QtGuardedBySign -Node $c) | Should -BeTrue }
    }

    It '签名模块只在 -Sign 时导入(不带 -Sign 时连 signing\ 都不碰)' {
        $imports = @($script:BuildAst.FindAll({
                    param($n)
                    $n -is [System.Management.Automation.Language.CommandAst] -and
                    $n.GetCommandName() -eq 'Import-Module' -and
                    $n.Extent.Text -match 'QTrade\.Signing'
                }, $true))
        $imports.Count | Should -Be 1
        (Test-QtGuardedBySign -Node $imports[0]) | Should -BeTrue
    }

    It '不带 -Sign 时 ISCC 的输入仍是仓库 engine\(逐字与加签名之前一致)' {
        # $IssSourceDir 的默认赋值在顶部、无守卫;换成副本那一句必须在 if ($Sign) 里
        $assigns = @($script:BuildAst.FindAll({
                    param($n)
                    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                    $n.Left.Extent.Text -eq '$IssSourceDir'
                }, $true))
        $assigns.Count | Should -Be 2
        @($assigns | Where-Object { $_.Right.Extent.Text -eq '$EngineDir' }).Count | Should -Be 1
        $toCopy = @($assigns | Where-Object { $_.Right.Extent.Text -eq '$SignedEngineDir' })
        $toCopy.Count | Should -Be 1
        (Test-QtGuardedBySign -Node $toCopy[0]) | Should -BeTrue
    }

    It '-Sign 缺 -CertThumbprint → 明确拒绝(不静默签成别的证书)' {
        $script:BuildText | Should -Match '-Sign 必须同时给 -CertThumbprint'
    }

    It 'G6 门本身也在 if ($Sign) 里(不签名时不跑这道门)' {
        $script:BuildText | Should -Match "if \(\`$Sign\) \{\s*\r?\n\s*Write-Section 'G6 签名复核"
    }

    It '预签、签引擎脚本、签引擎 exe、签外壳四处,各自都有 if ($Sign) 守卫' {
        $ifSignCount = ([regex]::Matches($script:BuildText, 'if \(\$Sign\)')).Count
        $ifSignCount | Should -BeGreaterOrEqual 5 -Because '签名准备 + 步 0b + 步 1a + 步 1b + 步 5 + G6'
    }
}

# ============================================================================
Describe '🔴 预签方案依赖的那条事实:collect-payload.ps1 里 EnvVar 优先级最高' {

    It 'Resolve-QtSource:EnvVar 的 return 出现在 SourceRoot 之前' {
        # 预签副本靠「把 QT_SRC_* 指到已签名副本」生效。谁要是把这个优先级改了,
        # 出的包会**静默**退回未签名的载荷 —— 这条用例就是那道闸。
        $ast = Get-QtAstOf -Path $script:CollectPs1
        $fn = $ast.Find({
                param($n)
                $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $n.Name -eq 'Resolve-QtSource'
            }, $true)
        $fn | Should -Not -BeNullOrEmpty
        $body = $fn.Extent.Text
        $iEnv = $body.IndexOf('if ($env0) { return $env0 }')
        $iRoot = $body.IndexOf('if ($SourceRoot)')
        $iEnv | Should -BeGreaterThan -1
        $iRoot | Should -BeGreaterThan -1
        $iEnv | Should -BeLessThan $iRoot
    }

    It 'PayloadMap 里 winagent/app 与 console 都有 EnvVar(预签要靠它改源)' {
        $ast = Get-QtAstOf -Path $script:CollectPs1
        $assign = $ast.Find({
                param($n)
                $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left.Extent.Text -eq '$PayloadMap'
            }, $true)
        $map = & ([scriptblock]::Create($assign.Right.Extent.Text))
        (@($map | Where-Object { $_.Dest -eq 'winagent/app' })[0]).EnvVar | Should -Be 'QT_SRC_WA_APP'
        (@($map | Where-Object { $_.Dest -eq 'console' })[0]).EnvVar | Should -Be 'QT_SRC_CONSOLE'
    }

    It 'build.ps1 的预签源解析与 collect 同语义:EnvVar > SourceRoot > 相对路径' {
        $ast = Get-QtAstOf -Path $script:BuildPs1
        $fn = $ast.Find({
                param($n)
                $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $n.Name -eq 'Resolve-QtPresignSource'
            }, $true)
        $fn | Should -Not -BeNullOrEmpty
        $body = $fn.Extent.Text
        $body.IndexOf('if ($v) { return @($v) }') | Should -BeLessThan $body.IndexOf('if ($SourceRoot)')
    }

    It '预签副本把 QT_SRC_* 指到副本目录(Process 作用域,不污染用户环境变量)' {
        $script:BuildText | Should -Match "SetEnvironmentVariable\(\`$EnvVar, \`$dst, 'Process'\)"
    }
}

# ============================================================================
Describe '§2.2.3 应签对象清单(实现侧的唯一出处)' {

    It '模块里的清单覆盖 §2.2.3 点名的每一类' {
        $specs = @((Get-QtSignSpecTarget) | ForEach-Object { $_.Spec })
        foreach ($want in @('外壳 EXE', '引擎 EXE', 'qtrade-winagent-svc.exe', 'Electron 主程序', 'ps1')) {
            $specs | Should -Contain $want
        }
    }

    It 'build.ps1 的 $QtSignSpec 与模块清单一一对应' {
        $ast = Get-QtAstOf -Path $script:BuildPs1
        $assign = $ast.Find({
                param($n)
                $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left.Extent.Text -eq '$QtSignSpec'
            }, $true)
        $assign | Should -Not -BeNullOrEmpty
        $buildSpec = & ([scriptblock]::Create($assign.Right.Extent.Text))
        $a = @($buildSpec | ForEach-Object { $_.Key }) | Sort-Object
        $b = @((Get-QtSignSpecTarget) | ForEach-Object { $_.Key }) | Sort-Object
        $a | Should -Be $b
    }

    It '会话代理 qtrade-winagent-user.exe 也在清单里(R-14 实名,两个可执行体)' {
        @((Get-QtSignSpecTarget) | ForEach-Object { $_.Spec }) | Should -Contain 'qtrade-winagent-user.exe'
    }
}
