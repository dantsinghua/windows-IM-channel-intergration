# QTrade 安装引擎 —— CLIENTS_CHECKED:微信 PC 检测 / 版本矩阵 / 备份 / 重装引导 / 更新屏蔽
# 规格:docs/03 §2.9.1(检测三来源)、§2.9.2(版本↔DLL 矩阵与五个结论)、§2.9.3(重装引导,红线 8)、
#       §2.15(命令速查)、§7 `[wechat]`;hosts 屏蔽机制的唯一出处 = docs/04 §2.5.4(本册只调端点)。
#
# 🔴🔴 本册**不存在「静默卸载」这条路**(docs/03 §2.9.3 事实 1、§2.14 第 6 步):
#     微信 `Uninstall.exe /S` = **卸载并清空聊天记录与登录态**,任何路径都不得带 `/S` 调它。
#     `[wechat] silent_setup` **只作用于安装器**,且实测通过前恒 false;`/QT_WECHAT=reinstall` 静默时降级为 check。
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking

$script:QtBundledWeChatVersion = '4.1.12.26'        # docs/03 §7 [wechat] bundled_version(P-18)
$script:QtWeChatBackupCopyMaxGB = 5                 # docs/03 §2.9.3 第 3 步 backup_mode=auto 的分界
$script:QtWeChatProcessNames = @('Weixin', 'WeChat', 'WeixinUpdate', 'WeChatBackup', 'WeChatXFile', 'WeChatAppEx')

# docs/03 §2.9.2 随包矩阵(安装后写 02 的 `wechat_version_matrix` 表;运行期由 05 试钥回写)
$script:QtWeChatMatrix = @(
    [pscustomobject]@{ version = '4.1.12.26'; dll = 'wx_key2.dll'; status = 'verified'; source = 'bundled' }
    [pscustomobject]@{ version = '4.1.11.52'; dll = 'wx_key1.dll'; status = 'failed'; source = 'bundled' }
    [pscustomobject]@{ version = '4.1.13.12'; dll = ''; status = 'unknown'; source = 'bundled' }
)

function Get-QtBundledWeChatVersion { [CmdletBinding()][OutputType([string])] param() return $script:QtBundledWeChatVersion }
function Get-QtWeChatMatrix { [CmdletBinding()] param() return , $script:QtWeChatMatrix }
function Get-QtWeChatProcessNames { [CmdletBinding()] param() return , $script:QtWeChatProcessNames }

function ConvertTo-QtVersionSegments {
    # 私有助手:'4.1.12.26' -> @(4,1,12,26);不足四段补 0,非数字段按 0
    [CmdletBinding()][OutputType([int[]])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Version)
    $p = @($Version.Trim().Split('.') | ForEach-Object { $n = 0; [void][int]::TryParse($_, [ref]$n); $n })
    while ($p.Count -lt 4) { $p += 0 }
    return , ([int[]]$p)
}

function Compare-QtVersion {
    <#
    .SYNOPSIS
        四段逐段比较(docs/03 §2.9.2「版本 > 随包版本(逐段比较)」)。回 -1/0/1;非法串按 0 段补齐。
    #>
    [CmdletBinding()][OutputType([int])]
    param([Parameter(Mandatory)][string] $Left, [Parameter(Mandatory)][string] $Right)
    $a = ConvertTo-QtVersionSegments -Version $Left
    $b = ConvertTo-QtVersionSegments -Version $Right
    for ($i = 0; $i -lt 4; $i++) {
        if ($a[$i] -lt $b[$i]) { return -1 }
        if ($a[$i] -gt $b[$i]) { return 1 }
    }
    return 0
}

