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

## Stream Deck

`streamdeck/` holds an Elgato Stream Deck plugin with two actions:

| Action | What a press does |
|--------|-------------------|
| **PC Switch** | Moves every monitor in the PC-switch group to the other PC |
| **Mac / PC** | Hands this Stream Deck to the other computer. Does not change monitors |
| **Toggle Monitor Input** | Toggles one monitor between its two configured inputs |

Keys read the same `config.json` and switching state as the hotkeys and the
menu bar app, so all three stay in agreement — the key title shows the input
(or PC) that is currently active, even when you switched with a hotkey.

```bash
./streamdeck/install.sh
```

The script symlinks the plugin into Stream Deck's plugin folder and restarts
the app. Then drag an action onto a key; **Toggle Monitor Input** has a
dropdown to pick which monitor it controls. The list includes per-monitor
hotkey rows and every monitor included on the PC Switch tab, so a second
display that has no hotkey of its own can still have a toggle key.

Stream Deck runs the plugin as a plain Python process (`launch.sh` finds a
`python3` the same way `ddc_monitor.py` finds `m1ddc`). It needs no extra
packages, and opens no network port — presses call the DDC helpers in-process.

Errors go to `streamdeck.log` in this folder.

### Mac / PC key

Windows setup, in order, is
[docs/windows-streamdeck-handoff.md](../docs/windows-streamdeck-handoff.md).
The matching **HA** folder and monitor keys are in
[docs/windows-profile-handoff.md](../docs/windows-profile-handoff.md).

This key is separate from monitor switching. **PC Switch** and the hotkeys
only change monitor inputs. **Mac / PC** hands the Stream Deck itself to the
other computer, so the Elgato app there (Zoom included) takes over.

The deck stays plugged into the always-on VirtualHere server on **proxmox2**
(`192.168.68.70`, TCP `7575`, Elgato `0fd9:0080` only). This Mac is
`local_pc: "a"` (label **Mac**). The Windows PC is `local_pc: "b"` (label
**PC**). Each side listens on port `8765`.

In `config.json`:

```json
"streamdeck_hub": {
  "enabled": true,
  "local_pc": "a",
  "listen_port": 8765,
  "peer_url": "http://WINDOWS-LAN-IP:8765",
  "token": "",
  "client_path": "/Applications/VirtualHereUniversal.app/Contents/MacOS/VirtualHereUniversal",
  "device_match": "Stream Deck"
}
```

On Windows set `local_pc` to `"b"` and `peer_url` to this Mac’s `http://…:8765`.
Use the same `token` on both sides when you set one. Leave VirtualHere running
on both computers. The Windows client is `vhui64.exe`.

If one computer turns off, the one that is still on takes the deck. If both
are off, the one that wakes up takes it. While neither computer is using the
deck, proxmox2 shows **Turn on Mac**, **Turn on PC**, and the **HA** folder
on the keys. The same three controls are on the **Off** page in Elgato, one
press of Next from the monitor page. Wake-on-LAN has to be enabled on both.

Drag **Mac / PC** onto a key on both computers. Pressing it runs this plugin
(`launch.sh` on the Mac, `launch.exe` on Windows) and does not touch monitors.

What the Windows machine still needs:

1. VirtualHere client installed and running (`vhui64.exe`). It should see
   `proxmox2` and the Stream Deck. Do not turn on Auto-Use; this app claims
   and releases the deck itself.
2. This repo on the PC. Symlink the plugin folder into Stream Deck (do not
   copy it — `plugin.py` walks up to the repo root for the Windows DDC code):

```powershell
New-Item -ItemType SymbolicLink `
  -Path "$env:APPDATA\Elgato\StreamDeck\Plugins\com.dorperetz.monitorswitcher.sdPlugin" `
  -Target "C:\path\to\monitor-input-switcher\macos\streamdeck\com.dorperetz.monitorswitcher.sdPlugin"
```

3. In the Windows `config.json`, enable the hub. `local_pc` is `"b"`.
   `peer_url` is this Mac on port `8765`. Put the PC’s LAN address into the
   Mac’s `peer_url` the same way.
4. Drag **Mac / PC** onto a key, then quit and reopen Stream Deck.

Manual check from this folder:

```bash
python3 streamdeck_hub.py status
python3 streamdeck_hub.py claim
python3 streamdeck_hub.py release
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
| `streamdeck/` | Elgato Stream Deck plugin |
| `streamdeck_hub.py` | Mac / PC key that hands the Stream Deck to the other computer |

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
