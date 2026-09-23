<#
.SYNOPSIS
    QTrade 测试虚拟机实验室 —— 2b 步:生成无人值守应答文件并打成一张小 ISO。

.DESCRIPTION
    本脚本把同目录的 autounattend.xml.template 填好参数,生成真正的 autounattend.xml,
    再把它打成一张几百 KB 的小 ISO。把这张小 ISO 作为【第二个光驱】挂给虚拟机,
    Windows 安装程序启动时会自动扫描所有驱动器根目录、找到它,然后全自动装完系统。

    🔴 管理员账号密码在运行时向你询问,【不写死进仓库】。
       生成的 autounattend.xml 与 ISO 都落在虚拟机目录(默认 D:\HyperV\QTrade-Test\),
       不在 git 仓库里。

    会改动这台机器的什么:
      · 只在 -OutDir 下写两个文件(autounattend.xml 与 autounattend.iso)
      · 用 -ListImages 时会【临时挂载】一次安装 ISO 来读版本清单,读完立刻卸载
      · 不改注册表、不动网络、不重启

    打 ISO 的三档退路:
      1. 有 Windows ADK 的 oscdimg.exe → 用它(最标准)
      2. 没有 ADK → 用 Windows 自带的 IMAPI2 刻录组件(纯系统能力,不用装任何东西)
      3. 两条都不行 → 打印「手动装系统」的替代步骤(约 15 分钟),不要求你去装 ADK

.EXAMPLE
    # 先看清 ISO 里有哪些版本名(会临时挂载一次 ISO)
    powershell -NoProfile -ExecutionPolicy Bypass -File .\02b-制作应答ISO.ps1 -ListImages

.EXAMPLE
    # 生成应答 ISO(会问你要密码)
    powershell -NoProfile -ExecutionPolicy Bypass -File .\02b-制作应答ISO.ps1 -ImageName 'Windows 11 专业版'
#>

[CmdletBinding()]
param(
    [string] $TemplatePath = '',
    [string] $OutDir       = 'D:\HyperV\QTrade-Test',
    [string] $AdminUser    = 'qtest',
    [string] $ComputerName = 'QTRADE-TEST',
    [string] $ImageName    = 'Windows 11 Pro',
    [string] $ProductKey   = '',
    [string] $IsoPath      = 'D:\Win11_25H2_Chinese_Simplified_x64_v2.iso',
    [switch] $ListImages,
    [switch] $WhatIfOnly
)

$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'

# param 默认值阶段 $PSScriptRoot 可能为空;正文里再解析模板路径
if ([string]::IsNullOrWhiteSpace($TemplatePath)) {
    $scriptDir = $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($scriptDir)) {
        $scriptDir = (Get-Location).Path
    }
    if ([string]::IsNullOrWhiteSpace($scriptDir)) {
        Write-Host '  ❌ 无法定位脚本目录,应答模板路径为空。请用 -TemplatePath 显式指定。' -ForegroundColor Red
        exit 3
    }
    $TemplatePath = Join-Path $scriptDir 'autounattend.xml.template'
}

function Write-Head([string] $Text) {
    Write-Host ''
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
    Write-Host ("  $Text") -ForegroundColor Cyan
    Write-Host ('─' * 74) -ForegroundColor DarkCyan
}
function Write-Ok([string] $t)   { Write-Host ("      ✔ {0}" -f $t) -ForegroundColor Green }
function Write-Warn2([string] $t) { Write-Host ("      ⚠ {0}" -f $t) -ForegroundColor Yellow }
function Write-Info([string] $t) { Write-Host ("      · {0}" -f $t) -ForegroundColor Gray }

Write-Host ''
Write-Host 'QTrade 测试虚拟机实验室 —— 02b 制作无人值守应答 ISO' -ForegroundColor White

