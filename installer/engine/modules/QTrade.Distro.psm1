# QTrade 安装引擎 —— 发行版同名冲突 / 首启 / IMAGES_LOADED
# 规格:docs/03 §2.7.1(同名冲突四情形)、§2.7.2(导入)、§2.7.3(首启双 unit、docker 选段、install.env)、
#       §2.2.1(rootfs_contents G-10 复核)、§2.3(IMAGES_LOADED 判据与失败码)、验收 M1-12/M1-13
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Wsl.psm1') -DisableNameChecking

$script:QtDistroName = 'qtrade'
$script:QtAgentBase = 'http://127.0.0.1:17600'
# docs/03 §2.2.1 rootfs_contents 里的两个镜像键(G-10:与发行版内 /etc/qtrade/contents.json 逐字一致)
$script:QtImageKeys = @('redroid_image', 'napcat_image')

function Get-QtDistroName { [CmdletBinding()][OutputType([string])] param() return $script:QtDistroName }
function Get-QtAgentBaseUrl { [CmdletBinding()][OutputType([string])] param() return $script:QtAgentBase }

function Resolve-QtDistroConflict {
    <#
    .SYNOPSIS
        docs/03 §2.7.1 的四情形判定。**纯函数**:外部事实(注册表 BasePath、`.imported` 内容)由调用方采好。
    .PARAMETER HasStateRecord
        `install_state` 里有没有本机装过 qtrade 的记录。
    .PARAMETER BasePath / ExpectedBasePath
        注册表里 `qtrade` 的 BasePath;期望值 = `%ProgramData%\QTrade\wsl\distro`。
    .PARAMETER ImportedRootfsVersion / PackageRootfsVersion
        发行版内 `/etc/qtrade/.imported` 的 `rootfs_version` 与本包的。取不到传 ''。
    .OUTPUTS
        {Kind, Reason}
        Kind ∈ NOT_PRESENT(没有同名,直接导入)
             | REUSE(修复/重跑 → 跳过导入,幂等)
             | UPGRADE(走 §2.13 升级路径,**不在首装流程里静默换 rootfs**)
             | FOREIGN(残留/他人同名 → **停下问用户**)
    #>
    [CmdletBinding()]
    param(
        [bool] $Exists = $false,
        [bool] $HasStateRecord = $false,
        [AllowEmptyString()][string] $BasePath = '',
        [AllowEmptyString()][string] $ExpectedBasePath = '',
        [AllowEmptyString()][string] $ImportedRootfsVersion = '',
        [AllowEmptyString()][string] $PackageRootfsVersion = ''
    )
    if (-not $Exists) { return [pscustomobject]@{ Kind = 'NOT_PRESENT'; Reason = '' } }

    $basePathOk = $false
    if ($BasePath -and $ExpectedBasePath) {
        $basePathOk = ($BasePath.TrimEnd('\').ToLowerInvariant() -eq $ExpectedBasePath.TrimEnd('\').ToLowerInvariant())
    }
    # 「install_state 无记录 / BasePath 不在我们的目录 / 发行版里没有 .imported」任一成立 ⇒ 残留或他人同名
    if (-not $HasStateRecord) { return [pscustomobject]@{ Kind = 'FOREIGN'; Reason = 'no_install_state' } }
    if (-not $basePathOk) { return [pscustomobject]@{ Kind = 'FOREIGN'; Reason = 'base_path_mismatch' } }
    if ([string]::IsNullOrWhiteSpace($ImportedRootfsVersion)) { return [pscustomobject]@{ Kind = 'FOREIGN'; Reason = 'no_imported_marker' } }

    if ($ImportedRootfsVersion -eq $PackageRootfsVersion) { return [pscustomobject]@{ Kind = 'REUSE'; Reason = '' } }
    return [pscustomobject]@{ Kind = 'UPGRADE'; Reason = 'rootfs_version_older' }
}

function Read-QtImportedMarker {
    <#
    .SYNOPSIS
        读发行版内 `/etc/qtrade/.imported`(JSON:`{package_version, rootfs_version, imported_at}`)。
        读不到回 $null —— 那是 §2.7.1 判 FOREIGN 的依据之一,不是错误。
    #>
    [CmdletBinding()]
    param([string] $DistroName = 'qtrade', [int] $TimeoutSec = 60)
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'cat', '/etc/qtrade/.imported') -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($r.StdOut)) { return $null }
    try { return ($r.StdOut | ConvertFrom-Json) } catch { return $null }
}

