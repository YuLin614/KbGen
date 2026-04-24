param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "Installing build backend..."
& $PythonExe -m pip install --upgrade build

Write-Host "Building wheel and sdist..."
& $PythonExe -m build

$pyprojectPath = Join-Path $RepoRoot "pyproject.toml"
$versionLine = Get-Content $pyprojectPath | Where-Object { $_ -match '^version\s*=\s*"' } | Select-Object -First 1
if (-not $versionLine) {
    throw "Could not find version in pyproject.toml"
}
$version = ($versionLine -replace '^version\s*=\s*"', '') -replace '"\s*$', ''

$releaseDir = Join-Path $RepoRoot "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

$zipName = "kbgen-$version-windows.zip"
$zipPath = Join-Path $releaseDir $zipName
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

$tempDir = Join-Path $releaseDir "_tmp_kbgen_release"
if (Test-Path $tempDir) {
    Remove-Item $tempDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

Copy-Item -Path (Join-Path $RepoRoot "dist\*") -Destination $tempDir -Recurse -Force
Copy-Item -Path (Join-Path $RepoRoot "README.md") -Destination $tempDir -Force
Copy-Item -Path (Join-Path $RepoRoot "scripts\install-kbgen.ps1") -Destination $tempDir -Force

Compress-Archive -Path (Join-Path $tempDir "*") -DestinationPath $zipPath -Force
Remove-Item $tempDir -Recurse -Force

Write-Host "Release package ready: $zipPath"
