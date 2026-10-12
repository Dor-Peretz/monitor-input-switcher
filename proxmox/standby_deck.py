#!/usr/bin/env python3
"""Standby screen for the Stream Deck while it is back on proxmox2.

VirtualHere is configured with AutoAttachToKernel, so when neither the Mac
nor the PC is using the deck it reappears here as a HID device. This draws
Turn on Mac, Turn on PC, and the Home Assistant folder. When a computer
claims the deck, the device disappears and this waits until it returns.

No third-party packages. Home Assistant address and token, and the wake
addresses, come from config.json next to this file. That file stays on the
host and is not part of the git tree.
"""

from __future__ import annotations

import json
import os
import select
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
KEYS_DIR = APP_DIR / "keys"
LOG_PATH = APP_DIR / "standby.log"
VENDOR = "0fd9"
PRODUCT = "0080"
IMAGE_REPORT_LENGTH = 1024
IMAGE_HEADER_LENGTH = 8

# Same Home Assistant folder as the Elgato profile. column, row.
HA_KEYS = {
    (0, 0): {"image": "ha-back", "page": "main"},
    (1, 0): {
        "image": "ha-both",
        "service": "script.dor_s_table_light_and_lump_toggle",
        "state": "binary_sensor.dor_s_both_lights",
    },
    (2, 0): {
        "image": "ha-lamp",
        "service": "script.dor_s_lump_toggle",
        "state": "switch.power_plug_dor_s_lump",
    },
    (3, 0): {
        "image": "ha-light",
        "service": "script.dor_s_table_light_toggle",
        "state": "light.dor_s_table_light",
    },
    (1, 1): {
        "image": "ha-ac",
        "service": "script.dor_study_room_cool_toggle",
        "state": "climate.khdr_bvdh",
    },
    (2, 1): {
        "image": "ha-red",
        "service": "light.toggle",
        "entity": "light.dor_s_table_light",
        "data": {"rgbw_color": [255, 0, 0, 0], "brightness_pct": 100, "effect": "Solid"},
        "state": "light.dor_s_table_light",
    },
    (3, 2): {"image": "ha-on", "service": "script.dor_s_study_room_turn_on"},
    (4, 2): {"image": "ha-off", "service": "script.dor_s_study_room_shut_off"},
}

MAIN_KEYS = {
    (0, 0): {"image": "main-mac", "wake": "a"},
    (1, 0): {"image": "main-pc", "wake": "b"},
    (2, 0): {"image": "main-ha", "page": "ha"},
}


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def key_index(col: int, row: int) -> int:
    return row * 5 + col


def load_jpeg(name: str) -> bytes:
    path = KEYS_DIR / f"{name}.jpg"
    return path.read_bytes()


def find_hidraw() -> str | None:
    root = Path("/sys/class/hidraw")
    if not root.is_dir():
        return None
    for node in root.iterdir():
        uevent = node / "device" / "uevent"
        try:
            text = uevent.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hid_id = ""
        for line in text.splitlines():
            if line.startswith("HID_ID="):
                hid_id = line.split("=", 1)[1].lower()
        # HID_ID is bus:vendor:product, for example 0003:00000fd9:00000080.
        parts = hid_id.split(":")
        if len(parts) == 3 and parts[1].endswith(VENDOR) and parts[2].endswith(PRODUCT):
            return str(Path("/dev") / node.name)
    return None


def set_brightness(fd: int, percent: int) -> None:
    import fcntl

    percent = max(0, min(100, int(percent)))
    buffer = bytearray(32)
    buffer[0] = 0x03
    buffer[1] = 0x08
    buffer[2] = percent
    # HIDIOCSFEATURE(len) = _IOC(WRITE|READ, 'H', 0x06, len)
    request = (3 << 30) | (32 << 16) | (ord("H") << 8) | 0x06
    try:
        fcntl.ioctl(fd, request, buffer)
    except OSError as exc:
        log(f"brightness not set ({exc})")


def write_image(fd: int, index: int, image: bytes) -> None:
    payload_length = IMAGE_REPORT_LENGTH - IMAGE_HEADER_LENGTH
    page = 0
    remaining = len(image)
    while remaining > 0:
        this = min(remaining, payload_length)
        sent = page * payload_length
        header = bytearray(IMAGE_HEADER_LENGTH)
        header[0] = 0x02
        header[1] = 0x07
        header[2] = index & 0xFF
        header[3] = 1 if this == remaining else 0
        header[4] = this & 0xFF
        header[5] = (this >> 8) & 0xFF
        header[6] = page & 0xFF
        header[7] = (page >> 8) & 0xFF
        packet = bytes(header) + image[sent : sent + this]
        packet += bytes(IMAGE_REPORT_LENGTH - len(packet))
        os.write(fd, packet)
        remaining -= this
        page += 1


def send_wol(mac: str, broadcast: str) -> None:
    raw = mac.replace(":", "").replace("-", "")
    if len(raw) != 12:
        raise ValueError("wake address is missing")
    mac_bytes = bytes.fromhex(raw)
    packet = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            sock.sendto(packet, (broadcast, port))
    finally:
        sock.close()


