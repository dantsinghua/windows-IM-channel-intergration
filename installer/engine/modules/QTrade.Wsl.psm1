# QTrade 安装引擎 —— WSL:功能启用 / MSI / wsl_state / `.wslconfig` 合并写入 / 发行版导入
# 规格:docs/03 §2.4.4(wsl_state 判定顺序)、§2.4.5(.wslconfig 与 kernel_state)、§2.5(功能与 MSI)、
#       §2.6.2(合并规则 R1~R7)、§2.7(导入与首启)、§2.15(命令速查);
#       🔴 `.wslconfig` **受管键表(10 键)**与各键默认值的**唯一出处 = docs/04 §2.7.1**,本模块只引用、不另定义语义。
#requires -Version 5.1
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'QTrade.Native.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Exit.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.State.psm1') -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking

# ── 常量:docs/04 §2.7.1 的 `.wslconfig` **受管键表(10 键)**(归属 03 写的那些)与「不写、不动」集合 ──
# 🔴 裁决(总控 2026-09-20 初裁 ②):docs/04 §2.7.1 与 docs/03 §2.6.2 行文里的「十一键」
#    与两处**逐字列出的键**对不上(逐字列出的是 [wsl2] 8 + [experimental] 2 = 10 个)。
#    **以逐字列出的 10 个为准**,代码里的常量与措辞统一称「受管键表(10 键)」;文档措辞由总控改。
$script:QtWslManagedKeys = @(
    @{ Section = 'wsl2'; Name = 'kernel'; Rule = 'R5' }
    @{ Section = 'wsl2'; Name = 'memory'; Rule = 'R3prime' }
    @{ Section = 'wsl2'; Name = 'processors'; Rule = 'NoWrite' }
    @{ Section = 'wsl2'; Name = 'swap'; Rule = 'R4'; Default = '2GB' }
    @{ Section = 'wsl2'; Name = 'localhostForwarding'; Rule = 'R4'; Default = 'true' }
    @{ Section = 'wsl2'; Name = 'guiApplications'; Rule = 'R4'; Default = 'false' }
    @{ Section = 'wsl2'; Name = 'maxCrashDumpCount'; Rule = 'R7' }
    @{ Section = 'wsl2'; Name = 'crashDumpFolder'; Rule = 'R7' }
    @{ Section = 'experimental'; Name = 'autoMemoryReclaim'; Rule = 'R4'; Default = 'gradual' }
    @{ Section = 'experimental'; Name = 'sparseVhd'; Rule = 'R4'; Default = 'true' }
)

# 「不写、不动」(docs/04 §2.7.1 末两行 + docs/03 §2.6.2 R2)
$script:QtWslUntouchedKeys = @('networkingMode', 'dnsTunneling', 'autoProxy', 'firewall', 'vmIdleTimeout')

# docs/04 §2.7.1 `[wsl] memory_by_physical` 分档表(GB → GB)。本表是 04 的真值引用,改要先改 04。
$script:QtMemoryByPhysical = @(
    @{ MaxPhysGB = 8; MemoryGB = 5 }
    @{ MaxPhysGB = 12; MemoryGB = 8 }
    @{ MaxPhysGB = 16; MemoryGB = 11 }
    @{ MaxPhysGB = 24; MemoryGB = 16 }
    @{ MaxPhysGB = 32; MemoryGB = 24 }
)
# 微信模块开时的覆盖列(docs/04 §2.7.1:只钉了 16G 档 → 10GB)
$script:QtMemoryByPhysicalWeChatOn = @{ 16 = 10 }

function Get-QtWslManagedKeys { [CmdletBinding()] param() return , $script:QtWslManagedKeys }
function Get-QtWslUntouchedKeys { [CmdletBinding()] param() return , $script:QtWslUntouchedKeys }

function Get-QtMemoryByPhysical {
    <#
    .SYNOPSIS
        物理内存 → `.wslconfig memory=` 建议值(docs/04 §2.7.1 分档表,C-43 唯一出处)。
    .PARAMETER WeChatOn
        微信模块开时走 `memory_by_physical_wechat_on` 列(16G 档 11→10)。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][int] $PhysicalMB,
        [switch] $WeChatOn
    )
    $physGB = [math]::Round($PhysicalMB / 1024.0, 0)
    foreach ($row in $script:QtMemoryByPhysical) {
        if ($physGB -le $row.MaxPhysGB) {
            $gb = $row.MemoryGB
            if ($WeChatOn -and $script:QtMemoryByPhysicalWeChatOn.ContainsKey($row.MaxPhysGB)) {
                $gb = $script:QtMemoryByPhysicalWeChatOn[$row.MaxPhysGB]
            }
            return ('{0}GB' -f $gb)
        }
    }
    # >32G → 物理的 75%
    return ('{0}GB' -f [int][math]::Floor($physGB * 0.75))
}

