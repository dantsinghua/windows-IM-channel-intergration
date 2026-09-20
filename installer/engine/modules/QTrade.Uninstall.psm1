# QTrade 安装引擎 —— 卸载 `/QT_MODE=uninstall`
# 规格:docs/03 §2.14 第 1~7 步、§6(整机级改动清单三处都要还原)、§2.15;`/QT_KEEP_DATA=1|0` 见 §3.4。
#
# ⚠️ 执行顺序 ≠ 文档编号顺序:§2.14 第 5 步写「在第 4 步停服务**之前**先调 DELETE /wa/v1/firewall」,
#    第 6 步写「**在删 WinAgent 服务之前**调 update-block {enable:false}」。故实际序列为
#    1 → 2 → 3 → 5(防火墙)→ 6(hosts)→ 4(服务/计划任务)→ 7(删文件)。此点已记入 handoff「建议裁决」。
#
# 🔴 卸载 QTrade **永远不运行**微信 `Uninstall.exe`(更不带 `/S`);微信数据目录一字不动(§2.14 第 6 步)。
# 🔴 卸载**不 `wsl --shutdown`**(§2.14 第 3 步):删了 `kernel=` 后用户下次重启 WSL 自然回官方内核。
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Wsl.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Kernel.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.WinAgent.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.WeChat.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Firewall.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Console.psm1') -DisableNameChecking

# `/QT_KEEP_DATA=1` 时**保留**的子项(§2.14 第 1 步)
$script:QtKeepOnUninstall = @('winagent\winagent.db', 'wsl\data-backup-*', 'wsl\.wslconfig.bak-*')

function Get-QtUninstallKeepList { [CmdletBinding()] param() return , $script:QtKeepOnUninstall }

function Get-QtUninstallPlan {
    <#
    .SYNOPSIS
        纯函数:给定 `install_state` 与 `/QT_KEEP_DATA`,算出这次卸载**要做什么、保留什么**(便于单测与向导预览)。
    .OUTPUTS
        {KeepData, BackupData, RemovePaths[], KeepPaths[], WslConfig{RemoveKernelLine, RemoveKeys[], AskRestoreMemory, AskRestoreKernel}, Steps[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $State,
        [bool] $KeepData = $true,
        [string] $Root
    )
    $paths = Get-QtPaths -Root $Root
    $keysAdded = @()
    if ($State.wslconfig -and (Test-QtHasProperty -Object $State.wslconfig -Name 'keys_added')) {
        $keysAdded = @($State.wslconfig.keys_added)
    }
    $changedMemory = $false
    if ($State.wslconfig -and (Test-QtHasProperty -Object $State.wslconfig -Name 'changed')) {
        $changedMemory = (@($State.wslconfig.changed | Where-Object { $_.key -eq 'memory' }).Count -gt 0)
    }
    $replacedKernel = ''
    if ($State.wslconfig -and (Test-QtHasProperty -Object $State.wslconfig -Name 'replaced_kernel')) {
        $replacedKernel = [string]$State.wslconfig.replaced_kernel
    }

    $keepPaths = @()
    if ($KeepData) { $keepPaths = @($script:QtKeepOnUninstall | ForEach-Object { Join-Path $paths.Root $_ }) }

    return [pscustomobject]@{
        KeepData    = $KeepData
        BackupData  = $KeepData
        RemovePaths = @($paths.Root, (Join-Path (Get-QtEnvironmentPath -Name 'LocalAppData') 'QTrade'))
        KeepPaths   = $keepPaths
        WslConfig   = [pscustomobject]@{
            RemoveKernelLine = $true
            RemoveKeys       = @($keysAdded | Where-Object { $_ -ne 'kernel' })
            AskRestoreMemory = $changedMemory
            AskRestoreKernel = (-not [string]::IsNullOrWhiteSpace($replacedKernel))
            ReplacedKernel   = $replacedKernel
        }
        Steps       = @('data', 'distro', 'wslconfig', 'firewall', 'hosts', 'service', 'files')
    }
}

function Invoke-QtUninstallData {
    <#
    .SYNOPSIS
        §2.14 第 1 步:【保留数据】把 `/var/lib/qtrade` tar 到 `wsl\data-backup-<ts>.tar`;【删除全部数据】不备份。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][bool] $KeepData,
        [string] $DistroName = 'qtrade',
        [Parameter(Mandatory)][string] $WslDir,
        [string] $Stamp,
        [int] $TimeoutSec = 1800
    )
    if (-not $KeepData) { return [pscustomobject]@{ Ok = $true; BackupTar = ''; Skipped = $true } }
    if (-not $Stamp) { $Stamp = Get-QtTimestamp }
    New-QtDirectory -Path $WslDir | Out-Null
    $tarWin = Join-QtPath -Path $WslDir -ChildPath ('data-backup-{0}.tar' -f $Stamp)
    $tarWsl = ConvertTo-QtWslPath -WindowsPath $tarWin
    $cmd = 'tar -cf {0} -C / var/lib/qtrade etc/qtrade' -f $tarWsl
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c', $cmd) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) {
        return [pscustomobject]@{ Ok = $false; BackupTar = ''; Skipped = $false }
    }
    return [pscustomobject]@{ Ok = $true; BackupTar = $tarWin; Skipped = $false }
}

