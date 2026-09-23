<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 第 4 步:把安装包送进虚拟机,并给出静默安装的骨架命令。

.DESCRIPTION
    ⚠️ 本脚本会【改动虚拟机】(往里拷文件;加 -RunInstall 时还会在里面跑安装器),
       但【不碰主机】的任何设置 —— 主机的 WSL、容器、网络、注册表一律不动。

    两条传输通道,默认自动选:
      · PowerShell Direct(推荐):New-PSSession -VMName + Copy-Item -ToSession。
        走 VMBus,不需要虚拟机联网,速度快,而且拷完就能在同一个会话里直接执行命令。
        要求:虚拟机里已有可登录的本地账号(默认 qtest),主机侧管理员运行。
      · Copy-VMFile(退路):要求虚拟机启用「来宾服务接口」集成服务。
        只能拷文件,不能执行命令。

    默认行为:检查 → 拷贝 → 打印「在虚拟机里静默安装并取回退出码与日志」的骨架命令。
    加 -RunInstall 才会真的去跑安装器(跑之前仍会要求你输入 YES 确认)。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\04-把安装包送进虚拟机.ps1 -WhatIfOnly
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\04-把安装包送进虚拟机.ps1
.EXAMPLE
    # 拷完直接在虚拟机里静默安装,并把退出码与日志取回主机
    powershell -NoProfile -ExecutionPolicy Bypass -File .\04-把安装包送进虚拟机.ps1 -RunInstall
#>

[CmdletBinding()]
param(
    [string] $VMName     = 'QTrade-Test-Win11',
    [string] $GuestUser  = 'qtest',
    [string] $GuestDir   = 'C:\QTrade-Test',
    # 主机上要送进去的文件;路径相对仓库根目录解析(默认值勿用 $PSScriptRoot,见正文)
    [string] $RepoRoot   = '',
    [string] $SetupExe   = 'installer\out\QTrade-Setup-1.0.0.exe',
    [string] $EvidencePs1 = 'fieldtest\collect-evidence.ps1',
    [ValidateSet('Auto', 'Direct', 'VMFile')]
    [string] $Method     = 'Auto',
    [string] $EvidenceOutDir = '',
    [switch] $SkipCopy,
    [switch] $RunInstall,
    [switch] $WhatIfOnly
)

$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'

# param 默认值阶段 $PSScriptRoot 可能为空;正文里再解析仓库根
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $baseDir = $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($baseDir)) {
        $baseDir = (Get-Location).Path
    }
    if ([string]::IsNullOrWhiteSpace($baseDir)) {
        Write-Host '  ❌ 无法定位目录以解析仓库根。请用 -RepoRoot 显式指定。' -ForegroundColor Red
        exit 3
    }
    $candidate = Join-Path $baseDir '..\..'
    try {
        $RepoRoot = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    } catch {
        Write-Host ("  ❌ 无法解析仓库根目录:{0}" -f $candidate) -ForegroundColor Red
        Write-Host '     请用 -RepoRoot 显式指定仓库根路径。' -ForegroundColor Yellow
        exit 3
    }
}

function Write-Head([string] $Text) {
    Write-Host ''
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
    Write-Host ("  $Text") -ForegroundColor Cyan
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
}
function Write-Cmd([string] $t)   { Write-Host ("    > {0}" -f $t) -ForegroundColor White }
function Write-Ok([string] $t)    { Write-Host ("      ✔ {0}" -f $t) -ForegroundColor Green }
function Write-Warn2([string] $t) { Write-Host ("      ⚠ {0}" -f $t) -ForegroundColor Yellow }
function Write-Info([string] $t)  { Write-Host ("      · {0}" -f $t) -ForegroundColor Gray }

