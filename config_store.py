"""Load and save monitor switcher configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from runtime_paths import is_frozen, user_data_dir

CONFIG_VERSION = 2

DEFAULT_CONFIG: dict = {
    "version": CONFIG_VERSION,
    "monitor_bindings": [],
    "pc_switch": {
        "enabled": False,
        "hotkey": "ctrl+shift+1",
        "pc_a_name": "PC 1",
        "pc_b_name": "PC 2",
        "monitors": [],
    },
}

INPUT_CHOICES: list[tuple[str, int]] = [
    ("DisplayPort-1 (0x0F)", 0x0F),
    ("DisplayPort-2 (0x10)", 0x10),
    ("HDMI-1 (0x11)", 0x11),
    ("HDMI-2 (0x12)", 0x12),
    ("USB-C (0x1B)", 0x1B),
    ("DVI-1 (0x03)", 0x03),
    ("VGA (0x01)", 0x01),
]


def config_path() -> Path:
    if is_frozen():
        return user_data_dir() / "config.json"
    return Path(__file__).with_name("config.json")


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.exists():
        save_config(deepcopy(DEFAULT_CONFIG), path)
        return deepcopy(DEFAULT_CONFIG)

    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)

    return migrate_config(data)


def save_config(data: dict, path: Path | None = None) -> None:
    path = path or config_path()
    data = migrate_config(data)
    data["version"] = CONFIG_VERSION
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def migrate_config(data: dict) -> dict:
    if data.get("version") == CONFIG_VERSION:
        return _ensure_pc_switch(data)

    if "bindings" in data:
        monitor_bindings = []
        for binding in data.get("bindings", []):
            monitor_bindings.append(
                {
                    "enabled": True,
                    "monitor": binding.get("monitor", ""),
                    "device": "",
                    "hotkey": binding.get("hotkey", ""),
                    "input_a": binding["inputs"][0],
                    "input_b": binding["inputs"][1],
                }
            )
        return {
            "version": CONFIG_VERSION,
            "monitor_bindings": monitor_bindings,
            "pc_switch": deepcopy(DEFAULT_CONFIG["pc_switch"]),
        }

    return _ensure_pc_switch(deepcopy(DEFAULT_CONFIG) | data)


def _ensure_pc_switch(data: dict) -> dict:
    if "pc_switch" not in data:
        data["pc_switch"] = deepcopy(DEFAULT_CONFIG["pc_switch"])
    if "monitor_bindings" not in data:
        data["monitor_bindings"] = []
    return data


def input_label(value: int) -> str:
    for label, code in INPUT_CHOICES:
        if code == value:
            return label
    return f"Custom (0x{value:02X})"


def input_value_from_label(label: str) -> int | None:
    for choice_label, code in INPUT_CHOICES:
        if choice_label == label:
            return code
    if label.startswith("Custom (0x") and label.endswith(")"):
        hex_part = label[len("Custom (0x") : -1]
        return int(hex_part, 16)
    return None
