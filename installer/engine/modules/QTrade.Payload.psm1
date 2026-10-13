# QTrade 安装引擎 —— 载荷 manifest 与 sha256 校验(PAYLOAD_STAGED)
# 规格:docs/03 §2.2.1(manifest.json 结构、critical、rootfs_contents)、§2.1(两层校验、失败清理、P0-12)、§2.3(幂等判据)
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking

# 🔴 R6-38:manifest 里 kernel 的 coredump_l2 值恒为 "D",其它值 CI 构建失败(docs/03 §2.2.1 / §2.6.9)
$script:QtCoredumpL2Required = 'D'

function Read-QtManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { throw ('manifest 不存在:{0}' -f $Path) }
    return (Read-QtTextFile -Path $Path | ConvertFrom-Json)
}

function Expand-QtPayloadPath {
    <#
    .SYNOPSIS
        把 manifest 的 `dest`(含 `%ProgramData%`)或相对 `path` 解析成绝对路径。
        `dest` 缺省时 = <暂存根>\<path>。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)] $File,
        [Parameter(Mandatory)][string] $StageRoot
    )
    $dest = ''
    if (Test-QtHasProperty -Object $File -Name 'dest') { $dest = [string]$File.dest }
    if ([string]::IsNullOrWhiteSpace($dest) -or $dest -like '*…*') {
        # 用 [IO.Path]::Combine 而不是 Join-Path —— 后者会校验驱动器是否存在,对未挂载盘直接抛
        return [IO.Path]::Combine($StageRoot, ([string]$File.path -replace '/', '\'))
    }
    $expanded = $dest -replace '%ProgramData%', (Get-QtEnvironmentPath -Name 'ProgramData')
    return $expanded
}

function Test-QtManifestEntry {
    <#
    .SYNOPSIS
        单个 manifest 条目的校验结果。
    .OUTPUTS
        {path, dest, critical, status ∈ Ok|Missing|HashMismatch|SkippedGlob|SkippedNoHash, actual, expected}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $File,
        [Parameter(Mandatory)][string] $StageRoot,
        [string] $DestinationRoot = ''
    )
    $relPath = ''
    if (Test-QtHasProperty -Object $File -Name 'path') { $relPath = [string]$File.path }
    $critical = $false
    if (Test-QtHasProperty -Object $File -Name 'critical') { $critical = [bool]$File.critical }
    $expected = ''
    if (Test-QtHasProperty -Object $File -Name 'sha256') { $expected = ([string]$File.sha256).ToLowerInvariant() }
    $result = [ordered]@{ path = $relPath; dest = ''; critical = $critical; status = 'InvalidManifest'; actual = ''; expected = $expected }
    $normalized = $relPath -replace '\\', '/'
    $parts = @($normalized -split '/')
    # Only exact relative file paths and full hashes are valid in new packages.
    if ([string]::IsNullOrWhiteSpace($relPath) -or [IO.Path]::IsPathRooted($relPath) -or
        [Management.Automation.WildcardPattern]::ContainsWildcardCharacters($relPath) -or
        $relPath.Contains(':') -or $relPath -match '[\x00-\x1f]' -or
        @($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' -or $_ -match '[. ]$' }).Count -gt 0 -or
        $expected -notmatch '\A[0-9a-f]{64}\z') {
        return [pscustomobject]$result
    }
    try {
        if (-not $DestinationRoot) { $DestinationRoot = $StageRoot }
        $declared = [IO.Path]::GetFullPath((Expand-QtPayloadPath -File $File -StageRoot $DestinationRoot))
        $required = [IO.Path]::GetFullPath([IO.Path]::Combine($DestinationRoot, ($normalized -replace '/', '\')))
        if (-not [string]::Equals($declared, $required, [StringComparison]::OrdinalIgnoreCase)) {
            return [pscustomobject]$result
        }
        $abs = [IO.Path]::GetFullPath([IO.Path]::Combine($StageRoot, ($normalized -replace '/', '\')))
    } catch { return [pscustomobject]$result }
    $result.dest = $abs
    if (-not (Test-QtPath -Path $abs)) { $result.status = 'Missing'; return [pscustomobject]$result }
    try {
        $info = Get-Item -LiteralPath $abs -Force -ErrorAction Stop
        if ($info.PSIsContainer -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            return [pscustomobject]$result
        }
        if (Test-QtHasProperty -Object $File -Name 'size') {
            $size = [long]$File.size
            if ($size -lt 0) { return [pscustomobject]$result }
            if ($info.Length -ne $size) { $result.status = 'HashMismatch'; return [pscustomobject]$result }
        }
        $actual = Get-QtFileHash -Path $abs
    } catch {
        $result.status = 'ReadError'
        return [pscustomobject]$result
    }
    $result.actual = $actual
    $result.status = 'Ok'
    if ($actual -ne $expected) { $result.status = 'HashMismatch' }
    return [pscustomobject]$result
}

function Invoke-QtPayloadVerify {
    <#
    .SYNOPSIS
        内层校验:按 manifest 逐文件 sha256 复核(docs/03 §2.1「两层都过才算 PAYLOAD_STAGED」)。
    .OUTPUTS
        {Ok, Entries[], Missing[], Mismatched[], CriticalFailed[], VerifiedCount}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Manifest,
        [Parameter(Mandatory)][string] $StageRoot,
        [string] $DestinationRoot = ''
    )
    $entries = @()
    $seen = @{}
    if (Test-QtHasProperty -Object $Manifest -Name 'files') {
        foreach ($f in $Manifest.files) {
            $entry = Test-QtManifestEntry -File $f -StageRoot $StageRoot -DestinationRoot $DestinationRoot
            $key = $entry.path -replace '\\', '/'
            if ($seen.ContainsKey($key)) { $entry.status = 'InvalidManifest' }
            $seen[$key] = $true
            $entries += $entry
        }
    }
    $missing = @($entries | Where-Object { $_.status -eq 'Missing' })
    $mismatch = @($entries | Where-Object { $_.status -eq 'HashMismatch' })
    $invalid = @($entries | Where-Object { $_.status -in @('InvalidManifest', 'ReadError') })
    $criticalFailed = @($entries | Where-Object { $_.critical -and $_.status -ne 'Ok' })
    $verified = @($entries | Where-Object { $_.status -eq 'Ok' })
    return [pscustomobject]@{
        Ok             = (($entries.Count -gt 0) -and ($verified.Count -eq $entries.Count))
        Entries        = $entries
        Missing        = $missing
        Mismatched     = $mismatch
        Invalid        = $invalid
        CriticalFailed = $criticalFailed
        VerifiedCount  = $verified.Count
    }
}

function Clear-QtInconsistentPayload {
    <#
    .SYNOPSIS
        SFX 解压中途失败留下的不一致文件由**下一次运行**的引擎清掉(docs/03 §2.1「失败清理」);
        **不留半成品当「已暂存」**。只删 sha256 不一致的那些,已一致的留着(重跑时 skip,§2.1 末)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $VerifyResult)
    $removed = @()
    foreach ($e in $VerifyResult.Mismatched) {
        if (Test-QtPath -Path $e.dest) {
            Remove-QtItem -Path $e.dest
            $removed += $e.path
        }
    }
    return , $removed
}

function Test-QtPayloadStaged {
    <#
    .SYNOPSIS
        PAYLOAD_STAGED 的幂等判据:目录里全部文件 sha256 一致(docs/03 §2.3)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    if (-not (Test-QtPath -Path $Context.ManifestPath)) { return $false }
    $m = Read-QtManifest -Path $Context.ManifestPath
    $r = Invoke-QtPayloadVerify -Manifest $m -StageRoot $Context.StageRoot
    return $r.Ok
}

function Test-QtManifestCoredumpL2 {
    <#
    .SYNOPSIS
        🔴 R6-38 CI 硬门:kernel 条目的 `coredump_l2` **恒为 "D"**,其它值不得出包(docs/03 §2.2.1 / §2.6.9)。
        引擎侧同样复核一次 —— 值不对说明这份包不是合法交付形态。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Manifest)
    $kernel = @($Manifest.files | Where-Object { ([string]$_.path) -like 'kernel/bzImage*' })
    if ($kernel.Count -eq 0) { return $false }
    foreach ($k in $kernel) {
        if (-not (Test-QtHasProperty -Object $k -Name 'coredump_l2')) { return $false }
        if ([string]$k.coredump_l2 -ne $script:QtCoredumpL2Required) { return $false }
    }
    return $true
}

function Get-QtManifestKernel {
    <#
    .SYNOPSIS
        取 kernel 条目(sha256/size/version 三元组由 CI 写入,人不手填,docs/03 §2.6.8 第 4 条)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Manifest, [string] $Line = '6.6')
    $rel = 'kernel/bzImage-{0}' -f $Line
    $k = @($Manifest.files | Where-Object { ([string]$_.path) -eq $rel })
    if ($k.Count -eq 0) { throw ('manifest 里没有内核条目:{0}(内核线恒 6.6,A-3)' -f $rel) }
    return $k[0]
}

function Get-QtManifestWeChatMatrix {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Manifest)
    if (-not (Test-QtHasProperty -Object $Manifest -Name 'wechat_version_matrix')) { return @() }
    return @($Manifest.wechat_version_matrix)
}

# 注册 PAYLOAD_STAGED 的幂等判据
Register-QtStepCheck -Step 'PAYLOAD_STAGED' -Check { param($ctx) Test-QtPayloadStaged -Context $ctx }

Export-ModuleMember -Function Read-QtManifest, Expand-QtPayloadPath, Test-QtManifestEntry,
Invoke-QtPayloadVerify, Clear-QtInconsistentPayload, Test-QtPayloadStaged,
Test-QtManifestCoredumpL2, Get-QtManifestKernel, Get-QtManifestWeChatMatrix