function Get-QtWeChatInstalls {
    <#
    .SYNOPSIS
        docs/03 §2.9.1:卸载键(**实测在 WOW6432Node**,64 位路径下没有)+ 用户键 `InstallPath` + 兜底扫描。
        🔴 **不用卸载键 `DisplayVersion`**——它只在首装时写入、应用内更新不刷新。
    .OUTPUTS
        [{path, uninstall_string, source}]
    #>
    [CmdletBinding()]
    param()
    $found = @()
    foreach ($key in @(
            @{ Path = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Weixin'; Src = 'uninstall_4x' }
            @{ Path = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\WeChat'; Src = 'uninstall_3x' }
        )) {
        $loc = Get-QtRegistryValue -Path $key.Path -Name 'InstallLocation'
        if (-not $loc) { $loc = Get-QtRegistryValue -Path $key.Path -Name 'DisplayIcon' }
        if ($loc) {
            $found += [pscustomobject]@{
                path             = ([string]$loc).Trim('"')
                uninstall_string = [string](Get-QtRegistryValue -Path $key.Path -Name 'UninstallString')
                source           = $key.Src
            }
        }
    }
    foreach ($key in @(
            @{ Path = 'HKCU:\Software\Tencent\Weixin'; Src = 'hkcu_4x' }
            @{ Path = 'HKCU:\Software\Tencent\WeChat'; Src = 'hkcu_3x' }
        )) {
        $loc = Get-QtRegistryValue -Path $key.Path -Name 'InstallPath'
        if ($loc) { $found += [pscustomobject]@{ path = [string]$loc; uninstall_string = ''; source = $key.Src } }
    }
    # 兜底扫描(只补漏,以卸载键 / InstallPath 为准)
    foreach ($p in @(
            (Join-Path (Get-QtEnvironmentPath -Name 'ProgramFiles') 'Tencent\Weixin')
            (Join-Path (Get-QtEnvironmentPath -Name 'ProgramFilesX86') 'Tencent\WeChat')
            (Join-Path (Get-QtEnvironmentPath -Name 'LocalAppData') 'Programs\Tencent\Weixin')
        )) {
        if (Test-QtPath -Path $p) { $found += [pscustomobject]@{ path = $p; uninstall_string = ''; source = 'scan' } }
    }
    # 正在运行的路径也算一处安装(§2.9.1「运行中的路径不在上面任何一处 → 也算一处安装」)
    foreach ($proc in (Get-QtProcessByName -Name @('Weixin', 'WeChat'))) {
        $dir = ''
        try { $dir = Split-Path -Parent $proc.Path } catch { }
        if ($dir) { $found += [pscustomobject]@{ path = $dir; uninstall_string = ''; source = 'running' } }
    }
    # 去重(路径大小写不敏感)
    $seen = @{}
    $uniq = @()
    foreach ($f in $found) {
        $k = ([string]$f.path).TrimEnd('\').ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($k) -or $seen.ContainsKey($k)) { continue }
        $seen[$k] = $true
        $uniq += $f
    }
    return , $uniq
}

function Get-QtWeChatDataRoot {
    <#
    .SYNOPSIS
        docs/03 §2.9.1(4.x):数据根 = `%APPDATA%\Tencent\xwechat\config\<32 位 hex>.ini` 的**第一行**
        (多个 ini 取 LastWriteTime 最新;`MyDocument:` / `Appdata:` 前缀按旧写法展开)。
        🔴 **不在** `Documents\xwechat_files`,也**不读** `HKCU…\FileSavePath`。
    .OUTPUTS
        {IniPath, DataRoot, DataDir, AppDataDir}
    #>
    [CmdletBinding()]
    param([string] $AppDataPath)
    if (-not $AppDataPath) { $AppDataPath = Get-QtEnvironmentPath -Name 'AppData' }
    $appdataDir = Join-Path $AppDataPath 'Tencent\xwechat'
    $cfgDir = Join-Path $appdataDir 'config'
    $result = [pscustomobject]@{ IniPath = ''; DataRoot = ''; DataDir = ''; AppDataDir = $appdataDir }
    if (-not (Test-QtPath -Path $cfgDir)) { return $result }
    $inis = @(Get-ChildItem -LiteralPath $cfgDir -Filter '*.ini' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^[0-9a-f]{32}\.ini$' } | Sort-Object LastWriteTime -Descending)
    if ($inis.Count -eq 0) { return $result }
    $ini = $inis[0].FullName
    $first = ''
    try { $first = (($(Read-QtTextFile -Path $ini) -split "`r?`n")[0]).Trim() } catch { $first = '' }
    if ([string]::IsNullOrWhiteSpace($first)) { return [pscustomobject]@{ IniPath = $ini; DataRoot = ''; DataDir = ''; AppDataDir = $appdataDir } }
    $root = Expand-QtWeChatPathPrefix -Value $first
    return [pscustomobject]@{ IniPath = $ini; DataRoot = $root; DataDir = (Join-Path $root 'xwechat_files'); AppDataDir = $appdataDir }
}

function Expand-QtWeChatPathPrefix {
    <#
    .SYNOPSIS
        展开 ini 首行的 `MyDocument:` / `Appdata:` 前缀(docs/03 §2.9.1「按旧写法展开」)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Value)
    $v = $Value.Trim()
    if ($v -match '^(?i)MyDocument:\s*(.*)$') {
        return (Join-Path ([Environment]::GetFolderPath('MyDocuments')) $Matches[1].TrimStart('\'))
    }
    if ($v -match '^(?i)Appdata:\s*(.*)$') {
        return (Join-Path (Get-QtEnvironmentPath -Name 'AppData') $Matches[1].TrimStart('\'))
    }
    return $v
}

function Get-QtWeChatVersion {
    <#
    .SYNOPSIS
        docs/03 §2.9.1 **三来源必须一致**:①`Weixin.exe`/`WeChat.exe` 的 `VersionInfo.FileVersion`;
        ②安装目录下的版本子目录名(多个取最大);③`<数据根>\xwechat_files\all_users\whatsnew.dat` 内容。
        不一致 → 以 ① 为准 + 记警告 `WECHAT_VERSION_AMBIGUOUS`。
    .OUTPUTS
        {Version, ExeVersion, VersionSubdir, WhatsNew, Ambiguous}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $InstallPath,
        [string] $DataRoot = ''
    )
    $exe = Join-Path $InstallPath 'Weixin.exe'
    if (-not (Test-QtPath -Path $exe)) { $exe = Join-Path $InstallPath 'WeChat.exe' }
    $exeVersion = Get-QtFileVersion -Path $exe

    $subdir = ''
    $dirs = @(Get-QtChildDirectory -Path $InstallPath | Where-Object { $_.Name -match '^\d+(\.\d+){3}$' })
    if ($dirs.Count -gt 0) {
        $subdir = (@($dirs | Sort-Object { [version]$_.Name } -Descending)[0]).Name
    }

    $whatsnew = ''
    if ($DataRoot) {
        $wn = Join-Path $DataRoot 'xwechat_files\all_users\whatsnew.dat'
        if (Test-QtPath -Path $wn) { try { $whatsnew = (Read-QtTextFile -Path $wn).Trim() } catch { $whatsnew = '' } }
    }

    $present = @($exeVersion, $subdir, $whatsnew | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $ambiguous = (@($present | Select-Object -Unique).Count -gt 1)
    return [pscustomobject]@{
        Version       = $exeVersion      # 不一致时以 ① 为准
        ExeVersion    = $exeVersion
        VersionSubdir = $subdir
        WhatsNew      = $whatsnew
        Ambiguous     = $ambiguous
    }
}

function Get-QtWeChatMatch {
    <#
    .SYNOPSIS
        docs/03 §2.9.2 的五个结论(基线 §8.6)。**纯函数**,便于单测。
    .OUTPUTS
        {Match, Action, Dll}
        Match ∈ NOT_INSTALLED | MULTIPLE_INSTALLS | SUPPORTED | UNSUPPORTED_NEWER | UNSUPPORTED_OLDER
        Action ∈ KEEP | REINSTALL_BUNDLED | BLOCK | SELECT_ONE
    #>
    [CmdletBinding()]
    param(
        [int] $InstallCount = 0,
        [AllowEmptyString()][string] $Version = '',
        [string] $BundledVersion = '',
        $Matrix
    )
    if (-not $BundledVersion) { $BundledVersion = $script:QtBundledWeChatVersion }
    if ($null -eq $Matrix) { $Matrix = $script:QtWeChatMatrix }
    if ($InstallCount -le 0) { return [pscustomobject]@{ Match = 'NOT_INSTALLED'; Action = 'BLOCK'; Dll = '' } }
    if ($InstallCount -ge 2) { return [pscustomobject]@{ Match = 'MULTIPLE_INSTALLS'; Action = 'SELECT_ONE'; Dll = '' } }

    $row = @($Matrix | Where-Object { [string]$_.version -eq $Version })
    if ($row.Count -gt 0 -and [string]$row[0].status -eq 'verified') {
        return [pscustomobject]@{ Match = 'SUPPORTED'; Action = 'KEEP'; Dll = [string]$row[0].dll }
    }
    $cmp = Compare-QtVersion -Left $Version -Right $BundledVersion
    if ($cmp -gt 0) { return [pscustomobject]@{ Match = 'UNSUPPORTED_NEWER'; Action = 'REINSTALL_BUNDLED'; Dll = '' } }
    if ($cmp -lt 0) { return [pscustomobject]@{ Match = 'UNSUPPORTED_OLDER'; Action = 'REINSTALL_BUNDLED'; Dll = '' } }
    # 版本 == 随包版本但矩阵无记录(不应发生)→ SUPPORTED
    $dll = ''
    if ($row.Count -gt 0) { $dll = [string]$row[0].dll }
    return [pscustomobject]@{ Match = 'SUPPORTED'; Action = 'KEEP'; Dll = $dll }
}

function Get-QtWeChatPlan {
    <#
    .SYNOPSIS
        CLIENTS_CHECKED 的决策表(§2.9.2 五个结论 × `/QT_WECHAT` 模式 × 是否交互)。**纯函数**。

        🔴 两条硬口径:
        - `reinstall` 在**静默模式**下不可用,已由 Resolve-QtWeChatMode 降级为 `check`(P-19);
          `check` 模式下**不会自己动手重装** —— 重装必须有用户在向导上点(红线 8)。
        - 用户拒绝 / 无人确认 ⇒ `BLOCK`(微信通道禁用,**安装继续**,`P-SET` 微信模块开关默认关)。
    .PARAMETER UserApproved
        用户在向导上是否点了【备份并覆盖安装】/【不备份,直接覆盖安装】。静默模式恒 $false。
    .OUTPUTS
        {Action, NeedBackup, NeedReinstall, NeedSelect, Reason}
        Action ∈ KEEP | REINSTALL_BUNDLED | BLOCK | SELECT_ONE
    #>
    [CmdletBinding()]
    param(
        [ValidateSet('check', 'skip', 'reinstall')][string] $Mode = 'check',
        [Parameter(Mandatory)][string] $Match,
        [bool] $UserApproved = $false,
        [ValidateSet('auto', 'copy', 'rename', 'skip')][string] $BackupMode = 'auto'
    )
    if ($Mode -eq 'skip') {
        return [pscustomobject]@{ Action = 'BLOCK'; NeedBackup = $false; NeedReinstall = $false; NeedSelect = $false; Reason = 'user_skipped' }
    }
    switch ($Match) {
        'SUPPORTED' {
            return [pscustomobject]@{ Action = 'KEEP'; NeedBackup = $false; NeedReinstall = $false; NeedSelect = $false; Reason = '' }
        }
        'MULTIPLE_INSTALLS' {
            # 需要用户选一处;没人选就不动任何一处(§2.9.3 末:其余不卸、不改)
            if (-not $UserApproved) {
                return [pscustomobject]@{ Action = 'BLOCK'; NeedBackup = $false; NeedReinstall = $false; NeedSelect = $true; Reason = 'awaiting_selection' }
            }
            return [pscustomobject]@{ Action = 'SELECT_ONE'; NeedBackup = $false; NeedReinstall = $false; NeedSelect = $true; Reason = '' }
        }
        'NOT_INSTALLED' {
            if (-not $UserApproved) {
                return [pscustomobject]@{ Action = 'BLOCK'; NeedBackup = $false; NeedReinstall = $false; NeedSelect = $false; Reason = 'declined_fresh_install' }
            }
            # 首装无需备份(§2.9.3 方案①「NOT_INSTALLED 首装(无需备份)」)
            return [pscustomobject]@{ Action = 'REINSTALL_BUNDLED'; NeedBackup = $false; NeedReinstall = $true; NeedSelect = $false; Reason = 'fresh_install' }
        }
        default {
            # UNSUPPORTED_NEWER / UNSUPPORTED_OLDER
            if (-not $UserApproved) {
                return [pscustomobject]@{ Action = 'BLOCK'; NeedBackup = $false; NeedReinstall = $false; NeedSelect = $false; Reason = 'declined_reinstall' }
            }
            return [pscustomobject]@{
                Action = 'REINSTALL_BUNDLED'; NeedBackup = ($BackupMode -ne 'skip'); NeedReinstall = $true; NeedSelect = $false; Reason = ''
            }
        }
    }
}

function Select-QtWeChatBackupMode {
    <#
    .SYNOPSIS
        docs/03 §2.9.3 第 3 步:`backup_mode=auto` 时数据总字节 ≤ `backup_copy_max_gb`(默认 5)推荐 `copy`,
        否则推荐 `rename`;数据根未知(读不到 ini)时只剩 `skip`。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][ValidateSet('auto', 'copy', 'rename', 'skip')][string] $Requested,
        [long] $TotalBytes = 0,
        [bool] $DataRootKnown = $true,
        [int] $CopyMaxGB = 0
    )
    if (-not $DataRootKnown) { return 'skip' }
    if ($Requested -ne 'auto') { return $Requested }
    if ($CopyMaxGB -le 0) { $CopyMaxGB = $script:QtWeChatBackupCopyMaxGB }
    if (($TotalBytes / 1GB) -le $CopyMaxGB) { return 'copy' }
    return 'rename'
}

function Get-QtWeChatBackupEstimate {
    <#
    .SYNOPSIS
        §2.9.3 第 3 步的估时估空间:按 80 MB/s + 每万文件 10 s 估;空间按总字节 ×1.05。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][long] $Bytes, [Parameter(Mandatory)][int] $Files)
    $seconds = [int]([math]::Ceiling($Bytes / (80MB)) + [math]::Ceiling($Files / 10000.0) * 10)
    return [pscustomobject]@{
        Seconds       = $seconds
        Minutes       = [math]::Round($seconds / 60.0, 1)
        RequiredBytes = [long]([math]::Ceiling($Bytes * 1.05))
    }
}

function Invoke-QtWeChatBackupCopy {
    <#
    .SYNOPSIS
        §2.9.3 `copy`:robocopy `/E /COPY:DAT /R:1 /W:1 /NP /LOG:`;**退出码 < 8 算成功**(robocopy 退出码是位标志)。
    .OUTPUTS
        {Ok, ExitCode}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Source,
        [Parameter(Mandatory)][string] $Destination,
        [Parameter(Mandatory)][string] $LogPath,
        [int] $TimeoutSec = 3600
    )
    if (-not (Test-QtPath -Path $Source)) { return [pscustomobject]@{ Ok = $false; ExitCode = -1 } }
    New-QtDirectory -Path $Destination | Out-Null
    $r = Invoke-QtProcess -FilePath 'robocopy.exe' `
        -ArgumentList @($Source, $Destination, '/E', '/COPY:DAT', '/R:1', '/W:1', '/NP', ('/LOG:' + $LogPath)) `
        -TimeoutSec $TimeoutSec
    return [pscustomobject]@{ Ok = ($r.ExitCode -lt 8 -and -not $r.TimedOut); ExitCode = $r.ExitCode }
}

function Invoke-QtWeChatBackupRename {
    <#
    .SYNOPSIS
        §2.9.3 `rename`:装前把 `xwechat_files` 与 `%APPDATA%\Tencent\xwechat` 各重命名为 `<原名>.qtbak-<ts>`;
        装完(三来源版本核对通过后)**改回原名**。零磁盘开销、秒级。
        🔴 中途失败一律**先改回目录名**再报错,`.qtbak-*` 不许残留。
    .OUTPUTS
        {Ok, Renamed[{from,to}]}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]] $Directories, [string] $Stamp)
    if (-not $Stamp) { $Stamp = Get-QtTimestamp }
    $done = @()
    try {
        foreach ($d in $Directories) {
            if (-not (Test-QtPath -Path $d)) { continue }
            $newName = (Split-Path -Leaf $d) + ('.qtbak-{0}' -f $Stamp)
            Rename-Item -LiteralPath $d -NewName $newName -ErrorAction Stop
            $done += [pscustomobject]@{ from = $d; to = (Join-Path (Split-Path -Parent $d) $newName) }
        }
        return [pscustomobject]@{ Ok = $true; Renamed = $done }
    } catch {
        # 先改回再报错
        Restore-QtWeChatBackupRename -Renamed $done | Out-Null
        return [pscustomobject]@{ Ok = $false; Renamed = @() }
    }
}

