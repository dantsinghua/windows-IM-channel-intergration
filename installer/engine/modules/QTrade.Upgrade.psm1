# QTrade 安装引擎 —— 升级 `/QT_MODE=upgrade`(§2.13)与修复 `/QT_MODE=repair`(§5.2)
# 规格:docs/03 §2.13 第 1~8 步、升级保留清单(A-5;基线 §11.14 [UPGRADE])、schema 迁移边界、
#       三件套版本兼容矩阵(G-01)、§5.2 自愈、§3.4 退出码 120/121/122/123;验收 M1-22 ~ M1-25
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Wsl.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Distro.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.WinAgent.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Console.psm1') -DisableNameChecking

# §2.13「升级保留清单」(A-5;基线 §11.14 [UPGRADE])——下列任何一项在升级中被清空都是**缺陷**,不是「重装」
$script:QtUpgradeRetain = @(
    [pscustomobject]@{ item = '/var/lib/qtrade 全部(账号容器卷、agent.db、媒体、邮件归档、images/)'; where = 'distro'; how = 'rootfs 换时先 tar 出、导入后原样恢复;rootfs 不变时根本不动' }
    [pscustomobject]@{ item = 'qq_data(NapCat 登录态)与企点数据卷(redroid /data,含登录态与设备身份)'; where = 'distro'; how = '随 /var/lib/qtrade 整体;不 pm clear、不重建容器卷(重登有风控频次风险)' }
    [pscustomobject]@{ item = '/etc/qtrade(agent.toml、winagent.token、.imported)'; where = 'distro'; how = '同上;新版本新增键只补缺省' }
    [pscustomobject]@{ item = 'Vault blobs(DPAPI 机器级 + 熵文件)'; where = 'windows'; how = '停服务时不动,覆盖只覆 python\ / app\' }
    [pscustomobject]@{ item = 'winagent.db(install_state、install_history、wechat_install、probe_results)'; where = 'windows'; how = '不动;schema_version 向前迁移' }
    [pscustomobject]@{ item = '.wslconfig(用户的 memory= 等,含 R3′ 改过的值)'; where = 'windows'; how = '内核 sha 不变时一字不动;变了只改 kernel= 一行' }
    [pscustomobject]@{ item = '微信数据目录与备份目录'; where = 'windows'; how = '升级从不碰;也**不运行**微信 Uninstall.exe' }
)

# §2.13 第 7 步 G-01:停/覆盖顺序固定,理由是依赖方向
# (控制台依赖 Agent 与 WinAgent;Agent 依赖 WinAgent 服务;会话代理依赖服务)
$script:QtTriadStopOrder = @('console', 'winagent_user', 'winagent_svc', 'agent')
$script:QtTriadStartOrder = @('winagent_svc', 'winagent_user', 'agent', 'console')

function Get-QtUpgradeRetainList { [CmdletBinding()] param() return , $script:QtUpgradeRetain }
function Get-QtTriadStopOrder { [CmdletBinding()][OutputType([string[]])] param() return , $script:QtTriadStopOrder }
function Get-QtTriadStartOrder { [CmdletBinding()][OutputType([string[]])] param() return , $script:QtTriadStartOrder }

function Compare-QtPackageVersion {
    <#
    .SYNOPSIS
        §2.13 兼容矩阵末行:`/QT_MODE=upgrade` **只接受** `install_state.package_version < 自身`;
        🔴 **不支持降级**(`>` 时退 `E_INSTALL_DOWNGRADE_REFUSED` 122,文案指向卸载再装)。
    .OUTPUTS
        {Kind ∈ UPGRADE|SAME|DOWNGRADE, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $Installed,
        [Parameter(Mandatory)][string] $Package
    )
    if ([string]::IsNullOrWhiteSpace($Installed)) { return [pscustomobject]@{ Kind = 'UPGRADE'; Reason = 'no_installed_version' } }
    $a = $null; $b = $null
    try { $a = [version]$Installed; $b = [version]$Package } catch {
        return [pscustomobject]@{ Kind = 'SAME'; Reason = 'unparsable_version' }
    }
    if ($a -lt $b) { return [pscustomobject]@{ Kind = 'UPGRADE'; Reason = '' } }
    if ($a -eq $b) { return [pscustomobject]@{ Kind = 'SAME'; Reason = '' } }
    return [pscustomobject]@{ Kind = 'DOWNGRADE'; Reason = 'DOWNGRADE_REFUSED' }
}

function Get-QtFingerprintValue {
    # 私有助手:安全读指纹字段,缺字段回 ''(两边都缺 ⇒ 判「没变」,不会误触发换件)
    [CmdletBinding()][OutputType([string])]
    param([AllowNull()] $Object, [Parameter(Mandatory)][string] $Name)
    if (Test-QtHasProperty -Object $Object -Name $Name) { return [string]$Object.$Name }
    return ''
}

function Get-QtUpgradePlan {
    <#
    .SYNOPSIS
        §2.13 第 3~6 步的「变了才做」判定。**纯函数**:两份 manifest 的关键指纹由调用方采好。
    .PARAMETER Current / Next
        {kernel_sha256, rootfs_version, agent_version, image_tar_sha256, schema_breaking}
    .OUTPUTS
        {Kernel, Rootfs, AgentCode, Images, SchemaBreaking, Steps[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Current,
        [Parameter(Mandatory)] $Next
    )
    $kernel = ((Get-QtFingerprintValue -Object $Current -Name 'kernel_sha256') -ne (Get-QtFingerprintValue -Object $Next -Name 'kernel_sha256'))
    $rootfs = ((Get-QtFingerprintValue -Object $Current -Name 'rootfs_version') -ne (Get-QtFingerprintValue -Object $Next -Name 'rootfs_version'))
    $agent = ((Get-QtFingerprintValue -Object $Current -Name 'agent_version') -ne (Get-QtFingerprintValue -Object $Next -Name 'agent_version'))
    $images = ((Get-QtFingerprintValue -Object $Current -Name 'image_tar_sha256') -ne (Get-QtFingerprintValue -Object $Next -Name 'image_tar_sha256'))
    $breaking = $false
    if ($null -ne $Next -and (Test-QtHasProperty -Object $Next -Name 'schema_breaking')) { $breaking = [bool]$Next.schema_breaking }

    $steps = @('drain', 'stage')
    if ($kernel) { $steps += 'kernel' }
    if ($rootfs) { $steps += 'rootfs' }
    # 🔴 第 5 步:**rootfs 没变但 Agent 版本变**才单独覆盖 /opt/qtrade/agent(rootfs 变了已经带新 Agent)
    if ((-not $rootfs) -and $agent) { $steps += 'agent_code' }
    if ($images) { $steps += 'images' }
    $steps += 'triad'
    $steps += 'selftest'
    return [pscustomobject]@{
        Kernel         = $kernel
        Rootfs         = $rootfs
        AgentCode      = ((-not $rootfs) -and $agent)
        Images         = $images
        SchemaBreaking = $breaking
        Steps          = $steps
    }
}

function Invoke-QtDrain {
    <#
    .SYNOPSIS
        §2.13 第 1 步:`POST /api/v1/system/drain` —— 停所有账号容器,**账号数据卷不动**。
        🔴 红线 6:停容器不等于 shutdown WSL,但**会中断账号** —— 同样要明示并让用户选时机,
        故本函数同样要求 -Confirmed(静默模式由 `/QT_ACCEPT_SHUTDOWN=1` 给出,B-7 同口径)。
    .OUTPUTS
        {Ok, StatusCode}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][bool] $Confirmed,
        [string] $BaseUrl = 'http://127.0.0.1:17600',
        [int] $TimeoutSec = 300
    )
    if (-not $Confirmed) {
        throw '拒绝执行 drain:停账号容器会中断所有账号,须用户明示确认(红线 6;静默模式带 /QT_ACCEPT_SHUTDOWN=1)'
    }
    $r = Invoke-QtHttp -Uri ($BaseUrl + '/api/v1/system/drain') -Method POST -Body '{}' -TimeoutSec $TimeoutSec
    return [pscustomobject]@{ Ok = $r.Ok; StatusCode = $r.StatusCode }
}