function Export-QtDistroBackup {
    <#
    .SYNOPSIS
        §2.7.1 FOREIGN 情形的【注销它并重新导入】:**先 `wsl --export` 再 `--unregister`**;
        🔴 导出失败则**不注销**(验收 M1-13:备份 tar 必须能再 `--import` 回去)。
    .OUTPUTS
        {Ok, TarPath}
    #>
    [CmdletBinding()]
    param(
        [string] $DistroName = 'qtrade',
        [Parameter(Mandatory)][string] $WslDir,
        [string] $Stamp,
        [int] $TimeoutSec = 3600
    )
    if (-not $Stamp) { $Stamp = Get-QtTimestamp }
    New-QtDirectory -Path $WslDir | Out-Null
    $tar = Join-QtPath -Path $WslDir -ChildPath ('backup-{0}-{1}.tar' -f $DistroName, $Stamp)
    $r = Invoke-QtWsl -WslArgs @('--export', $DistroName, $tar) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0 -or -not (Test-QtPath -Path $tar)) {
        return [pscustomobject]@{ Ok = $false; TarPath = '' }
    }
    return [pscustomobject]@{ Ok = $true; TarPath = $tar }
}

function Write-QtInstallEnv {
    <#
    .SYNOPSIS
        §2.7.3:选定的 docker 段等经 `/mnt/c/ProgramData/QTrade/install/install.env` 传进发行版,
        首启 oneshot `qtrade-docker-config.service`(`Before=docker.service`)据此写 `/etc/docker/daemon.json`。
        🔴 R5-9:本文件必须在**首起 dockerd 之前**就位,否则默认 `172.17` 已被占、选段白做。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $DockerCidr,
        [AllowEmptyString()][string] $ApkUrl = ''
    )
    $bip = ''
    if ($DockerCidr -match '^(\d+)\.(\d+)\.') { $bip = ('{0}.{1}.0.1/24' -f $Matches[1], $Matches[2]) }
    $lines = @(
        '# 由安装引擎生成,首启 oneshot 读它(docs/03 §2.7.3);改这里不影响已建好的 docker 网络'
        ('QTRADE_DOCKER_POOL_BASE={0}' -f $DockerCidr)
        ('QTRADE_DOCKER_BIP={0}' -f $bip)
        ('QTRADE_APK_URL={0}' -f $ApkUrl)
    )
    New-QtDirectory -Path (Split-Path -Parent $Path) | Out-Null
    # LF 换行:这份文件是给发行版里的 shell 读的
    [IO.File]::WriteAllText($Path, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
    return $Path
}

function Get-QtDockerImages {
    <#
    .SYNOPSIS
        `docker images --format '{{.Repository}}:{{.Tag}}'`(§2.15)。
    #>
    [CmdletBinding()][OutputType([string[]])]
    param([string] $DistroName = 'qtrade', [int] $TimeoutSec = 120)
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'images', '--format', '{{.Repository}}:{{.Tag}}') -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return , @() }
    return , @(($r.StdOut -split "`r?`n") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_.Trim() })
}

function Test-QtImageRefsPresent {
    <#
    .SYNOPSIS
        纯函数:`docker images` 输出里是否两个镜像都在(§2.3 IMAGES_LOADED 判据)。
        manifest 里的 ref 形如 `redroid/redroid:11.0.0-latest`。
    #>
    [CmdletBinding()][OutputType([bool])]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Present,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Expected
    )
    if ($Expected.Count -eq 0) { return $false }
    foreach ($e in $Expected) {
        if ($Present -notcontains $e) { return $false }
    }
    return $true
}

