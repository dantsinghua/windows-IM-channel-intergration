# Pester 5 —— 评审 B7:qtrade-setup-engine.iss 的 GetRunningDistros 在本机 Windows PowerShell 上**实跑**
# 不是正则静态检查:从 .iss 里把 PsScript / PsParams 两段 Pascal 赋值原样解析出来(字面量 + 变量 + IntToStr(常量)),
# 按 Inno Exec 的方式起 powershell.exe,验证:
#   ① 挂住的「wsl」在 RUNNING_DISTROS_TIMEOUT_MS(3 s)后真被杀掉,退出码 1;
#   ② 正常路径输出是 UTF-8(无 NUL)、能按行读,且与 wsl.exe 直接列出的一致;
#   ③ 反向对照:去掉 $env:WSL_UTF8='1' 那一句,输出就带 NUL(= UTF-16LE,即回归时的乱码来源)。
BeforeAll {
    $script:Iss = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\qtrade-setup-engine.iss'
    $script:Src = [IO.File]::ReadAllText($script:Iss)
    $script:PsExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $script:FakeWsl = Join-Path $TestDrive ('fake-wsl-' + [Guid]::NewGuid().ToString('N') + '.exe')
    $script:PreviousFakeMode = $env:QT_FAKE_WSL_MODE
    $script:PreviousFakePid = $env:QT_FAKE_WSL_PID_FILE
    $fakeSource = @'
using System;
using System.IO;
using System.Text;
using System.Diagnostics;
using System.Threading;
public static class FakeWsl {
    public static int Main(string[] args) {
        if (String.Join("|", args) != "--list|--running|--quiet") return 19;
        if (Environment.GetEnvironmentVariable("QT_FAKE_WSL_MODE") == "hang") {
            File.WriteAllText(Environment.GetEnvironmentVariable("QT_FAKE_WSL_PID_FILE"), Process.GetCurrentProcess().Id.ToString());
            Thread.Sleep(30000);
            return 18;
        }
        string text = "fixture-alpha\r\nfixture-beta\r\n";
        Encoding encoding = Environment.GetEnvironmentVariable("WSL_UTF8") == "1" ? new UTF8Encoding(false) : Encoding.Unicode;
        byte[] bytes = encoding.GetBytes(text);
        Stream output = Console.OpenStandardOutput();
        output.Write(bytes, 0, bytes.Length);
        output.Flush();
        return 0;
    }
}
'@
    Add-Type -TypeDefinition $fakeSource -OutputAssembly $script:FakeWsl -OutputType ConsoleApplication
    $env:QT_FAKE_WSL_MODE = 'list'
    $script:PrevUtf8 = $env:WSL_UTF8
    $env:WSL_UTF8 = $null   # 测试进程自己不带,免得对照组被环境「帮」成 UTF-8

    $fs = $script:Src.IndexOf('function GetRunningDistros')
    $fe = $script:Src.IndexOf("`nend;", $fs)
    $script:Body = $script:Src.Substring($fs, $fe - $fs)
    $script:TimeoutMs = [int]([regex]::Match($script:Src, '(?m)^\s*RUNNING_DISTROS_TIMEOUT_MS\s*=\s*(\d+)\s*;').Groups[1].Value)

    # 把 `Name := 'a' + X + IntToStr(C) + 'b';` 求值成字符串;只认 '字面量'(内含 '' 转义)、标识符、IntToStr(标识符)、+
    function script:Get-PascalConcat([string] $Body, [string] $Name, [hashtable] $Vars) {
        $m = [regex]::Match($Body, '(?m)^\s*' + [regex]::Escape($Name) + '\s*:=')
        if (-not $m.Success) { throw ('.iss 的 GetRunningDistros 里没找到 {0} :=' -f $Name) }
        $e = $Body.Substring($m.Index + $m.Length)
        $sb = New-Object Text.StringBuilder
        $i = 0
        while ($true) {
            if ($i -ge $e.Length) { throw ('{0} 的赋值没有以 ; 结束' -f $Name) }
            $c = $e[$i]
            if ($c -eq "'") {
                $i++
                while ($true) {
                    if ($i -ge $e.Length) { throw '字符串字面量未闭合' }
                    if ($e[$i] -eq "'") {
                        if ($i + 1 -lt $e.Length -and $e[$i + 1] -eq "'") { [void]$sb.Append("'"); $i += 2; continue }
                        $i++; break
                    }
                    [void]$sb.Append($e[$i]); $i++
                }
            } elseif ($c -eq ';') {
                break
            } elseif ($c -eq '+' -or [char]::IsWhiteSpace($c)) {
                $i++
            } elseif ($c -match '[A-Za-z_]') {
                $t = [regex]::Match($e.Substring($i), '^(?:IntToStr\(\s*([A-Za-z_]\w*)\s*\)|([A-Za-z_]\w*))')
                $id = if ($t.Groups[1].Success) { $t.Groups[1].Value } else { $t.Groups[2].Value }
                if (-not $Vars.ContainsKey($id)) { throw ('{0} 里出现未预期的标识符 {1}' -f $Name, $id) }
                [void]$sb.Append([string]$Vars[$id])
                $i += $t.Length
            } else {
                throw ('{0} 里出现未预期的字符 {1}' -f $Name, $c)
            }
        }
        return $sb.ToString()
    }

    # 与 Inno 的 Exec(PsExe, PsParams, ...) 同形:直接 CreateProcess powershell.exe,参数串原样
    function script:Invoke-Detect([string] $WslExe, [string] $OutFile, [switch] $DropUtf8) {
        $ps = Get-PascalConcat -Body $script:Body -Name 'PsScript' -Vars @{
            WslExe = $WslExe; OutFile = $OutFile; RUNNING_DISTROS_TIMEOUT_MS = $script:TimeoutMs
        }
        if ($DropUtf8) { $ps = $ps.Replace("`$env:WSL_UTF8='1'; ", '') }
        $params = Get-PascalConcat -Body $script:Body -Name 'PsParams' -Vars @{ PsScript = $ps }
        $psi = New-Object Diagnostics.ProcessStartInfo($script:PsExe, $params)
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $p = [Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit(60000)) { $p.Kill(); throw '检测命令 60 s 未返回' }
        $sw.Stop()
        return [pscustomobject]@{ ExitCode = $p.ExitCode; Ms = $sw.ElapsedMilliseconds; Script = $ps; Params = $params }
    }

    # 模拟 Inno LoadStringsFromFile(UTF-8 有无 BOM 皆可)+ GetRunningDistros 的逐行 Trim、跳空行
    function script:Read-Lines([string] $Path) {
        $bytes = [IO.File]::ReadAllBytes($Path)
        $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes)   # 严格解码:非法 UTF-8 直接抛
        return @($text -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    }
}