function Restore-QtWeChatBackupRename {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Renamed)
    $restored = @()
    foreach ($r in @($Renamed)) {
        if (-not (Test-QtPath -Path $r.to)) { continue }
        Rename-Item -LiteralPath $r.to -NewName (Split-Path -Leaf $r.from) -ErrorAction SilentlyContinue
        $restored += $r.from
    }
    return , $restored
}

function Assert-QtNoSilentUninstall {
    <#
    .SYNOPSIS
        🔴 红线守卫:任何调微信 `Uninstall.exe` 的参数串里**不得**出现 `/S`(docs/03 §2.9.3 事实 1、§2.14 第 6 步)。
        `/S` = 卸载并清空聊天记录与登录态;而且参数串必须恰好 `/S` 才进静默分支,带别的参数会落回 GUI。
        本函数把这条禁令做成可单测的硬校验,而不是靠注释。
    #>
    [CmdletBinding()]
    param([string[]] $Arguments = @())
    foreach ($a in $Arguments) {
        if ($a -match '(?i)^\s*/S\s*$') {
            throw '禁止以 /S 运行微信 Uninstall.exe:/S = 卸载并清空聊天记录与登录态(docs/03 §2.9.3 事实 1)'
        }
    }
    return $true
}

function Start-QtWeChatInteractiveUninstall {
    <#
    .SYNOPSIS
        §2.9.3 方案②:**以交互方式**启动卸载键 `UninstallString`(**不加任何参数**),
        文案引导用户确认「保留本地数据」勾选框;向导轮询卸载键消失且无 `Un.exe`/`Uninstall.exe` 进程。
        NSIS 会把自己拷到 `%TEMP%\~ns*.tmp\Un.exe` 运行,**父进程退出码无意义**。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $UninstallString,
        [string] $UninstallKeyPath = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Weixin',
        [int] $TimeoutSec = 600,
        [int] $PollSec = 5
    )
    Assert-QtNoSilentUninstall -Arguments @() | Out-Null
    $exe = $UninstallString.Trim('"')
    Start-Process -FilePath $exe | Out-Null    # 🔴 无任何参数
    $deadline = (Get-QtNow).AddSeconds($TimeoutSec)
    while ((Get-QtNow) -lt $deadline) {
        $keyGone = -not (Test-QtRegistryKey -Path $UninstallKeyPath)
        $procGone = (@(Get-QtProcessByName -Name @('Un', 'Uninstall')).Count -eq 0)
        if ($keyGone -and $procGone) { return [pscustomobject]@{ Ok = $true; Reason = '' } }
        Start-QtSleep -Seconds $PollSec
    }
    return [pscustomobject]@{ Ok = $false; Reason = 'UNINSTALL_TIMEOUT' }
}