def ha_call(config: dict, service: str, entity: str | None, data: dict | None) -> str:
    base = str(config.get("ha_url") or "").rstrip("/")
    token = str(config.get("ha_token") or "")
    if not base or not token:
        raise RuntimeError("Home Assistant address or token is not set in config.json")
    domain, _, name = service.partition(".")
    if not domain or not name:
        raise RuntimeError(f"bad service {service}")
    body: dict = {}
    if entity:
        body["entity_id"] = entity
    if data:
        body.update(data)
    request = urllib.request.Request(
        f"{base}/api/services/{domain}/{name}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(200).decode("utf-8", errors="replace")
        raise RuntimeError(f"Home Assistant {exc.code} {detail}") from exc
    return service


def ha_states(config: dict, entities: list[str]) -> dict[str, str]:
    base = str(config.get("ha_url") or "").rstrip("/")
    token = str(config.get("ha_token") or "")
    if not base or not token or not entities:
        return {}
    found: dict[str, str] = {}
    for entity in entities:
        request = urllib.request.Request(f"{base}/api/states/{entity}")
        request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            found[entity] = str(payload.get("state") or "")
    return found


def entity_is_on(state: str) -> bool:
    return state not in ("", "off", "unavailable", "unknown", "idle")


class Screen:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.page = "main"
        self.blank = load_jpeg("blank")
        self.images = {
            "main-mac": load_jpeg("main-mac"),
            "main-pc": load_jpeg("main-pc"),
            "main-ha": load_jpeg("main-ha"),
            "ha-back": load_jpeg("ha-back"),
            "ha-on": load_jpeg("ha-on"),
            "ha-off": load_jpeg("ha-off"),
        }
        for name in ("both", "lamp", "light", "ac", "red"):
            self.images[f"ha-{name}-off"] = load_jpeg(f"ha-{name}-off")
            self.images[f"ha-{name}-on"] = load_jpeg(f"ha-{name}-on")
        self.states: dict[str, str] = {}
        self.states_at = 0.0

    def layout(self) -> dict[int, dict]:
        source = HA_KEYS if self.page == "ha" else MAIN_KEYS
        return {key_index(col, row): spec for (col, row), spec in source.items()}

    def refresh_states(self) -> None:
        if self.page != "ha":
            return
        if time.monotonic() - self.states_at < 3:
            return
        entities = []
        for spec in HA_KEYS.values():
            entity = spec.get("state")
            if entity and entity not in entities:
                entities.append(entity)
        self.states = ha_states(self.config, entities)
        self.states_at = time.monotonic()

    def image_for(self, spec: dict) -> bytes:
        name = spec["image"]
        entity = spec.get("state")
        if entity:
            suffix = "on" if entity_is_on(self.states.get(entity, "")) else "off"
            return self.images[f"{name}-{suffix}"]
        return self.images[name]

    def paint(self, fd: int) -> None:
        self.refresh_states()
        layout = self.layout()
        for index in range(15):
            spec = layout.get(index)
            image = self.image_for(spec) if spec else self.blank
            write_image(fd, index, image)

    def press(self, index: int) -> str:
        spec = self.layout().get(index)
        if not spec:
            return ""
        if spec.get("page"):
            self.page = spec["page"]
            self.states_at = 0.0
            return f"page {self.page}"
        if spec.get("wake"):
            which = spec["wake"]
            mac = str(self.config.get(f"wol_{which}") or "")
            broadcast = str(self.config.get("wol_broadcast") or "255.255.255.255")
            send_wol(mac, broadcast)
            label = "Mac" if which == "a" else "PC"
            return f"wake {label}"
        if spec.get("service"):
            ha_call(self.config, spec["service"], spec.get("entity"), spec.get("data"))
            self.states_at = 0.0
            return spec["service"]
        return ""


def run_deck(path: str, config: dict) -> None:
    screen = Screen(config)
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        log(f"deck attached at {path}")
        set_brightness(fd, 80)
        screen.paint(fd)
        previous = [0] * 15
        logged_report = False
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                if screen.page == "ha" and time.monotonic() - screen.states_at >= 3:
                    screen.paint(fd)
                continue
            try:
                report = os.read(fd, 512)
            except BlockingIOError:
                continue
            if not logged_report:
                log("first report " + report[:20].hex())
                logged_report = True
            if len(report) < 19:
                continue
            states = [1 if report[4 + i] else 0 for i in range(15)]
            pressed = False
            for index, (now, before) in enumerate(zip(states, previous)):
                if now and not before:
                    pressed = True
                    try:
                        note = screen.press(index)
                        log(note or f"key {index}")
                    except Exception as exc:
                        log(f"key {index} failed: {exc}")
                        set_brightness(fd, 20)
                        time.sleep(0.15)
                        set_brightness(fd, 80)
            previous = states
            if pressed:
                screen.paint(fd)
    finally:
        os.close(fd)
        log("deck released")


def main() -> None:
    log("standby screen waiting for the Stream Deck")
    announced = False
    while True:
        path = find_hidraw()
        if not path:
            if not announced:
                log("Stream Deck is with a computer. Waiting.")
                announced = True
            time.sleep(1)
            continue
        announced = False
        try:
            run_deck(path, load_config())
        except OSError as exc:
            log(f"deck closed ({exc})")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