# ---------- -ListImages:列出 ISO 里的版本名 ----------
if ($ListImages) {
    Write-Head ("列出安装介质里的版本:{0}" -f $IsoPath)
    if (-not (Test-Path -LiteralPath $IsoPath)) {
        Write-Host ("  ❌ 找不到 ISO:{0}" -f $IsoPath) -ForegroundColor Red
        exit 3
    }
    Write-Host '  会临时挂载这个 ISO 读一下版本清单,读完立刻卸载。' -ForegroundColor Yellow
    Write-Host ''
    $img = $null
    try {
        $img = Mount-DiskImage -ImagePath $IsoPath -PassThru -ErrorAction Stop
        $drive = ($img | Get-Volume).DriveLetter
        if (-not $drive) { throw '挂载后拿不到盘符' }
        Write-Ok ("已挂载到 {0}:" -f $drive)
        $wim = @("${drive}:\sources\install.wim", "${drive}:\sources\install.esd") |
               Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $wim) { throw '在 sources 下找不到 install.wim / install.esd' }
        Write-Info ("镜像文件:{0}" -f $wim)
        Write-Host ''
        Get-WindowsImage -ImagePath $wim | ForEach-Object {
            Write-Host ("    [{0}] {1}" -f $_.ImageIndex, $_.ImageName) -ForegroundColor White
        }
        Write-Host ''
        Write-Host '  把上面【方括号右边那串名字】原样填给 -ImageName 参数。' -ForegroundColor Yellow
    } catch {
        Write-Host ("  ❌ 读取失败:{0}" -f $_.Exception.Message) -ForegroundColor Red
    } finally {
        if ($img) {
            Dismount-DiskImage -ImagePath $IsoPath -ErrorAction SilentlyContinue | Out-Null
            Write-Ok '已卸载 ISO'
        }
    }
    Write-Host ''
    exit 0
}

# ---------- 前置检查 ----------
Write-Head '前置检查'
if (-not (Test-Path -LiteralPath $TemplatePath)) {
    Write-Host ("  ❌ 找不到模板:{0}" -f $TemplatePath) -ForegroundColor Red
    exit 3
}
Write-Ok ("模板:{0}" -f $TemplatePath)

# 产品密钥:不填时按版本名自动挑微软公开的「通用安装密钥」。
# 这类密钥只用于让安装程序确定装哪个版本并跳过输入密钥页,【不能用于激活】。
if (-not $ProductKey) {
    if ($ImageName -match '家庭|Home') {
        $ProductKey = 'YTMG3-N6DKC-DKB77-7M9GH-8HVX7'   # Windows 11 家庭版 通用安装密钥
    } else {
        $ProductKey = 'VK7JG-NPHTM-C97JM-9MPGT-3V66T'   # Windows 11 专业版 通用安装密钥
    }
    Write-Info ("产品密钥未指定,按版本名自动选用微软公开的通用安装密钥(只选版本、不激活)")
}
Write-Ok ("目标版本:{0}" -f $ImageName)
Write-Ok ("计算机名:{0}" -f $ComputerName)
Write-Ok ("管理员账号:{0}" -f $AdminUser)
Write-Ok ("输出目录:{0}" -f $OutDir)

$xmlOut = Join-Path $OutDir 'autounattend.xml'
$isoOut = Join-Path $OutDir 'autounattend.iso'

Write-Host ''
Write-Host '  将要产生的文件:' -ForegroundColor Yellow
Write-Host ("    {0}" -f $xmlOut) -ForegroundColor Gray
Write-Host ("    {0}" -f $isoOut) -ForegroundColor Gray
Write-Host '  撤销办法:直接删掉这两个文件即可,主机没有任何别的改动。' -ForegroundColor Gray
Write-Host ''

if ($WhatIfOnly) {
    Write-Host '  -WhatIfOnly:只打印计划,未做任何改动,现在退出。' -ForegroundColor Cyan
    exit 0
}

# ---------- 询问密码 ----------
Write-Head ("为虚拟机里的管理员账号 {0} 设置密码" -f $AdminUser)
Write-Host '  🔴 密码只在本机内存里用一次,不会写进 git 仓库。' -ForegroundColor Yellow
Write-Host '     它会以 base64 混淆形式写进生成的 autounattend.xml —— 注意:base64 只是混淆、不是加密,' -ForegroundColor Yellow
Write-Host '     所以别用你真正在用的密码,给这台一次性测试虚拟机单独起一个就行。' -ForegroundColor Yellow
Write-Host ''

function ConvertFrom-SecureStringPlain([System.Security.SecureString] $Sec) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Sec)
    try   { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$plain = $null
for ($try = 1; $try -le 3; $try++) {
    $s1 = Read-Host '  请输入密码(至少 8 位)' -AsSecureString
    $s2 = Read-Host '  请再输入一次确认      ' -AsSecureString
    $p1 = ConvertFrom-SecureStringPlain $s1
    $p2 = ConvertFrom-SecureStringPlain $s2
    if ($p1 -cne $p2) {
        Write-Host '  ⚠ 两次输入不一致,请重来。' -ForegroundColor Yellow
        continue
    }
    if ($p1.Length -lt 8) {
        Write-Host '  ⚠ 太短了(至少 8 位),请重来。' -ForegroundColor Yellow
        continue
    }
    $plain = $p1
    break
}
if (-not $plain) {
    Write-Host '  ❌ 三次都没设成,已放弃,什么都没生成。' -ForegroundColor Red
    exit 4
}
Write-Ok '密码已接收'

# 应答文件的密码混淆规则:base64( UTF16LE( 明文 + 该 XML 元素名 ) )
function ConvertTo-UnattendPassword {
    param([string] $Plain, [string] $ElementName)
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($Plain + $ElementName)
    return [Convert]::ToBase64String($bytes)
}

# ---------- 生成 autounattend.xml ----------
Write-Head '生成 autounattend.xml'
if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    Write-Ok ("已创建目录 {0}" -f $OutDir)
}

