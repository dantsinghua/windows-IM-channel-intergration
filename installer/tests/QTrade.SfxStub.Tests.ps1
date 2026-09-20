#requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0.0' }
<#
    自编 SFX 存根工具链(installer/sfx-stub/)的单测。

    这批测试的重点不是「补丁写得对不对」——那要真编译才知道,而本机没有 MSVC——
    而是「取源 / 打补丁 / 验货」这三段**自动化本身**靠不靠谱:
      * sha256 关卡真的会拦;
      * 补丁应用器严格到上下文差一个字符就炸(绝不模糊匹配);
      * 验货函数真的能把「官方存根」和「打了补丁的存根」区分开
        —— 用仓库里那个**真的** 7zSD.sfx 做阴性对照。
#>

BeforeAll {
    $script:StubDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'sfx-stub'
    $script:BuildDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'build'
    Import-Module (Join-Path $script:StubDir 'QTrade.SfxStub.psm1') -Force

    # 合成夹具:三行文本 + 一个改中间那行的补丁
    $script:MakeFixture = {
        param([string] $Root)
        New-Item -ItemType Directory -Path (Join-Path $Root 'sdk\sub') -Force | Out-Null
        $f = Join-Path $Root 'sdk\sub\a.txt'
        [System.IO.File]::WriteAllText($f, "line1`r`nline2`r`nline3`r`n")
        return $f
    }
    $script:FixturePatch = @"
--- a/sub/a.txt
+++ b/sub/a.txt
@@ -1,3 +1,4 @@
 line1
-line2
+LINE-TWO
+inserted
 line3
"@
}

Describe 'Get-QtSfxSdkInfo' {
    It '把 LZMA SDK 2301 的 sha256 逐字钉死' {
        # 🔴 这个值是安琳在任务里给定的,任何人改它都必须是有意为之
        (Get-QtSfxSdkInfo).Sha256 |
            Should -BeExactly '317DD834D6BBFD95433488B832E823CD3D4D420101436422C03AF88507DD1370'
    }
    It '只解重编需要的两棵子树' {
        (Get-QtSfxSdkInfo).Subtrees | Should -Be @('C', 'CPP')
    }
    It '工程目录指向 SFXSetup' {
        (Get-QtSfxSdkInfo).ProjectDir | Should -BeExactly 'CPP\7zip\Bundles\SFXSetup'
    }
}

Describe 'Assert-QtSha256' {
    BeforeAll {
        $script:TmpFile = Join-Path $TestDrive 'probe.bin'
        [System.IO.File]::WriteAllText($script:TmpFile, 'qtrade')
        $script:TrueHash = (Get-FileHash -LiteralPath $script:TmpFile -Algorithm SHA256).Hash.ToUpperInvariant()
    }
    It '摘要一致时通过并回填实际值' {
        Assert-QtSha256 -Path $script:TmpFile -Expected $script:TrueHash | Should -BeExactly $script:TrueHash
    }
    It '大小写不敏感' {
        { Assert-QtSha256 -Path $script:TmpFile -Expected $script:TrueHash.ToLowerInvariant() } | Should -Not -Throw
    }
    It '🔴 摘要不符必须抛 —— 这是供应链上唯一的关卡,没有旁路' {
        { Assert-QtSha256 -Path $script:TmpFile -Expected ('0' * 64) } | Should -Throw -ExpectedMessage '*sha256 不符*'
    }
    It '文件不存在也抛' {
        { Assert-QtSha256 -Path (Join-Path $TestDrive 'nope.bin') -Expected ('0' * 64) } | Should -Throw
    }
}

Describe 'ConvertTo-QtDiffLines' {
    It '剥掉行尾 CR(源码是 CRLF,补丁里 CR 是行内容的一部分)' {
        # 用 -join 比,不走管道:`, $array` 经过管道会变成「一个元素、内容是数组」,
        # Pester 的集合比较在那种形状下会给出「明明一样却不相等」的迷惑报错。
        (ConvertTo-QtDiffLines -Text "a`r`nb`r`n") -join '|' | Should -BeExactly 'a|b'
    }
    It '被二次 CRLF 化的补丁(行尾 CR CR)也剥干净' {
        (ConvertTo-QtDiffLines -Text "a`r`r`nb`r`r`n") -join '|' | Should -BeExactly 'a|b'
    }
    It '以换行结尾时不多出一个空行' {
        (ConvertTo-QtDiffLines -Text "a`n").Count | Should -Be 1
    }
    It '不以换行结尾时末行保留' {
        (ConvertTo-QtDiffLines -Text "a`nb") -join '|' | Should -BeExactly 'a|b'
    }
}

Describe 'ConvertFrom-QtUnifiedDiff（解析真补丁）' {
    BeforeAll {
        $script:RealPatch = [System.IO.File]::ReadAllText((Join-Path $script:StubDir 'qtrade-sfx.patch'))
        $script:Parsed = ConvertFrom-QtUnifiedDiff -PatchText $script:RealPatch
    }
    It '正好三个文件段' {
        $script:Parsed.Count | Should -Be 3
    }
    It '剥掉 a/ b/ 前缀后是 SDK 内的相对路径' {
        $script:Parsed.Path | Should -Contain 'CPP\7zip\Bundles\SFXSetup\SfxSetup.cpp'
        $script:Parsed.Path | Should -Contain 'CPP\7zip\Bundles\SFXSetup\ExtractEngine.cpp'
        $script:Parsed.Path | Should -Contain 'CPP\7zip\Bundles\SFXSetup\ExtractEngine.h'
    }
    It '每个文件段都至少有一个 hunk' {
        foreach ($f in $script:Parsed) { $f.Hunks.Count | Should -BeGreaterThan 0 }
    }
    It '🔴 补丁只碰 SFXSetup 这一个目录 —— 绝不改 SDK 的公共代码' {
        foreach ($f in $script:Parsed) {
            $f.Path | Should -BeLike 'CPP\7zip\Bundles\SFXSetup\*'
        }
    }
}

