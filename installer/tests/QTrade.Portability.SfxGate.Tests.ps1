# Independent release-gate regression. No executable or installer is launched.
#requires -Version 5.1
BeforeAll {
    $script:Installer = Split-Path -Parent $PSScriptRoot
    Import-Module (Join-Path $script:Installer 'sfx-stub\QTrade.SfxStub.psm1') -DisableNameChecking
    $source = [IO.File]::ReadAllText((Join-Path $script:Installer 'build\build.ps1'))
    $start = $source.IndexOf("Write-Section ('G5 ")
    $end = $source.IndexOf("`nif (`$CheckOnly)", $start)
    if ($start -lt 0 -or $end -lt $start) { throw 'SFX gate boundary not found.' }
    $script:Gate = [scriptblock]::Create($source.Substring($start, $end - $start))
    function script:Write-Section { param($Text) }
    function script:Write-Ok { param($Text) }
    function script:Write-Bad { param($Text) }
    $script:NewStub = $env:QTRADE_TEST_SFX_STUB
    $script:OldStub = Join-Path $script:Installer 'build\QTradeSD.sfx'
}

Describe 'SFX configuration and binary jointly prove secure random staging' {
    BeforeEach {
        $script:BuildDir = $TestDrive
        $script:InstallerRoot = $script:Installer
        $script:SfxConfigName = 'sfx-config-qtrade.txt'
        $script:StubKind = 'qtrade'
        $script:ChainHeadName = 'run-engine.cmd'
        $script:SfxAllowedKeys = @('Title','BeginPrompt','Progress','Directory','RunProgram','ExecuteFile','ExecuteParameters','InstallPath')
        $script:SfxStub = $script:NewStub
        $script:ConfigPath = Join-Path $TestDrive $script:SfxConfigName
        $script:Config = 'Title="Fixture"' + "`n" + 'InstallPath="%ProgramData%\\QTrade-Staging"' + "`n" + 'RunProgram="install\\engine\\run-engine.cmd"' + "`n"
    }
    It 'accepts the exact staging token with the new verified native stub' {
        if (-not $script:NewStub) { Set-ItResult -Skipped -Because 'QTRADE_TEST_SFX_STUB not supplied.'; return }
        [IO.File]::WriteAllText($script:ConfigPath, $script:Config)
        { & $script:Gate } | Should -Not -Throw
        (Test-QtSfxStubBinary -Path $script:NewStub).Ok | Should -BeTrue
    }
    It 'rejects a live-root extraction value even though InstallPath is a known key' {
        [IO.File]::WriteAllText($script:ConfigPath, $script:Config.Replace('QTrade-Staging', 'QTrade'))
        { & $script:Gate } | Should -Throw
    }
    It 'rejects duplicate InstallPath values rather than depending on parser winner order' {
        [IO.File]::WriteAllText($script:ConfigPath, $script:Config + 'InstallPath="%ProgramData%\\QTrade-Staging"')
        { & $script:Gate } | Should -Throw
    }
    It 'rejects an old patched binary that lacks the unique staging implementation' {
        $r = Test-QtSfxStubBinary -Path $script:OldStub
        $r.Ok | Should -BeFalse
        ($r.Problems -join ' ') | Should -Match 'QTRADE_UNIQUE_STAGE_V1'
    }
    It 'keeps old binary out of CheckOnly validation as well as final assembly' {
        $script:SfxStub = $script:OldStub
        [IO.File]::WriteAllText($script:ConfigPath, $script:Config)
        { & $script:Gate } | Should -Throw
    }
}
