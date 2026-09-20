# QTrade 安装引擎 —— 退出码与原因码
# 🔴 唯一出处 = docs/03 §3.4「退出码」表。本表逐字照抄,任何改动必须先改 docs/03 再改这里;
#    installer/tests/test_spec_consistency.py 会从 docs/03 解析该表并断言本文件与 .iss 逐字一致。
# 原因码(写进 install_state 的 `FAILED:<步名>:<原因码>`)= 退出码名去掉 `E_INSTALL_` 前缀(docs/03 §3.4 首句)。
#requires -Version 5.1
Set-StrictMode -Version Latest

# 顺序即 docs/03 §3.4 表行顺序,便于逐行对照
$script:QtExitTable = [ordered]@{
    'OK'                                    = 0
    'E_INSTALL_WAIT_USER'                   = 10
    'E_INSTALL_REBOOT_REQUIRED'             = 3010
    'E_INSTALL_WIN_TOO_OLD'                 = 20
    'E_INSTALL_NOT_X64'                     = 21
    'E_INSTALL_NOT_ADMIN'                   = 22
    'E_INSTALL_ELEVATED_AS_OTHER_USER'      = 23
    'E_INSTALL_VIRT_DISABLED'               = 24
    'E_INSTALL_POLICY_BLOCKED'              = 25
    'E_INSTALL_DISK_LOW'                    = 26
    'E_INSTALL_MEM_LOW'                     = 27
    'E_INSTALL_OTHER_CUSTOM_KERNEL_DECLINED' = 28
    'E_INSTALL_ALREADY_RUNNING'             = 29
    'E_INSTALL_PAYLOAD_CORRUPT'             = 30
    'E_INSTALL_FEATURE_ENABLE_FAILED'       = 40
    'E_INSTALL_RESUME_ENGINE_MISSING'       = 41
    'E_INSTALL_WSL_MSI_FAILED'              = 50
    'E_INSTALL_WSL_BROKEN'                  = 51
    'E_INSTALL_KERNEL_SHA_MISMATCH'         = 60
    'E_INSTALL_WSLCONFIG_PARSE_FAILED'      = 61
    'E_INSTALL_KERNEL_BOOT_TIMEOUT'         = 62
    'E_INSTALL_KERNEL_BOOT_FAILED'          = 63
    'E_INSTALL_KERNEL_NO_BINDER'            = 64
    'E_INSTALL_KERNEL_ROLLBACK_FAILED'      = 65
    'E_INSTALL_KERNEL_SHUTDOWN_TIMEOUT'     = 66
    'E_INSTALL_KCHECK_IMPORT_FAILED'        = 67
    'E_INSTALL_DISTRO_NAME_CONFLICT_DECLINED' = 70
    'E_INSTALL_IMPORT_FAILED'               = 71
    'E_INSTALL_SYSTEMD_NOT_READY'           = 72
    'E_INSTALL_DOCKER_NOT_READY'            = 73
    'E_INSTALL_IMAGE_LOAD_FAILED'           = 74
    'E_INSTALL_AGENT_NOT_READY'             = 75
    'E_INSTALL_DOCKER_CIDR_EXHAUSTED'       = 76
    'E_INSTALL_VCREDIST_FAILED'             = 80
    'E_INSTALL_SERVICE_INSTALL_FAILED'      = 81
    'E_INSTALL_WINAGENT_NOT_READY'          = 82
    'E_INSTALL_WECHAT_BACKUP_FAILED'        = 90
    'E_INSTALL_WECHAT_REINSTALL_FAILED'     = 91
    'E_INSTALL_SELFTEST_REDROID_BOOT'       = 100
    'E_INSTALL_SELFTEST_AGENT'              = 101
    'E_INSTALL_SELFTEST_WINAGENT'           = 102
    'E_INSTALL_UPGRADE_DATA_BACKUP_FAILED'  = 120
    'E_INSTALL_UNINSTALL_PARTIAL'           = 121
    'E_INSTALL_DOWNGRADE_REFUSED'           = 122
    'E_INSTALL_DISK_FULL'                   = 123
    'E_INSTALL_INTERNAL'                    = 200
}