function ConvertFrom-QtMemorySize {
    <#
    .SYNOPSIS
        `11GB` / `6144MB` / `8192` → MB。解析不了回 $null(交由调用方当「无法比较」处理)。
    #>
    [CmdletBinding()]
    param([AllowNull()][AllowEmptyString()][string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $m = [regex]::Match($Value.Trim(), '^(?<n>\d+(?:\.\d+)?)\s*(?<u>GB|MB|KB|B)?$', 'IgnoreCase')
    if (-not $m.Success) { return $null }
    $n = [double]$m.Groups['n'].Value
    $u = $m.Groups['u'].Value.ToUpperInvariant()
    switch ($u) {
        'GB' { return [int]($n * 1024) }
        'MB' { return [int]$n }
        'KB' { return [int]($n / 1024) }
        'B' { return [int]($n / 1MB) }
        default { return [int]$n }   # 无单位按 MB(WSL 的宽松写法)
    }
}

#region .wslconfig 解析 / 校验 / 备份
function ConvertFrom-QtWslConfig {
    <#
    .SYNOPSIS
        逐行解析 `.wslconfig`:**保留原文行,不重排、不去注释**(docs/03 §2.4.5)。
    .OUTPUTS
        {Lines[], Entries[{Index, Section, Key, Value, Raw}]}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Text)
    $lines = @()
    if ($Text.Length -gt 0) { $lines = @($Text -split "`r?`n") }
    $entries = @()
    $section = ''
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $ms = [regex]::Match($line, '^\s*\[(?<s>[^\]]+)\]\s*$')
        if ($ms.Success) { $section = $ms.Groups['s'].Value.Trim(); continue }
        $mk = [regex]::Match($line, '^\s*(?<k>[A-Za-z][A-Za-z0-9_]*)\s*=\s*(?<v>.*)$')
        if ($mk.Success) {
            $entries += [pscustomobject]@{
                Index   = $i
                Section = $section
                Key     = $mk.Groups['k'].Value
                Value   = $mk.Groups['v'].Value.Trim()
                Raw     = $line
            }
        }
    }
    return [pscustomobject]@{ Lines = $lines; Entries = $entries }
}

function Get-QtWslConfigValue {
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)] $Parsed,
        [Parameter(Mandatory)][string] $Section,
        [Parameter(Mandatory)][string] $Key
    )
    $hit = @($Parsed.Entries | Where-Object { $_.Section -eq $Section -and $_.Key -eq $Key })
    if ($hit.Count -eq 0) { return $null }
    return $hit[-1].Value   # 同键多行时 WSL 取最后一行
}

function Test-QtWslConfigParsable {
    <#
    .SYNOPSIS
        写配置前三道前置校验里的第二道(docs/03 §2.6.8 第 9 条 / W3):**无 BOM、无 `\0`、段与键正则全部命中**。
    .OUTPUTS
        {Ok, Reason}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { return [pscustomobject]@{ Ok = $true; Reason = 'NotExists' } }
    $bytes = Read-QtFileBytes -Path $Path -First 3
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return [pscustomobject]@{ Ok = $false; Reason = 'BOM' }       # W3:带 BOM 的 .wslconfig 被 WSL 整段忽略
    }
    $text = Read-QtTextFile -Path $Path
    if ($text.Contains([char]0)) { return [pscustomobject]@{ Ok = $false; Reason = 'UTF16' } }
    foreach ($line in ($text -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -match '^\s*[#;]') { continue }
        if ($line -match '^\s*\[[^\]]+\]\s*$') { continue }
        if ($line -match '^\s*[A-Za-z][A-Za-z0-9_]*\s*=') { continue }
        return [pscustomobject]@{ Ok = $false; Reason = ('BadLine:' + $line.Trim()) }
    }
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}