function Invoke-QtUninstallDistro {
    <#
    .SYNOPSIS
        §2.14 第 2 步:停账号容器 → 停 `qtrade-agent` → `wsl --terminate qtrade` → `wsl --unregister qtrade`。
        🔴 **只注销 `qtrade`**(`qtrade-kcheck` 若残留一并注销);**用户的发行版不动**。
        这一步是**唯一真正释放 ~8 GB vhdx 空间**的地方(E-18 / H23:vhdx 只涨不缩)。
    #>
    [CmdletBinding()]
    param([string] $DistroName = 'qtrade', [string] $KCheckDir = '', [int] $TimeoutSec = 300)
    Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c',
        'systemctl stop qtrade-agent || true; docker ps -q | xargs -r docker stop') -TimeoutSec $TimeoutSec | Out-Null
    Invoke-QtWsl -WslArgs @('--terminate', $DistroName) -TimeoutSec 120 | Out-Null
    $r = Unregister-QtDistro -Name $DistroName -TimeoutSec $TimeoutSec
    Remove-QtKCheck -Directory $KCheckDir | Out-Null
    return [pscustomobject]@{ Ok = ($r.ExitCode -eq 0); ExitCode = $r.ExitCode }
}

function Invoke-QtUninstallWslConfig {
    <#
    .SYNOPSIS
        §2.14 第 3 步:`kernel_state=OURS` → **只删我们的 `kernel=` 行**;`keys_added` 里的键一并删;
        `changed[]` 里的 `memory` 询问是否恢复原值(缺省恢复);`replaced_kernel` 非空询问是否恢复那一行。
        **其它行原样**。卸载**不 shutdown**。
    .OUTPUTS
        {Ok, Text, Backup}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $WslConfigPath,
        [Parameter(Mandatory)][string] $BackupDir,
        [string[]] $KeysAdded = @(),
        [string] $RestoreMemoryValue = '',
        [string] $RestoreKernelValue = '',
        [string] $Stamp
    )
    if (-not (Test-QtPath -Path $WslConfigPath)) { return [pscustomobject]@{ Ok = $true; Text = ''; Backup = '' } }
    $backup = Backup-QtWslConfig -Path $WslConfigPath -BackupDir $BackupDir -Stamp $Stamp
    $text = Read-QtTextFile -Path $WslConfigPath
    $text = Remove-QtWslConfigKeys -Text $text -KeysAdded $KeysAdded -RemoveKernelLine

    if ($RestoreMemoryValue) {
        $text = Set-QtIniValue -Text $text -Section 'wsl2' -Key 'memory' -Value $RestoreMemoryValue
    }
    if ($RestoreKernelValue) {
        $text = Set-QtIniValue -Text $text -Section 'wsl2' -Key 'kernel' -Value ($RestoreKernelValue -replace '\\', '\\')
    }
    Write-QtUtf8NoBom -Path $WslConfigPath -Text $text | Out-Null
    return [pscustomobject]@{ Ok = $true; Text = $text; Backup = $backup }
}

