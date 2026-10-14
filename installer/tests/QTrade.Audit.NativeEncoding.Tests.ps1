# Regression for the actual PS5/.NET Framework parent-console UTF8 BOM case.
BeforeAll { Import-Module (Join-Path $PSScriptRoot '..\engine\modules\QTrade.Native.psm1') -Force -DisableNameChecking }
Describe 'Audit native stdin encoding' {
    It 'does not inherit the parent console UTF8 preamble' {
        $before = [Console]::InputEncoding
        try {
            [Console]::InputEncoding = New-Object Text.UTF8Encoding($true)
            $code = '$m=New-Object IO.MemoryStream;[Console]::OpenStandardInput().CopyTo($m);[Console]::Write([Convert]::ToBase64String($m.ToArray()))'
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
            $text = "audit`n" + [char]0x4e2d
            $r = Invoke-QtProcess -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile','-EncodedCommand',$encoded) -StandardInput $text -TimeoutSec 10
            $r.ExitCode | Should -Be 0
            $r.StdOut | Should -Be ([Convert]::ToBase64String((New-Object Text.UTF8Encoding($false)).GetBytes($text)))
        } finally { [Console]::InputEncoding = $before }
    }
}