function Backup-QtAgentData {
    <#
    .SYNOPSIS
        §2.13 第 4 步:换 rootfs 前先 `tar` 出 `/var/lib/qtrade` 与 `/etc/qtrade` 到
        `%ProgramData%\QTrade\wsl\data-backup-<ts>.tar`(经 `/mnt/c`)。
        失败 → `UPGRADE_DATA_BACKUP_FAILED`(120)—— 🔴 没备份成功**绝不** `--unregister`。
    .OUTPUTS
        {Ok, TarPath, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $WslDir,
        [string] $DistroName = 'qtrade',
        [string] $Stamp,
        [int] $TimeoutSec = 3600
    )
    if (-not $Stamp) { $Stamp = Get-QtTimestamp }
    New-QtDirectory -Path $WslDir | Out-Null
    $tarWin = Join-QtPath -Path $WslDir -ChildPath ('data-backup-{0}.tar' -f $Stamp)
    $tarWsl = ConvertTo-QtWslPath -WindowsPath $tarWin
    $cmd = 'tar -cf {0} -C / var/lib/qtrade etc/qtrade' -f $tarWsl
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c', $cmd) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0 -or -not (Test-QtPath -Path $tarWin)) {
        return [pscustomobject]@{ Ok = $false; TarPath = ''; Reason = 'UPGRADE_DATA_BACKUP_FAILED' }
    }
    return [pscustomobject]@{ Ok = $true; TarPath = $tarWin; Reason = '' }
}