$xml = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8
$xml = $xml.Replace('{{IMAGE_NAME}}',   [System.Security.SecurityElement]::Escape($ImageName))
$xml = $xml.Replace('{{PRODUCT_KEY}}',  [System.Security.SecurityElement]::Escape($ProductKey))
$xml = $xml.Replace('{{COMPUTER_NAME}}',[System.Security.SecurityElement]::Escape($ComputerName))
$xml = $xml.Replace('{{ADMIN_USER}}',   [System.Security.SecurityElement]::Escape($AdminUser))
$xml = $xml.Replace('{{USER_PASSWORD_B64}}',          (ConvertTo-UnattendPassword $plain 'Password'))
$xml = $xml.Replace('{{ADMINISTRATOR_PASSWORD_B64}}', (ConvertTo-UnattendPassword $plain 'AdministratorPassword'))

if ($xml -match '\{\{[A-Z0-9_]+\}\}') {
    Write-Host ("  ❌ 模板里还有没替换掉的占位符:{0}" -f $Matches[0]) -ForegroundColor Red
    exit 5
}

# 用完立刻把明文密码从内存变量里抹掉
$plain = $null
Remove-Variable -Name plain -ErrorAction SilentlyContinue
[GC]::Collect()

# Windows Setup 读 autounattend.xml 时,UTF-8 无 BOM 最保险
[System.IO.File]::WriteAllText($xmlOut, $xml, (New-Object System.Text.UTF8Encoding($false)))
Write-Ok ("已写出 {0}({1} 字节)" -f $xmlOut, (Get-Item -LiteralPath $xmlOut).Length)

# 顺手做一次 XML 格式校验,免得带着语法错误装机才发现
try {
    [void] ([xml](Get-Content -LiteralPath $xmlOut -Raw))
    Write-Ok 'XML 格式校验通过'
} catch {
    Write-Host ("  ❌ 生成的 XML 语法有问题:{0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 5
}

# ---------- 打成 ISO ----------
Write-Head '把 autounattend.xml 打成一张小 ISO'

# 先把文件放进一个干净的临时目录 —— ISO 根目录只能有这一个文件
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("qtrade-unattend-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Copy-Item -LiteralPath $xmlOut -Destination (Join-Path $stage 'autounattend.xml') -Force

$isoMade = $false

# —— 退路 1:Windows ADK 的 oscdimg ——
$oscdimg = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe",
    "${env:ProgramFiles}\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if ($oscdimg) {
    Write-Info ("找到 oscdimg:{0}" -f $oscdimg)
    $cmd = ('"{0}" -m -o -u2 -udfver102 -lUNATTEND "{1}" "{2}"' -f $oscdimg, $stage, $isoOut)
    Write-Host ("    > {0}" -f $cmd) -ForegroundColor White
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Default
        $out = & $oscdimg -m -o -u2 -udfver102 -lUNATTEND $stage $isoOut 2>&1
        $code = $LASTEXITCODE
    } finally { [Console]::OutputEncoding = $prevEnc }
    if ($code -eq 0 -and (Test-Path -LiteralPath $isoOut)) {
        Write-Ok 'oscdimg 打包成功'
        $isoMade = $true
    } else {
        Write-Warn2 ("oscdimg 失败(退出码 {0}),改用系统自带的 IMAPI2" -f $code)
        $out | ForEach-Object { Write-Host ("      | $_") -ForegroundColor DarkGray }
    }
} else {
    Write-Info '没装 Windows ADK(没有 oscdimg)—— 改用 Windows 自带的 IMAPI2 刻录组件,不需要你装任何东西。'
}