function Get-QtExpectedImageRefs {
    <#
    .SYNOPSIS
        从 manifest 的 `rootfs_contents` 取两个镜像的 ref(G-10)。
    #>
    [CmdletBinding()][OutputType([string[]])]
    param([Parameter(Mandatory)] $Manifest)
    $refs = @()
    if (-not (Test-QtHasProperty -Object $Manifest -Name 'rootfs_contents')) { return , $refs }
    foreach ($k in $script:QtImageKeys) {
        $node = $Manifest.rootfs_contents
        if ((Test-QtHasProperty -Object $node -Name $k) -and (Test-QtHasProperty -Object $node.$k -Name 'ref')) {
            $refs += [string]$node.$k.ref
        }
    }
    return , $refs
}

function Invoke-QtRootfsContentsVerify {
    <#
    .SYNOPSIS
        §2.2.1 G-10:`IMAGES_LOADED` 步在发行版内复核 `rootfs_contents`
        —— `sha256sum` 各路径、`docker images --digests` 比对 digest;不一致 → `IMAGE_LOAD_FAILED`(附不一致项)。
    .OUTPUTS
        {Ok, Mismatched[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Manifest,
        [string] $DistroName = 'qtrade',
        [int] $TimeoutSec = 600
    )
    $mismatched = @()
    if (-not (Test-QtHasProperty -Object $Manifest -Name 'rootfs_contents')) {
        return [pscustomobject]@{ Ok = $true; Mismatched = $mismatched }   # 轻量包没有这一节
    }
    $contents = $Manifest.rootfs_contents
    foreach ($name in @($contents.PSObject.Properties | ForEach-Object { $_.Name })) {
        $node = $contents.$name
        if ((Test-QtHasProperty -Object $node -Name 'path') -and (Test-QtHasProperty -Object $node -Name 'sha256') -and ([string]$node.sha256) -notlike '*…*') {
            $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sha256sum', [string]$node.path) -TimeoutSec $TimeoutSec
            $actual = ''
            if (-not $r.TimedOut -and $r.ExitCode -eq 0) { $actual = (($r.StdOut).Trim() -split '\s+')[0] }
            if ($actual -ne ([string]$node.sha256).ToLowerInvariant()) {
                $mismatched += [pscustomobject]@{ item = $name; expected = [string]$node.sha256; actual = $actual }
            }
        }
        if ((Test-QtHasProperty -Object $node -Name 'ref') -and (Test-QtHasProperty -Object $node -Name 'digest') -and ([string]$node.digest) -notlike '*…*') {
            $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'images', '--digests', '--format', '{{.Repository}}:{{.Tag}} {{.Digest}}') -TimeoutSec $TimeoutSec
            $hit = @(($r.StdOut -split "`r?`n") | Where-Object { $_ -like (([string]$node.ref) + ' *') })
            $actual = ''
            if ($hit.Count -gt 0) { $actual = (($hit[0].Trim() -split '\s+')[-1]) }
            if ($actual -ne [string]$node.digest) {
                $mismatched += [pscustomobject]@{ item = $name; expected = [string]$node.digest; actual = $actual }
            }
        }
    }
    return [pscustomobject]@{ Ok = ($mismatched.Count -eq 0); Mismatched = $mismatched }
}