function Restore-QtAgentData {
    <#
    .SYNOPSIS
        §2.13 第 4 步末 / §5.2:导入新 rootfs、首启之后把 `/var/lib/qtrade` 与 `/etc/qtrade` 原样恢复。
        也是 `/QT_MODE=repair --restore-data <tar>` 的执行体。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $TarPath,
        [string] $DistroName = 'qtrade',
        [int] $TimeoutSec = 3600
    )
    if (-not (Test-QtPath -Path $TarPath)) { return [pscustomobject]@{ Ok = $false; Reason = 'BACKUP_NOT_FOUND' } }
    $tarWsl = ConvertTo-QtWslPath -WindowsPath $TarPath
    $cmd = 'tar -xf {0} -C /' -f $tarWsl
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c', $cmd) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return [pscustomobject]@{ Ok = $false; Reason = 'RESTORE_FAILED' } }
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Get-QtLatestDataBackup {
    <#
    .SYNOPSIS
        §5.2 修复:从最近一份 `data-backup-*.tar` 恢复(若有)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $WslDir)
    if (-not (Test-QtPath -Path $WslDir)) { return '' }
    $f = @(Get-ChildItem -LiteralPath $WslDir -Filter 'data-backup-*.tar' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    if ($f.Count -eq 0) { return '' }
    return $f[0].FullName
}

function Stop-QtTriad {
    <#
    .SYNOPSIS
        §2.13 第 7 步 ①②③:控制台先退(5 s 未退 taskkill)→ 会话代理退 + 计划任务 Disable
        (防止覆盖中途被重新拉起)→ 服务 `sc stop`。
        🔴 顺序不能动:依赖方向决定的(验收 M1-25 抓时间序)。
    .OUTPUTS
        {Ok, Order[], ConsoleKilled}
    #>
    [CmdletBinding()]
    param([int] $ConsoleGraceSec = 5)
    $console = Stop-QtConsole -GraceSec $ConsoleGraceSec
    $names = Get-QtWinAgentNames
    # ②会话代理:停计划任务(Disable 而不是 Unregister —— 升级完还要 Enable 回来)
    try { Disable-ScheduledTask -TaskPath $names.task_path -TaskName $names.task_name -ErrorAction Stop | Out-Null } catch { }
    # ③服务
    Invoke-QtSc -ScArgs @('stop', $names.service_name) -TimeoutSec 60 | Out-Null
    return [pscustomobject]@{ Ok = $true; Order = (Get-QtTriadStopOrder); ConsoleKilled = $console.Killed }
}

function Start-QtTriad {
    <#
    .SYNOPSIS
        §2.13 第 7 步 ③④ 的回程:服务起 → 计划任务 Enable + Start → 等 `user_agent:true` → **最后**拉起控制台
        (避免控制台对着一半新一半旧的 Agent/WinAgent 报错)。
    .OUTPUTS
        {Ok, UserAgent, Order[]}
    #>
    [CmdletBinding()]
    param(
        [string] $ConsoleExePath = '',
        [int] $HealthTimeoutSec = 60
    )
    $names = Get-QtWinAgentNames
    Invoke-QtSc -ScArgs @('start', $names.service_name) -TimeoutSec 60 | Out-Null
    try { Enable-ScheduledTask -TaskPath $names.task_path -TaskName $names.task_name -ErrorAction Stop | Out-Null } catch { }
    try { Start-QtScheduledTask -TaskPath $names.task_path -TaskName $names.task_name } catch { }
    $h = Wait-QtWinAgentHealthy -BaseUrl $names.base_url -TimeoutSec $HealthTimeoutSec
    if ($ConsoleExePath -and (Test-QtPath -Path $ConsoleExePath)) {
        Start-Process -FilePath $ConsoleExePath | Out-Null
    }
    return [pscustomobject]@{ Ok = $h.Ok; UserAgent = $h.UserAgent; Order = (Get-QtTriadStartOrder) }
}

