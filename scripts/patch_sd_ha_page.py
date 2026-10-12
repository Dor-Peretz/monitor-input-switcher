"""Rebuild the Mac/PC Stream Deck page with HA keys on the left."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

PROFILE = Path.home() / (
    "AppData/Roaming/Elgato/StreamDeck/ProfilesV2/"
    "DB70F360-1125-438B-AD10-1FEE13501B01.sdProfile/"
    "Profiles/ORT6IBW4CL24WFV3RMT09C5EWKZ/manifest.json"
)


def state(title: str = "", size: int = 11) -> dict:
    out = {
        "FontFamily": "",
        "FontSize": size,
        "FontStyle": "",
        "FontUnderline": False,
        "OutlineThickness": 2,
        "ShowTitle": True,
        "TitleAlignment": "bottom",
        "TitleColor": "#ffffff",
    }
    if title:
        out["Title"] = title
    return out


def empty_service() -> dict:
    return {"serviceId": "", "entityId": "", "serviceData": ""}


def ha_settings(title: str, service_id: str, display_entity: str, service_data: str = "") -> dict:
    domain = display_entity.split(".", 1)[0]
    return {
        "version": 8,
        "display": {
            "domain": domain,
            "entityId": display_entity,
            "useCustomTitle": True,
            "buttonTitle": title,
            "enableServiceIndicator": True,
            "useCustomButtonLabels": False,
            "buttonLabels": "",
            "useStateImagesForOnOffStates": False,
            "iconSettings": "PREFER_HA",
            "iconLayout": "STANDARD",
            "labelFontSize": 48,
            "backgroundColor": "",
            "backgroundColorEnd": "",
        },
        "button": {
            "serviceShortPress": {
                "serviceId": service_id,
                "entityId": "",
                "serviceData": service_data,
            },
            "serviceLongPress": empty_service(),
            "serviceRotation": empty_service(),
            "serviceTap": empty_service(),
        },
        "controllerType": "Keypad",
        "rotationTickMultiplier": 1,
        "rotationTickBucketSizeMs": 300,
    }


def ha_action(title: str, service_id: str, display_entity: str, service_data: str = "") -> dict:
    return {
        "ActionID": str(uuid.uuid4()),
        "LinkedTitle": False,
        "Name": "Entity (generic)",
        "Plugin": {
            "Name": "Home Assistant",
            "UUID": "de.perdoctus.streamdeck.homeassistant",
            "Version": "3.8.4.66",
        },
        "Settings": ha_settings(title, service_id, display_entity, service_data),
        "State": 0,
        "States": [state(title, 9)],
        "UUID": "de.perdoctus.streamdeck.homeassistant.entity",
    }


def plugin_action(
    action_id: str,
    name: str,
    uuid_str: str,
    settings: dict,
    states: list,
    state_idx: int = 0,
) -> dict:
    return {
        "ActionID": action_id,
        "LinkedTitle": True,
        "Name": name,
        "Plugin": {
            "Name": "Monitor Input Switcher",
            "UUID": "com.dorperetz.monitorswitcher",
            "Version": "1.0.0",
        },
        "Settings": settings,
        "State": state_idx,
        "States": states,
        "UUID": uuid_str,
    }


def main() -> None:
    two_states = [state(), state()]
    acts = {
        "0,2": {
            "ActionID": "e5f89458-2468-4d0b-8b64-2ddfe985bdb6",
            "LinkedTitle": True,
            "Name": "Previous Page",
            "Plugin": {
                "Name": "Pages",
                "UUID": "com.elgato.streamdeck.page",
                "Version": "1.0",
            },
            "Settings": {},
            "State": 0,
            "States": [{}],
            "UUID": "com.elgato.streamdeck.page.previous",
        },
        "3,0": plugin_action(
            "4d19962c-ae27-4b6c-bbaa-26861d4ae747",
            "Mac / PC",
            "com.dorperetz.monitorswitcher.deck",
            {},
            two_states,
        ),
        "3,1": plugin_action(
            "df3610aa-4b1b-4656-8a45-d0e01017530e",
            "Toggle Monitor Input",
            "com.dorperetz.monitorswitcher.input",
            {"target": "DELL P2721Q"},
            two_states,
        ),
        "4,0": plugin_action(
            "ec717985-2b7a-4e85-9a5c-84183fd405fb",
            "PC Switch",
            "com.dorperetz.monitorswitcher.pcswitch",
            {},
            two_states,
            state_idx=1,
        ),
        "4,1": plugin_action(
            "f93b65be-e49e-410b-82bc-305907c93b86",
            "Toggle Monitor Input",
            "com.dorperetz.monitorswitcher.input",
            {"target": "DELL U2720Q"},
            two_states,
        ),
        # HA on the left of the Mac/PC page (Mac lights-page entities).
        "0,1": ha_action(
            "Both Lights",
            "script.dor_s_table_light_and_lump_toggle",
            "light.dor_s_table_light",
        ),
        "1,1": ha_action(
            "Lamp",
            "script.dor_s_lump_toggle",
            "switch.power_plug_dor_s_lump",
        ),
        "2,1": ha_action(
            "Table Light",
            "script.dor_s_table_light_toggle",
            "light.dor_s_table_light",
        ),
        "0,0": ha_action(
            "AC",
            "script.dor_study_room_cool_toggle",
            "climate.khdr_bvdh",
        ),
        "1,0": ha_action(
            "Office Off",
            "script.dor_s_study_room_shut_off",
            "script.dor_s_study_room_shut_off",
        ),
        "2,2": ha_action(
            "Bright Red",
            "script.dor_s_table_light_bright_red_toggle",
            "light.dor_s_table_light",
        ),
    }

    data = {"Controllers": [{"Actions": acts, "Type": "Keypad"}], "Icon": "", "Name": ""}
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PROFILE} ({PROFILE.stat().st_size} bytes)")
    for key in sorted(acts):
        print(f"  {key}: {acts[key]['UUID']} / {acts[key]['Name']}")


if __name__ == "__main__":
    main()