function Invoke-QtDockerLoadImages {
    <#
    .SYNOPSIS
        §2.13 第 6 步:**新镜像 tar 变了 → `docker load`**。

        🔴 **旧镜像保留** —— 账号容器按 `account_runtime` 记的镜像 tag 起,
        逐账号升级由 **Agent** 做、**不在安装器里**(§2.13 第 6 步原文)。
        所以本函数**绝不** `docker rmi` / `image prune`:把旧 tag 删了,正在跑的账号下次起不来。
    .PARAMETER TarOverrideDir
        载荷侧若另带了镜像 tar(`wsl\images\*.tar`),优先用它;否则用 manifest
        `rootfs_contents.*.tar` 里记的**发行版内**路径。
    .OUTPUTS
        {Ok, Loaded[{item, ref, tar}], Failed[{item, ref, tar, reason}]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Manifest,
        [string] $DistroName = 'qtrade',
        [AllowEmptyString()][string] $TarOverrideDir = '',
        [int] $TimeoutSec = 1800
    )
    $loaded = @()
    $failed = @()
    if (-not (Test-QtHasProperty -Object $Manifest -Name 'rootfs_contents')) {
        return [pscustomobject]@{ Ok = $true; Loaded = $loaded; Failed = $failed }
    }
    $contents = $Manifest.rootfs_contents
    foreach ($key in $script:QtImageKeys) {
        if (-not (Test-QtHasProperty -Object $contents -Name $key)) { continue }
        $node = $contents.$key
        if (-not (Test-QtHasProperty -Object $node -Name 'tar')) { continue }
        $ref = ''
        if (Test-QtHasProperty -Object $node -Name 'ref') { $ref = [string]$node.ref }
        $tar = [string]$node.tar

        # 载荷侧覆盖件(若有)先推进发行版再 load
        if ($TarOverrideDir) {
            $leaf = Split-Path -Leaf $tar
            $override = Join-QtPath -Path $TarOverrideDir -ChildPath $leaf
            if (Test-QtPath -Path $override) { $tar = ConvertTo-QtWslPath -WindowsPath $override }
        }

        $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'load', '-i', $tar) -TimeoutSec $TimeoutSec
        if ($r.TimedOut -or $r.ExitCode -ne 0) {
            $failed += [pscustomobject]@{ item = $key; ref = $ref; tar = $tar; reason = (($r.StdErr + ' ' + $r.StdOut).Trim()) }
            continue
        }
        # load 完确认 ref 真的在了 —— `docker load` 退出 0 不等于你要的那个 tag 进来了
        if ($ref) {
            $present = Get-QtDockerImages -DistroName $DistroName
            if ($present -notcontains $ref) {
                $failed += [pscustomobject]@{ item = $key; ref = $ref; tar = $tar; reason = 'load 成功但 docker images 里没有该 ref' }
                continue
            }
        }
        $loaded += [pscustomobject]@{ item = $key; ref = $ref; tar = $tar }
    }
    return [pscustomobject]@{ Ok = ($failed.Count -eq 0); Loaded = $loaded; Failed = $failed }
}

function Get-QtAgentHealth {
    <#
    .SYNOPSIS
        `GET /api/v1/system/health`(免鉴权,C-33)。经 localhostForwarding 从 Windows 侧访问。
    .OUTPUTS
        {Ok, StatusCode, Docker, Db, WinAgentReachable, Version, Raw}
    #>
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17600', [int] $TimeoutSec = 10)
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/api/v1/system/health') -Method GET -TimeoutSec $TimeoutSec
    $docker = ''; $db = ''; $wa = $false; $ver = ''
    if ($r.Ok -and $r.Body) {
        try {
            $j = $r.Body | ConvertFrom-Json
            if (Test-QtHasProperty -Object $j -Name 'docker') { $docker = [string]$j.docker }
            if (Test-QtHasProperty -Object $j -Name 'db') { $db = [string]$j.db }
            if (Test-QtHasProperty -Object $j -Name 'winagent_reachable') { $wa = [bool]$j.winagent_reachable }
            if (Test-QtHasProperty -Object $j -Name 'version') { $ver = [string]$j.version }
        } catch { }
    }
    return [pscustomobject]@{
        Ok                = ($r.Ok -and $r.StatusCode -eq 200)
        StatusCode        = $r.StatusCode
        Docker            = $docker
        Db                = $db
        WinAgentReachable = $wa
        Version           = $ver
        Raw               = $r.Body
    }
}

function Test-QtAgentActive {
    <#
    .SYNOPSIS
        `systemctl is-active qtrade-agent` = active(§2.3 / §2.15)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([string] $DistroName = 'qtrade', [int] $TimeoutSec = 60)
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--exec', 'systemctl', 'is-active', 'qtrade-agent') -TimeoutSec $TimeoutSec
    return ((-not $r.TimedOut) -and ($r.StdOut).Trim() -eq 'active')
}

