# Pester 5 —— 发行版同名冲突 / 首启 install.env / IMAGES_LOADED(docs/03 §2.7.1、§2.7.3、§2.2.1 G-10、§2.3)
BeforeAll {
    $script:ModulesDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($m in @('QTrade.Exit', 'QTrade.Native', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl', 'QTrade.Distro')) {
        Import-Module (Join-Path $script:ModulesDir ($m + '.psm1')) -DisableNameChecking
    }
    $script:Expected = 'C:\ProgramData\QTrade\wsl\distro'
}

Describe '§2.7.1 同名冲突四情形' {

    It '没有同名 → NOT_PRESENT(直接导入)' {
        (Resolve-QtDistroConflict -Exists $false).Kind | Should -Be 'NOT_PRESENT'
    }

    It '有记录 + 路径对 + .imported 版本 == 本包 → REUSE(修复/重跑,跳过导入)' {
        (Resolve-QtDistroConflict -Exists $true -HasStateRecord $true -BasePath $script:Expected `
                -ExpectedBasePath $script:Expected -ImportedRootfsVersion 'rootfs-1.0.0' -PackageRootfsVersion 'rootfs-1.0.0').Kind |
            Should -Be 'REUSE'
    }

    It '🔴 .imported 版本低于本包 → UPGRADE(不在首装流程里静默换 rootfs)' {
        (Resolve-QtDistroConflict -Exists $true -HasStateRecord $true -BasePath $script:Expected `
                -ExpectedBasePath $script:Expected -ImportedRootfsVersion 'rootfs-0.9.0' -PackageRootfsVersion 'rootfs-1.0.0').Kind |
            Should -Be 'UPGRADE'
    }

    It '🔴 install_state 无记录 → FOREIGN(停下问用户)' {
        $r = Resolve-QtDistroConflict -Exists $true -HasStateRecord $false -BasePath $script:Expected `
            -ExpectedBasePath $script:Expected -ImportedRootfsVersion 'rootfs-1.0.0' -PackageRootfsVersion 'rootfs-1.0.0'
        $r.Kind | Should -Be 'FOREIGN'
        $r.Reason | Should -Be 'no_install_state'
    }

    It '🔴 BasePath 不在我们的目录 → FOREIGN' {
        (Resolve-QtDistroConflict -Exists $true -HasStateRecord $true -BasePath 'D:\somewhere\else' `
                -ExpectedBasePath $script:Expected -ImportedRootfsVersion 'rootfs-1.0.0' -PackageRootfsVersion 'rootfs-1.0.0').Reason |
            Should -Be 'base_path_mismatch'
    }

    It '🔴 发行版里没有 .imported → FOREIGN' {
        (Resolve-QtDistroConflict -Exists $true -HasStateRecord $true -BasePath $script:Expected `
                -ExpectedBasePath $script:Expected -ImportedRootfsVersion '' -PackageRootfsVersion 'rootfs-1.0.0').Reason |
            Should -Be 'no_imported_marker'
    }

    It 'BasePath 比较忽略大小写与尾部反斜杠' {
        (Resolve-QtDistroConflict -Exists $true -HasStateRecord $true -BasePath ($script:Expected.ToUpperInvariant() + '\') `
                -ExpectedBasePath $script:Expected -ImportedRootfsVersion 'r1' -PackageRootfsVersion 'r1').Kind |
            Should -Be 'REUSE'
    }
}

Describe '🔴 §2.7.1 FOREIGN:先导出再注销,导出失败就不注销(验收 M1-13)' {

    It '导出成功 → Ok + tar 路径' {
        Mock -ModuleName QTrade.Distro New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Test-QtPath { $true }
        $r = Export-QtDistroBackup -WslDir 'X:\wsl' -Stamp '20260920-190000'
        $r.Ok | Should -BeTrue
        $r.TarPath | Should -Match 'backup-qtrade-20260920-190000\.tar$'
    }

    It '导出命令失败 → Ok=false(调用方据此**不注销**)' {
        Mock -ModuleName QTrade.Distro New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = 'nope'; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Test-QtPath { $true }
        (Export-QtDistroBackup -WslDir 'X:\wsl').Ok | Should -BeFalse
    }

    It 'tar 没真落盘 → Ok=false(退出码 0 也不能信)' {
        Mock -ModuleName QTrade.Distro New-QtDirectory { $Path }
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Test-QtPath { $false }
        (Export-QtDistroBackup -WslDir 'X:\wsl').Ok | Should -BeFalse
    }
}