# —— 退路 2:Windows 自带的 IMAPI2 ——
if (-not $isoMade) {
    try {
        if (-not ('QTradeIsoWriter' -as [type])) {
            $cs = @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class QTradeIsoWriter
{
    public static void Save(string path, object imageStream, int blockSize, int totalBlocks)
    {
        IStream src = imageStream as IStream;
        if (src == null) throw new ArgumentException("传入的对象不是 IStream");
        byte[] buffer = new byte[blockSize];
        IntPtr read = Marshal.AllocHGlobal(sizeof(int));
        try
        {
            using (FileStream fs = File.Open(path, FileMode.Create, FileAccess.Write))
            {
                while (totalBlocks-- > 0)
                {
                    src.Read(buffer, blockSize, read);
                    int n = Marshal.ReadInt32(read);
                    if (n <= 0) break;
                    fs.Write(buffer, 0, n);
                }
                fs.Flush();
            }
        }
        finally { Marshal.FreeHGlobal(read); }
    }
}
'@
            Add-Type -TypeDefinition $cs -Language CSharp
        }

        Write-Host '    > 使用 IMAPI2(IMAPI2FS.MsftFileSystemImage)生成 ISO9660 + Joliet 映像' -ForegroundColor White
        $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
        $fsi.FileSystemsToCreate = 3      # 1=ISO9660, 2=Joliet,按位或 = 3
        $fsi.VolumeName = 'UNATTEND'
        $fsi.Root.AddTree($stage, $false)
        $res = $fsi.CreateResultImage()
        [QTradeIsoWriter]::Save($isoOut, $res.ImageStream, $res.BlockSize, $res.TotalBlocks)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($fsi)
        if (Test-Path -LiteralPath $isoOut) {
            Write-Ok 'IMAPI2 打包成功'
            $isoMade = $true
        }
    } catch {
        Write-Warn2 ("IMAPI2 也失败了:{0}" -f $_.Exception.Message)
    }
}

Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue

# ---------- 结果 ----------
Write-Head '结果'
if ($isoMade) {
    $iso = Get-Item -LiteralPath $isoOut
    Write-Ok ("应答 ISO:{0}({1} KB)" -f $iso.FullName, [math]::Round($iso.Length / 1KB, 1))
    Write-Host ''
    Write-Host '  下一步:把它作为第二个光驱挂给虚拟机' -ForegroundColor White
    Write-Host ("    · 还没建虚拟机:  .\02-建测试虚拟机.ps1 -AnswerIsoPath '{0}'" -f $isoOut) -ForegroundColor Gray
    Write-Host ("    · 虚拟机已建好:  Add-VMDvdDrive -VMName 'QTrade-Test-Win11' -Path '{0}'" -f $isoOut) -ForegroundColor Gray
    Write-Host ''
    Write-Host '  然后开机:虚拟机会自动分区、自动装完系统、自动建好本地管理员账号,全程不用你点。' -ForegroundColor Gray
    Write-Host '  (唯一要动手的地方:开机那几秒屏幕提示 Press any key to boot from CD 时按一下键盘)' -ForegroundColor DarkGray
    Write-Host ''
    exit 0
} else {
    Write-Host '  ❌ 两条打 ISO 的路都没走通。' -ForegroundColor Red
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor Yellow
    Write-Host '  替代方案:手动装系统(约 15 分钟,不需要你去装 Windows ADK)' -ForegroundColor Yellow
    Write-Host ('=' * 74) -ForegroundColor Yellow
    Write-Host '   1) 直接跑 02-建测试虚拟机.ps1(不加 -AnswerIsoPath),开机进安装界面' -ForegroundColor Gray
    Write-Host '   2) 语言选「中文(简体,中国)」→ 下一步 → 现在安装' -ForegroundColor Gray
    Write-Host '   3) 密钥页点「我没有产品密钥」→ 版本选专业版 → 接受条款' -ForegroundColor Gray
    Write-Host '   4) 安装类型选「自定义」→ 选中那块 120 GB 未分配空间 → 下一步(让它自己分区)' -ForegroundColor Gray
    Write-Host '   5) 若报「这台电脑无法运行 Windows 11」:按 Shift+F10 打开命令行,执行' -ForegroundColor Gray
    Write-Host '        reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f' -ForegroundColor DarkGray
    Write-Host '        reg add HKLM\SYSTEM\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f' -ForegroundColor DarkGray
    Write-Host '      然后关掉命令行、退回上一步重试' -ForegroundColor Gray
    Write-Host '   6) 装完进 OOBE 时,若卡在「让我们为你连接到网络」:按 Shift+F10 执行' -ForegroundColor Gray
    Write-Host '        reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f' -ForegroundColor DarkGray
    Write-Host '        shutdown /r /t 0' -ForegroundColor DarkGray
    Write-Host '      重启后该页会多出「我没有 Internet 连接」,点它即可建本地账号' -ForegroundColor Gray
    Write-Host ("   7) 本地账号名建议就用 {0},密码自己记牢(04 脚本要用它做 PowerShell Direct 登录)" -f $AdminUser) -ForegroundColor Gray
    Write-Host ''
    Write-Host ("  已生成的 autounattend.xml 仍保留在 {0},将来装了 ADK 可以再来打 ISO。" -f $xmlOut) -ForegroundColor DarkGray
    Write-Host ''
    exit 6
}