function Backup-QtWslConfig {
    <#
    .SYNOPSIS
        R1:每次写都新建 `.wslconfig.bak-<yyyyMMdd-HHmmss>`,**永不覆盖旧备份**
        (docs/03 §2.6.2 R1 / docs/04 §2.7.1 R6-14:后缀统一 `bak-<yyyyMMdd-HHmmss>`)。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $BackupDir,
        [string] $Stamp
    )
    if (-not (Test-QtPath -Path $Path)) { return '' }
    if (-not $Stamp) { $Stamp = Get-QtTimestamp }
    New-QtDirectory -Path $BackupDir | Out-Null
    $dest = Join-Path $BackupDir ('.wslconfig.bak-{0}' -f $Stamp)
    $n = 1
    while (Test-QtPath -Path $dest) {
        $dest = Join-Path $BackupDir ('.wslconfig.bak-{0}-{1}' -f $Stamp, $n)
        $n++
    }
    Copy-Item -LiteralPath $Path -Destination $dest -Force
    return $dest
}

function Get-QtWslConfigRollbackBaseline {
    <#
    .SYNOPSIS
        🔴 回滚基线 = **当前文件去掉所有 `kernel=` 行**的内存副本,**不是旧备份文件**(docs/03 §2.6.2 末 / W4)。
        用户其它行逐字保留(含 R3′ 改过的 `memory=`:内核回滚不连带撤销内存调整)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Text)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $kept = @()
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '(?m)^\s*kernel\s*=') { continue }
        $kept += $line
    }
    return ($kept -join "`r`n")
}

function Test-QtWslConfigBackupPoisoned {
    <#
    .SYNOPSIS
        W4:已有备份里若匹配 `(?m)^\s*kernel\s*=` 视为**坏备份**(v2 脚本第 66~69 行的检查)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { return $false }
    return [bool]((Read-QtTextFile -Path $Path) -match '(?m)^\s*kernel\s*=')
}

function Format-QtKernelLine {
    <#
    .SYNOPSIS
        W2:`kernel=C:\\ProgramData\\QTrade\\kernel\\bzImage-6.6` —— 双反斜杠、**不加引号、不用正斜杠**。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $KernelPath)
    return ('kernel={0}' -f ($KernelPath -replace '\\', '\\'))
}