function Set-QtIniValue {
    <#
    .SYNOPSIS
        在指定段里写/改一个键(卸载恢复用;逐行操作,其它行原样)。段不存在则创建。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $Text,
        [Parameter(Mandatory)][string] $Section,
        [Parameter(Mandatory)][string] $Key,
        [Parameter(Mandatory)][string] $Value
    )
    $lines = [System.Collections.ArrayList]::new()
    foreach ($l in ($Text -split "`r?`n")) { [void]$lines.Add($l) }
    $secIdx = -1
    $secEnd = $lines.Count
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $m = [regex]::Match($lines[$i], '^\s*\[(?<s>[^\]]+)\]\s*$')
        if ($m.Success) {
            if ($m.Groups['s'].Value.Trim() -eq $Section) { $secIdx = $i; $secEnd = $lines.Count }
            elseif ($secIdx -ge 0) { $secEnd = $i; break }
        }
    }
    if ($secIdx -lt 0) {
        if ($lines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($lines[$lines.Count - 1])) { [void]$lines.Add('') }
        [void]$lines.Add('[' + $Section + ']')
        [void]$lines.Add(('{0}={1}' -f $Key, $Value))
        return ($lines -join "`r`n")
    }
    for ($i = $secIdx + 1; $i -lt $secEnd; $i++) {
        if ($lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            $lines[$i] = ('{0}={1}' -f $Key, $Value)
            return ($lines -join "`r`n")
        }
    }
    $lines.Insert($secEnd, ('{0}={1}' -f $Key, $Value))
    return ($lines -join "`r`n")
}

function Remove-QtInstallFiles {
    <#
    .SYNOPSIS
        §2.14 第 7 步:删 `%ProgramData%\QTrade\`(按第 1 步选择保留子项)、`%LOCALAPPDATA%\QTrade\`、
        快捷方式、RunOnce、卸载键。
        🔴 **不做**:不禁用 Windows 功能、不卸 WSL MSI、不卸微信/VC 运行库、不删微信备份目录、不动 Docker Desktop。
    .OUTPUTS
        {Removed[], Kept[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Plan,
        [switch] $WhatIf
    )
    $removed = @()
    $kept = @()
    foreach ($root in $Plan.RemovePaths) {
        if (-not (Test-QtPath -Path $root)) { continue }
        if (-not $Plan.KeepData) {
            if (-not $WhatIf) { Remove-QtItem -Path $root -Recurse }
            $removed += $root
            continue
        }
        # 保留模式:逐项删,命中保留模式的跳过
        foreach ($child in (Get-ChildItem -LiteralPath $root -Force -ErrorAction SilentlyContinue)) {
            $keep = $false
            foreach ($pat in $Plan.KeepPaths) {
                if ($child.FullName -like $pat -or $child.FullName -like (Split-Path -Parent $pat)) { $keep = $true; break }
            }
            if ($keep) { $kept += $child.FullName; continue }
            if ($child.PSIsContainer) {
                # 目录里可能有要保留的东西,逐文件判
                $hasKeep = $false
                foreach ($pat in $Plan.KeepPaths) { if ($pat -like ($child.FullName + '*')) { $hasKeep = $true; break } }
                if ($hasKeep) {
                    foreach ($f in (Get-ChildItem -LiteralPath $child.FullName -Force -Recurse -File -ErrorAction SilentlyContinue)) {
                        $fk = $false
                        foreach ($pat in $Plan.KeepPaths) { if ($f.FullName -like $pat) { $fk = $true; break } }
                        if ($fk) { $kept += $f.FullName; continue }
                        if (-not $WhatIf) { Remove-QtItem -Path $f.FullName }
                        $removed += $f.FullName
                    }
                    continue
                }
            }
            if (-not $WhatIf) { Remove-QtItem -Path $child.FullName -Recurse }
            $removed += $child.FullName
        }
    }
    return [pscustomobject]@{ Removed = $removed; Kept = $kept }
}

function Get-QtOrphanQtbakDirs {
    <#
    .SYNOPSIS
        §2.14 第 6 步末:`rename` 备份模式因中断残留的 `*.qtbak-<ts>` 目录,卸载页**列出路径提示用户自行处理**,
        🔴 **不代删、不代改名**。
    #>
    [CmdletBinding()]
    param([string[]] $SearchRoots = @())
    $found = @()
    foreach ($root in $SearchRoots) {
        if (-not (Test-QtPath -Path $root)) { continue }
        foreach ($d in (Get-QtChildDirectory -Path $root)) {
            if ($d.Name -match '\.qtbak-\d{8}-\d{6}') { $found += $d.FullName }
        }
    }
    return , $found
}

Export-ModuleMember -Function Get-QtUninstallKeepList, Get-QtUninstallPlan, Invoke-QtUninstallData,
Invoke-QtUninstallDistro, Invoke-QtUninstallWslConfig, Set-QtIniValue, Remove-QtInstallFiles,
Get-QtOrphanQtbakDirs
