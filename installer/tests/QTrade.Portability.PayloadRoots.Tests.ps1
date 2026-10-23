# Stage bytes and destination identity are separate trust boundaries.
#requires -Version 5.1
BeforeAll {
    Import-Module (Join-Path $PSScriptRoot '..\engine\modules\QTrade.Payload.psm1') -DisableNameChecking
}
Describe 'Verify a detached stage without reading the old live payload' {
    BeforeEach {
        $script:Stage = Join-Path $TestDrive 'source-stage'
        $script:Live = Join-Path $TestDrive 'destination-live'
        foreach ($root in @($script:Stage, $script:Live)) { New-Item -ItemType Directory -Path (Join-Path $root 'console') -Force | Out-Null }
        [IO.File]::WriteAllText((Join-Path $script:Stage 'console\app.dat'), 'new-good')
        [IO.File]::WriteAllText((Join-Path $script:Live 'console\app.dat'), 'old-live')
        $script:Entry = [pscustomobject]@{ path = 'console/app.dat'; dest = (Join-Path $script:Live 'console\app.dat'); size = 8;
            sha256 = (Get-FileHash -LiteralPath (Join-Path $script:Stage 'console\app.dat') -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    It 'verifies stage bytes while the declared destination belongs to live' {
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($script:Entry) }) -StageRoot $script:Stage -DestinationRoot $script:Live
        $r.Ok | Should -BeTrue
        [IO.File]::ReadAllText((Join-Path $script:Live 'console\app.dat')) | Should -Be 'old-live'
    }
    It 'does not let a valid old live file conceal corrupted stage bytes' {
        [IO.File]::WriteAllText((Join-Path $script:Live 'console\app.dat'), 'new-good')
        [IO.File]::WriteAllText((Join-Path $script:Stage 'console\app.dat'), 'tampered')
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($script:Entry) }) -StageRoot $script:Stage -DestinationRoot $script:Live
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'HashMismatch'
    }
    It 'rejects a declared destination outside the selected live root' {
        $script:Entry.dest = Join-Path $TestDrive 'unrelated\app.dat'
        $r = Invoke-QtPayloadVerify -Manifest ([pscustomobject]@{ files = @($script:Entry) }) -StageRoot $script:Stage -DestinationRoot $script:Live
        $r.Ok | Should -BeFalse
        $r.Entries[0].status | Should -Be 'InvalidManifest'
    }
}