function Get-QtDockerBridgeSubnet {
    <#
    .SYNOPSIS
        验收 M1-12(R5-9):`docker network inspect bridge` 的 subnet 必须 ⊂ `env.docker_cidr` 且**不是** `172.17.0.0/16`
        —— 这是「选段 + daemon.json 在 dockerd **首启前**生效、默认段从未被创建」的唯一证明。
    #>
    [CmdletBinding()][OutputType([string])]
    param([string] $DistroName = 'qtrade', [int] $TimeoutSec = 120)
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'network', 'inspect', 'bridge',
        '--format', '{{range .IPAM.Config}}{{.Subnet}}{{end}}') -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return '' }
    return ($r.StdOut).Trim()
}

function Test-QtDockerPoolApplied {
    <#
    .SYNOPSIS
        纯函数:bridge 子网是否落在选定段内、且不是默认 `172.17.0.0/16`(M1-12)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param(
        [AllowEmptyString()][string] $BridgeSubnet,
        [AllowEmptyString()][string] $SelectedCidr
    )
    if ([string]::IsNullOrWhiteSpace($BridgeSubnet) -or [string]::IsNullOrWhiteSpace($SelectedCidr)) { return $false }
    if ($BridgeSubnet.Trim() -eq '172.17.0.0/16') { return $false }
    return (Test-QtCidrOverlap -A $BridgeSubnet -B $SelectedCidr)
}

function Get-QtContainerLogsTail {
    <#
    .SYNOPSIS
        §2.7.3 / §2.11:失败时附 `docker logs` 前 N 行(`up.sh` 做法,默认 40)。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Container,
        [int] $Lines = 40,
        [string] $DistroName = 'qtrade',
        [int] $TimeoutSec = 60
    )
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'logs', '--tail', [string]$Lines, $Container) -TimeoutSec $TimeoutSec
    return (($r.StdOut + "`n" + $r.StdErr).Trim())
}

function Test-QtImagesLoaded {
    <#
    .SYNOPSIS
        IMAGES_LOADED 幂等判据(§2.3):`docker images` 两镜像存在 **且** `qtrade-agent` active。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    $expected = @()
    if (Test-QtHasProperty -Object $Context -Name 'ExpectedImages') { $expected = @($Context.ExpectedImages) }
    if ($expected.Count -eq 0) { return $false }
    $present = Get-QtDockerImages -DistroName ([string]$Context.DistroName)
    if (-not (Test-QtImageRefsPresent -Present $present -Expected $expected)) { return $false }
    return (Test-QtAgentActive -DistroName ([string]$Context.DistroName))
}

function Test-QtDistroImported {
    <#
    .SYNOPSIS
        DISTRO_IMPORTED 幂等判据(§2.3):`wsl -l` 有 `qtrade` 且 `/etc/qtrade/.imported` 版本 == 本包。
        🔴 R5-9:`.imported` 是**后置证明**,不可反当选段的前置闸门。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    $l = Invoke-QtWsl -WslArgs @('--list', '--quiet') -TimeoutSec 60
    if ($l.StdOut -notmatch ('(?m)^\s*' + [regex]::Escape([string]$Context.DistroName) + '\s*$')) { return $false }
    $marker = Read-QtImportedMarker -DistroName ([string]$Context.DistroName)
    if ($null -eq $marker) { return $false }
    if (-not (Test-QtHasProperty -Object $marker -Name 'rootfs_version')) { return $false }
    return ([string]$marker.rootfs_version -eq [string]$Context.RootfsVersion)
}

Register-QtStepCheck -Step 'DISTRO_IMPORTED' -Check { param($ctx) Test-QtDistroImported -Context $ctx }
Register-QtStepCheck -Step 'IMAGES_LOADED' -Check { param($ctx) Test-QtImagesLoaded -Context $ctx }

Export-ModuleMember -Function Get-QtDistroName, Get-QtAgentBaseUrl, Resolve-QtDistroConflict,
Read-QtImportedMarker, Export-QtDistroBackup, Write-QtInstallEnv, Get-QtDockerImages,
Test-QtImageRefsPresent, Get-QtExpectedImageRefs, Invoke-QtRootfsContentsVerify,
Invoke-QtDockerLoadImages, Get-QtAgentHealth,
Test-QtAgentActive, Get-QtDockerBridgeSubnet, Test-QtDockerPoolApplied, Get-QtContainerLogsTail,
Test-QtImagesLoaded, Test-QtDistroImported
