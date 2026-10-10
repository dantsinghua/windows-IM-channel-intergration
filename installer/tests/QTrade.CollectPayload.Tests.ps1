# Pester 5 —— collect-payload.ps1 的缺件判定(docs/03 §2.2.1)
#
# 🔴 安琳 2026-09-21 拍板:**13 项一个都不能少,PayloadMap 里不许有「可选项」**。
#    `winagent/python` 也必带 —— 目标机可能没有 Python;即便 PyInstaller onedir 的
#    `_internal` 已自带运行时,也要随包带一份独立的嵌入式运行时作兜底/排障用。
#    (曾按 §2.2.1 Hint 的「onedir 路线可缺」把它做成 `Optional = $true`,已按拍板还原;
#     这组用例就是防它被再次改回去的闸。)
#
# 注意别和装机时的 `critical` 混为一谈:
#   Critical:**装机时**校验失败算不算 `E_INSTALL_PAYLOAD_CORRUPT`(不可续跑)
#   缺件    :**打包时**少了东西 —— 现在一律拒绝出正式包,与 critical 与否无关。
BeforeAll {
    $script:BuildDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'build'
    $script:Collect = Join-Path $script:BuildDir 'collect-payload.ps1'

    # 只解析脚本、取出 $PayloadMap,不执行主流程(主流程会真去拷 GB 级载荷)。
    function Get-QtPayloadMap {
        $err = $null; $tok = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($script:Collect, [ref]$tok, [ref]$err)
        if ($err) { throw ('collect-payload.ps1 解析失败:' + ($err -join '; ')) }
        $assign = $ast.Find({
                param($n)
                $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left.Extent.Text -eq '$PayloadMap'
            }, $true)
        if (-not $assign) { throw '在 collect-payload.ps1 里找不到 $PayloadMap 赋值' }
        return (& ([scriptblock]::Create($assign.Right.Extent.Text)))
    }

    $script:Map = Get-QtPayloadMap
    function Get-QtItem { param([string] $Dest) return ($script:Map | Where-Object { $_.Dest -eq $Dest }) }
}

Describe 'PayloadMap:13 项必备,没有可选项(§2.2.1 + 安琳 2026-09-21 拍板)' {

    It 'all 16 payload items present (13 + R6-92 pyweixin wheel / LICENSE / SOURCE.txt)' {
        # 2026-10-10: pyweixin is vendored from upstream source and required; "every item mandatory, none optional" still holds.
        $script:Map.Count | Should -Be 16
    }

    It 'PayloadMap 里一个 Optional 项都没有' {
        # 任何一项被标成可选,都等于允许出一个少东西的「正式」包。
        $optional = @($script:Map | Where-Object { $_.ContainsKey('Optional') -and $_.Optional })
        $optional.Count | Should -Be 0 -Because ('这些项被标了 Optional:' + (($optional | ForEach-Object { $_.Dest }) -join ', '))
    }

    It 'winagent/python 必带,不是可选项' {
        # 目标机可能没有 Python;onedir 的 _internal 自带运行时也不算数,要独立带一份兜底。
        $i = Get-QtItem -Dest 'winagent/python'
        $i | Should -Not -BeNullOrEmpty
        ($i.ContainsKey('Optional') -and $i.Optional) | Should -BeFalse
    }

    It '§2.2.1 critical:true is exactly these 6 items (R6-92 adds the pyweixin wheel)' {
        $crit = @($script:Map | Where-Object { $_.Critical } | ForEach-Object { $_.Dest }) | Sort-Object
        $crit | Should -Be (@(
                'kernel/bzImage-6.6'
                'pkg/pyweixin/pywechat127-1.9.8-py3-none-any.whl'
                'pkg/wechat/weixin_4.1.12.26.exe'
                'wsl/kcheck-rootfs.tar'
                'wsl/rootfs.tar'
                'wsl/wsl.msi'
            ) | Sort-Object)
    }

    It 'Critical=false 的项同样必备 —— 缺了照样拒绝出正式包' {
        foreach ($d in @('pkg/adb', 'pkg/scrcpy', 'pkg/chatlog', 'console', 'winagent/app', 'winagent/python', 'wsl/agent', 'pkg/vcredist/VC_redist.x64.exe')) {
            $i = Get-QtItem -Dest $d
            $i | Should -Not -BeNullOrEmpty -Because ('PayloadMap 里应有 ' + $d)
            $isOpt = ($i.ContainsKey('Optional') -and $i.Optional)
            $isOpt | Should -BeFalse -Because ('{0} 不该是 Optional' -f $d)
        }
    }

    It '随包微信钉死了 sha256(B-1 / R2-6:两候选包 VersionInfo 相同,只能靠它分辨)' {
        $i = Get-QtItem -Dest 'pkg/wechat/weixin_4.1.12.26.exe'
        $i.PinnedSha256 | Should -Be '58997cfe4513ab71f107c2137bb570ade030f228115c14688544cec80e604053'
    }

    It 'pyweixin wheel is critical, pinned by sha256, and shipped with LICENSE + SOURCE.txt (R6-92)' {
        $w = Get-QtItem -Dest 'pkg/pyweixin/pywechat127-1.9.8-py3-none-any.whl'
        $w.Critical | Should -BeTrue
        $w.PinnedSha256 | Should -Be 'a98ab028d284917099201a2cefbcbe1ba8c36a02ea136b4b2632539167e7792e'
        (Get-QtItem -Dest 'pkg/pyweixin/LICENSE') | Should -Not -BeNullOrEmpty
        (Get-QtItem -Dest 'pkg/pyweixin/SOURCE.txt') | Should -Not -BeNullOrEmpty
    }

    It 'the vendored pyweixin wheel in the repo matches the pinned sha256' {
        $repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        $whl = Join-Path $repo 'winagent\vendor\pyweixin\pywechat127-1.9.8-py3-none-any.whl'
        Test-Path -LiteralPath $whl | Should -BeTrue
        (Get-FileHash -LiteralPath $whl -Algorithm SHA256).Hash.ToLowerInvariant() | Should -Be 'a98ab028d284917099201a2cefbcbe1ba8c36a02ea136b4b2632539167e7792e'
    }
}

Describe 'collect-payload.ps1 的缺件分流实现' {

    BeforeAll { $script:Src = [IO.File]::ReadAllText($script:Collect) }

    It '任何缺件都进 $missing —— 没有分流出口' {
        # 曾经有过一条 `if ($item.Optional) { $optionalAbsent += ... }` 的分流,已按拍板删除。
        $script:Src | Should -Not -Match '\$optionalAbsent'
        $script:Src | Should -Not -Match "ContainsKey\('Optional'\)"
    }

    It 'manifest 里没有 optional_absent 字段' {
        $script:Src | Should -Not -Match 'optional_absent'
        $script:Src | Should -Match 'missing\s+=\s+@\(\$missing'
    }

    It '缺件即 throw(不带 -AllowMissing 时)' {
        $script:Src | Should -Match '\$missing\.Count -gt 0 -and -not \$AllowMissing'
    }

    It 'rootfs_contents 由 contents.json 登记,不再恒为空(G-10)' {
        # 空清单会让安装期 IMAGES_LOADED 的镜像复核形同虚设。
        $script:Src | Should -Match 'Get-QtRootfsContents'
        $script:Src | Should -Match 'contents\.json'
        $script:Src | Should -Not -Match 'rootfs_contents\s+=\s+\[ordered\]@\{\}'
    }
}
