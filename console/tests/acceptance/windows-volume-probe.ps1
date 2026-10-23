param(
    [Parameter(Mandatory = $true)]
    [string]$RequestBase64
)

$ErrorActionPreference = 'Stop'
$requestJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($RequestBase64))
$targets = ConvertFrom-Json -InputObject $requestJson
$results = @()

foreach ($target in $targets) {
    $inputPath = [string]$target.path
    $backingPath = $inputPath
    $distro = $null
    $basePath = $null
    $vhdFileName = $null
    $match = [regex]::Match($inputPath, '^\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)\\')
    if ($match.Success) {
        $distro = $match.Groups[1].Value
        $entries = @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' |
            Get-ItemProperty | Where-Object { $_.DistributionName -eq $distro })
        if ($entries.Count -ne 1) {
            throw 'Expected exactly one registry entry for the requested WSL distribution.'
        }
        $entry = $entries[0]
        $basePath = [string]$entry.BasePath
        $vhdFileName = [string]$entry.VhdFileName
        if ([string]::IsNullOrWhiteSpace($vhdFileName)) {
            $vhdFileName = 'ext4.vhdx'
        }
        $backingPath = Join-Path -Path $basePath -ChildPath $vhdFileName
        if (-not (Test-Path -LiteralPath $backingPath -PathType Leaf)) {
            throw 'The registered WSL virtual disk does not exist.'
        }
    }
    $volume = @(Get-Volume -FilePath $backingPath -ErrorAction Stop)
    if ($volume.Count -ne 1) {
        throw 'Expected exactly one backing volume per input path.'
    }
    $item = $volume[0]
    $results += [pscustomobject]@{
        role = [string]$target.role
        input_path = $inputPath
        backing_path = $backingPath
        distro = $distro
        registry_base_path = $basePath
        registry_vhd_filename = $vhdFileName
        drive_letter = [string]$item.DriveLetter
        volume_unique_id = [string]$item.UniqueId
        total_bytes = [Int64]$item.Size
        free_bytes = [Int64]$item.SizeRemaining
        captured_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
}

ConvertTo-Json -InputObject @($results) -Compress -Depth 5
