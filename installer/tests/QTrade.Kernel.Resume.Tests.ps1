# Execute only parsed dispatcher branches with fake boundaries, never run-step itself.
#requires -Version 5.1
BeforeAll {
    if (Test-Path -LiteralPath 'C:\ProgramData\QTrade') { throw 'Protected ProgramData root exists.' }
    $modules = $env:QT_KERNEL_TEST_MODULES
    if (-not $modules) { $modules = Join-Path $PSScriptRoot '..\engine\modules' }
    foreach ($name in @('QTrade.Kernel', 'QTrade.Payload', 'QTrade.Native', 'QTrade.Exit', 'QTrade.State', 'QTrade.Log', 'QTrade.Wsl', 'QTrade.Preflight')) {
        Import-Module (Join-Path $modules ($name + '.psm1')) -DisableNameChecking -Force
    }
    $source = $env:QT_KERNEL_TEST_RUNSTEP
    if (-not $source) { $source = Join-Path $PSScriptRoot '..\engine\run-step.ps1' }
    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Dispatcher parser failed.' }
    $script:Dispatcher = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.SwitchStatementAst] -and $node.Condition.Extent.Text -eq '$Step'
    }, $true))[0]
    function script:Get-QtOpt { param($Name, $Default); if ($Name -eq 'accept_shutdown') { return $true }; return $Default }
    function script:Write-QtStepResult {
        param($Ok, $State, $Reason, $Message, $Data, $ExitName)
        $script:StepResult = [pscustomobject]@{ Ok = $Ok; State = $State; Reason = $Reason; ExitName = $ExitName; Data = $Data }
        throw 'CAPTURED_STEP_RESULT'
    }
    if (-not (Get-Command Test-QtKCheckBaseline -ErrorAction SilentlyContinue)) {
        function script:Test-QtKCheckBaseline { throw 'BASELINE_NOT_IMPLEMENTED' }
    }
    function script:Invoke-IsolatedStep([string] $Name) {
        $clause = @($script:Dispatcher.Clauses | Where-Object { $_.Item1.Value -eq $Name })[0]
        $body = $clause.Item2.Extent.Text
        try {
            & ([scriptblock]::Create($body.Substring(1, $body.Length - 2)))
            throw 'DISPATCHER_DID_NOT_RETURN_RESULT'
        } catch {
            if ($_.Exception.Message -ne 'CAPTURED_STEP_RESULT') { throw }
        }
    }
}
AfterAll { (Test-Path -LiteralPath 'C:\ProgramData\QTrade') | Should -BeFalse }

Describe 'Kernel dispatcher stop conditions with isolated paths' {
    BeforeEach {
        $script:StepResult = $null
        $script:state = [pscustomobject]@{ state = 'KERNEL_STAGED'; env = [pscustomobject]@{
            wslconfig_path = (Join-Path $TestDrive '.wslconfig'); mem_total_mb = 16384; kernel_state = 'NONE'; distros = @()
        } }
        $script:paths = [pscustomobject]@{
            Manifest = (Join-Path $TestDrive 'manifest.json'); Kernel = (Join-Path $TestDrive 'kernel')
            Wsl = (Join-Path $TestDrive 'wsl'); KCheck = (Join-Path $TestDrive 'kcheck')
            StateFile = (Join-Path $TestDrive 'state.json'); EngineExe = (Join-Path $TestDrive 'unused-engine.exe')
            Logs = (Join-Path $TestDrive 'logs'); KernelPtr = (Join-Path $TestDrive 'pointer.json')
        }
        Mock Read-QtManifest { [pscustomobject]@{} }
        Mock Get-QtManifestKernel { [pscustomobject]@{ sha256 = 'fake'; version = '6.6.123.2-binder' } }
        Mock Get-QtWslPolicyFacts { [pscustomobject]@{ CustomKernelForbidden = $false } }
        Mock Test-QtKernelPreconditions { [pscustomobject]@{ Ok = $true } }
        Mock Write-QtInstallState { }
        Mock Set-QtState { }
        Mock Set-QtSubstate { }
        Mock Set-QtRunOnce { }
        Mock Clear-QtRunOnce { }
        Mock Write-QtLog { }
        Mock Remove-QtKCheck { }
        Mock -ModuleName QTrade.Native Invoke-QtProcess { throw 'FORBIDDEN_NATIVE_PROCESS' }
        Mock Backup-QtWslConfig { throw 'FORBIDDEN_CONFIG_WRITE' }
        Mock Write-QtUtf8NoBom { throw 'FORBIDDEN_CONFIG_WRITE' }
        Mock Invoke-QtKernelVerify { throw 'FORBIDDEN_VERIFY' }
        Mock Invoke-QtKernelRollback { throw 'FORBIDDEN_ROLLBACK' }
        Mock Invoke-QtWsl { throw 'FORBIDDEN_REAL_WSL' }
        Mock Test-QtKCheckBaseline { [pscustomobject]@{ Ok = $false; Reason = 'KCHECK_BOOT_FAILED'; Uname = ''; Records = @{ probe = 'failed' } } }
        Mock Import-QtKCheck { [pscustomobject]@{ Ok = $false; Reason = 'KCHECK_IMPORT_FAILED'; Records = @() } }
    }
    It 'rechecks an already staged kcheck before writing configuration on resume' {
        Invoke-IsolatedStep 'KERNEL_SWITCH'
        $script:StepResult.Ok | Should -BeFalse
        $script:StepResult.Reason | Should -Be 'KCHECK_BOOT_FAILED'
        $script:StepResult.ExitName | Should -Be 'E_INSTALL_KCHECK_BOOT_FAILED'
        Should -Invoke Test-QtKCheckBaseline -Times 1 -Exactly
        Should -Invoke Backup-QtWslConfig -Times 0 -Exactly
        Should -Invoke Write-QtUtf8NoBom -Times 0 -Exactly
        Should -Invoke Invoke-QtKernelVerify -Times 0 -Exactly
        Should -Invoke Set-QtRunOnce -Times 0 -Exactly
    }
    It 'stops verify-kernel after failed replacement import without pretending a baseline passed' {
        $script:state.state = 'WSLCONFIG_WRITTEN'
        Invoke-IsolatedStep 'verify-kernel'
        $script:StepResult.Ok | Should -BeFalse
        $script:StepResult.Reason | Should -Be 'KCHECK_IMPORT_FAILED'
        $script:StepResult.ExitName | Should -Be 'E_INSTALL_KCHECK_IMPORT_FAILED'
        Should -Invoke Import-QtKCheck -Times 1 -Exactly
        Should -Invoke Test-QtKCheckBaseline -Times 0 -Exactly
        Should -Invoke Invoke-QtKernelVerify -Times 0 -Exactly
        Should -Invoke Invoke-QtKernelRollback -Times 0 -Exactly
        Should -Invoke Clear-QtRunOnce -Times 0 -Exactly
    }
}
