# TestDrive fixtures and fake OS APIs only. Real live installations are never inspected or changed.
#requires -Version 5.1
BeforeAll {
    $modules = $env:QT_REMAINING_TEST_MODULES
    if (-not $modules) { $modules = Join-Path $PSScriptRoot '..\engine\modules' }
    Import-Module (Join-Path $modules 'QTrade.Stage.psm1') -DisableNameChecking -ErrorAction Stop
    function New-StageAsset {
        param([string] $Relative, [string] $Content)
        $path = Join-Path $script:StageRoot $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
        [IO.File]::WriteAllText($path, $Content)
        [pscustomobject]@{ path = $Relative.Replace('\','/'); size = (Get-Item -LiteralPath $path).Length;
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    function Write-OwnerState {
        [IO.File]::WriteAllText((Join-Path $script:LiveRoot 'install\install_state.json'),
            ($script:OwnerState | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
    }
}

Describe 'Clean reinstall validates payload, consent and precise ownership before effects' {
    BeforeEach {
        $base = Join-Path $TestDrive ('install fixture ' + [guid]::NewGuid().ToString('N'))
        $script:StageRoot = Join-Path $base 'stage'
        $script:LiveRoot = Join-Path $base 'live'
        $script:OtherRoot = Join-Path $base 'customer-wechat'
        foreach ($directory in @($script:StageRoot, (Join-Path $script:LiveRoot 'install'), $script:OtherRoot)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        [IO.File]::WriteAllText((Join-Path $script:OtherRoot 'keep.db'), 'customer-data')
        [IO.File]::WriteAllText((Join-Path $script:LiveRoot 'old-state.db'), 'old-install-only')
        $script:Sid = 'S-1-5-21-1111-2222-3333-1001'
        $script:OwnerState = [pscustomobject]@{ state = 'DONE'; package_version = '1.0.1';
            env = [pscustomobject]@{ install_user_sid = $script:Sid };
            distro = [pscustomobject]@{ name = 'qtrade'; dir = (Join-Path $script:LiveRoot 'wsl\distro'); imported_at = '2026-10-01T00:00:00Z' } }
        Write-OwnerState
        $kernel = New-StageAsset 'kernel\bzImage-6.6' 'fixture-kernel-never-booted'
        $kernel | Add-Member -NotePropertyName coredump_l2 -NotePropertyValue 'D'
        $files = @(
            $kernel
            New-StageAsset 'console\resources\app.asar' 'new-console'
            New-StageAsset 'winagent\app\qtrade-winagent-svc.exe' 'new-service-never-executed'
            New-StageAsset 'wsl\rootfs.tar' 'new-rootfs-never-imported'
        )
        New-Item -ItemType Directory -Path (Join-Path $script:StageRoot 'install') -Force | Out-Null
        $script:Manifest = [pscustomobject]@{ package_version = '1.0.2'; files = $files; coredump_l2 = 'D' }
        [IO.File]::WriteAllText((Join-Path $script:StageRoot 'install\manifest.json'),
            ($script:Manifest | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
        $script:Distro = [pscustomobject]@{ name = 'qtrade'; base_path = (Join-Path $script:LiveRoot 'wsl\distro') }
        $script:Service = [pscustomobject]@{ PathName = ('"' + (Join-Path $script:LiveRoot 'winagent\app\qtrade-winagent-svc.exe') + '" --service --root "' + (Join-Path $script:LiveRoot 'winagent') + '" --install-user-sid ' + $script:Sid); StartName = 'LocalSystem' }
        $script:Task = [pscustomobject]@{ present = $true; execute = (Join-Path $script:LiveRoot 'winagent\app\qtrade-winagent-user.exe'); arguments = ''; user_id = $script:Sid; multiple_actions = $false }
        $script:ArpPresent = $false
        $script:ArpLocation = $script:LiveRoot
        $script:ArpCommand = ('"' + (Join-Path $script:LiveRoot 'unins000.exe') + '"')
        $script:ArpKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{9B2F1C64-2F1E-4E1A-9A1B-0C7A5E3D1B02}_is1'
        $script:ServicePresent = $true
        $script:Effects = New-Object Collections.Generic.List[string]
        Mock -ModuleName QTrade.Stage Set-QtInstallRootAcl { [pscustomobject]@{ Ok = $true; Changed = $false; Problems = @() } }
        Mock -ModuleName QTrade.Stage Get-QtServiceStatus { 'Stopped' }
        Mock -ModuleName QTrade.Stage Get-QtEnvironmentPath { Join-Path $TestDrive 'unused-user-profile' }
        Mock -ModuleName QTrade.Stage Test-QtRegistryKey { $script:ArpPresent }
        Mock -ModuleName QTrade.Stage Get-QtRegistryValue {
            if ($Path -ne $script:ArpKey) { throw 'Unexpected registry query.' }
            if ($Name -eq 'InstallLocation') { return $script:ArpLocation }
            if ($Name -eq 'UninstallString') { return $script:ArpCommand }
            throw 'Unexpected registry value.'
        }
        Mock -ModuleName QTrade.Stage Get-QtCurrentUserSid { $script:Sid }
        Mock -ModuleName QTrade.Stage Get-QtRegisteredDistro { return , @($script:Distro | Where-Object { $null -ne $_ }) }
        Mock -ModuleName QTrade.Stage Get-QtCim {
            if ($ClassName -eq 'Win32_Service') { if ($script:ServicePresent) { return $script:Service }; return $null }
            if ($ClassName -eq 'Win32_Process') { return @() }
            throw 'Unexpected CIM query.'
        }
        Mock -ModuleName QTrade.Stage Invoke-QtSc {
            $action = $ScArgs[0]
            if ($action -in @('stop','delete')) { $script:Effects.Add(('sc:' + $action)) }
            if ($action -eq 'delete') { $script:ServicePresent = $false; return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false } }
            [pscustomobject]@{ ExitCode = $(if ($script:ServicePresent) { 0 } else { 1060 }); StdOut = 'STATE : 1 STOPPED'; StdErr = ''; TimedOut = $false }
        }
        Mock -ModuleName QTrade.Stage Invoke-QtProcess {
            if ([IO.Path]::GetFileName($FilePath) -eq 'powershell.exe') {
                return [pscustomobject]@{ ExitCode = 0; StdOut = ($script:Task | ConvertTo-Json -Compress); StdErr = ''; TimedOut = $false }
            }
            if ([IO.Path]::GetFileName($FilePath) -eq 'schtasks.exe') {
                $script:Effects.Add(('task:' + ($ArgumentList -join ' ')))
                if ($ArgumentList -contains '/Delete') { $script:Task.present = $false }
                return [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false }
            }
            throw 'Unexpected native process blocked.'
        }
        Mock -ModuleName QTrade.Stage Invoke-QtWsl {
            $script:Effects.Add(('wsl:' + ($WslArgs -join ' ')))
            if ($WslArgs -contains '--unregister') { $script:Distro = $null }
            [pscustomobject]@{ ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false }
        }
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            if ($Path -eq $script:ArpKey) { $script:Effects.Add('remove-owned-arp'); $script:ArpPresent = $false; return }
            if ($Path -ne $script:LiveRoot -and (Split-Path -Parent $Path) -ne $script:LiveRoot) { throw 'Removal escaped the verified fixture live root.' }
            if ($Path -eq $script:LiveRoot) {
                $Recurse | Should -BeFalse
                @(Get-ChildItem -LiteralPath $Path -Force).Count | Should -Be 0
                $script:Effects.Add('remove-live')
            }
            # Preserve exact fixture children outside the live root.
            Move-Item -LiteralPath $Path -Destination (Join-Path $script:StageRoot ('removed-' + [guid]::NewGuid().ToString('N')))
        }
    }
    AfterEach {
        [IO.File]::ReadAllText((Join-Path $script:OtherRoot 'keep.db')) | Should -Be 'customer-data'
    }

    It 'requires explicit clean-reinstall consent for an existing live root' {
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $false
        $r.Ok | Should -BeFalse
        $r.RequiresConsent | Should -BeTrue -Because ($r | ConvertTo-Json -Depth 4 -Compress)
        $script:Effects.Count | Should -Be 0
    }

    It 'does not touch live state when the staged payload hash fails' {
        [IO.File]::WriteAllText((Join-Path $script:StageRoot 'console\resources\app.asar'), 'corrupt')
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $script:Effects.Count | Should -Be 0
        Test-Path -LiteralPath (Join-Path $script:LiveRoot 'old-state.db') | Should -BeTrue
    }

    It 'refuses stage and live path overlap' {
        $r = Invoke-QtCleanReinstall -StageRoot $script:LiveRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $script:Effects.Count | Should -Be 0
    }

    It 'rejects foreign ownership from <Boundary> before any side effect' -ForEach @(
        @{ Boundary = 'sid' }, @{ Boundary = 'distro' }, @{ Boundary = 'service' }, @{ Boundary = 'task' }, @{ Boundary = 'task-actions' }
    ) {
        switch ($Boundary) {
            'sid' { $script:OwnerState.env.install_user_sid = 'S-1-5-21-1111-2222-3333-2002'; Write-OwnerState }
            'distro' { $script:Distro.base_path = $script:OtherRoot }
            'service' { $script:Service.PathName = ('"' + (Join-Path $script:OtherRoot 'foreign.exe') + '"') }
            'task' { $script:Task.execute = Join-Path $script:OtherRoot 'foreign.exe' }
            'task-actions' { $script:Task.multiple_actions = $true }
        }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $script:Effects.Count | Should -Be 0
    }

    It 'cleans only verified QTrade objects and publishes the already verified new payload' {
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeTrue -Because ($r | ConvertTo-Json -Depth 4 -Compress)
        @($script:Effects | Where-Object { $_ -eq 'wsl:--unregister qtrade' }).Count | Should -Be 1
        @($script:Effects | Where-Object { $_ -eq 'remove-live' }).Count | Should -Be 1
        [IO.File]::ReadAllText((Join-Path $script:LiveRoot 'console\resources\app.asar')) | Should -Be 'new-console'
        Test-Path -LiteralPath (Join-Path $script:LiveRoot 'old-state.db') | Should -BeFalse
        $r.State.state | Should -Be 'PRECHECK'
        $r.State.package_version | Should -Be '1.0.2'
        $r.State.env.install_user_sid | Should -Be $script:Sid
        $r.State.package_manifest_sha256 | Should -Be ((Get-FileHash -LiteralPath (Join-Path $script:StageRoot 'install\manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant())
    }

    It 'resumes the same prepared package after reboot without repeating cleanup' {
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Ok | Should -BeTrue -Because $first.Reason
        $statePath = Join-Path $script:LiveRoot 'install\install_state.json'
        $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $saved.state = 'REBOOT_PENDING'
        [IO.File]::WriteAllText($statePath, ($saved | ConvertTo-Json -Depth 16))
        $script:Effects.Clear()
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $false
        $r.Ok | Should -BeTrue -Because $r.Reason
        $r.Resumed | Should -BeTrue
        $r.State.state | Should -Be 'REBOOT_PENDING'
        $script:Effects.Count | Should -Be 0
    }

    It 'rejects prepared journal identity drift for <Changed>' -ForEach @(
        @{ Changed = 'manifest' }, @{ Changed = 'sid' }
    ) {
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Ok | Should -BeTrue -Because $first.Reason
        if ($Changed -eq 'manifest') {
            [IO.File]::AppendAllText((Join-Path $script:StageRoot 'install\manifest.json'), ' ')
        } else { $script:Sid = 'S-1-5-21-1111-2222-3333-2002' }
        $script:Effects.Clear()
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'STAGE_IDENTITY_MISMATCH'
        $script:Effects.Count | Should -Be 0
    }

    It 'refuses a reparse descendant without traversing its target or mutating live state' {
        Mock -ModuleName QTrade.Stage Get-ChildItem {
            [pscustomobject]@{ Attributes = [IO.FileAttributes]::ReparsePoint; PSIsContainer = $true; FullName = $script:OtherRoot }
        } -ParameterFilter { $LiteralPath -eq $script:LiveRoot }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'REPARSE_POINT_REFUSED'
        $script:Effects.Count | Should -Be 0
        Should -Invoke -ModuleName QTrade.Stage Get-ChildItem -Times 0 -Exactly -ParameterFilter { $LiteralPath -eq $script:OtherRoot }
    }

    It 'does not claim DONE when removal is blocked and preserves the old live root' {
        Mock -ModuleName QTrade.Stage Remove-QtItem { throw 'Fixture access denied.' }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $r.State | Should -BeNullOrEmpty
        Test-Path -LiteralPath (Join-Path $script:LiveRoot 'old-state.db') | Should -BeTrue
        $saved = Get-Content -LiteralPath (Join-Path $script:LiveRoot 'install\install_state.json') -Raw | ConvertFrom-Json
        $saved.package_version | Should -Be '1.0.1'
    }

    It 'removes only the precisely owned obsolete ARP key' {
        $script:ArpPresent = $true
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeTrue -Because $r.Reason
        @($script:Effects | Where-Object { $_ -eq 'remove-owned-arp' }).Count | Should -Be 1
        Should -Invoke -ModuleName QTrade.Stage Remove-QtItem -Times 1 -Exactly -ParameterFilter { $Path -eq $script:ArpKey }
    }

    It 'refuses a foreign ARP <Field> without invoking any removal' -ForEach @(
        @{ Field = 'location' }, @{ Field = 'command' }
    ) {
        $script:ArpPresent = $true
        if ($Field -eq 'location') { $script:ArpLocation = $script:OtherRoot }
        else { $script:ArpCommand = 'C:\Foreign\unins000.exe' }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'UNINSTALL_ENTRY_OWNERSHIP_UNKNOWN'
        $script:Effects.Count | Should -Be 0
    }
    It 'revalidates prepared resume when a foreign <Boundary> appeared' -ForEach @(
        @{ Boundary = 'kcheck' }, @{ Boundary = 'service' }, @{ Boundary = 'task' }
    ) {
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Ok | Should -BeTrue -Because $first.Reason
        if ($Boundary -eq 'kcheck') { $script:Distro = [pscustomobject]@{ name = 'qtrade-kcheck'; base_path = $script:OtherRoot } }
        if ($Boundary -eq 'service') { $script:ServicePresent = $true; $script:Service.PathName = 'C:\Foreign\svc.exe' }
        if ($Boundary -eq 'task') { $script:Task.present = $true; $script:Task.execute = 'C:\Foreign\user.exe' }
        $script:Effects.Clear()
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2'
        $r.Ok | Should -BeFalse
        $r.Resumed | Should -BeFalse
        $script:Effects.Count | Should -Be 0
    }

    It 'resumes approved cleaning after the install state was removed first' {
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            if ($Path -ne $script:LiveRoot -and (Split-Path -Parent $Path) -ne $script:LiveRoot) { throw 'Unexpected removal.' }
            Move-Item -LiteralPath (Join-Path $script:LiveRoot 'install\install_state.json') -Destination (Join-Path $script:StageRoot 'removed-state.fixture')
            throw 'Fixture sharing violation after state deletion.'
        }
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Ok | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $script:LiveRoot 'install\install_state.json') | Should -BeFalse
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            if ($Path -ne $script:LiveRoot -and (Split-Path -Parent $Path) -ne $script:LiveRoot) { throw 'Unexpected removal.' }
            if ($Path -eq $script:LiveRoot) { $Recurse | Should -BeFalse; @(Get-ChildItem -LiteralPath $Path -Force).Count | Should -Be 0 }
            Move-Item -LiteralPath $Path -Destination (Join-Path $script:StageRoot ('removed-' + [guid]::NewGuid().ToString('N')))
        }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2'
        $r.Ok | Should -BeTrue -Because $r.Reason
        $r.State.state | Should -Be 'PRECHECK'
        Test-Path -LiteralPath (Join-Path $script:LiveRoot 'old-state.db') | Should -BeFalse
    }

    It 'refuses changed <Boundary> during interrupted cleaning before any new effect' -ForEach @(
        @{ Boundary = 'marker' }, @{ Boundary = 'root-identity' }, @{ Boundary = 'service' },
        @{ Boundary = 'kcheck' }, @{ Boundary = 'sid' }
    ) {
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            if ($Path -ne $script:LiveRoot -and (Split-Path -Parent $Path) -ne $script:LiveRoot) { throw 'Unexpected removal.' }
            Move-Item -LiteralPath (Join-Path $script:LiveRoot 'install\install_state.json') -Destination (Join-Path $script:StageRoot 'removed-state.fixture')
            throw 'Fixture interrupted deletion.'
        }
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Ok | Should -BeFalse
        switch ($Boundary) {
            'marker' { [IO.File]::WriteAllText((Join-Path $script:LiveRoot '.qtrade-clean-owner.json'), '{}') }
            'root-identity' { [IO.Directory]::SetCreationTimeUtc($script:LiveRoot, [datetime]::UtcNow.AddDays(-1)) }
            'service' { $script:ServicePresent = $true; $script:Service.PathName = 'C:\Foreign\svc.exe' }
            'kcheck' { $script:Distro = [pscustomobject]@{ name = 'qtrade-kcheck'; base_path = $script:OtherRoot } }
            'sid' { $script:Sid = 'S-1-5-21-1111-2222-3333-2002' }
        }
        $script:Effects.Clear()
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $r.Ok | Should -BeFalse
        $script:Effects.Count | Should -Be 0
        Should -Invoke -ModuleName QTrade.Stage Remove-QtItem -Times 1 -Exactly
    }

    It 'resumes after final empty-root removal failed without requiring the deleted marker' {
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            if ($Path -eq $script:LiveRoot) {
                $Recurse | Should -BeFalse
                @(Get-ChildItem -LiteralPath $Path -Force).Count | Should -Be 0
                throw 'Fixture empty-root sharing violation.'
            }
            if ((Split-Path -Parent $Path) -ne $script:LiveRoot) { throw 'Unexpected target.' }
            Move-Item -LiteralPath $Path -Destination (Join-Path $script:StageRoot ('removed-' + [guid]::NewGuid().ToString('N')))
        }
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Ok | Should -BeFalse
        $first.Reason | Should -Be 'Fixture empty-root sharing violation.'
        @(Get-ChildItem -LiteralPath $script:LiveRoot -Force).Count | Should -Be 0
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            $Path | Should -Be $script:LiveRoot
            $Recurse | Should -BeFalse
            @(Get-ChildItem -LiteralPath $Path -Force).Count | Should -Be 0
            Move-Item -LiteralPath $Path -Destination ($Path + '.removed')
        }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2'
        $r.Ok | Should -BeTrue -Because $r.Reason
        $r.State.state | Should -Be 'PRECHECK'
    }

    It 'refuses a new object after committing the empty-root boundary' {
        Mock -ModuleName QTrade.Stage Remove-QtItem {
            param($Path, [switch]$Recurse)
            if ($Path -eq $script:LiveRoot) { throw 'Fixture final boundary.' }
            if ((Split-Path -Parent $Path) -ne $script:LiveRoot) { throw 'Unexpected target.' }
            Move-Item -LiteralPath $Path -Destination (Join-Path $script:StageRoot ('removed-' + [guid]::NewGuid().ToString('N')))
        }
        $first = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2' -AcceptCleanReinstall $true
        $first.Reason | Should -Be 'Fixture final boundary.'
        [IO.File]::WriteAllText((Join-Path $script:LiveRoot 'new-owner.txt'), 'foreign')
        Mock -ModuleName QTrade.Stage Remove-QtItem { throw 'Forbidden after empty-root drift.' }
        $r = Invoke-QtCleanReinstall -StageRoot $script:StageRoot -LiveRoot $script:LiveRoot -PackageVersion '1.0.2'
        $r.Ok | Should -BeFalse
        $r.Reason | Should -Be 'CLEAN_ROOT_NOT_EMPTY'
        [IO.File]::ReadAllText((Join-Path $script:LiveRoot 'new-owner.txt')) | Should -Be 'foreign'
    }

}
