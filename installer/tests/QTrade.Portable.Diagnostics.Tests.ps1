BeforeAll {
    Import-Module ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../engine/modules/QTrade.Native.psm1"))) -DisableNameChecking
    Import-Module (Join-Path $PSScriptRoot '..\engine\modules\QTrade.WinAgent.psm1') -DisableNameChecking
}
Describe 'Portable precise startup diagnostics' {

        It 'retains authentication failure instead of claiming the service is stopped' {
            Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{Ok=$false;StatusCode=401;Body='' } }
            $r=Get-QtWinAgentHealth -Token ('A'*64)
            $r.Ok | Should -BeFalse
            $r.Cause | Should -Be 'WINAGENT_AUTH_FAILED'
            $r.UserAgentKnown | Should -BeFalse
        }
        It 'distinguishes authorization from authentication' {
            Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{Ok=$false;StatusCode=403;Body='' } }
            (Get-QtWinAgentHealth -Token ('A'*64)).Cause | Should -Be 'WINAGENT_FORBIDDEN'
        }
        It 'does not mislabel malformed health JSON as a missing user session' {
            Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{Ok=$true;StatusCode=200;Body='{bad'} }
            $r=Get-QtWinAgentHealth -Token ('A'*64)
            $r.Cause | Should -Be 'WINAGENT_HEALTH_INVALID'
            $r.UserAgentKnown | Should -BeFalse
        }
        It 'reports Vault readiness separately from the HTTP listener' {
            Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{Ok=$true;StatusCode=200;Body=' {"ok":true,"user_agent":true,"modules":{"vault":"missing"}}'} }
            $r=Get-QtWinAgentHealth -Token ('A'*64)
            $r.Cause | Should -Be 'WINAGENT_VAULT_NOT_READY'
            $r.UserAgentKnown | Should -BeTrue
        }
        It 'preserves a known transport timeout cause' {
            Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{Ok=$false;StatusCode=0;Body='';Reason='HTTP_TIMEOUT'} }
            (Get-QtWinAgentHealth -Token ('A'*64)).Cause | Should -Be 'HTTP_TIMEOUT'
        }
        It 'keeps unattended installation healthy without inventing an online user agent' {
            Mock -ModuleName QTrade.WinAgent Invoke-QtHttp { [pscustomobject]@{Ok=$true;StatusCode=200;Body='{"ok":true,"user_agent":false,"modules":{"vault":"ok"}}'} }
            $r=Get-QtWinAgentHealth -Token ('A'*64)
            $r.Ok | Should -BeTrue
            $r.UserAgentKnown | Should -BeTrue
            $r.UserAgent | Should -BeFalse
            $r.Cause | Should -Be ''
        }
}
Describe 'Portable native HTTP failure cause' {

        It 'does not retain raw exception text and distinguishes timeout' {
            Mock -ModuleName QTrade.Native Invoke-WebRequest { throw [Net.WebException]::new('synthetic-secret-error', [Net.WebExceptionStatus]::Timeout) }
            $r=Invoke-QtHttp -Uri 'http://127.0.0.1:1234/health'
            $r.Ok | Should -BeFalse
            $r.Reason | Should -Be 'HTTP_TIMEOUT'
            $r.Body | Should -Not -Match 'synthetic-secret'
        }
}



Describe 'Development profile diagnostics preserve the failed precondition' {
    It 'does not call a schema error missing Python: <Schema>' -ForEach @(@{Schema=2},@{Schema=$true},@{Schema='1'}) {
        $path=Join-Path $TestDrive 'profile.json'
        [IO.File]::WriteAllText($path,(@{schema=$Schema;windows_python=(Join-Path $TestDrive 'missing.exe')}|ConvertTo-Json -Compress))
        $entry=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../scripts/local_e2e/Start-Local.ps1'))
        $r=Invoke-QtProcess -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$entry,'-Context',$path,'-CheckOnly') -TimeoutSec 15
        $r.ExitCode | Should -Be 2
        $r.StdErr | Should -Match 'CONTEXT_SCHEMA_UNSUPPORTED'
        $r.StdErr | Should -Not -Match 'DEVELOPMENT_PYTHON_MISSING'
    }
    It 'reports a missing Python configuration field before checking the file system' {
        $path=Join-Path $TestDrive 'profile.json'
        [IO.File]::WriteAllText($path,'{"schema":1}')
        $entry=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../scripts/local_e2e/Start-Local.ps1'))
        $r=Invoke-QtProcess -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$entry,'-Context',$path,'-CheckOnly') -TimeoutSec 15
        $r.ExitCode | Should -Be 2
        $r.StdErr | Should -Match 'CONTEXT_FIELD_REQUIRED: windows_python'
    }
}