# ── 安装器退出码对照表(逐条抄自 docs/03-安装引导与自动化配置.md §3.4)──
$ExitCodeTable = @{
    0   = 'OK —— DONE,安装完成'
    10  = 'E_INSTALL_WAIT_USER —— 停车等用户确认(等 shutdown 确认 / 等用户关微信 / 等 UAC)'
    20  = 'E_INSTALL_WIN_TOO_OLD —— Windows 版本太旧(< 19044)'
    21  = 'E_INSTALL_NOT_X64 —— 不是 x64'
    22  = 'E_INSTALL_NOT_ADMIN —— 没有管理员权限'
    23  = 'E_INSTALL_ELEVATED_AS_OTHER_USER —— 提权账号≠登录账号'
    24  = 'E_INSTALL_VIRT_DISABLED —— CPU 虚拟化未开启'
    25  = 'E_INSTALL_POLICY_BLOCKED —— 被组策略挡住(子原因看日志与 install_state)'
    26  = 'E_INSTALL_DISK_LOW —— 磁盘入口门槛不足,直接拒装(机上无落盘)'
    27  = 'E_INSTALL_MEM_LOW —— 内存不足'
    28  = 'E_INSTALL_OTHER_CUSTOM_KERNEL_DECLINED —— 拒绝替换已有的第三方自定义内核'
    29  = 'E_INSTALL_ALREADY_RUNNING —— 互斥体:已有一个安装器在跑'
    30  = 'E_INSTALL_PAYLOAD_CORRUPT —— 载荷校验失败'
    31  = 'E_INSTALL_ACL_HARDEN_FAILED —— 安装目录/内核文件权限收紧失败(可续跑)'
    40  = 'E_INSTALL_FEATURE_ENABLE_FAILED —— 启用 Windows 功能失败'
    41  = 'E_INSTALL_RESUME_ENGINE_MISSING —— 重启续跑时找不到引擎'
    50  = 'E_INSTALL_WSL_MSI_FAILED —— WSL MSI 安装失败'
    51  = 'E_INSTALL_WSL_BROKEN —— WSL 处于损坏状态'
    60  = 'E_INSTALL_KERNEL_SHA_MISMATCH —— 内核文件内容校验不符'
    61  = 'E_INSTALL_WSLCONFIG_PARSE_FAILED —— .wslconfig 解析失败'
    62  = 'E_INSTALL_KERNEL_BOOT_TIMEOUT —— 内核启动超时(已回滚)'
    63  = 'E_INSTALL_KERNEL_BOOT_FAILED —— 内核启动失败(已回滚)'
    64  = 'E_INSTALL_KERNEL_NO_BINDER —— 内核起来了但没有 binder(已回滚)'
    65  = 'E_INSTALL_KERNEL_ROLLBACK_FAILED —— 回滚失败,需人工介入'
    66  = 'E_INSTALL_KERNEL_SHUTDOWN_TIMEOUT —— wsl --shutdown 60 秒无响应,需重启电脑'
    67  = 'E_INSTALL_KCHECK_IMPORT_FAILED —— kcheck 预导入失败(未写配置)'
    70  = 'E_INSTALL_DISTRO_NAME_CONFLICT_DECLINED —— 同名发行版冲突且用户拒绝处理'
    71  = 'E_INSTALL_IMPORT_FAILED —— wsl --import 失败'
    72  = 'E_INSTALL_SYSTEMD_NOT_READY —— systemd 未就绪'
    73  = 'E_INSTALL_DOCKER_NOT_READY —— docker 未就绪'
    74  = 'E_INSTALL_IMAGE_LOAD_FAILED —— docker load 失败'
    75  = 'E_INSTALL_AGENT_NOT_READY —— Agent 未就绪'
    76  = 'E_INSTALL_DOCKER_CIDR_EXHAUSTED —— 显式指定的 /QT_DOCKER_CIDR 网段冲突'
    80  = 'E_INSTALL_VCREDIST_FAILED —— VC 运行库安装失败'
    81  = 'E_INSTALL_SERVICE_INSTALL_FAILED —— 服务注册失败'
    82  = 'E_INSTALL_WINAGENT_NOT_READY —— WinAgent 未就绪'
    90  = 'E_INSTALL_WECHAT_BACKUP_FAILED —— 微信备份失败'
    91  = 'E_INSTALL_WECHAT_REINSTALL_FAILED —— 微信重装失败'
    100 = 'E_INSTALL_SELFTEST_REDROID_BOOT —— 自检:redroid 启动失败'
    101 = 'E_INSTALL_SELFTEST_AGENT —— 自检:Agent 不通'
    102 = 'E_INSTALL_SELFTEST_WINAGENT —— 自检:WinAgent 不通'
    120 = 'E_INSTALL_UPGRADE_DATA_BACKUP_FAILED —— 升级前数据备份失败'
    121 = 'E_INSTALL_UNINSTALL_PARTIAL —— 卸载只完成了一部分'
    122 = 'E_INSTALL_DOWNGRADE_REFUSED —— 拒绝降级'
    123 = 'E_INSTALL_DISK_FULL —— 安装过程中磁盘写满(可续跑)'
    200 = 'E_INSTALL_INTERNAL —— 未归类内部错误(日志里有异常栈)'
    3010 = 'E_INSTALL_REBOOT_REQUIRED —— 需要重启,RunOnce 已写好,重启后会自动续跑'
}

