# Monitor Input Switcher — macOS

macOS port of the Windows monitor input switcher. Switch monitor inputs
(DisplayPort, USB-C, HDMI, etc.) using DDC/CI, with per-monitor toggle hotkeys
or a single "switch the whole desk between two PCs" hotkey, plus a settings UI
and a background menu bar app.

> This is a separate build living in `macos/`. The original Windows files in the
> repo root are untouched. The `config.json` format is identical, so a config
> can be shared between the two builds (only default hotkeys differ).

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.10+ **with Tk** (the settings UI uses Tkinter). Homebrew's Python
  does not bundle Tk — install it with `brew install python-tk` (or use the
  python.org installer). The menu bar app and CLI work without Tk.
- DDC/CI enabled in each external monitor's OSD menu
- A DDC/CI command-line tool:
  - **Apple Silicon:** [`m1ddc`](https://github.com/waydabber/m1ddc) — `brew install m1ddc`
  - **Intel:** [`ddcctl`](https://github.com/kfix/ddcctl) — `brew install ddcctl`

## Setup

```bash
# 1. DDC tool (Apple Silicon shown; use ddcctl on Intel)
brew install m1ddc

# 2. Python dependencies
cd macos
pip3 install -r requirements.txt

# 3. Tk for the settings UI (skip if you only use the menu bar app / CLI)
brew install python-tk
```

### Permissions

Global hotkeys use the native macOS **Carbon `RegisterEventHotKey`** API, which
does **not** require Accessibility or Input Monitoring permission — the app
works out of the box with no privacy prompts.

> Earlier builds used `pynput`, which crashes on modern macOS (Sequoia) because
> its keyboard listener calls a Text Input Source API from a background thread
> (macOS aborts the process with `SIGTRAP`). The Carbon backend avoids this
> entirely by running on the app's main run loop.

## Quick start

**Background app (recommended)** — menu bar icon (`⇄`), hotkeys always active:

```bash
python3 menu_bar_app.py     # or ./start_menu_bar.sh
```

- Click the menu bar icon → **Open settings…**, **Reload hotkeys**,
  **Run at login**, **Quit**.

**Settings UI only:**

```bash
python3 ui.py               # or ./run-ui.sh
```

**CLI:**

```bash
python3 monitor_switcher.py list                      # list monitors
python3 monitor_switcher.py toggle --monitor right    # toggle one monitor
python3 monitor_switcher.py run                       # hotkeys in terminal
```

## Configuration

Settings are stored in `config.json` and edited through the UI.

### Per monitor

Each row defines a hotkey that toggles one monitor between **Input A** and
**Input B**.

### PC switch (group)

Define two PC profiles (e.g. Laptop / Desktop). For each monitor, set which
input belongs to PC A and PC B. One hotkey switches every included monitor to
the other PC.

Input codes (standard DDC/CI VCP 0x60 values, same as `m1ddc`):

| Input        | Code (decimal) |
|--------------|----------------|
| DisplayPort 1| 15             |
| DisplayPort 2| 16             |
| HDMI 1       | 17             |
| HDMI 2       | 18             |
| USB-C        | 27             |

Codes vary by monitor brand — use trial and error if needed.

### Hotkey format

Hotkeys are stored as `+`-separated strings, e.g. `cmd+shift+1`,
`ctrl+alt+shift+r`. Recognised modifiers: `cmd` (⌘), `ctrl`, `alt`/`option`,
`shift`. In the UI, click a hotkey field and press the shortcut to capture it.

## Startup on login

Enable **Run at login** from the menu bar, or manage manually (installs a
LaunchAgent at `~/Library/LaunchAgents/com.monitorinputswitcher.tray.plist`):

```bash
python3 startup_manage.py enable
python3 startup_manage.py disable
python3 startup_manage.py status
```

## Project layout

| File | Purpose |
|------|---------|
| `menu_bar_app.py` | Background menu bar app + hotkeys (rumps) |
| `ui.py` | Configuration UI (Tkinter) |
| `carbon_hotkey.py` | Native global hotkeys (Carbon `RegisterEventHotKey`) |
| `hotkey_service.py` | Hotkey parsing/binding helpers (legacy pynput listener) |
| `ddc_monitor.py` | DDC/CI control via `m1ddc` / `ddcctl` |
| `config_store.py` | Config load/save |
| `startup_manage.py` | LaunchAgent (login item) management |
| `monitor_switcher.py` | CLI entry point |

## Important differences from the Windows build

- **DDC read is not available.** Neither `m1ddc` nor `ddcctl` can reliably read
  the *current* input over DDC. Toggling therefore tracks the last value it set
  in `.switcher_state.json` and flips from there. The first toggle after a fresh
  start goes to **Input B** by default.
- **`m1ddc` does not control the built-in HDMI port** on some M1/entry-M2 Macs,
  and does not support Intel Macs (use `ddcctl` or
  [BetterDisplay](https://betterdisplay.pro) there).
- **DDC only works over the active input cable.** To switch a monitor *back*
  from another input, the Mac must be the machine currently driving that input.

## Notes

- If two identical monitors report the same name, input targeting may be
  ambiguous; connect them so the names differ, or use the CLI with explicit
  display numbers from `monitor_switcher.py list`.
