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
# docs/03 §2.2.1 rootfs_contents 里的两个镜像键(G-10:与发行版内 /etc/qtrade/contents.json 逐字一致)
$script:QtImageKeys = @('redroid_image', 'napcat_image')

function Get-QtDistroName { [CmdletBinding()][OutputType([string])] param() return $script:QtDistroName }
function Get-QtAgentBaseUrl {
    [CmdletBinding()][OutputType([string])]
    param([string] $DistroName = 'qtrade', [int] $TimeoutSec = 30)
    if ($DistroName -cne $script:QtDistroName) { return '' }
    # The route lookup sends no packets. Use its source, never the host gateway.
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'ip', '-j', '-4', 'route', 'get', '1.1.1.1') -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return '' }
    try {
        # PS 5.1 emits a JSON array as one pipeline object. Unwrap after parsing.
        $parsed = $r.StdOut | ConvertFrom-Json
        if ($parsed -isnot [array]) { return '' }
        $routes = @($parsed)
        if ($routes.Count -ne 1) { return '' }
        $source = ''
        foreach ($key in @('prefsrc', 'src')) {
            if (Test-QtHasProperty -Object $routes[0] -Name $key) { $source = [string]$routes[0].$key; break }
        }
        $ip = $null
        if (-not [Net.IPAddress]::TryParse($source, [ref]$ip)) { return '' }
        if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $ip.ToString() -cne $source) { return '' }
        $b = $ip.GetAddressBytes()
        $private = ($b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or ($b[0] -eq 192 -and $b[1] -eq 168))
        if (-not $private) { return '' }
        return ('http://{0}:17600' -f $source)
    } catch { return '' }
}

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

function Get-QtDockerPoolDefinition {
    [CmdletBinding()]
    param([AllowEmptyString()][string] $Cidr)
    if ($Cidr -cnotmatch '^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d{1,2})$') { return $null }
    $address = $Cidr.Split('/')[0]
    $prefix = [int]$Cidr.Split('/')[1]
    $ip = $null
    if ($prefix -lt 8 -or $prefix -gt 24 -or -not [Net.IPAddress]::TryParse($address, [ref]$ip)) { return $null }
    if ($ip.ToString() -cne $address) { return $null }
    $b = $ip.GetAddressBytes()
    $private = ($b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31 -and $prefix -ge 12) -or ($b[0] -eq 192 -and $b[1] -eq 168 -and $prefix -ge 16))
    if (-not $private) { return $null }
    $value = [uint64]$b[0] * 16777216 + [uint64]$b[1] * 65536 + [uint64]$b[2] * 256 + [uint64]$b[3]
    $block = [uint64][Math]::Pow(2, (32 - $prefix))
    if ($value % $block -ne 0) { return $null }
    return [pscustomobject]@{ Cidr = ($address + '/' + $prefix); Bip = ('{0}.{1}.{2}.1/24' -f $b[0], $b[1], $b[2]) }
}

function ConvertFrom-QtInstallEnv {
    [CmdletBinding()]
    param([AllowEmptyString()][string] $Text, [Parameter(Mandatory)][string] $PackageVersion)
    $values = @{}
    foreach ($line in ($Text -split "`r?`n")) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -cnotmatch '^(QTRADE_DOCKER_POOL_BASE|QTRADE_DOCKER_BIP|QTRADE_PACKAGE_VERSION|QTRADE_APK_URL)=([^\x00-\x1f\x7f-\uffff]*)$') { return $null }
        $key = $Matches[1]
        if ($values.ContainsKey($key)) { return $null }
        $values[$key] = $Matches[2]
    }
    $pool = Get-QtDockerPoolDefinition -Cidr ([string]$values['QTRADE_DOCKER_POOL_BASE'])
    if ($null -eq $pool -or $values['QTRADE_DOCKER_BIP'] -cne $pool.Bip -or $values['QTRADE_PACKAGE_VERSION'] -cne $PackageVersion) { return $null }
    return [pscustomobject]@{ Cidr = $pool.Cidr; Bip = $pool.Bip; ApkUrl = [string]$values['QTRADE_APK_URL'] }
}

