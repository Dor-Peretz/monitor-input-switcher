param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Clean) {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

python -m pip install -r requirements.txt -r requirements-build.txt
pyinstaller --noconfirm monitor_input_switcher.spec

$exe = Join-Path $root "dist\MonitorInputSwitcher.exe"
if (-not (Test-Path $exe)) {
    throw "Build failed: $exe not found"
}

$version = (python -c "from version import __version__; print(__version__)").Trim()
$zipName = "MonitorInputSwitcher-v$version-win64.zip"
$zipPath = Join-Path $root "dist\$zipName"
if (Test-Path $zipPath) { Remove-Item $zipPath }
Compress-Archive -Path $exe -DestinationPath $zipPath

Write-Host ""
Write-Host "Built: $exe"
Write-Host "Zip:   $zipPath"
