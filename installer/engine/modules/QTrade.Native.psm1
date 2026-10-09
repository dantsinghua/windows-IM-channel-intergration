# QTrade 安装引擎 —— 外部世界的唯一出入口(mock 接缝)
# 🔴 设计约束:**引擎里所有对 wsl.exe / sc.exe / msiexec / 注册表 / WMI / 文件系统 / HTTP / 时钟的访问都必须经本模块**,
#    别的模块一律不直接调原生命令。理由:①单测可以整模块 Mock,不碰真机(禁区:开发机绝不真装);
#    ②`wsl.exe` 挂起是常态故障,超时包装只写一处(docs/03 §2.6.7 W5);③UTF-16/`\0` 剥离只写一处(W6)。
#requires -Version 5.1
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'QTrade.Log.psm1') -DisableNameChecking
$script:QtWslLogPath = ''
$script:QtWslLogStep = ''

#region 时钟(测试可注入)
function Get-QtNow { [OutputType([datetime])] param() return (Get-Date) }
function Get-QtTimestamp { [OutputType([string])] param([datetime] $At) if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }; return $At.ToString('yyyyMMdd-HHmmss') }
function Get-QtIso8601 { [OutputType([string])] param([datetime] $At) if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }; return $At.ToString('yyyy-MM-ddTHH:mm:sszzz') }
function Get-QtEpochMs { [OutputType([long])] param([datetime] $At) if (-not $PSBoundParameters.ContainsKey('At')) { $At = Get-QtNow }; return [long]([DateTimeOffset]$At).ToUnixTimeMilliseconds() }
#endregion

#region 进程
function New-QtProcessResult {
    [CmdletBinding()]
    param([int] $ExitCode = 0, [string] $StdOut = '', [string] $StdErr = '', [bool] $TimedOut = $false, [double] $DurationMs = 0)
    return [pscustomobject]@{
        ExitCode   = $ExitCode
        StdOut     = $StdOut
        StdErr     = $StdErr
        TimedOut   = $TimedOut
        DurationMs = $DurationMs
    }
}

function ConvertTo-QtProcessArgument {
    [CmdletBinding()][OutputType([string])]
    param([AllowNull()][AllowEmptyString()][string] $Value)
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    # CRT argv: double backslashes before a quote and before the closing quote.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function ConvertFrom-QtProcessBytes {
    [CmdletBinding()][OutputType([string])]
    param([byte[]] $Bytes)
    if ($Bytes.Length -eq 0) { return '' }
    # Preserve ReadAllText's BOM detection as well as WSL's UTF-8 default.
    $stream = New-Object IO.MemoryStream(, $Bytes)
    $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
    try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
}