function Start-QtWeChatSetup {
    <#
    .SYNOPSIS
        §2.9.3 方案①(首选):**不卸载**,直接运行随包 `weixin_4.1.12.26.exe` 覆盖安装到检测到的同一目录。
        🔴 `SilentSetup`(`[wechat] silent_setup`)**实测通过前恒 false** —— 恒交互,向导轮询三来源版本。
    .OUTPUTS
        {Ok, Reason, FinalVersion}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $SetupExe,
        [Parameter(Mandatory)][string] $InstallPath,
        [string] $DataRoot = '',
        [string] $ExpectedVersion = '',
        [bool] $SilentSetup = $false,
        [int] $TimeoutSec = 600,
        [int] $PollSec = 5
    )
    if (-not $ExpectedVersion) { $ExpectedVersion = $script:QtBundledWeChatVersion }
    if (-not (Test-QtPath -Path $SetupExe)) { return [pscustomobject]@{ Ok = $false; Reason = 'WECHAT_REINSTALL_FAILED'; FinalVersion = '' } }
    # ⚠️ 变量名不用 $args —— 那是 PowerShell 自动变量,函数里会被清空(docs/03 §2.6.7 W5 的原坑)
    $setupArgs = @()
    if ($SilentSetup) { $setupArgs += '/S' }     # 只作用于**安装器**;卸载器永不加 /S
    if ($setupArgs.Count -gt 0) { Start-Process -FilePath $SetupExe -ArgumentList $setupArgs | Out-Null }
    else { Start-Process -FilePath $SetupExe | Out-Null }

    $deadline = (Get-QtNow).AddSeconds($TimeoutSec)
    while ((Get-QtNow) -lt $deadline) {
        $v = Get-QtWeChatVersion -InstallPath $InstallPath -DataRoot $DataRoot
        if ($v.ExeVersion -eq $ExpectedVersion -and -not $v.Ambiguous) {
            return [pscustomobject]@{ Ok = $true; Reason = ''; FinalVersion = $v.Version }
        }
        Start-QtSleep -Seconds $PollSec
    }
    return [pscustomobject]@{ Ok = $false; Reason = 'WECHAT_REINSTALL_FAILED'; FinalVersion = '' }
}

