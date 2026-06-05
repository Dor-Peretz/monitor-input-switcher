# Monitor Input Switcher

Switch monitor inputs on Windows using DDC/CI. Configure per-monitor hotkeys, toggle between two inputs (DisplayPort, USB-C, HDMI, etc.), or switch a whole desk between two PCs with one shortcut.

## Requirements

- Windows 10/11
- Python 3.10+
- DDC/CI enabled in each monitor's OSD menu

## Setup

```powershell
pip install -r requirements.txt
```

## Quick start

**Background app (recommended)** — runs at login, hotkeys always active, system tray icon:

```powershell
py -3 launch_tray.py
```

Or double-click `start_tray.bat`.

- **Double-click** the tray icon to open settings
- **Right-click** the tray icon for menu options (reload, startup, exit)

**Settings UI only:**

```powershell
py -3 ui.py
```

Or double-click `run-ui.bat`.

**CLI:**

```powershell
py -3 monitor_switcher.py list          # list monitors
py -3 monitor_switcher.py toggle --monitor right
py -3 monitor_switcher.py run           # hotkeys in terminal (no tray)
```

## Configuration

Settings are stored in `config.json` and edited through the UI.

### Per monitor

Each row defines a hotkey that toggles one monitor between **Input A** and **Input B**.

### PC switch (group)

Define two PC profiles (e.g. Laptop / Desktop). For each monitor, set which input belongs to PC A and PC B. One hotkey switches every included monitor to the other PC.

Example input codes:

| Input        | Code (decimal) |
|-------------|----------------|
| DisplayPort | 15             |
| USB-C       | 27             |
| HDMI-1      | 17             |

Codes vary by monitor brand — use trial and error if needed.

## Startup on login

The tray app enables startup automatically on first run. To manage manually:

```powershell
py -3 startup_manage.py enable
py -3 startup_manage.py disable
py -3 startup_manage.py status
```

Or run `install_startup.bat`.

## Project layout

| File | Purpose |
|------|---------|
| `tray_app.py` | Background tray app + hotkeys |
| `ui.py` | Configuration UI |
| `hotkey_service.py` | Global hotkey listener |
| `ddc_monitor.py` | DDC/CI monitor control |
| `config_store.py` | Config load/save |
| `monitor_switcher.py` | CLI entry point |

## Notes

- DDC/CI commands only work over the **active** input cable. To switch back from USB-C, the app must run on the machine that is currently connected to the monitor.
- If the tray icon is hidden, click the `^` arrow near the clock and look for **Monitor Input Switcher**. You can pin it in **Settings → Personalization → Taskbar → Other system tray icons**.