function Write-QtInstallEnv {
    # This local journal is copied through stdin before product units may start.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $DockerCidr,
        [AllowEmptyString()][string] $ApkUrl = '',
        [AllowEmptyString()][string] $PackageVersion = ''
    )
    $pool = Get-QtDockerPoolDefinition -Cidr $DockerCidr
    if ($null -eq $pool) { throw 'Invalid Docker pool.' }
    if ($PackageVersion -cmatch '[^A-Za-z0-9._+-]' -or $ApkUrl -cmatch '[\x00-\x20\x7f-\uffff]') { throw 'Invalid install environment value.' }
    $lines = @(
        '# Generated by the QTrade installer. Read as data, never source this file.'
        ('QTRADE_DOCKER_POOL_BASE={0}' -f $pool.Cidr)
        ('QTRADE_DOCKER_BIP={0}' -f $pool.Bip)
        ('QTRADE_PACKAGE_VERSION={0}' -f $PackageVersion)
        ('QTRADE_APK_URL={0}' -f $ApkUrl)
    )
    New-QtDirectory -Path (Split-Path -Parent $Path) | Out-Null
    # LF and ASCII are required by the guest reader.
    [IO.File]::WriteAllText($Path, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
    return $Path
}

function Initialize-QtDistroBootstrap {
    [CmdletBinding()]
    param(
        [string] $Name = 'qtrade',
        [Parameter(Mandatory)][string] $InstallEnvPath,
        [Parameter(Mandatory)][string] $PackageVersion,
        [AllowEmptyString()][string] $DockerCidr = '',
        [AllowEmptyString()][string] $ApkUrl = '',
        [string[]] $Candidates = @('10.213.0.0/16', '10.231.0.0/16', '10.247.0.0/16', '10.199.0.0/16'),
        [string[]] $OccupiedPrefixes = @()
    )
    $result = [pscustomobject]@{ Ok = $false; Reason = 'BOOTSTRAP_INVALID'; Cidr = ''; Systemd = $null }
    if ($Name -cne $script:QtDistroName -or $PackageVersion -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._+-]*$') { return $result }
    if ($DockerCidr -and $null -eq (Get-QtDockerPoolDefinition -Cidr $DockerCidr)) { return $result }
    # The caller must first prove ownership. This starts only that named distro.
    $boot = Invoke-QtWsl -WslArgs @('-d', $Name, '--user', 'root', '--exec', 'true') -TimeoutSec 180
    if ($boot.TimedOut -or $boot.ExitCode -ne 0) { $result.Reason = 'DISTRO_BOOT_FAILED'; return $result }
    $read = Invoke-QtWsl -WslArgs @('-d', $Name, '--user', 'root', '--exec', 'sh', '-c', 'if [ -e /etc/qtrade/install.env ]; then test ! -L /etc/qtrade/install.env && cat /etc/qtrade/install.env; else exit 44; fi') -TimeoutSec 60
    if ($read.TimedOut -or $read.ExitCode -notin @(0, 44)) { $result.Reason = 'INSTALL_ENV_READ_FAILED'; return $result }
    $guest = $null; $local = $null
    if ($read.ExitCode -eq 0) {
        $guest = ConvertFrom-QtInstallEnv -Text $read.StdOut -PackageVersion $PackageVersion
        if ($null -eq $guest) { $result.Reason = 'INSTALL_ENV_INVALID'; return $result }
    }
    try {
        if (Test-QtPath -Path $InstallEnvPath) {
            $local = ConvertFrom-QtInstallEnv -Text (Read-QtTextFile -Path $InstallEnvPath) -PackageVersion $PackageVersion
            if ($null -eq $local) { $result.Reason = 'INSTALL_ENV_INVALID'; return $result }
        }
    } catch { $result.Reason = 'INSTALL_ENV_READ_FAILED'; return $result }
    if ($null -ne $guest -and $null -ne $local -and ($guest.Cidr -cne $local.Cidr -or $guest.ApkUrl -cne $local.ApkUrl)) {
        $result.Reason = 'INSTALL_ENV_CONFLICT'; return $result
    }
    $existing = if ($null -ne $guest) { $guest } else { $local }
    if ($null -ne $existing) {
        if (($DockerCidr -and $DockerCidr -cne $existing.Cidr) -or ($ApkUrl -and $ApkUrl -cne $existing.ApkUrl)) {
            $result.Reason = 'INSTALL_ENV_CONFLICT'; return $result
        }
        $DockerCidr = $existing.Cidr; $ApkUrl = $existing.ApkUrl
    } else {
        $network = Get-QtOccupiedNetworkPrefixes
        if (-not $network.Ok) { $result.Reason = 'NETWORK_DISCOVERY_FAILED'; return $result }
        $occupied = @(@($network.Prefixes) + @($OccupiedPrefixes) | Where-Object { $_ -and $_ -ne '0.0.0.0/0' -and $_ -ne '::/0' } | Select-Object -Unique)
        $valid = @()
        foreach ($candidate in $(if ($DockerCidr) { @($DockerCidr) } else { @($Candidates) })) {
            $pool = Get-QtDockerPoolDefinition -Cidr $candidate
            if ($null -ne $pool) { $valid += $pool.Cidr }
        }
        if ($valid.Count -eq 0) { $result.Reason = 'DOCKER_CIDR_EXHAUSTED'; return $result }
        $pick = Select-QtDockerPool -Candidates $valid -OccupiedPrefixes $occupied
        if ($pick.AllConflict) { $result.Reason = 'DOCKER_CIDR_EXHAUSTED'; return $result }
        $DockerCidr = $pick.Cidr
    }
    try {
        if ($null -eq $local) { Write-QtInstallEnv -Path $InstallEnvPath -DockerCidr $DockerCidr -ApkUrl $ApkUrl -PackageVersion $PackageVersion | Out-Null }
        if ($null -eq $guest) {
            $text = Read-QtTextFile -Path $InstallEnvPath
            $copy = 'set -e; umask 077; test ! -L /etc/qtrade; mkdir -p /etc/qtrade; test ! -L /etc/qtrade/install.env; tmp=$(mktemp /etc/qtrade/install.env.XXXXXX); cat > "$tmp"; chmod 600 "$tmp"; chown root:root "$tmp"; mv -f -- "$tmp" /etc/qtrade/install.env'
            $write = Invoke-QtWsl -WslArgs @('-d', $Name, '--user', 'root', '--exec', 'sh', '-c', $copy) -StandardInput $text -TimeoutSec 60
            if ($write.TimedOut -or $write.ExitCode -ne 0) { $result.Reason = 'INSTALL_ENV_WRITE_FAILED'; return $result }
        }
    } catch { $result.Reason = 'INSTALL_ENV_WRITE_FAILED'; return $result }
    $result.Cidr = $DockerCidr
    $result.Systemd = Wait-QtDistroSystemd -Name $Name -TimeoutSec 180
    if (-not $result.Systemd.Ok) { $result.Reason = 'SYSTEMD_NOT_READY'; return $result }
    $result.Ok = $true; $result.Reason = ''
    return $result
}