# docs/03 §2.3 状态机键名(基线 §8.2);`FAILED:<步名>:<原因码>` 与 `parked` 不是状态本身
$script:QtStates = @(
    'PRECHECK', 'PAYLOAD_STAGED', 'WSL_FEATURE', 'REBOOT_PENDING', 'WSL_MSI',
    'KERNEL_STAGED', 'WSLCONFIG_WRITTEN', 'KERNEL_VERIFIED', 'KERNEL_ROLLED_BACK',
    'DISTRO_IMPORTED', 'IMAGES_LOADED', 'WINAGENT_INSTALLED', 'CLIENTS_CHECKED',
    'SELFTEST_OK', 'DONE'
)

# 顺序执行链(KERNEL_ROLLED_BACK 是终态之一、不在链上,docs/03 §2.3 图)
$script:QtStateChain = @(
    'PRECHECK', 'PAYLOAD_STAGED', 'WSL_FEATURE', 'WSL_MSI',
    'KERNEL_STAGED', 'WSLCONFIG_WRITTEN', 'KERNEL_VERIFIED',
    'DISTRO_IMPORTED', 'IMAGES_LOADED', 'WINAGENT_INSTALLED', 'CLIENTS_CHECKED',
    'SELFTEST_OK', 'DONE'
)

# `/QT_MODE=` 取值(docs/03 §3.4)
$script:QtModes = @('install', 'resume', 'upgrade', 'repair', 'uninstall', 'verify-kernel')

function Get-QtExitTable { [OutputType([System.Collections.Specialized.OrderedDictionary])] param() return $script:QtExitTable }
function Get-QtStateNames { [OutputType([string[]])] param() return , $script:QtStates }
function Get-QtStateChain { [OutputType([string[]])] param() return , $script:QtStateChain }
function Get-QtModes { [OutputType([string[]])] param() return , $script:QtModes }

function Get-QtExitCode {
    <#
    .SYNOPSIS
        名 → 码。接受完整名 `E_INSTALL_DISK_LOW` 或原因码 `DISK_LOW`(docs/03 §3.4 首句的对应关系)。
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param([Parameter(Mandatory)][string] $Name)
    $n = $Name.Trim()
    if ($script:QtExitTable.Contains($n)) { return [int]$script:QtExitTable[$n] }
    $full = 'E_INSTALL_' + $n
    if ($script:QtExitTable.Contains($full)) { return [int]$script:QtExitTable[$full] }
    throw ('未知退出码名:{0}(docs/03 §3.4 表里没有)' -f $Name)
}

function Get-QtExitName {
    <#
    .SYNOPSIS
        码 → 名。一码多名不存在(表里每码唯一)。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][int] $Code)
    foreach ($k in $script:QtExitTable.Keys) {
        if ([int]$script:QtExitTable[$k] -eq $Code) { return $k }
    }
    throw ('未知退出码:{0}' -f $Code)
}

function Get-QtReasonCode {
    <#
    .SYNOPSIS
        退出码名 → `FAILED:<步名>:<原因码>` 里的原因码(去 `E_INSTALL_` 前缀)。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $Name)
    $n = (Get-QtExitName -Code (Get-QtExitCode -Name $Name))
    if ($n -eq 'OK') { return 'OK' }
    return $n.Substring('E_INSTALL_'.Length)
}

function New-QtFailedState {
    <#
    .SYNOPSIS
        组 `FAILED:<步名>:<原因码>`(docs/03 §2.3)。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Step,
        [Parameter(Mandatory)][string] $Reason
    )
    if ($script:QtStates -notcontains $Step) { throw ('未知步名:{0}' -f $Step) }
    return ('FAILED:{0}:{1}' -f $Step, (Get-QtReasonCode -Name $Reason))
}

Export-ModuleMember -Function Get-QtExitTable, Get-QtStateNames, Get-QtStateChain, Get-QtModes,
Get-QtExitCode, Get-QtExitName, Get-QtReasonCode, New-QtFailedState