function Invoke-QtProcess {
    # Own the process handle; drain both pipes before waiting for its exit.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [string[]] $ArgumentList = @(),
        [ValidateRange(1, 2147483)][int] $TimeoutSec = 120,
        [string] $WorkingDirectory,
        [AllowEmptyString()][string] $StandardInput,
        [hashtable] $Environment = @{}
    )
    $p = New-Object Diagnostics.Process
    $so = New-Object IO.MemoryStream
    $se = New-Object IO.MemoryStream
    $started = $false
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $si = $p.StartInfo
        $si.FileName = $FilePath
        $si.Arguments = (@($ArgumentList | ForEach-Object { ConvertTo-QtProcessArgument -Value $_ }) -join ' ')
        $si.UseShellExecute = $false
        $si.CreateNoWindow = $true
        $si.RedirectStandardOutput = $true
        $si.RedirectStandardError = $true
        $si.RedirectStandardInput = $PSBoundParameters.ContainsKey('StandardInput')
        if ($WorkingDirectory) { $si.WorkingDirectory = $WorkingDirectory }
        foreach ($key in $Environment.Keys) { $si.EnvironmentVariables[$key] = [string]$Environment[$key] }
        $started = $p.Start()
        if (-not $started) { throw 'Process did not start.' }
        $outTask = $p.StandardOutput.BaseStream.CopyToAsync($so)
        $errTask = $p.StandardError.BaseStream.CopyToAsync($se)
        $timedOut = $false
        $inputError = ''
        if ($si.RedirectStandardInput) {
            $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($StandardInput)
            $inputTask = $p.StandardInput.BaseStream.WriteAsync($bytes, 0, $bytes.Length)
            $remaining = [Math]::Max(0, $TimeoutSec * 1000 - [int]$sw.ElapsedMilliseconds)
            try { $timedOut = -not $inputTask.Wait($remaining) }
            catch { $inputError = 'Standard input was not fully consumed.' }
            if (-not $timedOut) { try { $p.StandardInput.Close() } catch { $inputError = 'Standard input was not fully consumed.' } }
        }
        if (-not $timedOut) {
            $remaining = [Math]::Max(0, $TimeoutSec * 1000 - [int]$sw.ElapsedMilliseconds)
            $timedOut = -not $p.WaitForExit($remaining)
        }
        if ($timedOut) {
            # Only terminate this owned handle, never a name or a process group.
            if (-not $p.HasExited) { $p.Kill() }
            if (-not $p.WaitForExit(5000)) { throw 'Timed-out child did not exit.' }
        }
        $exitCode = if ($timedOut) { -1 } else { $p.ExitCode }
        if ($inputError -and $exitCode -eq 0) { $exitCode = -1 }
        # A descendant may retain an inherited pipe. Do not wait indefinitely.
        foreach ($task in @($outTask, $errTask)) {
            try { [void]$task.Wait(1000) } catch { }
        }
        $p.StandardOutput.Close()
        $p.StandardError.Close()
        $out = ConvertFrom-QtProcessBytes -Bytes $so.ToArray()
        $err = ConvertFrom-QtProcessBytes -Bytes $se.ToArray()
        if ($inputError) { $err += "`n" + $inputError }
        return (New-QtProcessResult -ExitCode $exitCode -StdOut $out -StdErr $err -TimedOut $timedOut -DurationMs $sw.Elapsed.TotalMilliseconds)
    } finally {
        if ($started) {
            try {
                if (-not $p.HasExited) { $p.Kill(); [void]$p.WaitForExit(5000) }
            } catch { }
        }
        $p.Dispose()
        $so.Dispose()
        $se.Dispose()
        $sw.Stop()
    }
}

function Set-QtWslLogContext {
    [CmdletBinding()]
    param([AllowEmptyString()][string] $Path = '', [string] $Step = '')
    $script:QtWslLogPath = $Path
    $script:QtWslLogStep = $Step
}

function Get-QtProcessDiagnostic {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Result, [string] $Stage = '', [switch] $Sensitive)
    $out = Protect-QtLogText -Text ([string]$Result.StdOut)
    $err = Protect-QtLogText -Text ([string]$Result.StdErr)
    if ($Sensitive) { $out = '[redacted: sensitive stdin]'; $err = '[redacted: sensitive stdin]' }
    if ($out.Length -gt 32768) { $out = $out.Substring(0, 32768) + '[truncated]' }
    if ($err.Length -gt 32768) { $err = $err.Substring(0, 32768) + '[truncated]' }
    return [pscustomobject]@{ stage = $Stage; exit = $Result.ExitCode; timed_out = $Result.TimedOut; duration_ms = $Result.DurationMs; stdout = $out; stderr = $err }
}

function ConvertFrom-QtWslOutput {
    <#
    .SYNOPSIS
        剥掉 wsl.exe 的 UTF-16 `\0` 与 BOM(docs/03 §2.6.7 W6)。
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([AllowNull()][AllowEmptyString()][string] $Text)
    if ($null -eq $Text) { return '' }
    # ⚠️ BOM 用 [char]0xFEFF 显式构造,不在源码里放一个**看不见的**字符 ——
    #    不可见字符经任何编辑器/规范化工具都可能被悄悄吃掉,那时剥 BOM 会静默失效
    $bom = [string][char]0xFEFF
    return ($Text -replace "`0", '' -replace ("^" + [regex]::Escape($bom)), '')
}

