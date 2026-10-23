<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 第 1 步:在 Windows 11 家庭版上启用 Hyper-V。

.DESCRIPTION
    ⚠️ 本脚本会【改动这台机器】:它会把 Hyper-V 的组件包装进系统并启用 Hyper-V 功能。

    做法:家庭版的「启用或关闭 Windows 功能」里没有 Hyper-V,但系统盘的
    %SystemRoot%\servicing\Packages 下其实躺着完整的 Hyper-V 组件包(.mum)。
    先用 DISM 把这些包逐个 /Add-Package 装进系统,再 /Enable-Feature 启用
    Microsoft-Hyper-V-All。这是社区长期使用的办法,但【不是微软官方支持的路径】,
    详见 README 的「已知限制与风险」。

    本脚本的安全约束:
      1. 未以管理员身份运行 → 中文提示后直接退出,什么都不做。
      2. 执行前把每一条将要运行的 DISM 命令【全部打印出来】,要求你输入 YES 才继续。
      3. 每条 DISM 命令都带 /NoRestart,脚本【绝不自动重启】。
      4. 全程写操作日志,便于事后追溯。

    跑完之后必须重启一次 Windows,Hyper-V 才真正生效。重启时机由你自己挑
    —— 重启会中断 WSL 里所有正在运行的容器。

.PARAMETER LogDir
    操作日志目录。默认写到当前管理员账户的 LOCALAPPDATA 下,不碰 %ProgramData%\QTrade。

.PARAMETER AllowWindowsUpdate
    默认 DISM 带 /LimitAccess(禁止联网去 Windows Update 取载荷,离线、快、可控)。
    若 /Enable-Feature 报「找不到源文件」,可加本开关去掉 /LimitAccess 重试。

.PARAMETER WhatIfOnly
    只打印将要执行的命令,连确认都不问,直接退出。用于给安琳过目。

.EXAMPLE
    # 先看看会做什么(不做任何改动)
    powershell -NoProfile -ExecutionPolicy Bypass -File .\01-启用HyperV.ps1 -WhatIfOnly

.EXAMPLE
    # 真正执行(需要管理员 PowerShell)
    powershell -NoProfile -ExecutionPolicy Bypass -File .\01-启用HyperV.ps1
#>

[CmdletBinding()]
param(
    [string] $LogDir = (Join-Path $env:LOCALAPPDATA 'QTrade-VMLab\logs'),
    [switch] $AllowWindowsUpdate,
    [switch] $WhatIfOnly
)

$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'

# ---------- 日志 ----------
$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = $null

function Initialize-Log {
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
    $script:logFile = Join-Path $LogDir ("enable-hyperv-{0}.log" -f $stamp)
    "QTrade VM-Lab 01-启用HyperV 操作日志  开始于 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" |
        Out-File -FilePath $script:logFile -Encoding UTF8
}

function Write-Log {
    param([string] $Message, [string] $Level = 'INFO', [string] $Color = 'Gray')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message
    Write-Host $Message -ForegroundColor $Color
    if ($script:logFile) { $line | Out-File -FilePath $script:logFile -Append -Encoding UTF8 }
}

function Write-LogOnly {
    param([string] $Message)
    if ($script:logFile) { $Message | Out-File -FilePath $script:logFile -Append -Encoding UTF8 }
}

# ---------- 原生命令调用:退出码必须被看见 ----------
# PowerShell 5.1 的 $ErrorActionPreference 管不住原生 exe,必须自己读 $LASTEXITCODE。
function Invoke-Dism {
    param(
        [Parameter(Mandatory = $true)][string[]] $Arguments,
        [int[]] $SuccessCodes = @(0, 3010)
    )
    $prevEnc = [Console]::OutputEncoding
    try {
        # 中文版 Windows 的 dism.exe 按 ANSI(GBK)输出,不切编码会看到乱码
        [Console]::OutputEncoding = [System.Text.Encoding]::Default
        $out = & dism.exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $prevEnc
    }
    foreach ($l in $out) { Write-LogOnly ("    | " + $l) }
    return [pscustomobject]@{
        ExitCode = $code
        Output   = $out
        Success  = ($SuccessCodes -contains $code)
    }
}

# ---------- 1. 管理员检查 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ''
    Write-Host '  ❌ 本脚本需要管理员权限。' -ForegroundColor Red
    Write-Host '     请在开始菜单搜索 PowerShell → 右键 →「以管理员身份运行」,再执行:' -ForegroundColor Yellow
    Write-Host ("     powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $PSCommandPath) -ForegroundColor White
    Write-Host ''
    exit 1
}

Initialize-Log
Write-Host ''
Write-Log 'QTrade 测试虚拟机实验室 —— 01 启用 Hyper-V(Windows 11 家庭版)' 'INFO' 'White'
Write-Log ("操作日志:{0}" -f $logFile) 'INFO' 'DarkGray'

