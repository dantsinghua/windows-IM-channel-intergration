# Build-only disk admission and bounded native compression. Never installed.
#requires -Version 5.1
Set-StrictMode -Version Latest

function Get-QtBuildSpaceVerdict {
    param([long]$FreeBytes, [long]$RequiredBytes, [long]$ReserveBytes)
    if ($FreeBytes -lt 0 -or $RequiredBytes -lt 0 -or $ReserveBytes -lt 0) { throw 'BUILD_SPACE_INVALID' }
    return ($FreeBytes -ge $ReserveBytes -and ($FreeBytes - $ReserveBytes) -ge $RequiredBytes)
}

function Assert-QtBuildSpace {
    param([Parameter(Mandatory)][string]$Path, [long]$RequiredBytes = 0, [long]$ReserveBytes = 60GB)
    $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path))
    $drive = New-Object IO.DriveInfo($root)
    if (-not $drive.IsReady -or -not (Get-QtBuildSpaceVerdict -FreeBytes $drive.AvailableFreeSpace -RequiredBytes $RequiredBytes -ReserveBytes $ReserveBytes)) {
        throw ('BUILD_DISK_BUDGET_EXCEEDED: drive={0}; required={1}; reserve={2}' -f $root, $RequiredBytes, $ReserveBytes)
    }
}

function Invoke-QtBuildNative {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments,
          [Parameter(Mandatory)][string]$BudgetPath, [long]$ReserveBytes = 60GB,
          [string]$WorkingDirectory = '', [int]$TimeoutSeconds = 3600)
    Assert-QtBuildSpace -Path $BudgetPath -ReserveBytes $ReserveBytes
    $quoted = @($Arguments | ForEach-Object {
        $value = [string]$_
        if ($value -and $value -notmatch '[\s"]') { $value }
        else {
            $value = [regex]::Replace($value, '(\\*)"', '$1$1\"')
            $value = [regex]::Replace($value, '(\\+)$', '$1$1')
            '"' + $value + '"'
        }
    })
    $process = New-Object Diagnostics.Process
    $process.StartInfo.FileName = $FilePath
    $process.StartInfo.Arguments = $quoted -join ' '
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    if ($WorkingDirectory) { $process.StartInfo.WorkingDirectory = $WorkingDirectory }
    $started = $false
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $started = $process.Start()
        if (-not $started) { throw 'BUILD_CHILD_NOT_STARTED' }
        while (-not $process.WaitForExit(1000)) {
            Assert-QtBuildSpace -Path $BudgetPath -ReserveBytes $ReserveBytes
            if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw 'BUILD_CHILD_TIMEOUT' }
        }
        return $process.ExitCode
    } finally {
        # Kill only the exact native process created by this function.
        if ($started -and -not $process.HasExited) {
            $process.Kill()
            [void]$process.WaitForExit(5000)
        }
        $process.Dispose()
        $watch.Stop()
    }
}

Export-ModuleMember -Function Get-QtBuildSpaceVerdict, Assert-QtBuildSpace, Invoke-QtBuildNative