function Invoke-QtWeChatUpdateBlock {
    <#
    .SYNOPSIS
        §2.9.3 第 5 步 B 层:引擎**只调** `POST /wa/v1/wechat/update-block {enable:bool}`。
        🔴 写法/备份/**行尾标记 `# QTrade-wechat-update-block`(R6-15:逐行行尾标记,不是 BEGIN/END 围栏)**/
        域名清单,全在 docs/04 §2.5.4,本册不直接改 hosts。
        失败由 04 降级为告警 `H21_WECHAT_HOSTS_BLOCK_FAILED`,安装仍继续(C 层版本守卫兜底)。
    .OUTPUTS
        {Ok, Action, Domains, Backup, FailedReason, HostsBlock}
        HostsBlock ∈ written | skipped_no_domains | degraded_readonly | degraded_edr
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][bool] $Enable,
        [string] $BaseUrl = 'http://127.0.0.1:17610',
        [int] $TimeoutSec = 60
    )
    $body = '{"enable":' + $(if ($Enable) { 'true' } else { 'false' }) + '}'
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/wa/v1/wechat/update-block') -Method POST -Body $body -TimeoutSec $TimeoutSec
    if (-not $r.Ok) {
        return [pscustomobject]@{ Ok = $false; Action = ''; Domains = @(); Backup = ''; FailedReason = 'unreachable'; HostsBlock = 'degraded_edr' }
    }
    $action = ''; $domains = @(); $backup = ''; $failed = ''
    try {
        $j = $r.Body | ConvertFrom-Json
        if (Test-QtHasProperty -Object $j -Name 'action') { $action = [string]$j.action }
        if (Test-QtHasProperty -Object $j -Name 'domains') { $domains = @($j.domains) }
        if (Test-QtHasProperty -Object $j -Name 'backup') { $backup = [string]$j.backup }
        if (Test-QtHasProperty -Object $j -Name 'failed_reason') { $failed = [string]$j.failed_reason }
    } catch { }
    $hostsBlock = 'written'
    if ($domains.Count -eq 0) { $hostsBlock = 'skipped_no_domains' }
    if ($failed -eq 'readonly') { $hostsBlock = 'degraded_readonly' }
    elseif ($failed) { $hostsBlock = 'degraded_edr' }
    return [pscustomobject]@{ Ok = ($failed -eq ''); Action = $action; Domains = $domains; Backup = $backup; FailedReason = $failed; HostsBlock = $hostsBlock }
}

