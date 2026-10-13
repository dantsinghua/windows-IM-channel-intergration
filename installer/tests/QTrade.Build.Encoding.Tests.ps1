#requires -Version 5.1
# Execute only parsed read-only archive/manifest gates, never the build script.
BeforeAll {
    $source = $env:QT_BUILD_ENCODING_SOURCE
    if (-not $source) { $source = Join-Path $PSScriptRoot '..\build\build.ps1' }
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Build source does not parse.' }
    $text = [IO.File]::ReadAllText($source)
    $starts = @($ast.EndBlock.Statements | Where-Object {
        $_.Extent.Text -match '^\$archiveOutputEncoding =' -or $_.Extent.Text -match '^\$listed = &'
    })
    $end = @($ast.EndBlock.Statements | Where-Object { $_.Extent.Text -match '^if \(\$lost.Count -gt 0\)' })[0]
    if (-not $starts.Count -or -not $end) { throw 'Archive gate not found.' }
    $start = $starts[0].Extent.StartOffset
    $script:ArchiveGate = [scriptblock]::Create($text.Substring($start, $end.Extent.EndOffset - $start))
    $hash = @($ast.FindAll({ param($n)
        $n -is [Management.Automation.Language.IfStatementAst] -and
        $n.Extent.Text -match '^if \(\(Test-Path -LiteralPath \$mfPath\) -and'
    }, $true))[0]
    $script:HashGate = [scriptblock]::Create($hash.Extent.Text)
    function Write-Bad { param($Message) }
    function Invoke-FixtureSevenZip {
        if ($script:ListThrows) { throw 'Fixture listing failed.' }
        $global:LASTEXITCODE = $script:ListExit
        foreach ($name in $script:FixtureNames) { 'Path = ' + $name }
    }
    function New-EncodingManifest {
        param([string[]] $Names, [bool] $Bom = $false)
        $files = @($Names | ForEach-Object { [pscustomobject]@{ path = $_; sha256 = ('a' * 64) } })
        [IO.File]::WriteAllText((Join-Path $script:StageDir 'install\manifest.json'),
            (@{ files = $files } | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($Bom)))
    }
    function Invoke-FixtureHashGate {
        param([string] $mfPath, [string] $engInStage)
        $g6Bad = @()
        . $script:HashGate
        return , $g6Bad
    }
    $script:Label = 'QTrade ' + [char]0x63A7 + [char]0x5236 + [char]0x53F0 + '.exe'
}

Describe 'Archive manifest encoding gate retains exact-path rejection' {
    BeforeEach {
        $script:StageDir = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $script:StageDir 'install') -Force | Out-Null
        $script:ArchivePath = Join-Path $TestDrive 'not-executed.7z'
        $script:SevenZip = 'Invoke-FixtureSevenZip'
        $script:FixtureNames = @()
        $script:ListThrows = $false
        $script:ListExit = 0
    }
    It 'matches ASCII and Chinese file names with UTF8 BOM=<Bom>' -ForEach @(@{Bom=$false},@{Bom=$true}) {
        $names = @('ascii/file.txt', ('console/' + $script:Label))
        New-EncodingManifest -Names $names -Bom $Bom
        $script:FixtureNames = @($names | ForEach-Object { $_.Replace('/','\') })
        { . $script:ArchiveGate } | Should -Not -Throw
    }
    It 'keeps ASCII-only manifests working' {
        New-EncodingManifest -Names @('ascii/file.txt')
        $script:FixtureNames = @('ascii\file.txt')
        { . $script:ArchiveGate } | Should -Not -Throw
    }
    It 'rejects a real missing <Kind> file' -ForEach @(@{Kind='ASCII'},@{Kind='Chinese'}) {
        $name = if ($Kind -eq 'ASCII') { 'ascii/missing.txt' } else { 'console/' + $script:Label }
        New-EncodingManifest -Names @($name)
        $script:FixtureNames = @('ascii\other.txt')
        { . $script:ArchiveGate } | Should -Throw
    }
    It 'rejects a nonzero listing exit even if output names happen to match' {
        New-EncodingManifest -Names @('ascii/file.txt')
        $script:FixtureNames = @('ascii\file.txt'); $script:ListExit = 2
        { . $script:ArchiveGate } | Should -Throw
    }
    It 'restores the caller console encoding after listing succeeds or throws' -ForEach @(@{Throws=$false},@{Throws=$true}) {
        New-EncodingManifest -Names @('ascii/file.txt')
        $script:FixtureNames = @('ascii\file.txt'); $script:ListThrows = $Throws
        $before = [Console]::OutputEncoding
        try {
            [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(437)
            if ($Throws) { { . $script:ArchiveGate } | Should -Throw }
            else { { . $script:ArchiveGate } | Should -Not -Throw }
            [Console]::OutputEncoding.CodePage | Should -Be 437
        } finally { [Console]::OutputEncoding = $before }
    }
    It 'continues detecting a real signed-engine hash mismatch' {
        $engine = Join-Path $TestDrive 'fixture-engine.exe'
        [IO.File]::WriteAllText($engine, 'fixture never executed')
        New-EncodingManifest -Names @('install/engine/qtrade-setup-engine.exe', ('console/' + $script:Label))
        $errors = Invoke-FixtureHashGate -mfPath (Join-Path $script:StageDir 'install\manifest.json') -engInStage $engine
        $errors.Count | Should -Be 1
    }
    It 'accepts the exact signed-engine hash in an UTF8 manifest containing Chinese paths' {
        $engine = Join-Path $TestDrive 'fixture-engine.exe'
        [IO.File]::WriteAllText($engine, 'fixture never executed')
        New-EncodingManifest -Names @('install/engine/qtrade-setup-engine.exe', ('console/' + $script:Label))
        $mfPath = Join-Path $script:StageDir 'install\manifest.json'
        $manifest = [IO.File]::ReadAllText($mfPath) | ConvertFrom-Json
        $manifest.files[0].sha256 = (Get-FileHash -LiteralPath $engine -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText($mfPath, ($manifest | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
        (Invoke-FixtureHashGate -mfPath $mfPath -engInStage $engine).Count | Should -Be 0
    }
}

Describe 'Actual immutable release archive is readable across Windows code pages' {
    It 'matches the real manifest using console page <Page> and restores it' -ForEach @(@{Page=20127},@{Page=437},@{Page=936},@{Page=65001}) {
        if (-not $env:QT_BUILD_ENCODING_ARCHIVE -or -not $env:QT_BUILD_ENCODING_STAGE -or -not $env:QT_BUILD_ENCODING_7Z) {
            Set-ItResult -Skipped -Because 'Explicit read-only archive inputs are not supplied.'
            return
        }
        $StageDir = $env:QT_BUILD_ENCODING_STAGE
        $ArchivePath = $env:QT_BUILD_ENCODING_ARCHIVE
        $SevenZip = $env:QT_BUILD_ENCODING_7Z
        $before = [Console]::OutputEncoding
        try {
            [Console]::OutputEncoding = [Text.Encoding]::GetEncoding($Page)
            { . $script:ArchiveGate } | Should -Not -Throw
            [Console]::OutputEncoding.CodePage | Should -Be $Page
        } finally { [Console]::OutputEncoding = $before }
    }
}
