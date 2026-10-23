#requires -Version 5.1
# Read defaults and use fake certificate seams only.
BeforeAll {
    $source = $env:QT_TEST_SIGNING_MODULE
    if (-not $source) { $source = Join-Path $PSScriptRoot '..\signing\QTrade.Signing.psm1' }
    Import-Module $source -Force -DisableNameChecking
    $script:SigningModuleName = (Get-Command Get-QtSigningDefault).ModuleName
}
Describe 'Signing defaults follow the current Windows user without creating credentials' {
    It 'derives the export location from the OS user-local known folder' {
        $expected = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'QTrade\signing'
        (Get-QtSigningDefault).CertDir | Should -Be $expected
    }
    It 'reading defaults performs no directory or certificate writes' {
        Mock -ModuleName $script:SigningModuleName New-Item { throw 'No write expected.' }
        Mock -ModuleName $script:SigningModuleName New-QtCertInStore { throw 'No certificate operation expected.' }
        Mock -ModuleName $script:SigningModuleName Export-QtCertFile { throw 'No export expected.' }
        Get-QtSigningDefault | Should -Not -BeNullOrEmpty
        Should -Invoke -ModuleName $script:SigningModuleName New-Item -Times 0 -Exactly
        Should -Invoke -ModuleName $script:SigningModuleName New-QtCertInStore -Times 0 -Exactly
        Should -Invoke -ModuleName $script:SigningModuleName Export-QtCertFile -Times 0 -Exactly
    }
    It 'an explicit export directory remains authoritative in dry-run mode' {
        Mock -ModuleName $script:SigningModuleName Get-QtCertFromStore { @() }
        Mock -ModuleName $script:SigningModuleName New-QtCertInStore { throw 'No real certificate.' }
        Mock -ModuleName $script:SigningModuleName Export-QtCertFile { throw 'No real export.' }
        $directory = Join-Path $TestDrive 'explicit-certs'
        $r = New-QtCodeSigningCertCore -Organization 'QTrade' -CertDir $directory -WhatIf
        $r.WhatIf | Should -BeTrue
        (Split-Path -Parent $r.CerPath) | Should -Be $directory
        Test-Path -LiteralPath $directory | Should -BeFalse
    }
}
