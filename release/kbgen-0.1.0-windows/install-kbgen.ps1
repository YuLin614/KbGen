param(
    [Parameter(Mandatory = $true)]
    [string]$WheelPath,

    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $WheelPath)) {
    throw "Wheel file not found: $WheelPath"
}

Write-Host "Installing kbgen from: $WheelPath"
& $PythonExe -m pip install --upgrade "$WheelPath"

Write-Host "Verifying kbgen command..."
kbgen --help | Out-Null

Write-Host "kbgen installed successfully."