Write-Host ''
Write-Host 'QTrade 测试虚拟机实验室 —— 04 把安装包送进虚拟机' -ForegroundColor White

# ---------- 前置 ----------
Write-Head '前置检查'
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host '  ❌ 本脚本需要管理员权限(Hyper-V 与 PowerShell Direct 都要求提权)。' -ForegroundColor Red
    exit 1
}
if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    Write-Host '  ❌ 找不到 Hyper-V 命令。请先跑 01 并重启,再跑 02 建虚拟机。' -ForegroundColor Red
    exit 2
}
$vm = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if (-not $vm) {
    Write-Host ("  ❌ 找不到虚拟机「{0}」。" -f $VMName) -ForegroundColor Red
    exit 2
}
Write-Ok ("虚拟机:{0}(状态 {1})" -f $vm.Name, $vm.State)
if ($vm.State -ne 'Running') {
    Write-Host ("  ❌ 虚拟机没在运行。请先开机并登录进桌面:" ) -ForegroundColor Red
    Write-Host ("     Start-VM -Name '{0}'; vmconnect.exe localhost '{0}'" -f $VMName) -ForegroundColor Yellow
    exit 2
}

# 主机侧要送进去的文件
$setupFull    = Join-Path $RepoRoot $SetupExe
$evidenceFull = Join-Path $RepoRoot $EvidencePs1

$toCopy = New-Object System.Collections.ArrayList
if (Test-Path -LiteralPath $setupFull) {
    $sz = [math]::Round((Get-Item -LiteralPath $setupFull).Length / 1GB, 2)
    Write-Ok ("安装包:{0}({1} GB)" -f $setupFull, $sz)
    [void] $toCopy.Add($setupFull)
} else {
    Write-Host ("  ❌ 找不到安装包:{0}" -f $setupFull) -ForegroundColor Red
    Write-Host '     请确认仓库里已经打好包,或用 -SetupExe 指定相对路径。' -ForegroundColor Yellow
    exit 3
}
if (Test-Path -LiteralPath $evidenceFull) {
    Write-Ok ("取证脚本:{0}" -f $evidenceFull)
    [void] $toCopy.Add($evidenceFull)
} else {
    Write-Warn2 ("取证脚本还不存在,跳过:{0}" -f $evidenceFull)
    Write-Info '(它由现场取证那条线负责交付;等它到位后重跑本脚本就会一并送进去。)'
}

