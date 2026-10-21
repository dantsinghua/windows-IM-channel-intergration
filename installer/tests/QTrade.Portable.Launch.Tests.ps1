BeforeAll { Import-Module (Join-Path $PSScriptRoot '..\engine\modules\QTrade.WinAgent.psm1') -DisableNameChecking; Import-Module ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../engine/modules/QTrade.Native.psm1"))) -DisableNameChecking; Import-Module (Join-Path $PSScriptRoot '..\engine\modules\QTrade.Launch.psm1') -DisableNameChecking }
Describe 'Machine-independent installed startup' {

        BeforeEach {
            $script:Root=Join-Path $TestDrive 'Alternate Drive Layout'
            $script:Sid='S-1-5-21-111-222-333-1001'
            foreach($relative in @('winagent\app\qtrade-winagent-svc.exe','winagent\app\qtrade-winagent-user.exe','winagent\winagent.toml','console\QTrade.exe')) {
                $file=Join-Path $script:Root $relative
                $null=New-Item -ItemType Directory -Path (Split-Path -Parent $file) -Force
                [IO.File]::WriteAllText($file,'synthetic-not-an-executable')
            }
            $cmd=(@((Join-Path $script:Root 'winagent\app\qtrade-winagent-svc.exe'),'--service','--root',(Join-Path $script:Root 'winagent'),'--install-user-sid',$script:Sid) | ForEach-Object {ConvertTo-QtProcessArgument -Value $_}) -join ' '
            $script:Svc=[pscustomobject]@{State='Running';PathName=$cmd}
            $script:Health=[pscustomobject]@{Ok=$true;UserAgent=$true;Cause='';StatusCode=200}
            $script:Now=[DateTime]::UtcNow
            Mock -ModuleName QTrade.Launch Get-QtNow { $script:Now }
            Mock -ModuleName QTrade.Launch Start-QtSleep { param($Seconds) $script:Now=$script:Now.AddSeconds($Seconds) }
            Mock -ModuleName QTrade.Launch Get-QtLaunchService { $script:Svc }
            Mock -ModuleName QTrade.Launch Get-QtLaunchIdentity { [pscustomobject]@{Sid=$script:Sid;Session=4} }
            Mock -ModuleName QTrade.Launch Get-QtConsoleBundle { [pscustomobject]@{Ok=$true;Reason='';ConsoleToken=('C'*64);AgentToken=('A'*64);AgentBaseUrl='http://127.0.0.1:17600'} }
            Mock -ModuleName QTrade.Launch Get-QtWinAgentHealth { $script:Health }
            Mock -ModuleName QTrade.Launch Request-QtLaunchService { [pscustomobject]@{Ok=$true} }
            Mock -ModuleName QTrade.Launch Start-QtOwnedUser { $script:Health.UserAgent=$true }
            Mock -ModuleName QTrade.Launch Start-QtInstalledConsole { }
            Mock -ModuleName QTrade.Launch Invoke-QtHttp { [pscustomobject]@{Ok=$true;StatusCode=200;Body='{"ok":true,"dockerd":true,"winagent":{"online":true,"user_agent":true}}'} }
        }
        It 'opens only after authenticated dependencies are ready' {
            $r=Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1' -TimeoutSec 10
            $r.Ok | Should -BeTrue
            $r.Stage | Should -Be 'ready'
            Should -Invoke -ModuleName QTrade.Launch Start-QtInstalledConsole -Times 1 -Exactly
            Should -Invoke -ModuleName QTrade.Launch Request-QtLaunchService -Times 0 -Exactly
        }
        It 'reports the missing service without inventing authentication failure' {
            Mock -ModuleName QTrade.Launch Get-QtLaunchService { $null }
            (Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1').Reason | Should -Be 'SERVICE_NOT_INSTALLED'
            Should -Invoke -ModuleName QTrade.Launch Get-QtConsoleBundle -Times 0 -Exactly
        }
        It 'never starts a service belonging to another product root' {
            $script:Svc.PathName='other.exe --service'
            (Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1').Reason | Should -Be 'SERVICE_OWNERSHIP_MISMATCH'
            Should -Invoke -ModuleName QTrade.Launch Request-QtLaunchService -Times 0 -Exactly
        }
        It 'reports WinAgent authentication failure without starting another user agent' {
            $script:Health=[pscustomobject]@{Ok=$false;UserAgent=$false;Cause='WINAGENT_AUTH_FAILED';StatusCode=401}
            $r=Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1'
            $r.Reason | Should -Be 'WINAGENT_AUTH_FAILED'
            $r.Stage | Should -Be 'winagent_health'
            Should -Invoke -ModuleName QTrade.Launch Start-QtOwnedUser -Times 0 -Exactly
        }
        It 'starts an absent user session once, then rechecks readiness' {
            $script:Health.UserAgent=$false
            $r=Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1' -TimeoutSec 10
            $r.Ok | Should -BeTrue
            Should -Invoke -ModuleName QTrade.Launch Start-QtOwnedUser -Times 1 -Exactly
        }
        It 'distinguishes Agent authentication from WinAgent authentication' {
            Mock -ModuleName QTrade.Launch Invoke-QtHttp { [pscustomobject]@{Ok=$false;StatusCode=401;Body=''} }
            $r=Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1'
            $r.Reason | Should -Be 'AGENT_AUTH_FAILED'
            $r.Stage | Should -Be 'agent_health'
        }
        It 'does not turn a console executable error into a JSON error' {
            Mock -ModuleName QTrade.Launch Start-QtInstalledConsole { throw [ComponentModel.Win32Exception]::new(193) }
            $r=Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1' -TimeoutSec 5
            $r.Reason | Should -Be 'EXECUTABLE_ARCHITECTURE_INVALID'
            $r.Stage | Should -Be 'console_start'
        }
        It 'does not launch UI from service session zero' {
            Mock -ModuleName QTrade.Launch Get-QtLaunchIdentity { [pscustomobject]@{Sid=$script:Sid;Session=0} }
            (Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1').Reason | Should -Be 'INTERACTIVE_SESSION_REQUIRED'
        }
        It 'does not treat a string true as authenticated boolean health' {
            Mock -ModuleName QTrade.Launch Invoke-QtHttp { [pscustomobject]@{Ok=$true;StatusCode=200;Body='{"ok":true,"dockerd":true,"winagent":{"online":"True","user_agent":"True"}}'} }
            $r=Start-QtInstalledRuntime -Root $script:Root -LauncherPath 'synthetic.ps1' -TimeoutSec 5
            $r.Ok | Should -BeFalse
            Should -Invoke -ModuleName QTrade.Launch Start-QtInstalledConsole -Times 0 -Exactly
        }
        It 'reports the original OS cause <Code>' -ForEach @(
            @{Code=5;Expected='ACCESS_DENIED'},@{Code=126;Expected='NATIVE_DEPENDENCY_MISSING'},
            @{Code=193;Expected='EXECUTABLE_ARCHITECTURE_INVALID'},@{Code=577;Expected='EXECUTABLE_SIGNATURE_REJECTED'},
            @{Code=1058;Expected='SERVICE_DISABLED'},@{Code=1068;Expected='SERVICE_DEPENDENCY_FAILED'},
            @{Code=1223;Expected='UAC_CANCELLED'},@{Code=1260;Expected='EXECUTION_POLICY_BLOCKED'},
            @{Code=10048;Expected='PORT_IN_USE'}
        ) { Get-QtLaunchNativeReason -Code $Code | Should -Be $Expected }
}


