# Pester 5: exact payload integrity; all files are isolated below TestDrive.
BeforeAll {
    $modules = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    Import-Module (Join-Path $modules 'QTrade.Payload.psm1') -DisableNameChecking
    function New-IntegrityFile {
        param([string] $Relative, [string] $Content = 'known-good')
        $path = Join-Path $script:Stage $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
        [IO.File]::WriteAllText($path, $Content)
        return [pscustomobject]@{
            path = $Relative.Replace('\', '/')
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            size = [long](Get-Item -LiteralPath $path).Length
        }
    }
}

Describe 'Exact payload integrity' {
    BeforeEach {
        $script:Stage = Join-Path $TestDrive ([Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:Stage | Out-Null
    }

    It 'verifies every byte-bearing entry including nested console and WinAgent files' {
        $files = @(
            New-IntegrityFile 'console/QTrade.exe'
            New-IntegrityFile 'console/resources/app.asar' 'renderer-content'
            New-IntegrityFile 'winagent/app/_internal/python312.dll' 'python-runtime'
            New-IntegrityFile 'pkg/chatlog/wx_key2.dll' 'key-extractor'
        )
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = $files }) -StageRoot $script:Stage
        $r.Ok | Should -BeTrue
        $r.VerifiedCount | Should -Be 4
        @($r.Entries | Where-Object status -ne 'Ok').Count | Should -Be 0
    }

    It 'rejects a missing complete console tree although its parent exists' {
        $good = New-IntegrityFile 'winagent/app/runtime.dll'
        $missing = [pscustomobject]@{ path = 'console/resources/app.asar'; sha256 = ('a' * 64); size = 7 }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($good, $missing) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.VerifiedCount | Should -Be 1
        $r.Missing.Count | Should -Be 1
        $r.Missing[0].path | Should -Be 'console/resources/app.asar'
    }

    It 'rejects a missing declared file inside an existing directory' {
        New-IntegrityFile 'console/resources/other.dat' | Out-Null
        $file = [pscustomobject]@{ path = 'console/resources/app.asar'; sha256 = ('a' * 64); size = 7 }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($file) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Missing.Count | Should -Be 1
    }

    It 'detects same-size corruption in <Relative>' -ForEach @(
        @{ Relative = 'console/resources/app.asar' }
        @{ Relative = 'winagent/app/_internal/python312.dll' }
        @{ Relative = 'pkg/chatlog/wx_key2.dll' }
    ) {
        $entry = New-IntegrityFile $Relative 'original'
        [IO.File]::WriteAllText((Join-Path $script:Stage $Relative), 'tampered')
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Mismatched.Count | Should -Be 1
        $r.Mismatched[0].status | Should -Be 'HashMismatch'
    }

    It 'rejects a directory impersonating a declared file without terminating verification' {
        New-Item -ItemType Directory -Path (Join-Path $script:Stage 'console\QTrade.exe') -Force | Out-Null
        $file = [pscustomobject]@{ path = 'console/QTrade.exe'; sha256 = ('a' * 64); size = 7 }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($file) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.VerifiedCount | Should -Be 0
        $r.Entries[0].status | Should -Not -Be 'Ok'
    }

    It 'rejects an incorrect declared size even if the digest matches' {
        $entry = New-IntegrityFile 'console/resources/app.asar'
        $entry.size += 1
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Mismatched.Count | Should -Be 1
        $r.VerifiedCount | Should -Be 0
    }

    It 'reports an unreadable critical file as failed and retains other valid evidence' {
        $entry = New-IntegrityFile 'kernel/bzImage-6.6'
        $entry | Add-Member -NotePropertyName critical -NotePropertyValue $true
        $good = New-IntegrityFile 'console/QTrade.exe'
        Mock Get-QtFileHash -ModuleName QTrade.Payload { throw 'Fixture read failure.' } -ParameterFilter { $Path -like '*bzImage-6.6' }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry, $good) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'ReadError'
        $r.CriticalFailed.Count | Should -Be 1
        $r.VerifiedCount | Should -Be 1
    }

    It 'rejects a destination pointing outside its declared stage path' {
        $entry = New-IntegrityFile 'console/QTrade.exe'
        $entry | Add-Member -NotePropertyName dest -NotePropertyValue (Join-Path $TestDrive 'outside.exe')
        Mock Test-QtPath -ModuleName QTrade.Payload { throw 'Mismatched destination reached filesystem access.' }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'InvalidManifest'
        Should -Invoke Test-QtPath -ModuleName QTrade.Payload -Times 0 -Exactly
    }

    It 'rejects a <Label> hash without clearing the payload' -ForEach @(
        @{ Label = 'missing'; Hash = $null }
        @{ Label = 'empty'; Hash = '' }
        @{ Label = 'short'; Hash = 'abc' }
        @{ Label = 'nonhex'; Hash = ('g' * 64) }
    ) {
        $file = New-IntegrityFile 'console/QTrade.exe'
        $file.sha256 = $Hash
        $file | Add-Member -NotePropertyName critical -NotePropertyValue $true
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($file) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.VerifiedCount | Should -Be 0
        $r.Entries[0].status | Should -Be 'InvalidManifest'
        $r.CriticalFailed.Count | Should -Be 1
        Clear-QtInconsistentPayload -VerifyResult $r | Out-Null
        Test-Path -LiteralPath (Join-Path $script:Stage 'console\QTrade.exe') | Should -BeTrue
    }

    It 'rejects an entry with no hash property' {
        $entry = New-IntegrityFile 'console/QTrade.exe'
        $entry.PSObject.Properties.Remove('sha256')
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'InvalidManifest'
    }

    It 'rejects an empty manifest instead of declaring an empty package staged' {
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @() }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.VerifiedCount | Should -Be 0
    }

    It 'rejects a manifest with no files collection without throwing' {
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ package_version = '1.0.2' }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.VerifiedCount | Should -Be 0
    }

    It 'rejects an entry with no path without throwing' {
        $entry = [pscustomobject]@{ sha256 = ('a' * 64) }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'InvalidManifest'
    }

    It 'rejects unsafe relative path <Relative> before reading a file' -ForEach @(
        @{ Relative = '../escape.dat' }
        @{ Relative = 'console/../../escape.dat' }
        @{ Relative = 'console\..\escape.dat' }
        @{ Relative = '/absolute.dat' }
        @{ Relative = 'C:\absolute.dat' }
        @{ Relative = '\\server\share\data' }
        @{ Relative = 'console/*.dll' }
        @{ Relative = 'console/?.dll' }
        @{ Relative = 'console/file:stream' }
        @{ Relative = '' }
    ) {
        Mock Test-QtPath -ModuleName QTrade.Payload { throw 'Unsafe path reached filesystem access.' }
        Mock Get-QtFileHash -ModuleName QTrade.Payload { throw 'Unsafe path reached file hashing.' }
        $entry = [pscustomobject]@{ path = $Relative; sha256 = ('a' * 64) }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'InvalidManifest'
        $r.VerifiedCount | Should -Be 0
        Should -Invoke Test-QtPath -ModuleName QTrade.Payload -Times 0 -Exactly
        Should -Invoke Get-QtFileHash -ModuleName QTrade.Payload -Times 0 -Exactly
    }

    It 'does not mistake a legacy console wildcard for a present console' {
        $entry = [pscustomobject]@{ path = 'console/*'; dest = (Join-Path $script:Stage 'console') }
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($entry) }) -StageRoot $script:Stage
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'InvalidManifest'
    }
}
