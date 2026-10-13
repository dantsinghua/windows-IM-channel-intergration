# Execute only the parsed upgrade dispatcher with isolated paths and fake effects.
#requires -Version 5.1
BeforeAll {
    $modules = Join-Path $PSScriptRoot '..\engine\modules'
    foreach ($name in @('QTrade.Native', 'QTrade.Exit', 'QTrade.State', 'QTrade.Log', 'QTrade.Payload',
            'QTrade.Preflight', 'QTrade.Wsl', 'QTrade.Distro', 'QTrade.Upgrade', 'QTrade.Console')) {
        Import-Module (Join-Path $modules ($name + '.psm1')) -DisableNameChecking
    }
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $PSScriptRoot '..\engine\run-step.ps1'), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Dispatcher parser failed.' }
    $switch = @($ast.FindAll({ param($node)
        $node -is [Management.Automation.Language.SwitchStatementAst] -and $node.Condition.Extent.Text -eq '$Step'
    }, $true))[0]
    $body = @($switch.Clauses | Where-Object { $_.Item1.Value -eq 'upgrade' })[0].Item2.Extent.Text
    $script:UpgradeBranch = [scriptblock]::Create($body.Substring(1, $body.Length - 2))
    function script:Get-QtOpt {
        param($Name, $Default)
        if ($Name -eq 'package_version') { return '1.0.2' }
        if ($Name -eq 'accept_shutdown') { return $true }
        return $Default
    }
    function script:Write-QtStepResult {
        param($Ok, $State, $Reason, $Message, $Data, $ExitName)
        $script:StepResult = [pscustomobject]@{ Ok = $Ok; State = $State; Reason = $Reason; ExitName = $ExitName }
        throw 'CAPTURED_RESULT'
    }
}

Describe 'Legacy upgrade cannot enter a clean reinstall without verified ownership and explicit consent' {
    BeforeEach {
        $script:paths = [pscustomobject]@{
            Root = $TestDrive; Install = (Join-Path $TestDrive 'install'); Manifest = (Join-Path $TestDrive 'manifest.json')
            InstalledManifest = (Join-Path $TestDrive 'install\installed-manifest.json')
            StateFile = (Join-Path $TestDrive 'install\install_state.json'); Wsl = (Join-Path $TestDrive 'wsl')
            Console = (Join-Path $TestDrive 'console'); Distro = (Join-Path $TestDrive 'distro')
        }
        $script:state = [pscustomobject]@{ state = 'DONE'; package_version = '1.0.1'; distro = [pscustomobject]@{ name = 'qtrade' } }
        $script:StepResult = $null
        Mock Get-QtDriveFreeBytes { 100GB }
        Mock Write-QtLog { }
        Mock Read-QtManifest { [pscustomobject]@{ package_version = '1.0.2'; schema_breaking = $false; items = @() } } `
            -ParameterFilter { $Path -eq $script:paths.Manifest }
        Mock Read-QtManifest { $null } -ParameterFilter { $Path -ne $script:paths.Manifest }
        Mock Invoke-QtDrain { [pscustomobject]@{ Ok = $true } }
        Mock Stop-QtTriad { [pscustomobject]@{ Ok = $true } }
        Mock Start-QtTriad { [pscustomobject]@{ Ok = $true } }
        Mock Write-QtInstallState { }
        Mock Add-QtHistory { }
        Mock Set-QtState { }
        Mock Invoke-QtHttp { throw 'REAL_HTTP_FORBIDDEN' }
        Mock Invoke-QtWsl { throw 'REAL_WSL_FORBIDDEN' }
        Mock -ModuleName QTrade.Native Invoke-QtProcess { throw 'REAL_PROCESS_FORBIDDEN' }
    }

    It 'refuses destructive work when ownership evidence and clean-reinstall consent are absent' {
        try { & $script:UpgradeBranch } catch {
            if ($_.Exception.Message -ne 'CAPTURED_RESULT') { throw }
        }
        $script:StepResult | Should -Not -BeNullOrEmpty
        $script:StepResult.Ok | Should -BeFalse
        $script:state.package_version | Should -Be '1.0.1'
        Should -Invoke Invoke-QtDrain -Times 0 -Exactly
        Should -Invoke Stop-QtTriad -Times 0 -Exactly
        Should -Invoke Start-QtTriad -Times 0 -Exactly
    }
}
