# Isolated service registration arguments. No call reaches the real SCM.
#requires -Version 5.1
BeforeAll {
    $script:Modules = Join-Path (Split-Path -Parent $PSScriptRoot) 'engine\modules'
    foreach ($name in @('QTrade.Native', 'QTrade.WinAgent')) {
        Import-Module (Join-Path $script:Modules ($name + '.psm1')) -DisableNameChecking
    }
}

Describe 'Service registration binds the target installation identity' {
    It 'passes explicit service mode, relocated root and installing user SID' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtSc { [pscustomobject]@{ ExitCode = 0 } }
        $r = Install-QtWinAgentService -ExePath 'E:\Trade Apps\winagent\app\qtrade-winagent-svc.exe' `
            -Root 'E:\Trade Apps\winagent' -InstallUserSid 'S-1-5-21-1111-2222-3333-1001'
        $r.Ok | Should -BeTrue
        $r.Commands.Count | Should -Be 4
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtSc -Times 1 -Exactly -ParameterFilter {
            $ScArgs[0] -eq 'create' -and ($ScArgs -join ' ') -match '--service' -and
            ($ScArgs -join ' ') -match '--install-user-sid S-1-5-21-1111-2222-3333-1001' -and
            ($ScArgs -join ' ') -match '--root "E:\\Trade Apps\\winagent"'
        }
    }

    It 'reconfigures an existing service with the same identity and path' {
        Mock -ModuleName QTrade.WinAgent Invoke-QtSc {
            [pscustomobject]@{ ExitCode = $(if ($ScArgs[0] -eq 'create') { 1073 } else { 0 }) }
        }
        $r = Install-QtWinAgentService -ExePath 'E:\Trade Apps\winagent\app\qtrade-winagent-svc.exe' `
            -Root 'E:\Trade Apps\winagent' -InstallUserSid 'S-1-5-21-1111-2222-3333-1001'
        $r.Ok | Should -BeTrue
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtSc -Times 1 -Exactly -ParameterFilter {
            $ScArgs[0] -eq 'config' -and ($ScArgs -join ' ') -match '--service' -and
            ($ScArgs -join ' ') -match '--install-user-sid S-1-5-21-1111-2222-3333-1001'
        }
    }

    It 'rejects invalid identity <Sid> without invoking sc.exe' -ForEach @(
        @{ Sid = '' }, @{ Sid = 'bad' }, @{ Sid = 'S-1-5-18' }
    ) {
        Mock -ModuleName QTrade.WinAgent Invoke-QtSc { throw 'No SCM call is allowed.' }
        $r = Install-QtWinAgentService -ExePath 'E:\Trade Apps\winagent\app\qtrade-winagent-svc.exe' `
            -Root 'E:\Trade Apps\winagent' -InstallUserSid $Sid
        $r.Ok | Should -BeFalse
        Should -Invoke -ModuleName QTrade.WinAgent Invoke-QtSc -Times 0 -Exactly
    }
}
