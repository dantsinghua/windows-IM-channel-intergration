# QTrade 安装引擎 —— 控制台部署与快捷方式(DONE 步)
# 规格:docs/03 §2.2.1(console/* = electron-builder --dir 产物)、§2.11 第 5 项(DONE:清 RunOnce、写快捷方式、
#       删暂存 rootfs.tar、写 install-summary.txt)、§2.13 第 7 步 ①(升级时控制台先退、最后拉起)
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking

$script:QtConsoleExeName = 'QTrade.exe'
$script:QtStartMenuFolder = 'QTrade'

function Get-QtConsoleNames {
    [CmdletBinding()]
    param()
    return [ordered]@{ exe = $script:QtConsoleExeName; start_menu_folder = $script:QtStartMenuFolder }
}

function New-QtShortcut {
    <#
    .SYNOPSIS
        建 .lnk。DONE 步写开始菜单/桌面快捷方式(docs/03 §2.11 第 5 项);
        `REBOOT_PENDING` 时另写「继续安装 QTrade」(§2.5.1)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $LinkPath,
        [Parameter(Mandatory)][string] $TargetPath,
        [string] $Arguments = '',
        [string] $WorkingDirectory = '',
        [string] $Description = ''
    )
    New-QtDirectory -Path (Split-Path -Parent $LinkPath) | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LinkPath)
    $lnk.TargetPath = $TargetPath
    if ($Arguments) { $lnk.Arguments = $Arguments }
    $lnk.WorkingDirectory = if ($WorkingDirectory) { $WorkingDirectory } else { Split-Path -Parent $TargetPath }
    if ($Description) { $lnk.Description = $Description }
    $lnk.Save()
    return $LinkPath
}

function Install-QtConsoleShortcuts {
    <#
    .SYNOPSIS
        DONE:开始菜单 + 桌面快捷方式。
    .OUTPUTS
        {Created[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $ConsoleDir,
        [string] $StartMenuRoot,
        [string] $DesktopDir,
        [switch] $NoDesktop
    )
    if (-not $StartMenuRoot) { $StartMenuRoot = Join-Path (Get-QtEnvironmentPath -Name 'ProgramData') 'Microsoft\Windows\Start Menu\Programs' }
    if (-not $DesktopDir) { $DesktopDir = [Environment]::GetFolderPath('CommonDesktopDirectory') }
    $target = Join-Path $ConsoleDir $script:QtConsoleExeName
    $created = @()
    $menuDir = Join-Path $StartMenuRoot $script:QtStartMenuFolder
    $created += (New-QtShortcut -LinkPath (Join-Path $menuDir 'QTrade 控制台.lnk') -TargetPath $target -Description 'QTrade 多实例 IM 控制台')
    if (-not $NoDesktop) {
        $created += (New-QtShortcut -LinkPath (Join-Path $DesktopDir 'QTrade 控制台.lnk') -TargetPath $target -Description 'QTrade 多实例 IM 控制台')
    }
    return [pscustomobject]@{ Created = $created }
}

function Install-QtResumeShortcut {
    <#
    .SYNOPSIS
        §2.5.1:UAC 被拒或需重启时,开始菜单留「继续安装 QTrade」→ 落盘引擎 `/QT_MODE=resume`。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $EnginePath,
        [string] $StartMenuRoot
    )
    if (-not $StartMenuRoot) { $StartMenuRoot = Join-Path (Get-QtEnvironmentPath -Name 'ProgramData') 'Microsoft\Windows\Start Menu\Programs' }
    $menuDir = Join-Path $StartMenuRoot $script:QtStartMenuFolder
    return (New-QtShortcut -LinkPath (Join-Path $menuDir '继续安装 QTrade.lnk') -TargetPath $EnginePath -Arguments '/QT_MODE=resume' -Description '继续未完成的 QTrade 安装')
}

function Remove-QtShortcuts {
    <#
    .SYNOPSIS
        §2.14 第 7 步:卸载删快捷方式。
    #>
    [CmdletBinding()]
    param([string] $StartMenuRoot, [string] $DesktopDir)
    if (-not $StartMenuRoot) { $StartMenuRoot = Join-Path (Get-QtEnvironmentPath -Name 'ProgramData') 'Microsoft\Windows\Start Menu\Programs' }
    if (-not $DesktopDir) { $DesktopDir = [Environment]::GetFolderPath('CommonDesktopDirectory') }
    $removed = @()
    $menuDir = Join-Path $StartMenuRoot $script:QtStartMenuFolder
    if (Test-QtPath -Path $menuDir) { Remove-QtItem -Path $menuDir -Recurse; $removed += $menuDir }
    $d = Join-Path $DesktopDir 'QTrade 控制台.lnk'
    if (Test-QtPath -Path $d) { Remove-QtItem -Path $d; $removed += $d }
    return , $removed
}