# ---------- 2. 现状检查 ----------
Write-Host ''
Write-Log '【检查】Hyper-V 当前状态…' 'INFO' 'Cyan'
$already = $false
try {
    $f = Get-WindowsOptionalFeature -Online -FeatureName 'Microsoft-Hyper-V-All' -ErrorAction Stop
    Write-Log ("  Microsoft-Hyper-V-All = {0}" -f $f.State) 'INFO' 'Gray'
    if ($f.State -eq 'Enabled') { $already = $true }
} catch {
    Write-Log '  Microsoft-Hyper-V-All 当前在本 SKU 上不可见(家庭版的典型表现,继续)。' 'INFO' 'Gray'
}
if ($already) {
    Write-Host ''
    Write-Log '  ✅ Hyper-V 已经启用,本脚本无需再跑。' 'INFO' 'Green'
    Write-Log '     若 Get-VMSwitch 仍报错,说明启用后还没重启过 —— 请择机重启一次。' 'INFO' 'Yellow'
    exit 0
}

# ---------- 3. 枚举组件包 ----------
$pkgDir = Join-Path $env:SystemRoot 'servicing\Packages'
Write-Host ''
Write-Log ("【检查】枚举 Hyper-V 组件包:{0}\*Hyper-V*.mum" -f $pkgDir) 'INFO' 'Cyan'
$mums = @(Get-ChildItem -Path $pkgDir -Filter '*Hyper-V*.mum' -File -ErrorAction SilentlyContinue |
          Sort-Object Name)
if ($mums.Count -eq 0) {
    Write-Log '  ❌ 一个组件包都没找到,家庭版 DISM 启用路径在这台机器上走不通。' 'ERROR' 'Red'
    Write-Log '     可能原因:系统被精简过,或 servicing\Packages 被清理工具删过。' 'ERROR' 'Yellow'
    exit 2
}
Write-Log ("  找到 {0} 个组件包。" -f $mums.Count) 'INFO' 'Green'

$limitAccess = if ($AllowWindowsUpdate) { '' } else { ' /LimitAccess' }

# ---------- 4. 打印将要执行的每一条命令 ----------
Write-Host ''
Write-Host ('─' * 74) -ForegroundColor DarkYellow
Write-Host '  下面是本脚本【将要执行】的全部命令,请过目:' -ForegroundColor Yellow
Write-Host ('─' * 74) -ForegroundColor DarkYellow
$i = 0
foreach ($m in $mums) {
    $i++
    $cmd = ('dism.exe /Online /Add-Package /PackagePath:"{0}" /NoRestart /Quiet /LogPath:"{1}"' -f $m.FullName, ($logFile -replace '\.log$', '.dism.log'))
    Write-Host ("  [{0,2}/{1}] {2}" -f $i, $mums.Count, $cmd) -ForegroundColor Gray
    Write-LogOnly ("PLAN [$i/$($mums.Count)] $cmd")
}
$enableCmd = ('dism.exe /Online /Enable-Feature /FeatureName:Microsoft-Hyper-V-All /All{0} /NoRestart /LogPath:"{1}"' -f $limitAccess, ($logFile -replace '\.log$', '.dism.log'))
Write-Host ''
Write-Host ("  [最后一条] {0}" -f $enableCmd) -ForegroundColor White
Write-LogOnly ("PLAN [final] $enableCmd")
Write-Host ('─' * 74) -ForegroundColor DarkYellow

Write-Host ''
Write-Host '  这些命令会改动的东西:' -ForegroundColor Yellow
Write-Host '    · 把 Hyper-V 的系统组件装进 Windows(组件存储 WinSxS 会增大约 1~2 GB)' -ForegroundColor Gray
Write-Host '    · 启用 Hyper-V 虚拟机监控程序:重启后 Windows 自身将运行在 Hyper-V 之上' -ForegroundColor Gray
Write-Host '    · 自动创建 Hyper-V 的 Default Switch(NAT),并新增一批 vm* 系统服务' -ForegroundColor Gray
Write-Host '  撤销办法:跑同目录的 01b-撤销HyperV.ps1(/Disable-Feature),同样需要重启一次。' -ForegroundColor Gray
Write-Host ''
Write-Host '  🔴 本脚本不会重启你的电脑。跑完后由你自己挑时间重启;' -ForegroundColor Yellow
Write-Host '     重启会中断 WSL 里所有正在运行的容器与终端。' -ForegroundColor Yellow
Write-Host ''

if ($WhatIfOnly) {
    Write-Log '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' 'INFO' 'Cyan'
    exit 0
}

# ---------- 5. 显式确认 ----------
$answer = Read-Host '  确认执行请输入大写 YES(其它任何输入都会取消)'
Write-LogOnly ("CONFIRM input = '$answer'")
if ($answer -cne 'YES') {
    Write-Log '  已取消,没有做任何改动。' 'INFO' 'Cyan'
    exit 0
}