Describe 'Invoke-QtUnifiedDiff' {
    BeforeEach {
        $script:Root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:File = & $script:MakeFixture $script:Root
        $script:Patch = Join-Path $script:Root 'p.patch'
        [System.IO.File]::WriteAllText($script:Patch, $script:FixturePatch)
    }

    It '按补丁改出正确内容' {
        Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') | Out-Null
        $text = [System.IO.File]::ReadAllText($script:File)
        $text.TrimStart([char]0xFEFF) | Should -BeExactly "line1`r`nLINE-TWO`r`ninserted`r`nline3`r`n"
    }

    It '输出是 CRLF' {
        Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') | Out-Null
        $bytes = [System.IO.File]::ReadAllBytes($script:File)
        $lf = ($bytes | Where-Object { $_ -eq 0x0A }).Count
        $crlf = 0
        for ($i = 0; $i -lt $bytes.Length - 1; $i++) { if ($bytes[$i] -eq 0x0D -and $bytes[$i + 1] -eq 0x0A) { $crlf++ } }
        $crlf | Should -Be $lf
    }

    It '🔴 输出带 UTF-8 BOM —— 否则 cl.exe 会按 GBK 解析补丁里的中文注释' {
        Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') | Out-Null
        $bytes = [System.IO.File]::ReadAllBytes($script:File)
        $bytes[0..2] | Should -Be @(0xEF, 0xBB, 0xBF)
    }

    It '🔴 上下文差一个字符就抛,绝不模糊匹配' {
        [System.IO.File]::WriteAllText($script:File, "line1`r`nline2`r`nline3X`r`n")
        { Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') } |
            Should -Throw -ExpectedMessage '*上下文对不上*'
    }

    It '🔴 重复应用必须炸 —— 不能悄悄打第二遍' {
        Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') | Out-Null
        { Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') } | Should -Throw
    }

    It '目标文件不在源码树里时,报「版本对不上」而不是默默跳过' {
        Remove-Item -LiteralPath $script:File -Force
        { Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') } |
            Should -Throw -ExpectedMessage '*不在源码树里*'
    }

    It '-DryRun 不落盘' {
        $before = [System.IO.File]::ReadAllText($script:File)
        Invoke-QtUnifiedDiff -PatchPath $script:Patch -Root (Join-Path $script:Root 'sdk') -DryRun | Out-Null
        [System.IO.File]::ReadAllText($script:File) | Should -BeExactly $before
    }
}

Describe 'Find-QtMsvcToolchain / Get-QtMsvcMissingMessage' {
    It 'vswhere 都找不到时,结论是「没装过 VS」而不是崩' {
        $p = Find-QtMsvcToolchain -VsWherePath (Join-Path $TestDrive 'no-such-vswhere.exe')
        $p.Found | Should -BeFalse
        $p.Missing.Count | Should -BeGreaterThan 0
    }
    It '🔴 缺件说明里必须写明「本脚本不会自行安装」' {
        $p = Find-QtMsvcToolchain -VsWherePath (Join-Path $TestDrive 'no-such-vswhere.exe')
        $msg = Get-QtMsvcMissingMessage -Probe $p
        $msg | Should -BeLike '*不会自行安装*'
        $msg | Should -BeLike '*VC.Tools.x86.x64*'
    }
    It '缺件说明要给出回退路径(官方存根 + 搬运),并提醒别读 EXE 退出码' {
        $p = Find-QtMsvcToolchain -VsWherePath (Join-Path $TestDrive 'no-such-vswhere.exe')
        $msg = Get-QtMsvcMissingMessage -Probe $p
        $msg | Should -BeLike '*last-exit-code.txt*'
    }
}

Describe 'Test-QtSfxStubBinary' {
    It '不是 PE 的文件直接判不合格' {
        $f = Join-Path $TestDrive 'notpe.bin'
        [System.IO.File]::WriteAllBytes($f, [byte[]](1..64))
        $v = Test-QtSfxStubBinary -Path $f
        $v.Ok | Should -BeFalse
    }

    It '🔴 阴性对照:官方 7zSD.sfx 必须被判「没有 InstallPath」' {
        # 这正是第四批用哑 EXE 实测出来的事实 —— 官方存根不认 InstallPath。
        # 验货函数如果连这个都分不出来,它就是摆设。
        $official = Join-Path $script:BuildDir '7zSD.sfx'
        if (-not (Test-Path -LiteralPath $official)) {
            Set-ItResult -Skipped -Because '本机没放官方 7zSD.sfx(它被 gitignore,按 build/README 放置)'
            return
        }
        $v = Test-QtSfxStubBinary -Path $official
        $v.Ok | Should -BeFalse
        ($v.Problems -join ' ') | Should -BeLike '*InstallPath*'
    }

    It '官方存根本身是 x86 —— 确认机器类型这一项没有误判' {
        $official = Join-Path $script:BuildDir '7zSD.sfx'
        if (-not (Test-Path -LiteralPath $official)) {
            Set-ItResult -Skipped -Because '本机没放官方 7zSD.sfx'
            return
        }
        (Test-QtSfxStubBinary -Path $official).Machine | Should -Be 0x014C
    }
}