Describe '§2.7.3 install.env —— 选段结果传进发行版(R5-9:必须在首起 dockerd 之前)' {

    BeforeEach {
        $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-env-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Tmp -Force | Out-Null
    }
    AfterEach { Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It '写出 pool base 与 bip,LF 换行(给发行版里的 shell 读)' {
        $p = Join-Path $script:Tmp 'install.env'
        Write-QtInstallEnv -Path $p -DockerCidr '10.213.0.0/16' -ApkUrl 'http://x/y.apk' | Out-Null
        $text = [IO.File]::ReadAllText($p)
        $text | Should -Match 'QTRADE_DOCKER_POOL_BASE=10\.213\.0\.0/16'
        $text | Should -Match 'QTRADE_DOCKER_BIP=10\.213\.0\.1/24'
        $text | Should -Match 'QTRADE_APK_URL=http://x/y\.apk'
        $text | Should -Not -Match "`r"
    }

    It 'apk_url 为空也能写(留空到 P-SET 再填,§2.9.4)' {
        $p = Join-Path $script:Tmp 'install.env'
        Write-QtInstallEnv -Path $p -DockerCidr '10.231.0.0/16' | Out-Null
        [IO.File]::ReadAllText($p) | Should -Match 'QTRADE_APK_URL=\s*$'
    }
}

Describe 'IMAGES_LOADED 判据(§2.3 / §2.7.3 末)' {

    It '两镜像都在 → true' {
        Test-QtImageRefsPresent -Present @('redroid/redroid:11.0.0-latest', 'mlikiowa/napcat-docker:v1') `
            -Expected @('redroid/redroid:11.0.0-latest', 'mlikiowa/napcat-docker:v1') | Should -BeTrue
    }
    It '少一个 → false' {
        Test-QtImageRefsPresent -Present @('redroid/redroid:11.0.0-latest') `
            -Expected @('redroid/redroid:11.0.0-latest', 'mlikiowa/napcat-docker:v1') | Should -BeFalse
    }
    It '期望为空 → false(轻量包不该被当成「已加载」)' {
        Test-QtImageRefsPresent -Present @('a:b') -Expected @() | Should -BeFalse
    }

    It '从 manifest.rootfs_contents 取 ref(G-10)' {
        $m = [pscustomobject]@{ rootfs_contents = [pscustomobject]@{
                redroid_image = [pscustomobject]@{ ref = 'redroid/redroid:11.0.0-latest' }
                napcat_image  = [pscustomobject]@{ ref = 'mlikiowa/napcat-docker:v1' }
            } }
        (Get-QtExpectedImageRefs -Manifest $m) -join ',' | Should -Be 'redroid/redroid:11.0.0-latest,mlikiowa/napcat-docker:v1'
    }

    It 'manifest 没有 rootfs_contents(轻量包)→ 空集合,不抛' {
        (Get-QtExpectedImageRefs -Manifest ([pscustomobject]@{})).Count | Should -Be 0
    }
}

Describe '🔴 验收 M1-12(R5-9):bridge 子网必须落在选定段内且不是 172.17' {

    It '落在选定段内 → true' {
        Test-QtDockerPoolApplied -BridgeSubnet '10.213.0.0/24' -SelectedCidr '10.213.0.0/16' | Should -BeTrue
    }
    It '🔴 正好是默认的 172.17.0.0/16 → false(说明选段晚于 dockerd 首启,选段白做)' {
        Test-QtDockerPoolApplied -BridgeSubnet '172.17.0.0/16' -SelectedCidr '10.213.0.0/16' | Should -BeFalse
    }
    It '落在别的段 → false' {
        Test-QtDockerPoolApplied -BridgeSubnet '10.231.0.0/24' -SelectedCidr '10.213.0.0/16' | Should -BeFalse
    }
    It '取不到子网 → false(不拿空值当通过)' {
        Test-QtDockerPoolApplied -BridgeSubnet '' -SelectedCidr '10.213.0.0/16' | Should -BeFalse
    }
}

Describe 'G-10 rootfs_contents 复核(§2.2.1)' {

    It 'sha256 一致 → Ok' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            [pscustomobject]@{ ExitCode = 0; StdOut = 'abc123  /opt/qtrade/platform-tools/adb'; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $m = [pscustomobject]@{ rootfs_contents = [pscustomobject]@{
                adb = [pscustomobject]@{ path = '/opt/qtrade/platform-tools/adb'; sha256 = 'abc123' }
            } }
        (Invoke-QtRootfsContentsVerify -Manifest $m).Ok | Should -BeTrue
    }

    It '🔴 sha256 不一致 → 列出不一致项(IMAGE_LOAD_FAILED 要附上它)' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            [pscustomobject]@{ ExitCode = 0; StdOut = 'deadbeef  /opt/qtrade/platform-tools/adb'; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        $m = [pscustomobject]@{ rootfs_contents = [pscustomobject]@{
                adb = [pscustomobject]@{ path = '/opt/qtrade/platform-tools/adb'; sha256 = 'abc123' }
            } }
        $r = Invoke-QtRootfsContentsVerify -Manifest $m
        $r.Ok | Should -BeFalse
        $r.Mismatched[0].item | Should -Be 'adb'
        $r.Mismatched[0].expected | Should -Be 'abc123'
        $r.Mismatched[0].actual | Should -Be 'deadbeef'
    }

    It '文档里的省略号占位不参与比对(轻量包/未定稿的 manifest 不误红)' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { throw '不该被调用' }
        $m = [pscustomobject]@{ rootfs_contents = [pscustomobject]@{
                adb = [pscustomobject]@{ path = '/opt/qtrade/platform-tools/adb'; sha256 = '…' }
            } }
        (Invoke-QtRootfsContentsVerify -Manifest $m).Ok | Should -BeTrue
    }

    It '没有 rootfs_contents 一节 → Ok(不阻断轻量包)' {
        (Invoke-QtRootfsContentsVerify -Manifest ([pscustomobject]@{})).Ok | Should -BeTrue
    }
}