AfterAll {
    $env:WSL_UTF8 = $script:PrevUtf8
    $env:QT_FAKE_WSL_MODE = $script:PreviousFakeMode
    $env:QT_FAKE_WSL_PID_FILE = $script:PreviousFakePid
}

Describe 'B7:GetRunningDistros 命令的形状(静态)' {

    It '超时常量是 3000 毫秒' {
        $script:TimeoutMs | Should -Be 3000
    }

    It '先设 WSL_UTF8=1 再 Start-Process;不再套 cmd /c' {
        $ps = Get-PascalConcat -Body $script:Body -Name 'PsScript' -Vars @{ WslExe = 'W'; OutFile = 'O'; RUNNING_DISTROS_TIMEOUT_MS = 3000 }
        $ps.IndexOf("`$env:WSL_UTF8='1'") | Should -BeGreaterThan -1
        $ps.IndexOf("`$env:WSL_UTF8='1'") | Should -BeLessThan $ps.IndexOf('Start-Process')
        $ps | Should -Not -Match '"'
        $script:Body | Should -Not -Match "\{cmd\}"
    }
}

Describe 'B7: real PS5.1 orchestration with an isolated synthetic executable' {
    It 'terminates the exact synthetic child after the three-second deadline' {
        $pidFile = Join-Path $TestDrive 'hang-child.pid'
        $env:QT_FAKE_WSL_MODE = 'hang'
        $env:QT_FAKE_WSL_PID_FILE = $pidFile
        try {
            $r = Invoke-Detect -WslExe $script:FakeWsl -OutFile (Join-Path $TestDrive 'hang-out.txt')
            $r.ExitCode | Should -Be 1
            $r.Ms | Should -BeGreaterOrEqual 2800
            $r.Ms | Should -BeLessThan 20000
            (Test-Path -LiteralPath $pidFile) | Should -BeTrue
            $childId = [int]([IO.File]::ReadAllText($pidFile))
            @(Get-Process -Id $childId -ErrorAction SilentlyContinue).Count | Should -Be 0
        } finally {
            $env:QT_FAKE_WSL_MODE = 'list'
        }
    }

    It 'returns the exact fixture lines as UTF8 without NUL bytes' {
        $out = Join-Path $TestDrive 'utf8-out.txt'
        $r = Invoke-Detect -WslExe $script:FakeWsl -OutFile $out
        $r.ExitCode | Should -Be 0
        (Test-Path -LiteralPath $out) | Should -BeTrue
        $bytes = [IO.File]::ReadAllBytes($out)
        @($bytes | Where-Object { $_ -eq 0 }).Count | Should -Be 0
        ((Read-Lines -Path $out) -join '|') | Should -Be 'fixture-alpha|fixture-beta'
    }

    It 'detects the same encoding regression when WSL_UTF8 is removed' {
        $out = Join-Path $TestDrive 'control-out.txt'
        $r = Invoke-Detect -WslExe $script:FakeWsl -OutFile $out -DropUtf8
        $r.ExitCode | Should -Be 0
        $bytes = [IO.File]::ReadAllBytes($out)
        $bytes.Length | Should -BeGreaterThan 0
        @($bytes | Where-Object { $_ -eq 0 }).Count | Should -BeGreaterThan 0
    }
}
