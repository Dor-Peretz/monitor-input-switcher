# Starts everything this PC needs for the shared Stream Deck + monitor switcher.
# Registered in HKCU Run as "MonitorInputSwitcher".
$ErrorActionPreference = "SilentlyContinue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$vh = Join-Path $env:LOCALAPPDATA "VirtualHere\vhui64.exe"
$streamDeck = Join-Path $env:ProgramFiles "Elgato\StreamDeck\StreamDeck.exe"
$pythonw = Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\pythonw.exe"
if (-not (Test-Path $pythonw)) {
  $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}

# VirtualHere client first (Auto-Use stays off via %APPDATA%\vhui.ini).
if ((Test-Path $vh) -and -not (Get-Process vhui64 -ErrorAction SilentlyContinue)) {
  Start-Process $vh
  Start-Sleep -Seconds 2
}

# Elgato Stream Deck software.
if ((Test-Path $streamDeck) -and -not (Get-Process StreamDeck -ErrorAction SilentlyContinue)) {
  Start-Process $streamDeck
}

# Tray app: hotkeys + streamdeck_hub listener on :8765.
$tray = Join-Path $repo "tray_app.py"
if ((Test-Path $pythonw) -and (Test-Path $tray)) {
  $already = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'tray_app\.py' }
  if (-not $already) {
    Start-Process -FilePath $pythonw -ArgumentList "`"$tray`"" -WorkingDirectory $repo -WindowStyle Hidden
  }
}
