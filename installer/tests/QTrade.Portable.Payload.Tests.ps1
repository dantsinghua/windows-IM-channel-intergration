# Synthetic payloads only; no binary in this file is executed.
BeforeAll { . (Join-Path $PSScriptRoot '../build/portable-payload.ps1') }
Describe 'Portable offline payload gate' {
    BeforeEach {
        $script:Stage=Join-Path $TestDrive 'payload'
        foreach($relative in Get-QtPortableRequiredFiles){
            $p=Join-Path $script:Stage $relative
            $null=New-Item -ItemType Directory -Path (Split-Path -Parent $p) -Force
            [IO.File]::WriteAllText($p,'synthetic')
        }
        foreach($relative in @('winagent/app/qtrade-winagent-svc.exe','winagent/app/qtrade-winagent-user.exe','winagent/app/_internal/python312.dll','console/QTrade.exe')){
            $bytes=New-Object byte[] 128; $bytes[0]=0x4d;$bytes[1]=0x5a;$bytes[60]=64;$bytes[64]=0x50;$bytes[65]=0x45;$bytes[68]=0x64;$bytes[69]=0x86
            [IO.File]::WriteAllBytes((Join-Path $script:Stage $relative),$bytes)
        }
    }
    It 'accepts the complete architecture-matched synthetic inventory' {
        $issues=Get-QtPortablePayloadIssues -Stage $script:Stage
        $issues.Count | Should -Be 0
    }
    It 'rejects a missing runtime dependency <Relative>' -ForEach @(
        @{Relative='winagent/app/_internal/python312.dll'},@{Relative='console/resources/app.asar'},
        @{Relative='pkg/vcredist/VC_redist.x64.exe'},@{Relative='install/engine/launch-installed.ps1'}) {
        Remove-Item -LiteralPath (Join-Path $script:Stage $Relative)
        (Get-QtPortablePayloadIssues -Stage $script:Stage) | Should -Contain ('PAYLOAD_DEPENDENCY_MISSING:'+$Relative)
    }
    It 'rejects x86 substituted for an x64 service' {
        $p=Join-Path $script:Stage 'winagent/app/qtrade-winagent-svc.exe';$bytes=[IO.File]::ReadAllBytes($p);$bytes[68]=0x4c;$bytes[69]=1;[IO.File]::WriteAllBytes($p,$bytes)
        (Get-QtPortablePayloadIssues -Stage $script:Stage) | Should -Contain 'PAYLOAD_ARCHITECTURE_INVALID:winagent/app/qtrade-winagent-svc.exe'
    }
    It 'rejects runtime state and installation-machine metadata <Name>' -ForEach @(
        @{Name='winagent.token'},@{Name='development.json'},@{Name='direct_url.json'}) {
        [IO.File]::WriteAllText((Join-Path $script:Stage $Name),'synthetic')
        (Get-QtPortablePayloadIssues -Stage $script:Stage) | Should -Contain ('PAYLOAD_PRIVATE_STATE:'+$Name)
    }
    It 'rejects another developer home in an executable startup script' {
        $p=Join-Path $script:Stage 'install/engine/launch-installed.ps1'
        [IO.File]::WriteAllText($p,'$script = "D:\Users\synthetic-developer\tool.exe"')
        (Get-QtPortablePayloadIssues -Stage $script:Stage) | Should -Contain 'PAYLOAD_WORKSTATION_PATH:install/engine/launch-installed.ps1'
    }
}
