param(
    [string]$BuildPythonExe = "python",
    [string]$InstallPythonExe = "py",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "[1/3] Building release artifacts..."
& (Join-Path $PSScriptRoot "build-release.ps1") -PythonExe $BuildPythonExe

if ($SkipInstall) {
    Write-Host "[2/3] Install step skipped."
    Write-Host "Done."
    exit 0
}

$pyprojectPath = Join-Path $RepoRoot "pyproject.toml"
$versionLine = Get-Content $pyprojectPath | Where-Object { $_ -match '^version\s*=\s*"' } | Select-Object -First 1
if (-not $versionLine) {
    throw "Could not find version in pyproject.toml"
}
$version = ($versionLine -replace '^version\s*=\s*"', '') -replace '"\s*$', ''

$wheelPath = Join-Path $RepoRoot ("dist\kbgen-{0}-py3-none-any.whl" -f $version)
if (-not (Test-Path $wheelPath)) {
    throw "Expected wheel not found: $wheelPath"
}

Write-Host "[2/3] Installing latest wheel locally..."
& (Join-Path $PSScriptRoot "install-kbgen.ps1") -WheelPath $wheelPath -PythonExe $InstallPythonExe

Write-Host "[3/3] Verifying kbgen command..."
kbgen --help | Out-Null

Write-Host "Release and local installation completed successfully."