# ---------- 6. 逐个 Add-Package ----------
$dismLog = $logFile -replace '\.log$', '.dism.log'
Write-Host ''
Write-Log '【执行】逐个装入组件包…' 'INFO' 'Cyan'
$okCount = 0
$skipCount = 0
$failList = New-Object System.Collections.ArrayList
$i = 0
foreach ($m in $mums) {
    $i++
    Write-Host ("  [{0,2}/{1}] {2}" -f $i, $mums.Count, $m.Name) -NoNewline -ForegroundColor DarkGray
    $r = Invoke-Dism -Arguments @(
        '/Online', '/Add-Package',
        ('/PackagePath:{0}' -f $m.FullName),
        '/NoRestart', '/Quiet',
        ('/LogPath:{0}' -f $dismLog)
    )
    if ($r.Success) {
        $okCount++
        Write-Host '  → 成功' -ForegroundColor Green
        Write-LogOnly ("ADD OK   $($m.Name) exit=$($r.ExitCode)")
    } elseif ($r.ExitCode -eq -2146498530 -or $r.ExitCode -eq 0x800f081e) {
        # CBS_E_NOT_APPLICABLE:这个包不适用于当前系统,属正常现象,跳过
        $skipCount++
        Write-Host '  → 跳过(此包不适用于本系统,正常)' -ForegroundColor DarkYellow
        Write-LogOnly ("ADD SKIP $($m.Name) exit=$($r.ExitCode) (NOT_APPLICABLE)")
    } else {
        [void] $failList.Add(("{0}(退出码 {1})" -f $m.Name, $r.ExitCode))
        Write-Host ("  → 失败,退出码 {0}" -f $r.ExitCode) -ForegroundColor Yellow
        Write-LogOnly ("ADD FAIL $($m.Name) exit=$($r.ExitCode)")
    }
}
Write-Host ''
Write-Log ("  组件包结果:成功 {0} / 跳过 {1} / 失败 {2}(共 {3})" -f $okCount, $skipCount, $failList.Count, $mums.Count) 'INFO' 'Gray'
if ($failList.Count -gt 0) {
    Write-Log '  下列包没装上(单个包失败不一定影响最终启用,继续往下走):' 'WARN' 'Yellow'
    $failList | ForEach-Object { Write-Log ("    - {0}" -f $_) 'WARN' 'Yellow' }
}

# ---------- 7. Enable-Feature ----------
Write-Host ''
Write-Log '【执行】启用 Microsoft-Hyper-V-All…' 'INFO' 'Cyan'
$enableArgs = @(
    '/Online', '/Enable-Feature',
    '/FeatureName:Microsoft-Hyper-V-All',
    '/All', '/NoRestart',
    ('/LogPath:{0}' -f $dismLog)
)
if (-not $AllowWindowsUpdate) { $enableArgs += '/LimitAccess' }

$er = Invoke-Dism -Arguments $enableArgs
$er.Output | ForEach-Object { if ("$_".Trim()) { Write-Host ("    $_") -ForegroundColor DarkGray } }

if ($er.Success) {
    Write-Host ''
    Write-Log ("  ✅ Hyper-V 已启用(DISM 退出码 {0})。" -f $er.ExitCode) 'INFO' 'Green'
} else {
    Write-Host ''
    Write-Log ("  ❌ 启用失败,DISM 退出码 {0}。" -f $er.ExitCode) 'ERROR' 'Red'
    if (-not $AllowWindowsUpdate) {
        Write-Log '     若报「找不到源文件 / 0x800f0906 / 0x800f081f」,可加 -AllowWindowsUpdate 去掉 /LimitAccess 再试一次。' 'ERROR' 'Yellow'
    }
    Write-Log ("     详细日志见:{0}" -f $dismLog) 'ERROR' 'Yellow'
    exit 3
}

# ---------- 8. 收尾:绝不自动重启 ----------
Write-Host ''
Write-Host ('=' * 74) -ForegroundColor Yellow
Write-Host '  🔴 需要重启一次 Windows,Hyper-V 才会真正生效。' -ForegroundColor Yellow
Write-Host '' -ForegroundColor Yellow
Write-Host '     本脚本【没有】也【不会】替你重启 —— 请你自行择机重启。' -ForegroundColor Yellow
Write-Host '     重启会中断 WSL 里正在运行的全部容器与终端,请先自行保存并停妥。' -ForegroundColor Yellow
Write-Host '' -ForegroundColor Yellow
Write-Host '     重启后验证是否生效(普通 PowerShell 即可):' -ForegroundColor White
Write-Host '        Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All | Select State' -ForegroundColor Gray
Write-Host '        Get-VMSwitch                    # 应能看到 Default Switch' -ForegroundColor Gray
Write-Host '        wsl -l -v                       # 确认你的发行版都还在' -ForegroundColor Gray
Write-Host '' -ForegroundColor Yellow
Write-Host '     然后跑 02-建测试虚拟机.ps1(管理员)。' -ForegroundColor White
Write-Host ('=' * 74) -ForegroundColor Yellow
Write-Host ''
Write-Log ("操作日志已写到:{0}" -f $logFile) 'INFO' 'DarkGray'
exit 0