function Update-QtAgentCode {
    <#
    .SYNOPSIS
        §2.13 第 5 步:rootfs 没变但 Agent 版本变 → 发行版内 `tar` 覆盖 `/opt/qtrade/agent` + `systemctl restart qtrade-agent`。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $AgentTarPath,
        [string] $DistroName = 'qtrade',
        [int] $TimeoutSec = 600
    )
    if (-not (Test-QtPath -Path $AgentTarPath)) { return [pscustomobject]@{ Ok = $false; Reason = 'AGENT_TAR_NOT_FOUND' } }
    $tarWsl = ConvertTo-QtWslPath -WindowsPath $AgentTarPath
    $cmd = 'tar -xf {0} -C / && systemctl restart qtrade-agent' -f $tarWsl
    $r = Invoke-QtWsl -WslArgs @('-d', $DistroName, '--user', 'root', '--exec', 'sh', '-c', $cmd) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return [pscustomobject]@{ Ok = $false; Reason = 'AGENT_UPDATE_FAILED' } }
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Get-QtSchemaBreakingPrompt {
    <#
    .SYNOPSIS
        §2.13「schema 迁移边界(A-5)」:`schema_breaking:false` 时向导**不得出现任何「清数据」字样**(验收 M1-22);
        `true` 才走 ①第一页明示 ②二次确认 ③自动先 data-backup 三步(验收 M1-22b)。
    .OUTPUTS
        {RequireConfirm, Text}
    #>
    [CmdletBinding()]
    param([bool] $SchemaBreaking = $false)
    if (-not $SchemaBreaking) {
        return [pscustomobject]@{ RequireConfirm = $false; Text = '' }
    }
    return [pscustomobject]@{
        RequireConfirm = $true
        Text           = '本次升级需要重建数据,登录态/消息将被清除,请先导出。升级前会自动做一份数据备份。'
    }
}

function Invoke-QtRepair {
    <#
    .SYNOPSIS
        §5.2 自愈:发行版被用户误 `--unregister` → 重导入 + 从最近 `data-backup` 恢复(若有)。
        也对应 `P-ENV` 的【修复】`qt-env-distro-repair`(服务拉起落盘引擎 `/QT_MODE=repair`)。
    .OUTPUTS
        {Ok, Reason, Restored, BackupUsed}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $DistroDir,
        [Parameter(Mandatory)][string] $RootfsTar,
        [Parameter(Mandatory)][string] $WslDir,
        [string] $DistroName = 'qtrade',
        [AllowEmptyString()][string] $RestoreDataTar = ''
    )
    $imp = Import-QtDistro -Name $DistroName -Directory $DistroDir -TarPath $RootfsTar
    if (-not $imp.Ok) { return [pscustomobject]@{ Ok = $false; Reason = $imp.Reason; Restored = $false; BackupUsed = '' } }
    $sysd = Wait-QtDistroSystemd -Name $DistroName
    if (-not $sysd.Ok) { return [pscustomobject]@{ Ok = $false; Reason = 'SYSTEMD_NOT_READY'; Restored = $false; BackupUsed = '' } }

    $tar = $RestoreDataTar
    if (-not $tar) { $tar = Get-QtLatestDataBackup -WslDir $WslDir }
    if (-not $tar) {
        # 没有备份不算失败:修复出的是一个干净发行版(§5.2「若有」)
        return [pscustomobject]@{ Ok = $true; Reason = ''; Restored = $false; BackupUsed = '' }
    }
    $res = Restore-QtAgentData -TarPath $tar -DistroName $DistroName
    if (-not $res.Ok) { return [pscustomobject]@{ Ok = $false; Reason = $res.Reason; Restored = $false; BackupUsed = $tar } }
    return [pscustomobject]@{ Ok = $true; Reason = ''; Restored = $true; BackupUsed = $tar }
}

Export-ModuleMember -Function Get-QtUpgradeRetainList, Get-QtTriadStopOrder, Get-QtTriadStartOrder,
Compare-QtPackageVersion, Get-QtUpgradePlan, Invoke-QtDrain, Backup-QtAgentData, Restore-QtAgentData,
Get-QtLatestDataBackup, Stop-QtTriad, Start-QtTriad, Update-QtAgentCode,
Get-QtSchemaBreakingPrompt, Invoke-QtRepair
