# Windows agent handoff — Stream Deck Mac / PC key

You are on the Windows PC. The Mac side is already coded and pushed. Finish this PC so the **Mac / PC** Stream Deck key can hand the deck here. Do not change monitor inputs. That is a different key.

Read this file and do the steps in order. Stop and report if a step fails.

## What this is

One Elgato Stream Deck MK.2 is plugged into **proxmox2** (`192.168.68.70`), not into the Mac or this PC. VirtualHere on that host shares only that deck (USB `0fd9:0080`, TCP **7575**). The server is already running. Do not reinstall it and do not run a VirtualHere **server** on Windows.

- **Mac / PC** key: moves the Stream Deck to the other computer. Elgato on that computer then runs (Zoom and the other plugins included).
- **PC Switch** key: monitor inputs only. Leave it alone.

This PC is computer **B**. The Mac is computer **A**.

| | Mac (already done) | This PC (you) |
|---|---|---|
| `local_pc` | `"a"` | `"b"` |
| Key label | Mac | PC |
| Peer | must be `http://<this-pc-ip>:8765` | `http://192.168.68.58:8765` |

The Mac was last seen at `192.168.68.58` (also try `192.168.68.55` if that address does not answer). Confirm with `ping` before writing it into config.

## 1. Get this repo

```powershell
git clone https://github.com/Dor-Peretz/monitor-input-switcher.git
cd monitor-input-switcher
git pull
```

If it is already cloned, `git pull` on `main`.

Python 3.10+ must be available as `py -3`. Elgato Stream Deck software must be installed.

## 2. VirtualHere client only

Download the Windows x64 client:

https://www.virtualhere.com/sites/default/files/usbclient/vhui64.exe

Put it at `C:\Program Files\VirtualHere\vhui64.exe` (or note the real path; you will set `client_path`).

Start it and leave it running. It should show server **proxmox2** and device **Stream Deck MK.2**.

Do **not** click Use. Do **not** turn on Auto-Use. This app claims and releases the deck. Auto-Use will steal it back.

## 3. This PC's LAN address

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.68.*' }
```

Call that address `PC_IP`. You will need it in the report at the end. The Mac cannot hand the deck here until its `peer_url` is `http://PC_IP:8765`.

## 4. Hub config

The listener reads `config.json` from the repo root when you run from source, and from `%APPDATA%\MonitorInputSwitcher\config.json` when you run the installed `MonitorInputSwitcher.exe`. Set **both** if both exist. Merge this object in; do not delete the monitor settings already there.

```json
"streamdeck_hub": {
  "enabled": true,
  "local_pc": "b",
  "listen_host": "0.0.0.0",
  "listen_port": 8765,
  "peer_url": "http://192.168.68.58:8765",
  "token": "",
  "client_path": "C:\\Program Files\\VirtualHere\\vhui64.exe",
  "device_match": "Stream Deck",
  "label_a": "Mac",
  "label_b": "PC"
}
```

Use the VirtualHere path from step 2 if it is different. Use the Mac address that actually answers `ping` and TCP `8765`.

Allow inbound TCP **8765** in Windows Firewall. The Mac calls `POST /claim` and `POST /release` on this port.

## 5. Stream Deck plugin

Symlink the plugin. Do not copy the folder. `plugin.py` walks up from that folder to the repo root for the Windows DDC code, and a copy breaks that.

```powershell
New-Item -ItemType SymbolicLink `
  -Path "$env:APPDATA\Elgato\StreamDeck\Plugins\com.dorperetz.monitorswitcher.sdPlugin" `
  -Target "$PWD\macos\streamdeck\com.dorperetz.monitorswitcher.sdPlugin"
```

`$PWD` must be the repo root. Quit and reopen Stream Deck. In the **Monitor Input Switcher** category, drag **Mac / PC** onto a key. Do not put it on top of **PC Switch**.

`launch.bat` runs `py -3`. If Stream Deck says the plugin failed, `py -3` is missing from PATH.

## 6. Start the listener before the deck arrives

The deck is still on the Mac, so this PC must already be listening or the key on the Mac has nobody to call.

From the repo root:

```powershell
py -3 tray_app.py
```

Or start the installed tray app. It listens on port 8765 when `streamdeck_hub.enabled` is true. The Stream Deck plugin listens too, once the app is open.

Check:

```powershell
py -3 streamdeck_hub.py status
netstat -an | findstr 8765
```

`status` must list **Stream Deck MK.2**. `in_use_by_me` should be false while the Mac still has it. `netstat` must show `0.0.0.0:8765` listening.

Do not run `streamdeck_hub.py claim` as a test. That takes the deck early.

## 7. Report back

Reply with:

- `PC_IP`
- VirtualHere path you configured
- Whether `status` shows the Stream Deck
- Whether port 8765 is listening
- Anything that failed

Someone on the Mac then sets `macos/config.json` → `streamdeck_hub.peer_url` to `http://PC_IP:8765` and reopens the Elgato app. Until that Mac field is set, the **Mac / PC** key stays on the Mac and shows an error instead of giving the deck away.

## Done when

- VirtualHere client is running, Auto-Use is off, Stream Deck MK.2 is visible under proxmox2.
- `streamdeck_hub` is enabled with `local_pc` `"b"` and `peer_url` pointing at the Mac.
- Port 8765 is listening.
- The **Mac / PC** key is on the Windows Stream Deck profile.
- You have reported `PC_IP`.
