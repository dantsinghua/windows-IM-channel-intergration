# QTrade 安装器 —— 载荷收集与 manifest 生成
# 规格:docs/03 §2.2.1(manifest.json 逐字段)、§2.2.2(体积与压缩)、§2.6.8 第 4 条(三元组由 CI 写、人不手填)、
#       §2.9.2/§2.9.3(随包微信版本与 sha256 钉死)、docs/02 §2.4(WinAgent 两个 PyInstaller 可执行体)
#
# 用法:
#   .\collect-payload.ps1 -Stage ..\out\payload -SourceRoot <产物根>               # 缺件即报错并列出
#   .\collect-payload.ps1 -Stage ..\out\payload -AllowMissing                      # 出「轻量验证包」(缺件占位)
#
# 🔴 本脚本**只收集与登记**,不下载任何东西(§11.5 [OFFLINE]);缺件一律明确报错,绝不静默放行。
#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Stage,
    [string] $SourceRoot = '',
    [string] $PackageVersion = '1.0.0',
    [switch] $AllowMissing,
    [switch] $Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 载荷来源映射 ────────────────────────────────────────────────────────────
# Sources :相对 $SourceRoot 的路径数组(或绝对路径;`EnvVar` 指定的环境变量优先)。
#           Kind=tree 时**多个源依次覆盖合并**到同一 Dest(WinAgent 的两个 PyInstaller 产物就这么合)。
# Dest    :载荷内路径(= manifest.files[].path,逐字对应 docs/03 §2.2.1)
# Kind    :file(单文件)| tree(目录内容)| glob(按 Pattern 从目录取文件)
# Critical:严格照 §2.2.1 标了 `critical:true` 的那 5 项 —— 它决定安装期校验失败是不是
#           `E_INSTALL_PAYLOAD_CORRUPT`(不可续跑);与「打包时缺不缺」是两回事,缺任何一项都不出正式包。
# Packed  :$true = **已压缩件**,打包时进非 solid 的 `-mx=0` 块(§2.2.2)
# Hint    :缺件时打印「怎么拿」。原机路径只作提示 —— 那些在 WSL 里,Windows 侧打包机看不到。
$PayloadMap = @(
    @{ Dest = 'kernel/bzImage-6.6'; Sources = @('kernel/out/bzImage'); Kind = 'file'; Critical = $true; Packed = $false
        Purpose = '自编 WSL2 内核(binder 内建 + 崩溃转储 L2 封装)'; EnvVar = 'QT_SRC_KERNEL'
        Hint = '原机在 WSL 里:~/work/qtrade-redroid-installer/kernel/out/bzImage(现役 v4);用 -SourceRoot 或 QT_SRC_KERNEL 指到 Windows 可见的路径' }

    @{ Dest = 'wsl/kcheck-rootfs.tar'; Sources = @('rootfs/out/kcheck-rootfs.tar'); Kind = 'file'; Critical = $true; Packed = $false
        Purpose = '内核验证用微型发行版(busybox),验证后注销'; EnvVar = 'QT_SRC_KCHECK'
        Hint = 'busybox 微型 rootfs,~3 MB;/etc/wsl.conf 里要关 systemd/interop/automount(§2.6.1)' }

    @{ Dest = 'wsl/wsl.msi'; Sources = @('third_party/wsl/wsl.msi'); Kind = 'file'; Critical = $true; Packed = $true
        Purpose = 'WSL 2.6.x 离线安装包(MSI)'; EnvVar = 'QT_SRC_WSL_MSI'
        Hint = '微软官方 WSL release 的 .msi;§2.5.2 要求装完 wsl --version ≥ 2.4.0' }

    @{ Dest = 'wsl/rootfs.tar'; Sources = @('rootfs/out/rootfs.tar'); Kind = 'file'; Critical = $true; Packed = $false
        Purpose = '发行版 qtrade:Ubuntu 22.04 + docker + 预载镜像 + Agent'; EnvVar = 'QT_SRC_ROOTFS'
        Hint = '原机在 WSL 里:~/work/qtrade-redroid-installer/rootfs/(Dockerfile 产出)' }

    @{ Dest = 'wsl/agent'; Sources = @('dist'); Kind = 'glob'; Pattern = 'qtrade_agent-*.whl'; Critical = $false; Packed = $false
        Purpose = 'Agent wheel(rootfs 构建输入;真值登记在 rootfs_contents.agent_wheel,G-10)'; EnvVar = 'QT_SRC_AGENT_WHEEL'
        Hint = '仓库根跑 `python -m build`(或 `pip wheel . -w dist`)出 dist/qtrade_agent-*.whl' }

    @{ Dest = 'pkg/adb'; Sources = @('third_party/platform-tools'); Kind = 'tree'; Critical = $false; Packed = $false
        Purpose = 'platform-tools(adb.exe,排障用,固定 -P 16000)'; EnvVar = 'QT_SRC_ADB'
        Hint = '🔴 G-11:必须与 rootfs 内 /opt/qtrade/platform-tools/adb **同一 platform-tools 版本**,否则 adb server 会被对方杀掉、企点账号集体 offline(docs/04 §2.7.4)' }

    @{ Dest = 'pkg/scrcpy'; Sources = @('third_party/scrcpy'); Kind = 'tree'; Critical = $false; Packed = $false
        Purpose = 'scrcpy(Windows 侧客户端,排障用)'; EnvVar = 'QT_SRC_SCRCPY'
        Hint = '客户端与 rootfs 内的 scrcpy-server 必须同版本(协议按版本严格匹配)' }

    @{ Dest = 'pkg/chatlog'; Sources = @('third_party/chatlog'); Kind = 'tree'; Critical = $false; Packed = $false
        Purpose = 'chatlog_alpha + wx_key 取钥 DLL(wx_key1.dll / wx_key2.dll)'; EnvVar = 'QT_SRC_CHATLOG'
        Hint = '原机在 /mnt/c/Users/anlin/Desktop/盈米/蜂鸟项目/南银理财/weChatlog/(SKILL §3「微信取钥」行)' }

    @{ Dest = 'pkg/wechat/weixin_4.1.12.26.exe'; Sources = @('third_party/wechat/weixin_4.1.12.26.exe'); Kind = 'file'; Critical = $true; Packed = $true
        Purpose = '随包微信 PC 安装包(B-1;sha256 已实测钉死,不一致即 PAYLOAD_CORRUPT)'; EnvVar = 'QT_SRC_WECHAT'
        PinnedSha256 = '58997cfe4513ab71f107c2137bb570ade030f228115c14688544cec80e604053'
        Hint = '🔴 R2-6:另一候选包 WeChatWin_4.1.12.exe 装出来是 4.1.12.55,两包外层 VersionInfo **完全相同**,只能靠 sha256 分辨;官方 CDN 只有当前版,历史版本走 B-1 的法务确认渠道' }

    @{ Dest = 'pkg/vcredist/VC_redist.x64.exe'; Sources = @('third_party/vcredist/VC_redist.x64.exe'); Kind = 'file'; Critical = $false; Packed = $true
        Purpose = 'VC++ 2015-2022 运行库(wx_key.dll/chatlog 依赖 MSVCP140/VCRUNTIME140)'; EnvVar = 'QT_SRC_VCREDIST'
        Hint = '微软官方 VC_redist.x64.exe' }

    @{ Dest = 'winagent/python'; Sources = @('winagent/dist/python'); Kind = 'tree'; Critical = $false; Packed = $false
        Purpose = 'Python 3.12 嵌入式运行时 + WinAgent 依赖'; EnvVar = 'QT_SRC_WA_PYTHON'
        Hint = '走 PyInstaller 单目录路线时运行时已在 app/_internal 里,本项可缺(§2.2.1 未标 critical)' }

    @{ Dest = 'winagent/app'; Sources = @('winagent/dist/qtrade-winagent-svc', 'winagent/dist/qtrade-winagent-user'); Kind = 'tree'; Critical = $false; Packed = $false
        Purpose = 'WinAgent:qtrade-winagent-svc.exe(服务)+ qtrade-winagent-user.exe(会话代理),R-14 实名'; EnvVar = 'QT_SRC_WA_APP'
        Hint = '在 winagent/ 跑 build/build.ps1(PyInstaller 单目录),出 dist/qtrade-winagent-svc/ 与 dist/qtrade-winagent-user/;本脚本把两个目录**合并**进 winagent/app/' }

    @{ Dest = 'console'; Sources = @('console/release/win-unpacked'); Kind = 'tree'; Critical = $false; Packed = $false
        Purpose = 'QTrade Console(Electron,electron-builder --dir 产物)'; EnvVar = 'QT_SRC_CONSOLE'
        Hint = '在 console/ 跑 `npm run build:dir`(electron-builder --dir;electron-builder.yml 的 directories.output=release)' }
)

