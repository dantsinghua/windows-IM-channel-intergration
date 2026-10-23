# Offline payload completeness gate. Reads only; never executes bundled binaries.
#requires -Version 5.1
function Get-QtPortableRequiredFiles {
    return @(
        'kernel/bzImage-6.6','wsl/kcheck-rootfs.tar','wsl/wsl.msi','wsl/rootfs.tar',
        'pkg/vcredist/VC_redist.x64.exe','pkg/adb/adb.exe','pkg/adb/AdbWinApi.dll','pkg/adb/AdbWinUsbApi.dll',
        'pkg/scrcpy/scrcpy.exe','pkg/chatlog/chatlog.exe','pkg/chatlog/ffmpeg.exe',
        'pkg/chatlog/wx_key1.dll','pkg/chatlog/wx_key2.dll',
        'winagent/app/qtrade-winagent-svc.exe','winagent/app/qtrade-winagent-user.exe',
        'winagent/app/_internal/python312.dll','winagent/app/_internal/base_library.zip',
        'winagent/app/_internal/VCRUNTIME140.dll','winagent/app/_internal/VCRUNTIME140_1.dll',
        'console/QTrade.exe','console/resources/app.asar','console/icudtl.dat','console/resources.pak',
        'install/engine/qtrade-setup-engine.exe','install/engine/launch-installed.ps1',
        'install/engine/modules/QTrade.Launch.psm1')
}
function Get-QtPayloadPeMachine {
    param([Parameter(Mandatory)][string]$Path)
    $stream=$null; $reader=$null
    try {
        $stream=[IO.File]::OpenRead($Path); $reader=New-Object IO.BinaryReader($stream)
        if($stream.Length -lt 128 -or $reader.ReadUInt16() -ne 0x5A4D){return -1}
        $stream.Position=60; $offset=$reader.ReadInt32()
        if($offset -lt 64 -or $offset -gt $stream.Length-24){return -1}
        $stream.Position=$offset
        if($reader.ReadUInt32() -ne 0x4550){return -1}
        return [int]$reader.ReadUInt16()
    } finally { if($null -ne $reader){$reader.Dispose()}elseif($null -ne $stream){$stream.Dispose()} }
}
function Get-QtPortablePayloadIssues {
    [CmdletBinding()] param([Parameter(Mandatory)][string]$Stage)
    $issues=New-Object 'System.Collections.Generic.List[string]'
    foreach($relative in Get-QtPortableRequiredFiles){
        $path=Join-Path $Stage $relative
        if(-not(Test-Path -LiteralPath $path -PathType Leaf)){ $issues.Add('PAYLOAD_DEPENDENCY_MISSING:'+$relative) }
        elseif((Get-Item -LiteralPath $path).Length -eq 0){$issues.Add('PAYLOAD_DEPENDENCY_EMPTY:'+$relative)}
    }
    foreach($relative in @('winagent/app/qtrade-winagent-svc.exe','winagent/app/qtrade-winagent-user.exe','winagent/app/_internal/python312.dll','console/QTrade.exe')){
        $path=Join-Path $Stage $relative
        if(Test-Path -LiteralPath $path -PathType Leaf){
            if((Get-QtPayloadPeMachine -Path $path) -ne 0x8664){$issues.Add('PAYLOAD_ARCHITECTURE_INVALID:'+$relative)}
        }
    }
    $root=[IO.Path]::GetFullPath($Stage).TrimEnd('\')+'\'
    foreach($file in Get-ChildItem -LiteralPath $Stage -Recurse -File -Force){
        $relative=$file.FullName.Substring($root.Length).Replace('\','/')
        if($relative -match '(^|/)(\.env[^/]*|[^/]+\.token|agent\.db|winagent\.db|development\.json|direct_url\.json)$' -or $relative -match '(^|/)(\.codex|\.tmp_forms|local_e2e)/'){
            $issues.Add('PAYLOAD_PRIVATE_STATE:'+$relative);continue
        }
        if($file.Extension -notin @('.toml','.conf','.ini','.pth','.ps1','.psm1','.cmd')){continue}
        if($file.Length -gt 1MB){$issues.Add('PAYLOAD_CONFIG_TOO_LARGE:'+$relative);continue}
        $text=[IO.File]::ReadAllText($file.FullName)
        if($text -match '(?i)([a-z]:[\\/]+Users[\\/]+|/home/[A-Za-z0-9_.-]+/|/mnt/[a-z]/Users/)'){
            $issues.Add('PAYLOAD_WORKSTATION_PATH:'+$relative)
        }
    }
    return ,$issues.ToArray()
}