function Stop-QtConsole {
    <#
    .SYNOPSIS
        §2.13 第 7 步 ①:升级时控制台**先退**(它只是外壳,无状态);5 s 未退则 taskkill。
    #>
    [CmdletBinding()]
    param([int] $GraceSec = 5)
    $procs = @(Get-QtProcessByName -Name @('QTrade'))
    if ($procs.Count -eq 0) { return [pscustomobject]@{ Ok = $true; Killed = $false } }
    foreach ($p in $procs) { try { $p.CloseMainWindow() | Out-Null } catch { } }
    Start-QtSleep -Seconds $GraceSec
    $left = @(Get-QtProcessByName -Name @('QTrade'))
    if ($left.Count -eq 0) { return [pscustomobject]@{ Ok = $true; Killed = $false } }
    foreach ($p in $left) { try { $p.Kill() } catch { } }
    return [pscustomobject]@{ Ok = $true; Killed = $true }
}

function Remove-QtStagedRootfs {
    <#
    .SYNOPSIS
        A-6:`DONE` 步**删暂存 `rootfs.tar`,不询问**(3.3 GB;修复时重新运行安装包即可,SFX 重新解压 3~5 分钟)。
        常驻占用由 ≈4.7 GB 回落到 ≈1.4 GB(§2.1 / §2.2.2)。
    .OUTPUTS
        {Removed, FreedBytes}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $WslDir)
    $tar = Join-Path $WslDir 'rootfs.tar'
    if (-not (Test-QtPath -Path $tar)) { return [pscustomobject]@{ Removed = $false; FreedBytes = [long]0 } }
    $size = Get-QtFileSize -Path $tar
    Remove-QtItem -Path $tar
    return [pscustomobject]@{ Removed = $true; FreedBytes = $size }
}

function Format-QtProbeLabel {
    # 私有助手:单个探测结果 → 完成页上的中文标签。
    # 🔴 SKIPPED 必须标「未探测」而**不是**「不通」(§2.10 第 3 行 / 验收 M1-17)——
    #    把「没探」说成「不通」会让用户去查一条根本没测过的链路。
    [CmdletBinding()][OutputType([string])]
    param([AllowNull()] $Row)
    if ($null -eq $Row) { return '—' }
    $r = [string]$Row.result
    if ($r -eq 'SKIPPED') { return '未探测(Agent 未就绪)' }
    if ($r -eq 'OK') { return '通' }
    return $r
}

function Format-QtProbeSummary {
    <#
    .SYNOPSIS
        §4 完成页:预检基础探测与自检全量探测**两列并列**;Agent 未就绪时自检列标「未探测」而不是「不通」
        (验收 M1-17)。纯函数,便于单测。
    .OUTPUTS
        每行 "目标  预检结果  自检结果" 的字符串数组
    #>
    [CmdletBinding()][OutputType([string[]])]
    param(
        [AllowNull()][AllowEmptyCollection()] $PrecheckProbes = @(),
        [AllowNull()][AllowEmptyCollection()] $SelftestProbes = @()
    )
    $targets = @()
    foreach ($p in @($PrecheckProbes)) { if ($p -and $targets -notcontains [string]$p.target) { $targets += [string]$p.target } }
    foreach ($p in @($SelftestProbes)) { if ($p -and $targets -notcontains [string]$p.target) { $targets += [string]$p.target } }
    $out = @()
    foreach ($t in $targets) {
        $a = @($PrecheckProbes | Where-Object { $_ -and [string]$_.target -eq $t })
        $b = @($SelftestProbes | Where-Object { $_ -and [string]$_.target -eq $t })
        $out += ('{0}  {1}  {2}' -f $t,
            (Format-QtProbeLabel -Row $(if ($a.Count -gt 0) { $a[0] } else { $null })),
            (Format-QtProbeLabel -Row $(if ($b.Count -gt 0) { $b[0] } else { $null })))
    }
    return , $out
}

function Write-QtInstallSummary {
    <#
    .SYNOPSIS
        §2.11 第 5 项:完成摘要写 `logs\install-summary.txt`
        (内核版本、WSL 版本、发行版、镜像、WinAgent、微信匹配、探测汇总)。
        🔴 §2.14 M1-19 验收要求「两份 summary 除时间戳外逐行相同」——故字段顺序固定、不含随机量。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)] $Summary
    )
    $lines = @()
    $lines += ('生成时间: {0}' -f (Get-QtIso8601))
    foreach ($k in @('package_version', 'kernel_version', 'wsl_version', 'distro', 'images', 'winagent',
            'wechat', 'docker_cidr', 'disk', 'memory', 'firewall', 'hosts_block', 'probes', 'notes')) {
        $v = ''
        if (Test-QtHasProperty -Object $Summary -Name $k) { $v = [string]$Summary.$k }
        $lines += ('{0}: {1}' -f $k, $v)
    }
    New-QtDirectory -Path (Split-Path -Parent $Path) | Out-Null
    [IO.File]::WriteAllText($Path, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
    return $Path
}

Export-ModuleMember -Function Get-QtConsoleNames, New-QtShortcut, Install-QtConsoleShortcuts,
Install-QtResumeShortcut, Remove-QtShortcuts, Stop-QtConsole, Write-QtInstallSummary,
Remove-QtStagedRootfs, Format-QtProbeSummary