function Start-QtDistroFirstboot {
    [CmdletBinding()]
    param([string] $Name = 'qtrade', [ValidateRange(1, 7200)][int] $TimeoutSec = 1800, [string] $PackageVersion = '')
    $result = [pscustomobject]@{ Ok = $false; Reason = 'FIRSTBOOT_FAILED' }
    if ($Name -cne $script:QtDistroName) { $result.Reason = 'DISTRO_NAME_INVALID'; return $result }
    $check = Invoke-QtWsl -WslArgs @('-d', $Name, '--user', 'root', '--exec', 'sh', '-c', 'test -s /etc/qtrade/install.env && test -s /etc/qtrade/winagent.token') -TimeoutSec 30
    if ($check.TimedOut -or $check.ExitCode -ne 0) { $result.Reason = 'BOOTSTRAP_NOT_READY'; return $result }
    # A successful oneshot is inactive after a later OS boot because its marker
    # condition skips it. Never rerun database initialization in that case.
    $marker = Read-QtImportedMarker -DistroName $Name -TimeoutSec 30
    if ($null -ne $marker) {
        if (-not (Test-QtHasProperty -Object $marker -Name 'package_version') -or -not (Test-QtHasProperty -Object $marker -Name 'rootfs_version') -or -not $marker.package_version -or -not $marker.rootfs_version -or ($PackageVersion -and [string]$marker.package_version -cne $PackageVersion)) {
            $result.Reason = 'IMPORTED_MARKER_INVALID'; return $result
        }
        if (-not (Test-QtAgentActive -DistroName $Name -TimeoutSec 30)) { $result.Reason = 'AGENT_NOT_READY'; return $result }
        $result.Ok = $true; $result.Reason = ''; return $result
    }
    $showArgs = @('-d', $Name, '--user', 'root', '--exec', 'systemctl', 'show', 'qtrade-firstboot.service', '--property=ActiveState', '--property=SubState', '--property=Result')
    $before = Invoke-QtWsl -WslArgs $showArgs -TimeoutSec 30
    $completed = (-not $before.TimedOut -and $before.ExitCode -eq 0 -and $before.StdOut -match '(?m)^ActiveState=active\r?$' -and $before.StdOut -match '(?m)^SubState=exited\r?$' -and $before.StdOut -match '(?m)^Result=success\r?$')
    if (-not $completed) {
        $reset = Invoke-QtWsl -WslArgs @('-d', $Name, '--user', 'root', '--exec', 'systemctl', 'reset-failed', 'qtrade-docker-config.service', 'qtrade-firstboot.service', 'qtrade-agent.service', 'docker.service', 'docker.socket') -TimeoutSec 30
        if ($reset.TimedOut -or $reset.ExitCode -ne 0) { return $result }
        $start = Invoke-QtWsl -WslArgs @('-d', $Name, '--user', 'root', '--exec', 'systemctl', 'start', 'qtrade-firstboot.service') -TimeoutSec $TimeoutSec
        if ($start.TimedOut -or $start.ExitCode -ne 0) { return $result }
        $after = Invoke-QtWsl -WslArgs $showArgs -TimeoutSec 30
        if ($after.TimedOut -or $after.ExitCode -ne 0 -or $after.StdOut -notmatch '(?m)^ActiveState=active\r?$' -or $after.StdOut -notmatch '(?m)^SubState=exited\r?$' -or $after.StdOut -notmatch '(?m)^Result=success\r?$') { return $result }
    }
    $result.Ok = $true; $result.Reason = ''
    return $result
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
        if ($name -in $script:QtImageKeys) {
            $valid = $true
            foreach ($field in @('tar', 'ref', 'sha256')) {
                if (-not (Test-QtHasProperty -Object $node -Name $field) -or [string]::IsNullOrWhiteSpace([string]$node.$field)) { $valid = $false }
            }
            if (-not $valid -or [string]$node.sha256 -cnotmatch '^[a-fA-F0-9]{64}$') {
                $mismatched += [pscustomobject]@{ item = $name; expected = 'verified image archive'; actual = 'invalid metadata' }
                continue
            }
            $hash = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sha256sum', [string]$node.tar) -TimeoutSec $TimeoutSec
            $archiveSha = ''
            if (-not $hash.TimedOut -and $hash.ExitCode -eq 0) { $archiveSha = (($hash.StdOut).Trim() -split '\s+')[0] }
            if ($archiveSha -cne ([string]$node.sha256).ToLowerInvariant()) {
                $mismatched += [pscustomobject]@{ item = $name; expected = [string]$node.sha256; actual = $archiveSha }
                continue
            }
            # docker save/load can omit RepoDigests. The authenticated archive's
            # config hash is the image ID and remains available on a cold host.
            $inspectArchive = @'
import hashlib,json,sys,tarfile
with tarfile.open(sys.argv[1]) as archive:
    manifest = json.load(archive.extractfile('manifest.json'))
    matches = [item for item in manifest if sys.argv[2] in item.get('RepoTags', [])]
    if len(matches) != 1:
        raise SystemExit(1)
    config = archive.extractfile(matches[0]['Config']).read()
    print('sha256:' + hashlib.sha256(config).hexdigest())
'@
            $expected = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', '/opt/qtrade/agent/venv/bin/python', '-c', $inspectArchive, [string]$node.tar, [string]$node.ref) -TimeoutSec $TimeoutSec
            $actual = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'image', 'inspect', [string]$node.ref, '--format', '{{.Id}}') -TimeoutSec $TimeoutSec
            $expectedId = $expected.StdOut.Trim(); $actualId = $actual.StdOut.Trim()
            if ($expected.TimedOut -or $expected.ExitCode -ne 0 -or $expectedId -cnotmatch '^sha256:[a-f0-9]{64}$' -or $actual.TimedOut -or $actual.ExitCode -ne 0 -or $actualId -cne $expectedId) {
                $mismatched += [pscustomobject]@{ item = $name; expected = $expectedId; actual = $actualId }
                continue
            }
            if ((Test-QtHasProperty -Object $node -Name 'digest') -and $node.digest) {
                $digests = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'docker', 'image', 'inspect', [string]$node.ref, '--format', '{{json .RepoDigests}}') -TimeoutSec $TimeoutSec
                try {
                    if ($digests.TimedOut -or $digests.ExitCode -ne 0) { throw 'Image inspect failed.' }
                    $found = @($digests.StdOut | ConvertFrom-Json | Where-Object { $_ } | ForEach-Object { ([string]$_ -split '@')[-1] })
                    if ($found.Count -gt 0 -and $found -notcontains ([string]$node.digest)) { throw 'Image digest differs.' }
                } catch {
                    $mismatched += [pscustomobject]@{ item = $name; expected = [string]$node.digest; actual = 'digest mismatch or inspect failure' }
                }
            }
            continue
        }
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
        Read the authenticated full health response, not the anonymous summary.
    .OUTPUTS
        {Ok, StatusCode, Docker, Db, WinAgentReachable, Version, Raw}
    #>
    [CmdletBinding()]
    param([string] $BaseUrl = 'http://127.0.0.1:17600', [int] $TimeoutSec = 10, [AllowEmptyString()][string] $Token = '')
    if (-not $Token) {
        return [pscustomobject]@{ Ok = $false; StatusCode = 0; Docker = ''; Db = ''; WinAgentReachable = $false; Version = ''; Raw = '' }
    }
    $headers = @{}
    if ($Token) { $headers.Authorization = 'Bearer ' + $Token }
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/api/v1/system/health') -Method GET -Headers $headers -TimeoutSec $TimeoutSec
    $docker = ''; $db = ''; $wa = $false; $ver = ''; $valid = $false
    if ($r.Ok -and $r.StatusCode -eq 200 -and $r.Body) {
        try {
            $j = $r.Body | ConvertFrom-Json
            $valid = ((Test-QtHasProperty -Object $j -Name 'ok') -and $j.ok -is [bool] -and $j.ok)
            if ($valid) {
                if ((Test-QtHasProperty -Object $j -Name 'dockerd') -and $j.dockerd -is [bool]) {
                    $docker = if ($j.dockerd) { 'ok' } else { 'failed' }
                }
                if ((Test-QtHasProperty -Object $j -Name 'winagent') -and
                    (Test-QtHasProperty -Object $j.winagent -Name 'online') -and $j.winagent.online -is [bool]) { $wa = $j.winagent.online }
                if (Test-QtHasProperty -Object $j -Name 'agent') {
                    if ((Test-QtHasProperty -Object $j.agent -Name 'version') -and $j.agent.version -is [string]) { $ver = $j.agent.version }
                    # This API produces these counts only after both SQLite SELECTs
                    # succeed. Size fields alone do not prove a readable database.
                    $databaseReady = (Test-QtHasProperty -Object $j -Name 'accounts')
                    foreach ($key in @('running', 'n')) {
                        if (-not $databaseReady) { break }
                        if (-not (Test-QtHasProperty -Object $j.accounts -Name $key)) { $databaseReady = $false; break }
                        $value = $j.accounts.$key
                        if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 0) { $databaseReady = $false }
                    }
                    if ($databaseReady -and $j.accounts.running -gt $j.accounts.n) { $databaseReady = $false }
                    foreach ($key in @('db_mb', 'wal_mb')) {
                        if (-not $databaseReady) { break }
                        if (-not (Test-QtHasProperty -Object $j.agent -Name $key)) { $databaseReady = $false; break }
                        $value = $j.agent.$key
                        if (($value -isnot [int] -and $value -isnot [long] -and $value -isnot [double] -and $value -isnot [decimal]) -or
                            $value -lt 0 -or [double]::IsNaN([double]$value) -or [double]::IsInfinity([double]$value)) { $databaseReady = $false }
                    }
                    if ($databaseReady) { $db = 'ok' }
                }
            }
        } catch { }
    }
    return [pscustomobject]@{
        Ok                = $valid
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
Read-QtImportedMarker, Export-QtDistroBackup, Write-QtInstallEnv, Initialize-QtDistroBootstrap, Start-QtDistroFirstboot, Get-QtDockerImages,
Test-QtImageRefsPresent, Get-QtExpectedImageRefs, Invoke-QtRootfsContentsVerify,
Invoke-QtDockerLoadImages, Get-QtAgentHealth,
Test-QtAgentActive, Get-QtDockerBridgeSubnet, Test-QtDockerPoolApplied, Get-QtContainerLogsTail,
Test-QtImagesLoaded, Test-QtDistroImported
