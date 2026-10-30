# Small admission and child-process tests; no disk filling or native launch.
#requires -Version 5.1
BeforeAll {
    Import-Module (Join-Path $PSScriptRoot '..\build\QTrade.BuildSpace.psm1') -DisableNameChecking -Force
}
Describe 'Build disk admission exact byte boundaries' {
    It 'allows exactly reserve plus required bytes' { Get-QtBuildSpaceVerdict -FreeBytes 68GB -RequiredBytes 8GB -ReserveBytes 60GB | Should -BeTrue }
    It 'rejects one byte below the budget' { Get-QtBuildSpaceVerdict -FreeBytes (68GB - 1) -RequiredBytes 8GB -ReserveBytes 60GB | Should -BeFalse }
    It 'does not overflow when the requested budget exceeds Int64 capacity in aggregate' { Get-QtBuildSpaceVerdict -FreeBytes ([long]::MaxValue) -RequiredBytes ([long]::MaxValue) -ReserveBytes 1 | Should -BeFalse }
    It 'rejects negative inputs' -ForEach @(@{Free=-1;Need=0;Keep=0},@{Free=0;Need=-1;Keep=0},@{Free=0;Need=0;Keep=-1}) {
        { Get-QtBuildSpaceVerdict -FreeBytes $Free -RequiredBytes $Need -ReserveBytes $Keep } | Should -Throw '*BUILD_SPACE_INVALID*'
    }
}
Describe 'Build process remains bounded to its own child' {
    BeforeEach {
        $script:Started=0; $script:Killed=0; $script:Disposed=0; $script:Waits=0; $script:Finished=$true; $script:Probes=0
        $script:Child=[pscustomobject]@{StartInfo=(New-Object Diagnostics.ProcessStartInfo);HasExited=$false;ExitCode=17}
        $script:Child | Add-Member ScriptMethod Start { $script:Started++; return $true }
        $script:Child | Add-Member ScriptMethod WaitForExit { param($Milliseconds); $script:Waits++; if($script:Finished){$this.HasExited=$true}; return $script:Finished }
        $script:Child | Add-Member ScriptMethod Kill { $script:Killed++; $this.HasExited=$true }
        $script:Child | Add-Member ScriptMethod Dispose { $script:Disposed++ }
        Mock -ModuleName QTrade.BuildSpace New-Object { $script:Child } -ParameterFilter { $TypeName -eq 'Diagnostics.Process' }
        Mock -ModuleName QTrade.BuildSpace Assert-QtBuildSpace { $script:Probes++ }
    }
    It 'refuses admission before creating or starting a child' {
        Mock -ModuleName QTrade.BuildSpace Assert-QtBuildSpace { throw 'BUILD_DISK_BUDGET_EXCEEDED' }
        { Invoke-QtBuildNative -FilePath 'never.exe' -BudgetPath $TestDrive } | Should -Throw '*BUILD_DISK_BUDGET_EXCEEDED*'
        $script:Started | Should -Be 0
        Should -Invoke -ModuleName QTrade.BuildSpace New-Object -Times 0 -Exactly -ParameterFilter {$TypeName -eq 'Diagnostics.Process'}
    }
    It 'quotes spaces, embedded quotes, trailing slashes and empty arguments without shell execution' {
        $result=Invoke-QtBuildNative -FilePath 'never.exe' -BudgetPath $TestDrive -Arguments @('', 'two words', 'a"b', 'C:\two words\') -WorkingDirectory $TestDrive
        $result | Should -Be 17
        $script:Child.StartInfo.Arguments | Should -Be '"" "two words" "a\"b" "C:\two words\\"'
        $script:Child.StartInfo.UseShellExecute | Should -BeFalse
        $script:Child.StartInfo.WorkingDirectory | Should -Be $TestDrive
        $script:Killed | Should -Be 0
        $script:Disposed | Should -Be 1
    }
    It 'timeout terminates only the exact child and always disposes its handle' {
        $script:Finished=$false
        { Invoke-QtBuildNative -FilePath 'never.exe' -BudgetPath $TestDrive -TimeoutSeconds 0 } | Should -Throw '*BUILD_CHILD_TIMEOUT*'
        $script:Started | Should -Be 1
        $script:Killed | Should -Be 1
        $script:Disposed | Should -Be 1
        $script:Waits | Should -Be 2
    }
    It 'a falling disk reserve terminates the same child on the next poll' {
        $script:Finished=$false
        Mock -ModuleName QTrade.BuildSpace Assert-QtBuildSpace {
            $script:Probes++
            if($script:Probes -gt 1){throw 'BUILD_DISK_BUDGET_EXCEEDED'}
        }
        { Invoke-QtBuildNative -FilePath 'never.exe' -BudgetPath $TestDrive } | Should -Throw '*BUILD_DISK_BUDGET_EXCEEDED*'
        $script:Started | Should -Be 1
        $script:Killed | Should -Be 1
        $script:Disposed | Should -Be 1
    }
}