# 🔴 R6-38:manifest 里 kernel 的 coredump_l2 恒为 "D",其它值构建失败(§2.2.1 / §2.6.9)
$CoredumpL2 = 'D'

function Resolve-QtSource {
    param([hashtable] $Item, [string] $Rel)
    $env0 = ''
    if ($Item.ContainsKey('EnvVar')) { $env0 = [Environment]::GetEnvironmentVariable($Item.EnvVar) }
    if ($env0) { return $env0 }
    if ($SourceRoot) { return [IO.Path]::Combine($SourceRoot, ($Rel -replace '/', '\')) }
    return ($Rel -replace '/', '\')
}

function Copy-QtPayloadItem {
    <#
        Kind:
          file —— 单文件
          tree —— 目录内容;**Sources 可以给多个**,依次覆盖合并到同一 Dest
                  (WinAgent 的两个 PyInstaller 单目录产物就是这么合的;共享运行时文件内容相同)
          glob —— 从目录里按 Pattern 取匹配的文件(Agent wheel 的版本号不定)
    #>
    param([hashtable] $Item, [string] $StageRoot)
    $dst = [IO.Path]::Combine($StageRoot, ($Item.Dest -replace '/', '\'))
    $found = @()
    $tried = @()

    foreach ($rel in @($Item.Sources)) {
        $src = Resolve-QtSource -Item $Item -Rel $rel
        $tried += $src
        if (-not (Test-Path -LiteralPath $src)) { continue }
        switch ($Item.Kind) {
            'file' {
                New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
                Copy-Item -LiteralPath $src -Destination $dst -Force
                $found += $src
            }
            'glob' {
                $hits = @(Get-ChildItem -LiteralPath $src -Filter $Item.Pattern -File -ErrorAction SilentlyContinue)
                if ($hits.Count -eq 0) { continue }
                New-Item -ItemType Directory -Path $dst -Force | Out-Null
                foreach ($h in $hits) { Copy-Item -LiteralPath $h.FullName -Destination $dst -Force }
                $found += $src
            }
            default {
                # ⚠️ 不能写 `Copy-Item -LiteralPath "<dir>\*"` —— -LiteralPath **不做通配展开**,
                #    那条路径字面上不存在,结果是「一声不吭什么都没拷」(目录建了、里面是空的)。
                #    逐个子项拷,既能正确展开,又保留 -LiteralPath 对特殊字符的安全性。
                New-Item -ItemType Directory -Path $dst -Force | Out-Null
                $children = @(Get-ChildItem -LiteralPath $src -Force -ErrorAction SilentlyContinue)
                if ($children.Count -eq 0) { continue }
                foreach ($c in $children) {
                    Copy-Item -LiteralPath $c.FullName -Destination $dst -Recurse -Force
                }
                $found += $src
            }
        }
    }
    return [pscustomobject]@{ Ok = ($found.Count -gt 0); Sources = $found; Tried = $tried; Dest = $dst }
}

function Get-QtSha256 {
    param([string] $Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-QtKernelVersionString {
    <#
        §2.6.8 第 4 条:manifest 的 `version` 由 CI 从 `file bzImage` 读出**逐字**写入,人不手填。
        Windows 上没有 `file`,退而求其次:从 bzImage 里搜内核版本串。
        K4 护栏:版本串必须以 `-binder` 结尾且**不含 `+`**。
    #>
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $bytes = [IO.File]::ReadAllBytes($Path)
    $text = [Text.Encoding]::ASCII.GetString($bytes)
    $m = [regex]::Match($text, '\b\d+\.\d+\.\d+(?:\.\d+)?-[A-Za-z0-9.\-]*binder\+?\b')
    if (-not $m.Success) { return '' }
    return $m.Value
}

# ── 主流程 ──────────────────────────────────────────────────────────────────
if ($Clean -and (Test-Path -LiteralPath $Stage)) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

$missing = @()
$copied = @()
foreach ($item in $PayloadMap) {
    $r = Copy-QtPayloadItem -Item $item -StageRoot $Stage
    if ($r.Ok) {
        $copied += [pscustomobject]@{ Item = $item; Dest = $r.Dest }
        Write-Host ('  [OK]   {0,-40} <- {1}' -f $item.Dest, ($r.Sources -join ' + '))
    } else {
        $hint = ''
        if ($item.ContainsKey('Hint')) { $hint = [string]$item.Hint }
        $missing += [pscustomobject]@{ Dest = $item.Dest; Tried = $r.Tried; Critical = $item.Critical; Purpose = $item.Purpose; Hint = $hint }
        Write-Host ('  [缺件] {0,-40} <- {1}' -f $item.Dest, ($r.Tried -join ' | ')) -ForegroundColor Yellow
    }
}

if ($missing.Count -gt 0 -and -not $AllowMissing) {
    Write-Host ''
    Write-Host '载荷缺件,拒绝出包。补齐后重跑,或加 -AllowMissing 出一个只能验流程的「轻量验证包」。' -ForegroundColor Red
    Write-Host ''
    foreach ($m in $missing) {
        Write-Host ('  ● {0}{1}' -f $m.Dest, $(if ($m.Critical) { '   [critical:缺它装不起来]' } else { '' })) -ForegroundColor Red
        Write-Host ('      用途     : {0}' -f $m.Purpose)
        Write-Host ('      找过这些 : {0}' -f ($m.Tried -join ' | '))
        if ($m.Hint) { Write-Host ('      怎么拿   : {0}' -f $m.Hint) -ForegroundColor DarkGray }
        Write-Host ''
    }
    throw ('载荷缺 {0} 项(见上面逐项说明)' -f $missing.Count)
}

# ── 生成 manifest.json(§2.2.1)────────────────────────────────────────────
$files = @()
foreach ($c in $copied) {
    $item = $c.Item
    $entry = [ordered]@{
        path    = $item.Dest
        dest    = ('%ProgramData%\QTrade\' + ($item.Dest -replace '/', '\'))
        purpose = $item.Purpose
    }
    if ($item.Kind -eq 'file') {
        $entry['sha256'] = Get-QtSha256 -Path $c.Dest
        $entry['size'] = [long](Get-Item -LiteralPath $c.Dest).Length
        # B-1 / R2-6:随包微信必须与钉死的 sha256 一致,否则会装成 4.1.12.55(两包外层 VersionInfo 完全相同)
        if ($item.ContainsKey('PinnedSha256') -and $entry['sha256'] -ne $item.PinnedSha256) {
            throw ('随包微信 sha256 不符(期望 {0},实得 {1});两候选包外层 VersionInfo 相同,只能靠 sha256 分辨(R2-6)' -f $item.PinnedSha256, $entry['sha256'])
        }
    } else {
        # 目录/通配条目在 manifest 里按 `<dest>/*` 登记(§2.2.1 的 `pkg/adb/*` 写法)
        $entry['path'] = $item.Dest + '/*'
    }
    if ($item.Critical) { $entry['critical'] = $true }
    if ($item.Dest -eq 'kernel/bzImage-6.6') {
        $ver = Get-QtKernelVersionString -Path $c.Dest
        # K4:版本串必须以 -binder 结尾且不含 `+`(源码目录无 git tag 时 setlocalversion 会追加 `+`)
        if ($ver -and ($ver.Contains('+') -or -not $ver.EndsWith('binder'))) {
            throw ('内核版本串不合法:{0}(K4:必须以 -binder 结尾、不含 `+`;build-kernel.sh 要固定 LOCALVERSION=)' -f $ver)
        }
        $entry['version'] = $ver
        $entry['coredump_l2'] = $CoredumpL2      # 🔴 R6-38:恒 "D"
    }
    $files += $entry
}

# 引擎自身(由 build.ps1 编译后放进 stage 再调本脚本的 -Rescan,或此处若已存在则登记)
$enginePath = Join-Path $Stage 'install\engine\qtrade-setup-engine.exe'
if (Test-Path -LiteralPath $enginePath) {
    $files += [ordered]@{
        path    = 'install/engine/qtrade-setup-engine.exe'
        dest    = '%ProgramData%\QTrade\install\engine\qtrade-setup-engine.exe'
        purpose = '无载荷引擎(续跑/升级/修复/卸载)'
        version = $PackageVersion
        sha256  = (Get-QtSha256 -Path $enginePath)
        size    = [long](Get-Item -LiteralPath $enginePath).Length
    }
}

$manifest = [ordered]@{
    package_version        = $PackageVersion
    built_at               = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    kernel_lines           = [ordered]@{ '6.6' = 'kernel/bzImage-6.6' }
    files                  = $files
    rootfs_contents        = [ordered]@{}   # 由 rootfs 构建写入 /etc/qtrade/contents.json,CI 在此登记同一份(G-10)
    wechat_version_matrix  = @(
        [ordered]@{ version = '4.1.12.26'; dll = 'wx_key2.dll'; status = 'verified'; source = 'bundled' }
        [ordered]@{ version = '4.1.11.52'; dll = 'wx_key1.dll'; status = 'failed'; source = 'bundled' }
        [ordered]@{ version = '4.1.13.12'; dll = ''; status = 'unknown'; source = 'bundled' }
    )
    probe_targets_default  = [ordered]@{}   # 04 定义的 probe_target 默认地址,现场可改
    schema_breaking        = $false         # A-5:CI 把本次升级跨越的迁移做 OR 写进来
    lightweight            = [bool]$AllowMissing
    missing                = @($missing | ForEach-Object { $_.Dest })
}

$manifestPath = Join-Path $Stage 'install\manifest.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $manifestPath) -Force | Out-Null
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))

Write-Host ''
Write-Host ('manifest 已生成:{0}({1} 个条目{2})' -f $manifestPath, $files.Count, $(if ($AllowMissing -and $missing.Count -gt 0) { ('、缺 ' + $missing.Count + ' 项 = 轻量验证包') } else { '' }))

# 供 build.ps1 读:哪些是**已压缩件**(单独非 solid 块,§2.2.2)
# ⚠️ 写成**每行一条的纯文本**而不是 JSON —— PowerShell 5.1 的 ConvertFrom-Json 对「顶层是数组」
#    的回值会把整个数组当一个对象传下去,读回来是嵌套数组,Where-Object 里直接类型错。
$packed = @($PayloadMap | Where-Object { $_.Packed } | ForEach-Object { $_.Dest })
[IO.File]::WriteAllLines((Join-Path $Stage 'install\.packed-entries.txt'),
    [string[]]$packed, (New-Object Text.UTF8Encoding($false)))

[pscustomobject]@{
    Stage        = $Stage
    ManifestPath = $manifestPath
    Copied       = $copied.Count
    Missing      = @($missing | ForEach-Object { $_.Dest })
    Lightweight  = [bool]$AllowMissing
}
