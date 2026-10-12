# Windows agent handoff — same Home Assistant folder and monitor keys

You are on the Windows PC. The Mac Stream Deck profile already has a **HA** folder and the monitor keys. Rebuild those two pieces here so this computer's profile looks the same when the deck arrives.

Do this after [windows-streamdeck-handoff.md](windows-streamdeck-handoff.md). The deck, VirtualHere, and the **Mac / PC** key are already described there.

Do not press the light keys. Do not press **PC Switch** or the monitor keys. Writing the layout is the job. Pressing those keys changes the office lights or the monitor inputs.

Profiles do not travel with the USB device. The Mac profile stays on the Mac. This PC needs its own copy of these keys.

Stream Deck MK.2 is 5 columns by 3 rows. Positions below are **column, row**. `0,0` is the top-left key. `4,2` is the bottom-right key.

## 1. Home Assistant plugin

Install the Stream Deck store plugin **Home Assistant** (`de.perdoctus.streamdeck.homeassistant`). The Mac is on **3.8.4.66**. Use that version, or the closest 3.8 the store offers.

In the plugin's global settings (the gear in the action inspector):

- **Server URL:** the same Home Assistant URL already saved on the Mac plugin. Read it from the Mac Stream Deck app. Do not guess an address, and do not write the URL into this repo.
- **Access token:** create a new long-lived token in Home Assistant for this PC. Do not copy a token into this repo, into chat, or into a config file in git.

Every key below is the action **Entity (generic)**. Short press only. Leave long-press, tap, and rotation empty. Turn **Linked Title** off. Set the button title to the name in the table. Turn the service indicator on. Let the plugin supply the icon.

## 2. HA folder

On the first page, column 1 row 0, add a folder titled **HA**. Elgato's **Create Folder** action does this. Open that folder and fill it like this. The top-left key is **Parent Folder** (Elgato adds that when you create the folder).

| Key | Title | Short press | State shown on the key |
|---|---|---|---|
| 1,0 | Both Lights | `script.dor_s_table_light_and_lump_toggle` | `binary_sensor.dor_s_both_lights` |
| 2,0 | Lamp | `script.dor_s_lump_toggle` | `switch.power_plug_dor_s_lump` |
| 3,0 | Table Light | `script.dor_s_table_light_toggle` | `light.dor_s_table_light` |
| 1,1 | AC | `script.dor_study_room_cool_toggle` | `climate.khdr_bvdh` |
| 2,1 | Bright Red | `light.toggle` on `light.dor_s_table_light` | `light.dor_s_table_light` |
| 3,2 | Office On | `script.dor_s_study_room_turn_on` | `script.dor_s_study_room_turn_on` |
| 4,2 | Office Off | `script.dor_s_study_room_shut_off` | `script.dor_s_study_room_shut_off` |

Bright Red service data, exactly:

```json
{"rgbw_color":[255,0,0,0],"brightness_pct":100,"effect":"Solid"}
```

The folder, top to bottom:

```text
Parent     Both Lights   Lamp          Table Light
           AC            Bright Red
                                      Office On    Office Off
```

## 3. HA lights page

The Mac also has a later page with the same lights, in a different arrangement. Bright Red on this page calls a script. It does not use the `light.toggle` data from the folder. Copy this page as its own page. Do not merge it into the folder.

| Key | Title | Short press | State shown on the key |
|---|---|---|---|
| 0,1 | Both Lights | `script.dor_s_table_light_and_lump_toggle` | `light.dor_s_table_light` |
| 1,1 | Lamp | `script.dor_s_lump_toggle` | `switch.power_plug_dor_s_lump` |
| 2,1 | Table Light | `script.dor_s_table_light_toggle` | `light.dor_s_table_light` |
| 3,1 | AC | `script.dor_study_room_cool_toggle` | `climate.khdr_bvdh` |
| 4,1 | Office Off | `script.dor_s_study_room_shut_off` | `script.dor_s_study_room_shut_off` |
| 2,2 | Bright Red | `script.dor_s_table_light_bright_red_toggle` | `light.dor_s_table_light` |
| 0,2 | Previous Page | Elgato **Previous Page** | |
| 4,2 | Next Page | Elgato **Next Page** | |

```text
Both Lights   Lamp    Table Light    AC    Office Off
Previous                      Bright Red          Next
```

There is no **Office On** key on this page.

## 4. Monitor keys

These four keys sit on the Mac's second page. Put them on the second page here, in the same positions. The volume keys around them can stay as this PC's own media keys.

| Key | Action | Setting |
|---|---|---|
| 3,0 | **Mac / PC** | none |
| 4,0 | **PC Switch** | none. It reads `pc_switch` from config. |
| 3,1 | **Toggle Monitor Input** | target `DELL P2721Q` |
| 4,1 | **Toggle Monitor Input** | target `DELL U2720Q` |

The target string is the monitor name, typed exactly. The property inspector lists it after the app can see the monitors.

```text
                              Mac / PC      PC Switch
                              P2721Q        U2720Q
```

**PC Switch** is monitors only. It does not hand the deck to the other computer. **Mac / PC** does not change monitor inputs.

## 5. Monitor config

Merge this into `pc_switch`. Do not delete `streamdeck_hub` or the other settings.

Write it in both places when both exist:

- repo root `config.json`
- `%APPDATA%\MonitorInputSwitcher\config.json` (the installed app)

```json
"pc_switch": {
  "enabled": true,
  "hotkey": "ctrl+shift+1",
  "pc_a_name": "Mac (HDMI)",
  "pc_b_name": "Other (USB-C)",
  "monitors": [
    {
      "enabled": true,
      "monitor": "left",
      "device": "DELL P2721Q",
      "input_a": 15,
      "input_b": 17
    },
    {
      "enabled": true,
      "monitor": "right",
      "device": "DELL U2720Q",
      "input_a": 15,
      "input_b": 27
    }
  ]
}
```

`pc_a_name` is the label while the monitors are on the Mac side. `pc_b_name` is the label for the other side. The numbers are DDC input codes, not HDMI port numbers:

| Code | Input |
|---|---|
| 15 | DisplayPort-1 |
| 17 | HDMI-1 |
| 27 | USB-C |

Left monitor **DELL P2721Q**: Mac side is 15, other side is 17. Right monitor **DELL U2720Q**: Mac side is 15, other side is 27. Keep those codes even though the Mac-side label says HDMI.

The hotkey is `ctrl+shift+1` because this is Windows. The Mac hotkey uses the Command key and is not copied here.

Quit and reopen Stream Deck after the plugin and the config are in place.

## Done when

- The **HA** folder is on the first page at column 1 row 0, with the seven keys in the positions above.
- The lights page matches the second table, including the script used for Bright Red.
- **Mac / PC**, **PC Switch**, and both **Toggle Monitor Input** keys are on the second page at 3,0 / 4,0 / 3,1 / 4,1.
- `pc_switch` in the config that the app actually reads matches the JSON above.
- You did not press a light key or a monitor key.

Report which config file you wrote, and whether Home Assistant shows connected in the plugin settings. Do not include the access token in that report.