function Invoke-QtWsl {
    <#
    .SYNOPSIS
        **所有** wsl.exe 调用的唯一入口:强制 `WSL_UTF8=1`、强制超时、剥 `\0`。
        docs/03 §2.6.7 W5(v1 脚本 `$args` 自动变量被清空的坑)/ W6 / §2.15。
    .OUTPUTS
        {ExitCode, StdOut, StdErr, TimedOut, DurationMs};超时时 ExitCode = -1、TimedOut = $true。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]] $WslArgs,
        [int] $TimeoutSec = 60,
        [AllowEmptyString()][string] $StandardInput
    )
    $invoke = @{ FilePath = 'wsl.exe'; ArgumentList = $WslArgs; TimeoutSec = $TimeoutSec; Environment = @{ WSL_UTF8 = '1' } }
    $sensitive = $PSBoundParameters.ContainsKey('StandardInput')
    if ($sensitive) { $invoke.StandardInput = $StandardInput }
    $r = Invoke-QtProcess @invoke
    $result = New-QtProcessResult -ExitCode $r.ExitCode `
                -StdOut (ConvertFrom-QtWslOutput -Text $r.StdOut) `
                -StdErr (ConvertFrom-QtWslOutput -Text $r.StdErr) `
                -TimedOut $r.TimedOut -DurationMs $r.DurationMs
    if ($script:QtWslLogPath) {
        $record = Get-QtProcessDiagnostic -Result $result -Stage $script:QtWslLogStep -Sensitive:$sensitive
        try {
            [IO.File]::AppendAllText($script:QtWslLogPath, ($record | ConvertTo-Json -Compress) + "`r`n", (New-Object Text.UTF8Encoding($false)))
        } catch { Write-Verbose 'Could not persist WSL diagnostic output.' }
    }
    return $result
}

function Get-QtProcessByName {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]] $Name)
    return @(Get-Process -Name $Name -ErrorAction SilentlyContinue)
}
#endregion

#region 文件系统
function Test-QtPath {
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Path)
    if ([string]::IsNullOrEmpty($Path)) { return $false }
    return [bool](Test-Path -LiteralPath $Path)
}

function New-QtDirectory {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    return $Path
}

function Remove-QtItem {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path, [switch] $Recurse)
    if (Test-QtPath -Path $Path) { Remove-Item -LiteralPath $Path -Force -Recurse:$Recurse -ErrorAction Stop }
}

function Read-QtTextFile {
    <#
    .SYNOPSIS
        读全文。**必须用 [IO.File]::ReadAllText,不用 Get-Content**——后者会剥 BOM,
        让 `.wslconfig` 的 BOM 预检误判「无 BOM」(docs/03 §2.6.7 W3)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $Path)
    return [IO.File]::ReadAllText($Path)
}

function Read-QtFileBytes {
    [CmdletBinding()][OutputType([byte[]])]
    param([Parameter(Mandatory)][string] $Path, [int] $First = 0)
    $b = [IO.File]::ReadAllBytes($Path)
    if ($First -gt 0 -and $b.Length -gt $First) { return $b[0..($First - 1)] }
    return $b
}

function Write-QtUtf8NoBom {
    <#
    .SYNOPSIS
        写 UTF-8 **无 BOM**、CRLF —— `.wslconfig` 唯一允许的写法(docs/03 §2.6.7 W3、§2.15)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Text
    )
    $normalized = ($Text -replace "`r`n", "`n") -replace "`n", "`r`n"
    [IO.File]::WriteAllText($Path, $normalized, (New-Object Text.UTF8Encoding($false)))
    return $Path
}

function Move-QtFileAtomic {
    <#
    .SYNOPSIS
        原子替换:先写临时文件再替换(docs/03 §2.3「每次状态变更原子写:写临时文件再 MoveFileEx 替换」)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Source,
        [Parameter(Mandatory)][string] $Destination
    )
    if (Test-QtPath -Path $Destination) {
        # ⚠️ 第三参必须是**真 null**:PowerShell 的 $null 传给 string 形参会变成空串,
        #    File.Replace 会抛「路径的形式不合法」——第二次写状态时才暴露,正是可重入路径上的坑
        [IO.File]::Replace($Source, $Destination, [NullString]::Value)
    } else {
        [IO.File]::Move($Source, $Destination)
    }
    return $Destination
}

function Get-QtFileHash {
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-QtFileSize {
    [CmdletBinding()][OutputType([long])]
    param([Parameter(Mandatory)][string] $Path)
    return [long](Get-Item -LiteralPath $Path).Length
}

function Get-QtFileVersion {
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { return '' }
    return [string](Get-Item -LiteralPath $Path).VersionInfo.FileVersion
}

function Get-QtChildDirectory {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { return @() }
    return @(Get-ChildItem -LiteralPath $Path -Directory -ErrorAction SilentlyContinue)
}

function Get-QtDirectorySize {
    <#
    .SYNOPSIS
        预扫 {files, bytes}(docs/03 §2.9.1 微信数据体量、§2.15)。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtPath -Path $Path)) { return [pscustomobject]@{ files = 0; bytes = [long]0 } }
    $m = Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum
    return [pscustomobject]@{ files = [int]$m.Count; bytes = [long]($m.Sum) }
}

function Test-QtHasProperty {
    <#
    .SYNOPSIS
        安全判断一个对象有没有某个属性。
        ⚠️ 不要用「取 .PSObject.Properties.Name 再 -contains」那种写法 —— 对象一个属性都没有时
        `.Properties` 是空集合,再取 `.Name` 在 `Set-StrictMode -Version Latest` 下直接抛
        「在此对象上找不到属性 Name」。轻量包的空 manifest 正好会走到这条路上。
        本函数用索引器 `$Object.PSObject.Properties[$Name]`,属性不存在时回 $null,不抛。
    #>
    [CmdletBinding()][OutputType([bool])]
    param([AllowNull()] $Object, [Parameter(Mandatory)][string] $Name)
    if ($null -eq $Object) { return $false }
    return [bool]($Object.PSObject.Properties[$Name])
}

function Join-QtPath {
    <#
    .SYNOPSIS
        拼路径。⚠️ **不用 `Join-Path`** —— 它会校验驱动器是否存在,对未挂载的盘符直接抛
        `DriveNotFoundException`(单测里的 `X:\` 与现场里暂时掉线的数据盘都会踩到)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $Path, [Parameter(Mandatory)][string] $ChildPath)
    return [IO.Path]::Combine($Path, $ChildPath)
}

function ConvertTo-QtWslPath {
    <#
    .SYNOPSIS
        Windows 路径 → WSL `/mnt/<盘符小写>/…`。盘符**从路径里取,不写死 c**
        (`%ProgramData%` 通常在 C:,但不保证;E-18 的数据盘选择还会把 vhdx 挪到别的盘)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $WindowsPath)
    if ($WindowsPath -notmatch '^[A-Za-z]:\\') { return ($WindowsPath -replace '\\', '/') }
    $drive = ($WindowsPath.Substring(0, 1)).ToLowerInvariant()
    return ('/mnt/{0}' -f $drive) + (($WindowsPath.Substring(2)) -replace '\\', '/')
}

function Get-QtDriveFreeBytes {
    <#
    .SYNOPSIS
        某盘可用字节(docs/03 §2.4.1 磁盘门槛)。$DriveLetter 形如 'C'。
    #>
    [CmdletBinding()][OutputType([long])]
    param([Parameter(Mandatory)][string] $DriveLetter)
    $d = Get-PSDrive -Name ($DriveLetter.TrimEnd(':', '\')) -PSProvider FileSystem -ErrorAction Stop
    return [long]$d.Free
}

function Get-QtLocalFixedDrive {
    <#
    .SYNOPSIS
        全部本地固定盘 {Letter, FreeBytes}(docs/03 §2.4.1「数据盘选择」)。
    #>
    [CmdletBinding()]
    param()
    return @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3' -ErrorAction SilentlyContinue |
        ForEach-Object { [pscustomobject]@{ Letter = $_.DeviceID.TrimEnd(':'); FreeBytes = [long]$_.FreeSpace } })
}
#endregion

#region 注册表 / WMI / 身份
function Get-QtRegistryValue {
    <#
    .SYNOPSIS
        读单个注册表值;键或值不存在回 $null(不抛)。docs/03 §2.15。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Name
    )
    $item = Get-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    return $item.$Name
}

function Test-QtRegistryKey {
    [CmdletBinding()][OutputType([bool])]
    param([Parameter(Mandatory)][string] $Path)
    return [bool](Test-Path -Path $Path)
}

function Get-QtRegistrySubKeyName {
    [CmdletBinding()][OutputType([string[]])]
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-QtRegistryKey -Path $Path)) { return @() }
    return @(Get-ChildItem -Path $Path -ErrorAction SilentlyContinue | ForEach-Object { $_.PSChildName })
}

function Set-QtRegistryValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Value
    )
    if (-not (Test-QtRegistryKey -Path $Path)) { New-Item -Path $Path -Force | Out-Null }
    Set-ItemProperty -Path $Path -Name $Name -Value $Value -Force
}

function Remove-QtRegistryValue {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path, [Parameter(Mandatory)][string] $Name)
    Remove-ItemProperty -Path $Path -Name $Name -Force -ErrorAction SilentlyContinue
}

function Get-QtCim {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $ClassName, [string] $Filter)
    if ($Filter) { return Get-CimInstance -ClassName $ClassName -Filter $Filter -ErrorAction SilentlyContinue }
    return Get-CimInstance -ClassName $ClassName -ErrorAction SilentlyContinue
}

# ── ACL 接缝 ──────────────────────────────────────────────────────────────
#  🔴 用 Get-Acl/Set-Acl 而**不是** icacls:
#     icacls 的输出是**本地化**的(中文 Windows 上组名和权限串都会变),
#     按文本比对读回结果在非英文系统上必然错判;而「读回 ACL 比对」正是幂等判据。
#     Get-Acl 给的是结构化对象,能按 **SID** 比,与系统语言无关。
function Get-QtAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)
    return Get-Acl -LiteralPath $Path
}

function Set-QtAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][object] $AclObject
    )
    Set-Acl -LiteralPath $Path -AclObject $AclObject
}

function Test-QtAdmin {
    [CmdletBinding()][OutputType([bool])]
    param()
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-QtCurrentUserSid {
    [CmdletBinding()][OutputType([string])]
    param()
    return [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Get-QtInteractiveUserSid {
    <#
    .SYNOPSIS
        本会话 explorer.exe 的所有者 SID(docs/03 §2.4.1「提权账号≠登录账号」、§2.15)。
        取不到回 $null(无桌面会话,如无人值守)。
    #>
    [CmdletBinding()][OutputType([string])]
    param()
    $mySession = (Get-Process -Id $PID).SessionId
    $exp = @(Get-Process -Name explorer -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $mySession })
    if ($exp.Count -eq 0) { return $null }
    try {
        $owner = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $exp[0].Id) -ErrorAction Stop
        $r = Invoke-CimMethod -InputObject $owner -MethodName GetOwnerSid -ErrorAction Stop
        return [string]$r.Sid
    } catch { return $null }
}

function Get-QtEnvironmentPath {
    <#
    .SYNOPSIS
        环境变量路径的唯一出口(测试里改这一个就能把整棵目录树搬到 tmp)。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][ValidateSet('ProgramData', 'UserProfile', 'LocalAppData', 'AppData', 'ProgramFiles', 'ProgramFilesX86', 'SystemRoot', 'Temp')][string] $Name)
    switch ($Name) {
        'ProgramData' { return $env:ProgramData }
        'UserProfile' { return $env:USERPROFILE }
        'LocalAppData' { return $env:LOCALAPPDATA }
        'AppData' { return $env:APPDATA }
        'ProgramFiles' { return $env:ProgramFiles }
        'ProgramFilesX86' { return ${env:ProgramFiles(x86)} }
        'SystemRoot' { return $env:SystemRoot }
        'Temp' { return $env:TEMP }
    }
}
#endregion

#region Windows 可选功能 / 服务 / 计划任务 / 防火墙 / HTTP
function Get-QtOptionalFeatureState {
    <#
    .OUTPUTS
        `Enabled` / `Disabled` / `DisabledWithPayloadRemoved` / `Unknown`。docs/03 §2.15。
    #>
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $FeatureName)
    try {
        $f = Get-WindowsOptionalFeature -Online -FeatureName $FeatureName -ErrorAction Stop
        return [string]$f.State
    } catch { return 'Unknown' }
}

function Enable-QtOptionalFeature {
    <#
    .OUTPUTS
        {RestartNeeded:bool, Ok:bool, HResult:string}
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]] $FeatureName)
    try {
        $r = Enable-WindowsOptionalFeature -Online -FeatureName $FeatureName -NoRestart -All -ErrorAction Stop
        return [pscustomobject]@{ Ok = $true; RestartNeeded = [bool]$r.RestartNeeded; HResult = '' }
    } catch {
        return [pscustomobject]@{ Ok = $false; RestartNeeded = $false; HResult = ('0x{0:X8}' -f $_.Exception.HResult) }
    }
}

function Invoke-QtSc {
    <#
    .SYNOPSIS
        `sc.exe` —— 用它而不是 New-Service(后者不设恢复策略)。docs/03 §2.15。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]] $ScArgs, [int] $TimeoutSec = 60)
    return Invoke-QtProcess -FilePath 'sc.exe' -ArgumentList $ScArgs -TimeoutSec $TimeoutSec
}

function Get-QtServiceStatus {
    [CmdletBinding()][OutputType([string])]
    param([Parameter(Mandatory)][string] $Name)
    $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $s) { return 'NotInstalled' }
    return [string]$s.Status
}

function Register-QtScheduledTask {
    <#
    .SYNOPSIS
        只为**安装用户**注册、该用户登录触发、不提权(docs/03 §2.8.2 / §2.15)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $TaskPath,
        [Parameter(Mandatory)][string] $TaskName,
        [Parameter(Mandatory)][string] $Execute,
        [Parameter(Mandatory)][string] $UserId
    )
    $action = New-ScheduledTaskAction -Execute $Execute
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}

function Start-QtScheduledTask {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $TaskPath, [Parameter(Mandatory)][string] $TaskName)
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
}

function Unregister-QtScheduledTask {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $TaskPath, [Parameter(Mandatory)][string] $TaskName)
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

function Remove-QtFirewallRuleByName {
    <#
    .SYNOPSIS
        卸载兜底用:只删固定规则名(docs/03 §2.14 第 5 步、§6「只删 QTrade-* 名字的」)。
        🔴 引擎**从不** New-NetFirewallRule —— 建规则唯一归 WinAgent 服务 `POST /wa/v1/firewall/ensure`。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $DisplayName)
    if ($DisplayName -notlike 'QTrade-*') { throw ('拒绝删除非 QTrade-* 规则:{0}' -f $DisplayName) }
    Remove-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
}

function Invoke-QtHttp {
    <#
    .SYNOPSIS
        引擎侧的 HTTP(健康检查 / 调 WinAgent 与 Agent 的端点)。回 {StatusCode, Body, Ok}。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Uri,
        [ValidateSet('GET', 'POST', 'PUT', 'PATCH', 'DELETE')][string] $Method = 'GET',
        [string] $Body,
        [hashtable] $Headers,
        [int] $TimeoutSec = 10
    )
    try {
        $splat = @{ Uri = $Uri; Method = $Method; TimeoutSec = $TimeoutSec; UseBasicParsing = $true }
        if ($PSBoundParameters.ContainsKey('Body')) { $splat['Body'] = $Body; $splat['ContentType'] = 'application/json' }
        if ($Headers) { $splat['Headers'] = $Headers }
        $r = Invoke-WebRequest @splat
        return [pscustomobject]@{ Ok = $true; StatusCode = [int]$r.StatusCode; Body = [string]$r.Content }
    } catch {
        $code = 0
        if ((Test-QtHasProperty -Object $_.Exception -Name 'Response') -and $_.Exception.Response) {
            try { $code = [int]$_.Exception.Response.StatusCode } catch { $code = 0 }
        }
        return [pscustomobject]@{ Ok = $false; StatusCode = $code; Body = [string]$_.Exception.Message }
    }
}

function Start-QtSleep {
    [CmdletBinding()]
    param([Parameter(Mandatory)][int] $Seconds)
    Start-Sleep -Seconds $Seconds
}

function New-QtMutex {
    <#
    .SYNOPSIS
        `Global\QTradeSetup` 互斥体(docs/03 §2.12「并发保护」;第二实例退出 29)。
    .OUTPUTS
        {Acquired:bool, Mutex}
    #>
    [CmdletBinding()]
    param([string] $Name = 'Global\QTradeSetup')
    $m = New-Object Threading.Mutex($false, $Name)
    $acquired = $false
    try { $acquired = $m.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    return [pscustomobject]@{ Acquired = $acquired; Mutex = $m }
}
#endregion

Export-ModuleMember -Function *-Qt*, ConvertFrom-QtWslOutput
