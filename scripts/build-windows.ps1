[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$DistPath = 'dist/1.1.0',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows application on Windows.' }

$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($Python)) {
    $LocalPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $LocalPython) { $LocalPython } else { 'python' }
}

Push-Location -LiteralPath $ProjectRoot
try {
    $DistributionRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $DistPath))
    if (-not $SkipTests) {
        & $Python -m pytest -q tests
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
    }

    & $Python scripts\prepare_tools.py
    if ($LASTEXITCODE -ne 0) { throw 'Native tool preparation failed.' }

    & $Python scripts\package_native_sources.py --output (Join-Path $DistributionRoot 'MetaMovie-Organiser-NativeToolSources.zip')
    if ($LASTEXITCODE -ne 0) { throw 'Native source archive creation failed.' }

    & $Python scripts\package_source.py --output (Join-Path $DistributionRoot 'MetaMovie-Organiser-Source.zip')
    if ($LASTEXITCODE -ne 0) { throw 'Source archive creation failed.' }

    & $Python -m PyInstaller --noconfirm --clean --distpath $DistributionRoot --workpath build\1.1.0 packaging\MetaMovie-Organiser.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    $OutputDirectory = Join-Path $DistributionRoot 'MetaMovie-Organiser'
    foreach ($Document in @('README.md', 'LICENSE', 'DISTRIBUTION-LICENCE.md', 'ATTRIBUTION.md', 'CHANGELOG.md')) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $Document) -Destination $OutputDirectory
    }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'docs') -Destination $OutputDirectory -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'docs\native-tool-sources.md') -Destination $OutputDirectory
    $SourceDirectory = Join-Path $OutputDirectory 'source'
    New-Item -ItemType Directory -Path $SourceDirectory -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $DistributionRoot 'MetaMovie-Organiser-Source.zip') -Destination $SourceDirectory
    Copy-Item -LiteralPath (Join-Path $DistributionRoot 'MetaMovie-Organiser-Source.zip.sha256') -Destination $SourceDirectory

    & $Python scripts\collect_licenses.py $OutputDirectory (Join-Path $DistributionRoot 'MetaMovie-Organiser-Source.zip')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency licence/source collection failed.' }

    $DependencyVersions = & $Python -c "import sys, importlib.metadata as m; print('Python ' + sys.version); print('\n'.join(p + '==' + m.version(p) for p in ['requests','blake3','xxhash','keyring','cryptography','Pillow','pymediainfo','mutagen','pyinstaller']))"
    if ($LASTEXITCODE -ne 0) { throw 'Could not record dependency versions.' }
    $DependencyVersions | Set-Content -LiteralPath (Join-Path $OutputDirectory 'DEPENDENCIES.txt') -Encoding utf8

    $ArchivePath = Join-Path $DistributionRoot 'MetaMovie-Organiser-Windows.zip'
    Compress-Archive -LiteralPath $OutputDirectory -DestinationPath $ArchivePath -Force
    $ArchiveHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText("$ArchivePath.sha256", "$ArchiveHash  $([IO.Path]::GetFileName($ArchivePath))`n", [Text.Encoding]::ASCII)
    Write-Host "Built: $OutputDirectory"
    Write-Host "Archive: $ArchivePath"
    Write-Host "Checksum: $ArchivePath.sha256"
}
finally { Pop-Location }
