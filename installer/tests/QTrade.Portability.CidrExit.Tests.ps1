#requires -Version 5.1
# Execute a parsed dispatcher clause; every host/guest process is mocked.
BeforeAll {
    $modules = $env:QT_REMAINING_TEST_MODULES
    if (-not $modules) { $modules = Join-Path $PSScriptRoot '..\engine\modules' }
    foreach ($name in @('Native','State','Preflight','Payload','Wsl','Distro','Exit')) {
        Import-Module (Join-Path $modules ('QTrade.' + $name + '.psm1')) -DisableNameChecking
    }
    $source = $env:QT_REMAINING_TEST_RUNSTEP
    if (-not $source) { $source = Join-Path $PSScriptRoot '..\engine\run-step.ps1' }
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Parse failed.' }
    $switches = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.SwitchStatementAst] -and $n.Condition.Extent.Text -eq '$Step' }, $true)
    $body = @($switches[0].Clauses | Where-Object { $_.Item1.Value -eq 'DISTRO_IMPORTED' })[0].Item2.Extent.Text
    Set-Item Function:Invoke-FixtureDistroStep -Value ([scriptblock]::Create($body.Substring(1, $body.Length - 2)))
    function Get-QtOpt { param($Name, $Default); return $Default }
    function Write-QtStepResult { param($Ok,$State,$Reason,$ExitName,$Message,$Data); throw 'Unmocked result.' }
}
Describe 'Exhausted network candidates preserve their public exit code' {
    BeforeEach {
        $script:paths = Get-QtPaths -Root (Join-Path $TestDrive 'live')
        $script:state = New-QtInstallState -PackageVersion '1.0.2'
        $script:Result = $null
        Mock Read-QtManifest { [pscustomobject]@{ files = @() } }
        Mock Invoke-QtWsl { [pscustomobject]@{ ExitCode = 0; TimedOut = $false; StdOut = ''; StdErr = '' } }
        Mock -ModuleName QTrade.Native Invoke-QtProcess { throw 'Forbidden real native process.' }
        Mock Import-QtDistro { [pscustomobject]@{ Ok = $true } }
        Mock Write-QtInstallState { }
        Mock Write-QtStepResult {
            $script:Result = [pscustomobject]@{ Ok=$Ok; Reason=$Reason; ExitName=$ExitName }
            throw 'FixtureStepFinished'
        }
        Mock -ModuleName QTrade.Distro Get-QtOccupiedNetworkPrefixes { [pscustomobject]@{ Ok=$true; Prefixes=@('10.0.0.0/8') } }
        Mock -ModuleName QTrade.Distro Invoke-QtWsl {
            if ($StandardInput) { throw 'Exhausted CIDR must not write the guest environment.' }
            if (($WslArgs -join ' ') -match 'cat /etc/qtrade/install.env') { return [pscustomobject]@{ ExitCode=44; TimedOut=$false; StdOut=''; StdErr='' } }
            [pscustomobject]@{ ExitCode=0; TimedOut=$false; StdOut=''; StdErr='' }
        }
        Mock -ModuleName QTrade.Distro Wait-QtDistroSystemd { throw 'Exhausted CIDR must not advance bootstrap.' }
        Mock -ModuleName QTrade.Distro Write-QtInstallEnv { throw 'Exhausted CIDR must not write install.env.' }
    }
    It 'maps actual four-candidate exhaustion to exit 76 without writing env or starting Docker' {
        { Invoke-FixtureDistroStep } | Should -Throw 'FixtureStepFinished'
        $script:Result.Ok | Should -BeFalse
        $script:Result.Reason | Should -Be 'DOCKER_CIDR_EXHAUSTED'
        $script:Result.ExitName | Should -Be 'E_INSTALL_DOCKER_CIDR_EXHAUSTED'
        (Get-QtExitCode -Name $script:Result.ExitName) | Should -Be 76
        Should -Invoke -ModuleName QTrade.Distro Write-QtInstallEnv -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Distro Wait-QtDistroSystemd -Times 0 -Exactly
        Should -Invoke -ModuleName QTrade.Distro Invoke-QtWsl -Times 0 -Exactly -ParameterFilter { $WslArgs -contains 'start' -or $StandardInput }
    }
    It 'preserves <Reason> as exit <Code>' -ForEach @(
        @{ Reason='SYSTEMD_NOT_READY'; Name='E_INSTALL_SYSTEMD_NOT_READY'; Code=72 },
        @{ Reason='UNRECOGNIZED_FIXTURE_FAILURE'; Name='E_INSTALL_INTERNAL'; Code=200 }
    ) {
        Mock Initialize-QtDistroBootstrap { [pscustomobject]@{ Ok=$false; Reason=$Reason } }
        { Invoke-FixtureDistroStep } | Should -Throw 'FixtureStepFinished'
        $script:Result.Reason | Should -Be $Reason
        $script:Result.ExitName | Should -Be $Name
        (Get-QtExitCode -Name $script:Result.ExitName) | Should -Be $Code
    }
}