Describe 'Agent 健康(§2.11 第 2 项的事实来源)' {

    It '解析 docker/db/winagent_reachable' {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp {
            [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '{"docker":"ok","db":"ok","winagent_reachable":true,"version":"1.0.0"}' }
        }
        $h = Get-QtAgentHealth
        $h.Ok | Should -BeTrue
        $h.Docker | Should -Be 'ok'
        $h.Db | Should -Be 'ok'
        $h.WinAgentReachable | Should -BeTrue
        $h.Version | Should -Be '1.0.0'
    }

    It '不可达 → Ok=false,字段回缺省而不是抛' {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $false; StatusCode = 0; Body = 'refused' } }
        $h = Get-QtAgentHealth
        $h.Ok | Should -BeFalse
        $h.WinAgentReachable | Should -BeFalse
    }

    It '回的不是 JSON 也不抛' {
        Mock -ModuleName QTrade.Distro Invoke-QtHttp { [pscustomobject]@{ Ok = $true; StatusCode = 200; Body = '<html>' } }
        (Get-QtAgentHealth).Ok | Should -BeTrue
    }
}

Describe '幂等判据' {

    It 'DISTRO_IMPORTED:`wsl -l` 有 qtrade 且 .imported 版本 == 本包' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = "Ubuntu-24.04`r`nqtrade`r`n"; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Read-QtImportedMarker { [pscustomobject]@{ rootfs_version = 'rootfs-1.0.0' } }
        Test-QtDistroImported -Context ([pscustomobject]@{ DistroName = 'qtrade'; RootfsVersion = 'rootfs-1.0.0' }) | Should -BeTrue
        Test-QtDistroImported -Context ([pscustomobject]@{ DistroName = 'qtrade'; RootfsVersion = 'rootfs-1.1.0' }) | Should -BeFalse
    }

    It 'DISTRO_IMPORTED:列表里没有 qtrade → false' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = "Ubuntu-24.04`r`n"; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Read-QtImportedMarker { [pscustomobject]@{ rootfs_version = 'rootfs-1.0.0' } }
        Test-QtDistroImported -Context ([pscustomobject]@{ DistroName = 'qtrade'; RootfsVersion = 'rootfs-1.0.0' }) | Should -BeFalse
    }

    It 'IMAGES_LOADED:镜像齐 + agent active → true' {
        Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @('redroid/redroid:11.0.0-latest') }
        Mock -ModuleName QTrade.Distro Test-QtAgentActive { $true }
        Test-QtImagesLoaded -Context ([pscustomobject]@{ DistroName = 'qtrade'; ExpectedImages = @('redroid/redroid:11.0.0-latest') }) | Should -BeTrue
    }

    It 'IMAGES_LOADED:agent 没起 → false' {
        Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @('redroid/redroid:11.0.0-latest') }
        Mock -ModuleName QTrade.Distro Test-QtAgentActive { $false }
        Test-QtImagesLoaded -Context ([pscustomobject]@{ DistroName = 'qtrade'; ExpectedImages = @('redroid/redroid:11.0.0-latest') }) | Should -BeFalse
    }
}


