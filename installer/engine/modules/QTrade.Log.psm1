# QTrade 安装引擎 —— 日志模块
# 规格:docs/03 §3.1(每行 `时间 级别 步名 消息`,UTF-8,不记密码/令牌)、§2.6.8 第 8 条(子进程输出落同目录同前缀子日志)
# 🔴 本文件必须 UTF-8 with BOM(docs/03 §2.6.7 W1;Windows PowerShell 5.1 按系统 ANSI 解析无 BOM 脚本,中文注释会引发 ParserError)
#requires -Version 5.1
Set-StrictMode -Version Latest

$script:QtLogPath = $null
$script:QtLogDir = $null
$script:QtLogPrefix = 'install'
$script:QtLogStep = 'ENGINE'

# 需要在日志里抹掉的敏感片段(docs/03 §3.1「不记密码/令牌」、§6「日志不含密码、令牌」)
$script:QtRedactPatterns = @(
    '(?i)(token|passwd|password|secret|entropy|api[_-]?key)(\s*[:=]\s*|"\s*:\s*")([^\s",]+)'
    '(?i)(Authorization:\s*Bearer\s+)(\S+)'
)

function Protect-QtLogText {
    <#
    .SYNOPSIS
        抹掉日志文本里的令牌/口令片段。docs/03 §3.1 / §6。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [AllowNull()][AllowEmptyString()]
        [string] $Text
    )
    if ([string]::IsNullOrEmpty($Text)) { return $Text }
    $out = $Text
    $out = [regex]::Replace($out, $script:QtRedactPatterns[0], { param($m) $m.Groups[1].Value + $m.Groups[2].Value + '***' })
    $out = [regex]::Replace($out, $script:QtRedactPatterns[1], { param($m) $m.Groups[1].Value + '***' })
    return $out
}

function Initialize-QtLog {
    <#
    .SYNOPSIS
        建 `%ProgramData%\QTrade\logs\install-<yyyyMMdd-HHmmss>.log`。docs/03 §3.1。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Directory,
        [string] $Prefix = 'install',
        [string] $Stamp
    )
    if (-not $Stamp) { $Stamp = (Get-Date).ToString('yyyyMMdd-HHmmss') }
    if (-not (Test-Path -LiteralPath $Directory)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }
    $script:QtLogDir = $Directory
    $script:QtLogPrefix = $Prefix
    $script:QtLogPath = Join-Path $Directory ('{0}-{1}.log' -f $Prefix, $Stamp)
    if (-not (Test-Path -LiteralPath $script:QtLogPath)) {
        [IO.File]::WriteAllText($script:QtLogPath, '', (New-Object Text.UTF8Encoding($false)))
    }
    return $script:QtLogPath
}

function Get-QtLogPath { [OutputType([string])] param() return $script:QtLogPath }

function Get-QtSubLogPath {
    <#
    .SYNOPSIS
        子进程日志:同目录同前缀(msiexec / robocopy / wsl 各自一份)。docs/03 §3.1。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $Name)
    if (-not $script:QtLogPath) { throw 'QTrade.Log 未初始化:先调用 Initialize-QtLog' }
    $base = [IO.Path]::GetFileNameWithoutExtension($script:QtLogPath)
    return (Join-Path $script:QtLogDir ('{0}.{1}.log' -f $base, $Name))
}

function Set-QtLogStep {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Step)
    $script:QtLogStep = $Step
}

function Get-QtLogStep { [OutputType([string])] param() return $script:QtLogStep }

function Format-QtLogLine {
    <#
    .SYNOPSIS
        单行格式化(供单测直接验证格式,不落盘)。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Level,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Message,
        [string] $Step,
        [datetime] $At
    )
    if (-not $Step) { $Step = $script:QtLogStep }
    if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-Date }
    $clean = (Protect-QtLogText -Text $Message) -replace '\r?\n', ' | '
    return ('{0} {1} {2} {3}' -f $At.ToString('yyyy-MM-dd HH:mm:ss.fff'), $Level.ToUpperInvariant().PadRight(5), $Step, $clean)
}

function Write-QtLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][AllowEmptyString()][string] $Message,
        [ValidateSet('DEBUG', 'INFO', 'WARN', 'ERROR')][string] $Level = 'INFO',
        [string] $Step
    )
    $line = Format-QtLogLine -Level $Level -Message $Message -Step $Step
    if ($script:QtLogPath) {
        # 追加写,UTF-8 无 BOM(日志给人看也给 CI grep,BOM 会污染首行)
        [IO.File]::AppendAllText($script:QtLogPath, $line + "`r`n", (New-Object Text.UTF8Encoding($false)))
    }
    Write-Verbose $line
    return $line
}

Export-ModuleMember -Function Initialize-QtLog, Get-QtLogPath, Get-QtSubLogPath, Set-QtLogStep,
Get-QtLogStep, Write-QtLog, Format-QtLogLine, Protect-QtLogText
