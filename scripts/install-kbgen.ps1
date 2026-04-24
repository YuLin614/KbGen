param(
    [Parameter(Mandatory = $true)]
    [string]$WheelPath,

    [string]$PythonExe = "py"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $WheelPath)) {
    throw "Wheel file not found: $WheelPath"
}

Write-Host "Installing/Updating pipx (user scope)..."
& $PythonExe -m pip install --user --upgrade pipx

Write-Host "Ensuring pipx path is configured..."
& $PythonExe -m pipx ensurepath | Out-Null

Write-Host "Installing kbgen with pipx from: $WheelPath"
& $PythonExe -m pipx install --force "$WheelPath"

$pipxBinDir = Join-Path $env:USERPROFILE ".local\bin"
$kbgenExe = Join-Path $pipxBinDir "kbgen.exe"

if ((Get-Command kbgen -ErrorAction SilentlyContinue) -eq $null -and (Test-Path $pipxBinDir)) {
    $env:Path += ";$pipxBinDir"
}

$cmd = Get-Command kbgen -ErrorAction SilentlyContinue
if ($null -ne $cmd) {
    Write-Host "kbgen is available on PATH."
    kbgen --help | Out-Null
    Write-Host "Try: kbgen --help"
} else {
    Write-Warning "kbgen is installed via pipx but not on PATH in this shell."
    if (Test-Path $kbgenExe) {
        Write-Host "Run directly now:"
        Write-Host "  $kbgenExe --help"
    }
    Write-Host ""
    Write-Host "Open a new PowerShell window after running pipx ensurepath, or run this temporary fix:"
    Write-Host "  `$env:Path += ';$pipxBinDir'"
}

Write-Host "kbgen installed successfully."