Describe '§2.13 第 6 步:新镜像 tar 变了 → docker load' {

    BeforeAll {
        $script:Mf = [pscustomobject]@{ rootfs_contents = [pscustomobject]@{
                redroid_image = [pscustomobject]@{ ref = 'redroid/redroid:11.0.0-latest'; tar = '/var/lib/qtrade/images/redroid-11.tar' }
                napcat_image  = [pscustomobject]@{ ref = 'mlikiowa/napcat-docker:v1'; tar = '/var/lib/qtrade/images/napcat.tar' }
            } }
    }

    It '两个镜像都 load 成功且 ref 出现在 docker images → Ok' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = 'Loaded image'; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @('redroid/redroid:11.0.0-latest', 'mlikiowa/napcat-docker:v1') }
        $r = Invoke-QtDockerLoadImages -Manifest $script:Mf
        $r.Ok | Should -BeTrue
        $r.Loaded.Count | Should -Be 2
    }

    It '🔴 `docker load` 退出 0 但 ref 没进来 → 判失败(退出 0 不等于你要的 tag 进来了)' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @() }
        $r = Invoke-QtDockerLoadImages -Manifest $script:Mf
        $r.Ok | Should -BeFalse
        $r.Failed.Count | Should -Be 2
        $r.Failed[0].reason | Should -Match 'docker images 里没有该 ref'
    }

    It 'load 命令本身失败 → 带回 stderr' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { [pscustomobject]@{ ExitCode = 1; StdOut = ''; StdErr = 'no space left'; TimedOut = $false; DurationMs = 1 } }
        Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @() }
        (Invoke-QtDockerLoadImages -Manifest $script:Mf).Failed[0].reason | Should -Match 'no space left'
    }

    It '🔴 **绝不**删旧镜像(账号容器按 account_runtime 记的 tag 起,逐账号升级归 Agent)' {
        $script:Cmds = @()
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            $script:Cmds += ($WslArgs -join ' ')
            [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
        }
        Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @('redroid/redroid:11.0.0-latest', 'mlikiowa/napcat-docker:v1') }
        Invoke-QtDockerLoadImages -Manifest $script:Mf | Out-Null
        ($script:Cmds -join ' | ') | Should -Not -Match 'rmi'
        ($script:Cmds -join ' | ') | Should -Not -Match 'prune'
        ($script:Cmds -join ' | ') | Should -Match 'docker load -i'
    }

    It '轻量包(没有 rootfs_contents)→ Ok,什么都不做' {
        Mock -ModuleName QTrade.Distro Invoke-QtWsl { throw '不该被调用' }
        (Invoke-QtDockerLoadImages -Manifest ([pscustomobject]@{})).Ok | Should -BeTrue
    }

    It '载荷侧带了同名 tar 时优先用它(TarOverrideDir)' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('qt-img-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        try {
            [IO.File]::WriteAllText((Join-Path $tmp 'redroid-11.tar'), 'x')
            $script:Used = @()
            Mock -ModuleName QTrade.Distro Invoke-QtWsl {
                $script:Used += ($WslArgs -join ' ')
                [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false; DurationMs = 1 }
            }
            Mock -ModuleName QTrade.Distro Get-QtDockerImages { , @('redroid/redroid:11.0.0-latest', 'mlikiowa/napcat-docker:v1') }
            Invoke-QtDockerLoadImages -Manifest $script:Mf -TarOverrideDir $tmp | Out-Null
            ($script:Used -join ' | ') | Should -Match '/mnt/[a-z]/.*redroid-11\.tar'
        } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