function Resolve-QtWeChatMode {
    <#
    .SYNOPSIS
        `/QT_WECHAT=check|skip|reinstall`。🔴 P-19:`reinstall` **在静默模式下不可用** → 降级为 `check`
        (方案①的安装器静默参数实测通过前,重装恒需交互)。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [ValidateSet('check', 'skip', 'reinstall')][string] $Requested = 'check',
        [bool] $Silent = $false
    )
    if ($Requested -eq 'reinstall' -and $Silent) { return 'check' }
    return $Requested
}

function Test-QtClientsChecked {
    <#
    .SYNOPSIS
        CLIENTS_CHECKED 幂等判据(docs/03 §2.3):`clients.wechat.action` 已落定(含 `BLOCK`/跳过)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)] $Context)
    if ($null -eq $Context.Clients) { return $false }
    if (-not (Test-QtHasProperty -Object $Context.Clients -Name 'wechat')) { return $false }
    $w = $Context.Clients.wechat
    if ($null -eq $w -or -not (Test-QtHasProperty -Object $w -Name 'action')) { return $false }
    return -not [string]::IsNullOrWhiteSpace([string]$w.action)
}

Register-QtStepCheck -Step 'CLIENTS_CHECKED' -Check { param($ctx) Test-QtClientsChecked -Context $ctx }

Export-ModuleMember -Function Get-QtBundledWeChatVersion, Get-QtWeChatMatrix, Get-QtWeChatProcessNames,
Compare-QtVersion, Get-QtWeChatInstalls, Get-QtWeChatDataRoot, Expand-QtWeChatPathPrefix,
Get-QtWeChatVersion, Get-QtWeChatMatch, Get-QtWeChatPlan, Select-QtWeChatBackupMode, Get-QtWeChatBackupEstimate,
Invoke-QtWeChatBackupCopy, Invoke-QtWeChatBackupRename, Restore-QtWeChatBackupRename,
Assert-QtNoSilentUninstall, Start-QtWeChatInteractiveUninstall, Start-QtWeChatSetup,
Invoke-QtWeChatUpdateBlock, Resolve-QtWeChatMode, Test-QtClientsChecked