# ---------- 计划 ----------
Write-Head '将要做的事'
$stepNo = 1
Write-Host ("    {0}) 在虚拟机里确保目录存在:{1}" -f $stepNo, $GuestDir) -ForegroundColor Gray
$stepNo++
foreach ($f in $toCopy) {
    Write-Host ("    {0}) 拷贝 {1}  →  虚拟机的 {2}\" -f $stepNo, (Split-Path $f -Leaf), $GuestDir) -ForegroundColor Gray
    $stepNo++
}
Write-Host ("    {0}) 打印「在虚拟机里静默安装 + 取回退出码与日志」的骨架命令" -f $stepNo) -ForegroundColor Gray
$stepNo++
if ($RunInstall) {
    Write-Host ("    {0}) -RunInstall:真的在虚拟机里跑一遍静默安装,并把日志取回主机" -f $stepNo) -ForegroundColor Yellow
}
$copyTotalBytes = ($toCopy | ForEach-Object { (Get-Item -LiteralPath $_).Length } | Measure-Object -Sum).Sum
$copyTotalGB = [math]::Round($copyTotalBytes / 1GB, 2)
Write-Host ''
Write-Host '  会改动什么:' -ForegroundColor Yellow
Write-Host ("    · 虚拟机里多出 {0} 下的文件(即将拷贝合计 {1} GB)" -f $GuestDir, $copyTotalGB) -ForegroundColor Gray
Write-Host '    · 主机:只读取仓库里的文件,不写、不改任何设置' -ForegroundColor Gray
Write-Host '  撤销办法:回滚到基线检查点(.\03-快照与回滚.ps1 -Restore),虚拟机里的一切改动一笔勾销' -ForegroundColor Gray
Write-Host ''

if ($WhatIfOnly) {
    Write-Host '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' -ForegroundColor Cyan
    exit 0
}

# ---------- 建立 PowerShell Direct 会话 ----------
$session = $null
$usedMethod = ''

if ($Method -eq 'Auto' -or $Method -eq 'Direct') {
    Write-Head 'PowerShell Direct 连接'
    Write-Info ("要用虚拟机里的账号登录。账号:{0}(密码 = 虚拟机本地账号的密码)" -f $GuestUser)
    Write-Cmd ("New-PSSession -VMName '{0}' -Credential <{1}>" -f $VMName, $GuestUser)
    try {
        $cred = Get-Credential -UserName $GuestUser -Message ("请输入虚拟机 {0} 里账号 {1} 的密码" -f $VMName, $GuestUser)
        $session = New-PSSession -VMName $VMName -Credential $cred -ErrorAction Stop
        Write-Ok 'PowerShell Direct 会话已建立(走 VMBus,不需要虚拟机联网)'
        $usedMethod = 'Direct'
    } catch {
        Write-Warn2 ("PowerShell Direct 连不上:{0}" -f $_.Exception.Message)
        Write-Info '常见原因:虚拟机还没走完 OOBE、账号名或密码不对、虚拟机不是 Windows 10+。'
        if ($Method -eq 'Direct') {
            Write-Host '  ❌ 指定了 -Method Direct,不再退回 Copy-VMFile。' -ForegroundColor Red
            exit 4
        }
        Write-Info '退回用 Copy-VMFile。'
    }
}

# ---------- 拷贝 ----------
if (-not $SkipCopy) {
    Write-Head '拷贝文件到虚拟机'

    if ($session) {
        Write-Cmd ("Invoke-Command -Session `$s -ScriptBlock {{ New-Item -ItemType Directory -Path '{0}' -Force }}" -f $GuestDir)
        Invoke-Command -Session $session -ScriptBlock {
            param($d) New-Item -ItemType Directory -Path $d -Force | Out-Null
        } -ArgumentList $GuestDir
        Write-Ok ("虚拟机里目录就绪:{0}" -f $GuestDir)

        foreach ($f in $toCopy) {
            $leaf = Split-Path $f -Leaf
            $sizeGB = [math]::Round((Get-Item -LiteralPath $f).Length / 1GB, 2)
            Write-Cmd ("Copy-Item -Path '{0}' -Destination '{1}' -ToSession `$s -Force" -f $f, $GuestDir)
            if ($sizeGB -ge 1) {
                Write-Info ("{0} 有 {1} GB,走 VMBus 大约要几分钟,请耐心等(没有进度条是正常的)" -f $leaf, $sizeGB)
            }
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            Copy-Item -Path $f -Destination $GuestDir -ToSession $session -Force
            $sw.Stop()
            Write-Ok ("{0} 已送达,用时 {1} 秒" -f $leaf, [math]::Round($sw.Elapsed.TotalSeconds, 1))
        }

        # 复核:在虚拟机里看一眼文件确实在、大小对得上
        $check = Invoke-Command -Session $session -ScriptBlock {
            param($d) Get-ChildItem -LiteralPath $d -File | Select-Object Name, Length
        } -ArgumentList $GuestDir
        Write-Host ''
        Write-Info '虚拟机里现在有:'
        $check | ForEach-Object {
            Write-Host ("        {0}  {1} 字节" -f $_.Name, $_.Length) -ForegroundColor Gray
        }
        $usedMethod = 'Direct'

    } else {
        # 退路:Copy-VMFile,需要「来宾服务接口」
        Write-Info '走 Copy-VMFile 通道,先确认「来宾服务接口」集成服务是开着的。'
        $gsi = Get-VMIntegrationService -VMName $VMName -Name 'Guest Service Interface' -ErrorAction SilentlyContinue
        if (-not $gsi) {
            $gsi = Get-VMIntegrationService -VMName $VMName | Where-Object { $_.Name -like '*Guest Service*' -or $_.Name -like '*来宾服务*' } | Select-Object -First 1
        }
        if ($gsi -and -not $gsi.Enabled) {
            Write-Cmd ("Enable-VMIntegrationService -VMName '{0}' -Name '{1}'" -f $VMName, $gsi.Name)
            Enable-VMIntegrationService -VMName $VMName -Name $gsi.Name
            Write-Ok '来宾服务接口已启用(这是对虚拟机的设置,不改主机)'
        } elseif ($gsi) {
            Write-Ok '来宾服务接口已启用'
        } else {
            Write-Host '  ❌ 找不到「来宾服务接口」集成服务,Copy-VMFile 用不了。' -ForegroundColor Red
            Write-Host '     请改用 PowerShell Direct(确认虚拟机已走完 OOBE 且账号密码正确)。' -ForegroundColor Yellow
            exit 4
        }

        foreach ($f in $toCopy) {
            $leaf = Split-Path $f -Leaf
            $dst = Join-Path $GuestDir $leaf
            Write-Cmd ("Copy-VMFile -Name '{0}' -SourcePath '{1}' -DestinationPath '{2}' -FileSource Host -CreateFullPath -Force" -f $VMName, $f, $dst)
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            Copy-VMFile -Name $VMName -SourcePath $f -DestinationPath $dst -FileSource Host -CreateFullPath -Force
            $sw.Stop()
            Write-Ok ("{0} 已送达,用时 {1} 秒" -f $leaf, [math]::Round($sw.Elapsed.TotalSeconds, 1))
        }
        $usedMethod = 'VMFile'
    }
} else {
    Write-Info '-SkipCopy:跳过拷贝。'
}

# ---------- 打印静默安装骨架 ----------
$guestExe = Join-Path $GuestDir (Split-Path $setupFull -Leaf)
$guestLog = Join-Path $GuestDir 'install.log'

Write-Head '在虚拟机里静默安装 —— 骨架命令(参数出处:docs/03 §3.4、§2.6.3)'
Write-Host @"
    # ① 建会话(在主机的管理员 PowerShell 里跑)
    `$cred = Get-Credential -UserName '$GuestUser'
    `$s = New-PSSession -VMName '$VMName' -Credential `$cred

    # ② 静默安装。🔴 必须用 Start-Process -Wait -PassThru:
    #    安装器是 GUI 程序,直接 & 调用会立刻返回,拿不到真的退出码。
    `$r = Invoke-Command -Session `$s -ScriptBlock {
        `$p = Start-Process -FilePath '$guestExe' ``
              -ArgumentList '/VERYSILENT', '/LOG=$guestLog', ``
                            '/QT_MODE=install', ``
                            '/QT_ACCEPT_SHUTDOWN=1', ``
                            '/QT_ACCEPT_REBOOT=0' ``
              -Wait -PassThru
        # 退出码两个来源(docs/03 §3.4):
        #   · 自编存根 QTradeSD.sfx 会把引擎退出码原样透传成 EXE 退出码
        #   · 走官方存根回退路径时 EXE 退出码恒 0,判据改读 last-exit-code.txt
        `$fallback = 'C:\ProgramData\QTrade\logs\last-exit-code.txt'
        [pscustomobject]@{
            ExeExitCode  = `$p.ExitCode
            FileExitCode = `$(if (Test-Path `$fallback) { (Get-Content `$fallback -Raw).Trim() } else { `$null })
        }
    }
    `$r

    # ③ 取回日志与状态文件到主机
    Copy-Item -FromSession `$s -Recurse -Force ``
        -Path 'C:\ProgramData\QTrade\logs' ``
        -Destination 'D:\HyperV\QTrade-Test\evidence\logs'
    Copy-Item -FromSession `$s -Force ``
        -Path 'C:\ProgramData\QTrade\install\install_state.json' ``
        -Destination 'D:\HyperV\QTrade-Test\evidence\'
    Copy-Item -FromSession `$s -Force -Path '$guestLog' -Destination 'D:\HyperV\QTrade-Test\evidence\'

    # ④ 收到 3010(需要重启)时的续跑:
    Invoke-Command -Session `$s -ScriptBlock { Restart-Computer -Force }
    Remove-PSSession `$s
    # 等虚拟机起来(通常 1~2 分钟),重新建会话;引擎的 RunOnce 会自己接着往下装。
    # 之后照 ② 的办法读 last-exit-code.txt 判最终结果。

    # ⑤ 卸载还原测试:
    #    Start-Process -FilePath '$guestExe' -ArgumentList '/VERYSILENT','/QT_MODE=uninstall','/QT_KEEP_DATA=0' -Wait -PassThru