function ConvertFrom-QtKernelLineValue {
    <#
    .SYNOPSIS
        `.wslconfig` 里 `kernel=` 的值 → 真实路径(`\\` → `\`,去首尾空白与引号)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([AllowNull()][AllowEmptyString()][string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    return ($Value.Trim().Trim('"') -replace '\\\\', '\')
}
#endregion

#region .wslconfig 合并写入(R1~R7)
# —— 模块内私有助手(不导出):逐行文本操作的三件小工具 ——
function Get-QtSectionRange {
    [CmdletBinding()]
    param($LineList, [string] $Section)
    $start = -1
    $end = $LineList.Count
    for ($i = 0; $i -lt $LineList.Count; $i++) {
        $m = [regex]::Match($LineList[$i], '^\s*\[(?<s>[^\]]+)\]\s*$')
        if ($m.Success) {
            if ($m.Groups['s'].Value.Trim() -eq $Section) { $start = $i }
            elseif ($start -ge 0) { $end = $i; break }
        }
    }
    return [pscustomobject]@{ Start = $start; End = $end }
}

function Find-QtKeyIndex {
    [CmdletBinding()]
    param($LineList, [string] $Section, [string] $Key)
    $r = Get-QtSectionRange -LineList $LineList -Section $Section
    if ($r.Start -lt 0) { return -1 }
    for ($i = $r.Start + 1; $i -lt $r.End; $i++) {
        if ($LineList[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) { return $i }
    }
    return -1
}

function Add-QtSectionIfMissing {
    [CmdletBinding()]
    param($LineList, [string] $Section)
    $r = Get-QtSectionRange -LineList $LineList -Section $Section
    if ($r.Start -ge 0) { return $r.Start }
    if ($LineList.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($LineList[$LineList.Count - 1])) { [void]$LineList.Add('') }
    [void]$LineList.Add('[' + $Section + ']')
    return ($LineList.Count - 1)
}

function Merge-QtWslConfig {
    <#
    .SYNOPSIS
        docs/03 §2.6.2 的合并规则 R1~R7。**逐行文本操作**,不用 INI 库重排;其它键与注释逐行原样保留。
    .PARAMETER Desired
        本次期望值 hashtable:key → value(只含归属 03 的键;`processors` 传进来也不写)。
    .PARAMETER KernelState
        `DEFAULT|OURS|OURS_STALE|OTHER_CUSTOM`(docs/03 §2.4.5)。`OTHER_CUSTOM` 必须 $ReplaceOtherKernel 才替换。
    .PARAMETER MemoryAdjust
        `raise_if_below`(R3′,缺省)/ `never`(退回 R3 只提示,等价 `/QT_KEEP_WSL_MEMORY=1`)。
    .OUTPUTS
        {Text, KeysAdded[], Kept[], Changed[], ReplacedKernel, KernelLineWritten, Warnings[]}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $Text,
        [Parameter(Mandatory)][hashtable] $Desired,
        [Parameter(Mandatory)][ValidateSet('DEFAULT', 'OURS', 'OURS_STALE', 'OTHER_CUSTOM')][string] $KernelState,
        [ValidateSet('raise_if_below', 'never')][string] $MemoryAdjust = 'raise_if_below',
        [switch] $ReplaceOtherKernel
    )
    $parsed = ConvertFrom-QtWslConfig -Text $Text
    $lines = [System.Collections.ArrayList]::new()
    foreach ($l in $parsed.Lines) { [void]$lines.Add($l) }

    $keysAdded = @()
    $kept = @()
    $changed = @()
    $replacedKernel = ''
    $kernelLineWritten = ''
    $warnings = @()

    foreach ($spec in $script:QtWslManagedKeys) {
        $section = $spec.Section
        $key = $spec.Name
        $rule = $spec.Rule
        if ($rule -eq 'NoWrite') { continue }          # `processors` 04 定「不写」则不写(R4)

        $idx = Find-QtKeyIndex -LineList $lines -Section $section -Key $key
        $existing = $null
        if ($idx -ge 0) {
            $existing = ([regex]::Match($lines[$idx], '^\s*[A-Za-z][A-Za-z0-9_]*\s*=\s*(?<v>.*)$')).Groups['v'].Value.Trim()
        }

        # 期望值:kernel / memory / crash dump 由调用方给;其余取 04 表默认值
        $want = $null
        if ($Desired.ContainsKey($key)) { $want = [string]$Desired[$key] }
        elseif ($spec.ContainsKey('Default')) { $want = [string]$spec.Default }
        if ($null -eq $want -or $want -eq '') {
            if ($rule -eq 'R7') { continue }           # crashDumpFolder 取不到数据盘 → 留系统盘默认、不写(§2.6.2 R7 ①)
            if ($rule -eq 'R5') { continue }
            continue
        }

        switch ($rule) {
            'R5' {
                # kernel:唯一允许改已有值的键
                $kernelLine = Format-QtKernelLine -KernelPath $want
                if ($idx -lt 0) {
                    # DEFAULT → 插到 [wsl2] 段首行
                    $secStart = Add-QtSectionIfMissing -LineList $lines -Section $section
                    $lines.Insert($secStart + 1, $kernelLine)
                    $keysAdded += $key
                } else {
                    if ($KernelState -eq 'OTHER_CUSTOM' -and -not $ReplaceOtherKernel) {
                        throw 'OTHER_CUSTOM 内核未获用户确认替换(docs/03 §2.4.5:E_INSTALL_OTHER_CUSTOM_KERNEL_DECLINED)'
                    }
                    if ($KernelState -eq 'OTHER_CUSTOM') {
                        $replacedKernel = ConvertFrom-QtKernelLineValue -Value $existing
                    }
                    if ($existing -ne ($kernelLine -replace '^kernel=', '')) {
                        $changed += [pscustomobject]@{ key = $key; from = $existing; to = ($kernelLine -replace '^kernel=', '') }
                    }
                    $lines[$idx] = $kernelLine
                }
                $kernelLineWritten = $kernelLine
            }
            'R3prime' {
                # memory:安装器可直接改(B-5),但只在「已有值低于查表值」时
                if ($idx -lt 0) {
                    $secStart = Add-QtSectionIfMissing -LineList $lines -Section $section
                    $r = Get-QtSectionRange -LineList $lines -Section $section
                    $lines.Insert($r.End, ('{0}={1}' -f $key, $want))
                    $keysAdded += $key
                } else {
                    $curMB = ConvertFrom-QtMemorySize -Value $existing
                    $wantMB = ConvertFrom-QtMemorySize -Value $want
                    if ($MemoryAdjust -eq 'never' -or $null -eq $curMB -or $null -eq $wantMB -or $curMB -ge $wantMB) {
                        $kept += [pscustomobject]@{ key = $key; value = $existing; suggested = $want }
                    } else {
                        $changed += [pscustomobject]@{ key = $key; from = $existing; to = $want }
                        $lines[$idx] = ('{0}={1}' -f $key, $want)
                    }
                }
            }
            'R7' {
                # crash dump 两键:①键缺失 → 必写我方值;②已有用户值 → 保留不改(>2 告警)
                if ($idx -lt 0) {
                    $secStart = Add-QtSectionIfMissing -LineList $lines -Section $section
                    $r = Get-QtSectionRange -LineList $lines -Section $section
                    $lines.Insert($r.End, ('{0}={1}' -f $key, $want))
                    $keysAdded += $key
                } else {
                    $kept += [pscustomobject]@{ key = $key; value = $existing; suggested = $want }
                    if ($key -eq 'maxCrashDumpCount') {
                        $cur = 0
                        if ([int]::TryParse($existing, [ref]$cur) -and $cur -gt 2) {
                            $warnings += ('maxCrashDumpCount={0} 高于建议值 2(每个转储实测 16 GB/个,docs/03 §2.6.2 R7)' -f $cur)
                        }
                    }
                }
            }
            default {
                # R3/R4:已有值一律不动只记 kept;无值才按 04 表默认写入
                if ($idx -lt 0) {
                    $secStart = Add-QtSectionIfMissing -LineList $lines -Section $section
                    $r = Get-QtSectionRange -LineList $lines -Section $section
                    $lines.Insert($r.End, ('{0}={1}' -f $key, $want))
                    $keysAdded += $key
                } else {
                    $kept += [pscustomobject]@{ key = $key; value = $existing; suggested = $want }
                }
            }
        }
    }

    return [pscustomobject]@{
        Text              = ($lines -join "`r`n")
        KeysAdded         = $keysAdded
        Kept              = $kept
        Changed           = $changed
        ReplacedKernel    = $replacedKernel
        KernelLineWritten = $kernelLineWritten
        Warnings          = $warnings
    }
}

function Remove-QtWslConfigKeys {
    <#
    .SYNOPSIS
        卸载:只删我们**新增**的键与我们的 `kernel=` 行,其它行逐字不动(docs/03 §2.14 第 3 步)。
    .PARAMETER KernelPathPattern
        我方内核行的识别正则,缺省 `^\s*kernel\s*=.*QTrade\\kernel.*$`(现有 uninstall.ps1 做法)。
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $Text,
        [string[]] $KeysAdded = @(),
        [string] $KernelPathPattern = '^\s*kernel\s*=.*QTrade\\\\kernel.*$',
        [switch] $RemoveKernelLine
    )
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $out = @()
    foreach ($line in ($Text -split "`r?`n")) {
        if ($RemoveKernelLine -and $line -match $KernelPathPattern) { continue }
        $drop = $false
        foreach ($k in $KeysAdded) {
            if ($k -eq 'kernel') { continue }
            if ($line -match ('^\s*' + [regex]::Escape($k) + '\s*=')) { $drop = $true; break }
        }
        if ($drop) { continue }
        $out += $line
    }
    return ($out -join "`r`n")
}
#endregion

#region wsl_state / kernel_state / 发行版
function Get-QtWslStateFromProbe {
    <#
    .SYNOPSIS
        docs/03 §2.4.4 的判定顺序,做成纯函数以便单测(外部事实由调用方先采好)。
    .PARAMETER Probe
        {VirtOk, PolicyBlocked, FeatureVmp, FeatureWsl, WslExeExists, VersionOk, VersionString, StatusOk, AllDistrosV1, DefaultVersion}
    .OUTPUTS
        `VIRT_DISABLED|POLICY_BLOCKED|NONE|FEATURE_OFF|WSL2_STORE|WSL2_INBOX|WSL1_ONLY`
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)] $Probe)
    if (-not $Probe.VirtOk) { return 'VIRT_DISABLED' }
    if ($Probe.PolicyBlocked) { return 'POLICY_BLOCKED' }
    $vmp = [string]$Probe.FeatureVmp
    $wsl = [string]$Probe.FeatureWsl
    if ($vmp -ne 'Enabled' -or $wsl -ne 'Enabled') {
        if ($vmp -eq 'Disabled' -and $wsl -eq 'Disabled' -and -not $Probe.WslExeExists) { return 'NONE' }
        return 'FEATURE_OFF'
    }
    if ($Probe.AllDistrosV1 -and [int]$Probe.DefaultVersion -eq 1) { return 'WSL1_ONLY' }
    if ($Probe.VersionOk) { return 'WSL2_STORE' }
    if ($Probe.StatusOk) { return 'WSL2_INBOX' }
    return 'FEATURE_OFF'
}

function Get-QtWslVersionString {
    <#
    .SYNOPSIS
        从 `wsl --version` 输出里抠版本(中英文两种首行)。docs/03 §2.15。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Output)
    $m = [regex]::Match($Output, '(?:WSL\s*版本|WSL\s*version)\s*[::]\s*(?<v>\d+\.\d+\.\d+)')
    if ($m.Success) { return $m.Groups['v'].Value }
    return ''
}

function Test-QtWslVersionAtLeast {
    <#
    .SYNOPSIS
        6.6 内核线的前提:`wsl --version` ≥ 2.4.0(docs/03 §2.5.2 / §2.6.7 K7)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string] $Version,
        [string] $Minimum = '2.4.0'
    )
    if ([string]::IsNullOrWhiteSpace($Version)) { return $false }
    try { return ([version]$Version -ge [version]$Minimum) } catch { return $false }
}

function Get-QtKernelState {
    <#
    .SYNOPSIS
        docs/03 §2.4.5 的 `kernel_state` 判定。纯函数:外部事实由调用方采好。
    .PARAMETER KernelValue
        `.wslconfig` 里 `kernel=` 的原始值(不存在传 $null)。
    .PARAMETER OurKernelSha256 / ActualSha256
        本包内核 sha256 与该路径文件实际 sha256(读不到传 '')。
    .OUTPUTS
        `DEFAULT|OURS|OURS_STALE|OTHER_CUSTOM`
    #>
    [CmdletBinding()][OutputType([string])]
    param(
        [AllowNull()][AllowEmptyString()][string] $KernelValue,
        [AllowEmptyString()][string] $OurKernelSha256 = '',
        [AllowEmptyString()][string] $ActualSha256 = '',
        [string[]] $OurPathPatterns = @('*\QTrade\kernel\bzImage-*', '*\.qtrade-redroid\bzImage*')
    )
    if ([string]::IsNullOrWhiteSpace($KernelValue)) { return 'DEFAULT' }
    $path = ConvertFrom-QtKernelLineValue -Value $KernelValue
    $isOurs = $false
    foreach ($p in $OurPathPatterns) { if ($path -like $p) { $isOurs = $true; break } }
    if (-not $isOurs) { return 'OTHER_CUSTOM' }
    if ([string]::IsNullOrWhiteSpace($ActualSha256)) { return 'OURS_STALE' }
    if ($ActualSha256.ToLowerInvariant() -eq $OurKernelSha256.ToLowerInvariant()) { return 'OURS' }
    return 'OURS_STALE'
}

function ConvertFrom-QtWslListVerbose {
    <#
    .SYNOPSIS
        解析 `wsl --list --verbose`(已剥 `\0`)。
    .OUTPUTS
        {Name, State, Version, Default}[]
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Output)
    $rows = @()
    foreach ($line in ($Output -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $m = [regex]::Match($line, '^\s*(?<d>\*)?\s*(?<n>\S+)\s+(?<s>\S+)\s+(?<v>\d+)\s*$')
        if (-not $m.Success) { continue }
        if ($m.Groups['n'].Value -in @('NAME', '名称')) { continue }
        $rows += [pscustomobject]@{
            Name    = $m.Groups['n'].Value
            State   = $m.Groups['s'].Value
            Version = [int]$m.Groups['v'].Value
            Default = $m.Groups['d'].Success
        }
    }
    return , $rows
}

function Enable-QtWslFeatures {
    <#
    .SYNOPSIS
        WSL_FEATURE:启用「虚拟机平台」+「适用于 Linux 的 Windows 子系统」两项。
        **不启用 Microsoft-Hyper-V**(docs/03 §2.5.1)。
    #>
    [CmdletBinding()]
    param()
    return Enable-QtOptionalFeature -FeatureName @('VirtualMachinePlatform', 'Microsoft-Windows-Subsystem-Linux')
}

function Test-QtWslFeaturesEnabled {
    <#
    .SYNOPSIS
        WSL_FEATURE 幂等判据:两功能 `Enabled`(docs/03 §2.3)。
    #>
    [CmdletBinding()][OutputType([bool])]
    param()
    return ((Get-QtOptionalFeatureState -FeatureName 'VirtualMachinePlatform') -eq 'Enabled' -and
        (Get-QtOptionalFeatureState -FeatureName 'Microsoft-Windows-Subsystem-Linux') -eq 'Enabled')
}

function Install-QtWslMsi {
    <#
    .SYNOPSIS
        WSL_MSI:`msiexec /i wsl.msi /qn /norestart /l*v <log>`(docs/03 §2.5.2 / §2.15)。
        🔴 **不调 `wsl --update`**(会联网,违反 §11.5 [OFFLINE])。
    .OUTPUTS
        {Ok, ExitCode, RebootRequired, Reason}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $MsiPath,
        [Parameter(Mandatory)][string] $LogPath,
        [int] $TimeoutSec = 600
    )
    $r = Invoke-QtProcess -FilePath 'msiexec.exe' -ArgumentList @('/i', $MsiPath, '/qn', '/norestart', '/l*v', $LogPath) -TimeoutSec $TimeoutSec
    switch ($r.ExitCode) {
        0 { return [pscustomobject]@{ Ok = $true; ExitCode = 0; RebootRequired = $false; Reason = '' } }
        1638 { return [pscustomobject]@{ Ok = $true; ExitCode = 1638; RebootRequired = $false; Reason = 'AlreadyNewer' } }   # 已装更高版本 = 成功
        3010 { return [pscustomobject]@{ Ok = $true; ExitCode = 3010; RebootRequired = $true; Reason = 'RebootRequired' } }
        default { return [pscustomobject]@{ Ok = $false; ExitCode = $r.ExitCode; RebootRequired = $false; Reason = 'WSL_MSI_FAILED' } }
    }
}

function Get-QtWslBrokenReason {
    <#
    .SYNOPSIS
        把 `wsl.exe` 的已知错误码翻成给用户的原因(docs/03 §2.5.2 末)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Output)
    if ($Output -match '0x80370102') { return 'VIRT_DISABLED_IN_BIOS' }   # 虚拟化未启用 → 指回 BIOS
    if ($Output -match '0x800701bc') { return 'INBOX_WSL_NO_KERNEL' }      # 内置 WSL 缺内核包 → 走 MSI
    if ($Output -match '0x80370114') { return 'VMP_NOT_ENABLED' }          # 「虚拟机平台」未启用 → 回 WSL_FEATURE
    return ''
}

function Import-QtDistro {
    <#
    .SYNOPSIS
        `wsl --import qtrade <dir> <tar> --version 2`(docs/03 §2.7.2)。
        磁盘不足表现为 `0x80070070` → 映射 `DISK_FULL`(123),**不是**预检门槛 `DISK_LOW`(26)。
    .OUTPUTS
        {Ok, Reason, ExitCode}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][string] $Directory,
        [Parameter(Mandatory)][string] $TarPath,
        [int] $TimeoutSec = 1800
    )
    New-QtDirectory -Path $Directory | Out-Null
    $r = Invoke-QtWsl -WslArgs @('--import', $Name, $Directory, $TarPath, '--version', '2') -TimeoutSec $TimeoutSec
    if ($r.TimedOut) { return [pscustomobject]@{ Ok = $false; Reason = 'IMPORT_FAILED'; ExitCode = -1 } }
    if ($r.ExitCode -eq 0) { return [pscustomobject]@{ Ok = $true; Reason = ''; ExitCode = 0 } }
    $all = ($r.StdOut + $r.StdErr)
    if ($all -match '0x80070070') { return [pscustomobject]@{ Ok = $false; Reason = 'DISK_FULL'; ExitCode = $r.ExitCode } }
    return [pscustomobject]@{ Ok = $false; Reason = 'IMPORT_FAILED'; ExitCode = $r.ExitCode }
}

function Unregister-QtDistro {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Name, [int] $TimeoutSec = 120)
    return Invoke-QtWsl -WslArgs @('--unregister', $Name) -TimeoutSec $TimeoutSec
}

function Wait-QtDistroSystemd {
    <#
    .SYNOPSIS
        首启就绪:`systemctl is-system-running --wait`,`running|degraded` 都算起(degraded 记警告)。
        超时 180 s(docs/03 §2.15)。
    .OUTPUTS
        {Ok, Status, Degraded}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Name, [int] $TimeoutSec = 180)
    $r = Invoke-QtWsl -WslArgs @('-d', $Name, '--exec', 'systemctl', 'is-system-running', '--wait') -TimeoutSec $TimeoutSec
    $status = ($r.StdOut).Trim()
    if ($r.TimedOut) { return [pscustomobject]@{ Ok = $false; Status = 'timeout'; Degraded = $false } }
    if ($status -in @('running', 'degraded')) {
        return [pscustomobject]@{ Ok = $true; Status = $status; Degraded = ($status -eq 'degraded') }
    }
    return [pscustomobject]@{ Ok = $false; Status = $status; Degraded = $false }
}

function Select-QtDockerPool {
    <#
    .SYNOPSIS
        安装期选 docker 地址池(docs/03 §2.7.3;🔴 候选表与冲突判定的**唯一出处 = docs/04 §2.7.4**,本册只引用)。
        取第一个与 Windows 路由表**不相交**的候选;全冲突 → 取第一个并记 `warn`(DOCKER_POOL_ALL_CONFLICT),**不拒装**。
    .PARAMETER Candidates
        来自 `agent.toml [net] docker_pool_candidates`(04 现定 10.213/10.231/10.247/10.199 四个 /16)。
    .PARAMETER OccupiedPrefixes
        `Get-NetRoute` 全部 Up 接口的目的前缀 + 本机地址前缀 + vEthernet(WSL) 子网。
    .OUTPUTS
        {Cidr, AllConflict, Warn}
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]] $Candidates,
        [string[]] $OccupiedPrefixes = @()
    )
    foreach ($c in $Candidates) {
        $hit = $false
        foreach ($p in $OccupiedPrefixes) {
            if (Test-QtCidrOverlap -A $c -B $p) { $hit = $true; break }
        }
        if (-not $hit) { return [pscustomobject]@{ Cidr = $c; AllConflict = $false; Warn = '' } }
    }
    return [pscustomobject]@{ Cidr = $Candidates[0]; AllConflict = $true; Warn = 'DOCKER_POOL_ALL_CONFLICT' }
}

function ConvertTo-QtCidrPair {
    # 私有助手:'10.213.0.0/16' -> {Net, Mask, Len};非法或非 IPv4 回 $null
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Cidr)
    $parts = $Cidr.Split('/')
    if ($parts.Count -ne 2) { return $null }
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($parts[0], [ref]$ip)) { return $null }
    if ($ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { return $null }
    $len = 0
    if (-not [int]::TryParse($parts[1], [ref]$len)) { return $null }
    if ($len -lt 0 -or $len -gt 32) { return $null }
    $b = $ip.GetAddressBytes()
    [array]::Reverse($b)
    $v = [uint32][BitConverter]::ToUInt32($b, 0)
    # ⚠️ 不能写 0xFFFFFFFF:PowerShell 把它当 [int] = -1,转 uint64 会溢出
    $allOnes = [uint64]4294967295
    $mask = if ($len -eq 0) { [uint32]0 } else { [uint32](($allOnes -shl (32 - $len)) -band $allOnes) }
    return [pscustomobject]@{ Net = ([uint32]($v -band $mask)); Mask = $mask; Len = $len }
}

function Test-QtCidrOverlap {
    <#
    .SYNOPSIS
        前缀相交判定(docs/04 §2.7.4 的 `ip_network.overlaps`)。IPv4。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][string] $A, [Parameter(Mandatory)][string] $B)
    $pa = ConvertTo-QtCidrPair -Cidr $A
    $pb = ConvertTo-QtCidrPair -Cidr $B
    if ($null -eq $pa -or $null -eq $pb) { return $false }
    $shorter = if ($pa.Len -le $pb.Len) { $pa.Mask } else { $pb.Mask }
    return (([uint32]($pa.Net -band $shorter)) -eq ([uint32]($pb.Net -band $shorter)))
}
#endregion

Register-QtStepCheck -Step 'WSL_FEATURE' -Check { param($ctx) Test-QtWslFeaturesEnabled }

Export-ModuleMember -Function Get-QtWslManagedKeys, Get-QtWslUntouchedKeys, Get-QtMemoryByPhysical,
ConvertFrom-QtMemorySize, ConvertFrom-QtWslConfig, Get-QtWslConfigValue, Test-QtWslConfigParsable,
Backup-QtWslConfig, Get-QtWslConfigRollbackBaseline, Test-QtWslConfigBackupPoisoned,
Format-QtKernelLine, ConvertFrom-QtKernelLineValue, Merge-QtWslConfig, Remove-QtWslConfigKeys,
Get-QtWslStateFromProbe, Get-QtWslVersionString, Test-QtWslVersionAtLeast, Get-QtKernelState,
ConvertFrom-QtWslListVerbose, Enable-QtWslFeatures, Test-QtWslFeaturesEnabled, Install-QtWslMsi,
Get-QtWslBrokenReason, Import-QtDistro, Unregister-QtDistro, Wait-QtDistroSystemd,
Select-QtDockerPool, Test-QtCidrOverlap