"@ -ForegroundColor Gray

Write-Host ''
Write-Host '  几个参数的出处与注意:' -ForegroundColor Yellow
Write-Host '    · /QT_ACCEPT_SHUTDOWN=1 —— docs/03 §2.6.3:内核切换要 wsl --shutdown,静默模式下' -ForegroundColor Gray
Write-Host '      必须显式带这个开关才执行,否则引擎停车、退出码 10。规格明确把它算作「用户确认」。' -ForegroundColor Gray
Write-Host '    · /QT_ACCEPT_REBOOT=0(缺省)—— 需要重启时不自动重启,而是退 3010、写好 RunOnce。' -ForegroundColor Gray
Write-Host '      测试时建议保持 0:这样你能亲眼看到 3010 与续跑路径是不是对的。' -ForegroundColor Gray
Write-Host '    · /QT_WECHAT=check —— 缺省即 check;reinstall 在静默模式下不可用,会自动降级为 check。' -ForegroundColor Gray
Write-Host '    · --restore-data 是【成对参数】不是 /QT_ 开关,别自造 /QT_RESTORE_DATA=。' -ForegroundColor Gray
Write-Host ''

# ---------- -RunInstall:真的跑一遍 ----------
if ($RunInstall) {
    Write-Head '真的在虚拟机里跑一遍静默安装'
    if (-not $session) {
        Write-Host '  ❌ -RunInstall 需要 PowerShell Direct 会话,但当前没有建立成功。' -ForegroundColor Red
        exit 4
    }
    Write-Host '  🔴 这会在虚拟机里真的装 QTrade:改虚拟机的注册表、装 WSL、切内核、注册服务。' -ForegroundColor Red
    Write-Host '     主机完全不受影响。想反悔随时用 .\03-快照与回滚.ps1 -Restore 一键回到基线。' -ForegroundColor Yellow
    Write-Host ''
    $answer = Read-Host '  确认执行请输入大写 YES(其它任何输入都会取消)'
    if ($answer -cne 'YES') {
        Write-Host '  已取消。文件已经拷进去了,你可以随时手动照上面的骨架跑。' -ForegroundColor Cyan
        if ($session) { Remove-PSSession $session }
        exit 0
    }

    Write-Info '开始安装,这一步可能要十几分钟,期间没有输出是正常的…'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $result = Invoke-Command -Session $session -ScriptBlock {
        param($exe, $log)
        $p = Start-Process -FilePath $exe `
             -ArgumentList '/VERYSILENT', ("/LOG=" + $log), '/QT_MODE=install', '/QT_ACCEPT_SHUTDOWN=1', '/QT_ACCEPT_REBOOT=0' `
             -Wait -PassThru
        $fallback = 'C:\ProgramData\QTrade\logs\last-exit-code.txt'
        $fileCode = $null
        if (Test-Path -LiteralPath $fallback) { $fileCode = (Get-Content -LiteralPath $fallback -Raw).Trim() }
        [pscustomobject]@{ ExeExitCode = $p.ExitCode; FileExitCode = $fileCode }
    } -ArgumentList $guestExe, $guestLog
    $sw.Stop()

    Write-Host ''
    Write-Ok ("安装器已退出,用时 {0} 分钟" -f [math]::Round($sw.Elapsed.TotalMinutes, 1))
    Write-Info ("EXE 退出码        : {0}" -f $result.ExeExitCode)
    Write-Info ("last-exit-code.txt: {0}" -f $(if ($null -ne $result.FileExitCode) { $result.FileExitCode } else { '(文件不存在)' }))

    # 判定用哪个码:EXE 码为 0 但文件码非空且非 0 → 以文件码为准(官方存根回退路径)
    $finalCode = $result.ExeExitCode
    if ($result.ExeExitCode -eq 0 -and $result.FileExitCode -and $result.FileExitCode -ne '0') {
        $finalCode = [int] $result.FileExitCode
        Write-Warn2 'EXE 退出码为 0 但 last-exit-code.txt 非 0 —— 按 §3.4,这说明走的是官方存根回退路径,以文件里的码为准。'
    }
    $desc = if ($ExitCodeTable.ContainsKey([int]$finalCode)) { $ExitCodeTable[[int]$finalCode] } else { '(退出码不在 §3.4 的表里,请查日志)' }
    Write-Host ''
    Write-Host ("  最终退出码 {0}:{1}" -f $finalCode, $desc) -ForegroundColor $(if ([int]$finalCode -eq 0) { 'Green' } elseif ([int]$finalCode -eq 3010 -or [int]$finalCode -eq 10) { 'Yellow' } else { 'Red' })

    # 取回证据
    if (-not $EvidenceOutDir) {
        $EvidenceOutDir = Join-Path 'D:\HyperV\QTrade-Test\evidence' (Get-Date -Format 'yyyyMMdd-HHmmss')
    }
    Write-Host ''
    Write-Head ("取回日志与状态到主机:{0}" -f $EvidenceOutDir)
    New-Item -ItemType Directory -Path $EvidenceOutDir -Force | Out-Null
    $grabs = @(
        @{ Path = 'C:\ProgramData\QTrade\logs';                        Recurse = $true  },
        @{ Path = 'C:\ProgramData\QTrade\install\install_state.json';  Recurse = $false },
        @{ Path = 'C:\ProgramData\QTrade\install\manifest.json';       Recurse = $false },
        @{ Path = $guestLog;                                           Recurse = $false }
    )
    foreach ($g in $grabs) {
        $exists = Invoke-Command -Session $session -ScriptBlock { param($p) Test-Path -LiteralPath $p } -ArgumentList $g.Path
        if (-not $exists) { Write-Warn2 ("虚拟机里没有:{0}" -f $g.Path); continue }
        Write-Cmd ("Copy-Item -FromSession `$s -Path '{0}' -Destination '{1}'{2}" -f $g.Path, $EvidenceOutDir, $(if ($g.Recurse) { ' -Recurse' } else { '' }))
        try {
            if ($g.Recurse) {
                Copy-Item -FromSession $session -Path $g.Path -Destination $EvidenceOutDir -Recurse -Force
            } else {
                Copy-Item -FromSession $session -Path $g.Path -Destination $EvidenceOutDir -Force
            }
            Write-Ok ("已取回:{0}" -f $g.Path)
        } catch {
            Write-Warn2 ("取回失败 {0}:{1}" -f $g.Path, $_.Exception.Message)
        }
    }
    Write-Host ''
    Write-Ok ("证据都在:{0}" -f $EvidenceOutDir)
    if ([int]$finalCode -eq 3010) {
        Write-Host ''
        Write-Host '  退出码 3010 = 需要重启续跑。下一步:' -ForegroundColor Yellow
        Write-Host ("     Invoke-Command -Session `$s -ScriptBlock {{ Restart-Computer -Force }}" ) -ForegroundColor Gray
        Write-Host '     等虚拟机起来后重新建会话,引擎的 RunOnce 会自己接着装。' -ForegroundColor Gray
    }
}

if ($session) { Remove-PSSession $session }

Write-Host ''
Write-Host ("  本次使用的通道:{0}" -f $(if ($usedMethod) { $usedMethod } else { '(未拷贝)' })) -ForegroundColor DarkGray
Write-Host '  下一轮测试之前,记得先回滚:.\03-快照与回滚.ps1 -Restore' -ForegroundColor White
Write-Host ''
exit 0
