#!/usr/bin/env python3
"""Standby screen for the Stream Deck while it is back on proxmox2.

VirtualHere is configured with AutoAttachToKernel, so when neither the Mac
nor the PC is using the deck it reappears here as a HID device. This draws
Mac, PC, and the Home Assistant folder. The Mac and PC keys hand the deck to
that computer when its hub answers, and send Wake-on-LAN when it does not.
When a computer claims the deck, the device disappears and this waits until
it returns.

No third-party packages. Home Assistant address and token, and the wake
addresses, come from config.json next to this file. That file stays on the
host and is not part of the git tree.
"""

from __future__ import annotations

import ipaddress
import json
import os
import select
import socket
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
KEYS_DIR = APP_DIR / "keys"
LOG_PATH = APP_DIR / "standby.log"
VENDOR = "0fd9"
PRODUCT = "0080"
IMAGE_REPORT_LENGTH = 1024
IMAGE_HEADER_LENGTH = 8
HUB_PORT = 8765
HUB_RESCAN_SECONDS = 600.0

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


def lan_broadcasts(extra: str) -> list[str]:
    """Every address that can carry a magic packet on this host.

    A /22 LAN's directed broadcast is not 192.168.68.255. Limited
    broadcast and each interface's real broadcast both need a copy.
    """
    found = ["255.255.255.255"]
    extra = str(extra or "").strip()
    if extra and extra not in found:
        found.append(extra)
    for iface in lan_interfaces():
        brd = str(iface.network.broadcast_address)
        if brd not in found:
            found.append(brd)
    return found


def _ip_addr_lines() -> list[list[str]]:
    """``ip -4 -o addr show``, one split line per address."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split() for line in out.splitlines()]


def lan_interfaces() -> list[ipaddress.IPv4Interface]:
    """This host's private LAN addresses, /22 to /30.

    Proxmox bridges often print no ``brd``, so the subnet comes from the
    address prefix itself.
    """
    found: list[ipaddress.IPv4Interface] = []
    for parts in _ip_addr_lines():
        if "inet" not in parts:
            continue
        try:
            iface = ipaddress.ip_interface(parts[parts.index("inet") + 1])
        except (ValueError, IndexError):
            continue
        network = iface.network
        if iface.ip.is_loopback or not network.is_private:
            continue
        if 22 <= network.prefixlen <= 30 and iface not in found:
            found.append(iface)
    return found


def lan_hosts() -> list[str]:
    """Every other address on this host's private LANs."""
    hosts: list[str] = []
    for iface in lan_interfaces():
        for host in iface.network.hosts():
            text = str(host)
            if host != iface.ip and text not in hosts:
                hosts.append(text)
    return hosts


def probe_hub(host: str, timeout: float = 1.0) -> str:
    """``a`` or ``b`` when a monitor-switcher hub answers on ``host``, else ``""``."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{HUB_PORT}/health", timeout=timeout
        ) as response:
            data = json.loads(response.read(4096).decode("utf-8", errors="replace"))
    except Exception:
        return ""
    side = str(data.get("local_pc") or "") if isinstance(data, dict) else ""
    return side if side in ("a", "b") else ""


def _hub_port_open(host: str) -> bool:
    try:
        with socket.create_connection((host, HUB_PORT), timeout=0.4):
            return True
    except OSError:
        return False


class HubFinder:
    """Finds the Mac and PC hubs on the LAN so a key can hand them the deck."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.urls = {"a": "", "b": ""}
        self.fixed = {"a": False, "b": False}
        self.token = ""
        self._scanning = False
        self._scanned_at = 0.0

    def configure(self, config: dict) -> None:
        with self._lock:
            self.token = str(config.get("hub_token") or "")
            for side in ("a", "b"):
                url = str(config.get(f"hub_{side}_url") or "").strip().rstrip("/")
                self.fixed[side] = bool(url)
                if url:
                    self.urls[side] = url

    def url(self, side: str) -> str:
        with self._lock:
            return self.urls.get(side, "")

    def rescan(self, force: bool = False) -> None:
        with self._lock:
            if all(self.fixed.values()) or self._scanning:
                return
            if not force and time.monotonic() - self._scanned_at < HUB_RESCAN_SECONDS:
                return
            self._scanning = True
        threading.Thread(target=self._scan, name="hub-scan", daemon=True).start()

    def _scan(self) -> None:
        found: dict[str, str] = {}
        try:
            hosts = lan_hosts()
            with ThreadPoolExecutor(max_workers=64) as pool:
                open_hosts = [
                    host
                    for host, is_open in zip(hosts, pool.map(_hub_port_open, hosts))
                    if is_open
                ]
            for host in open_hosts:
                side = probe_hub(host)
                if side and side not in found:
                    found[side] = f"http://{host}:{HUB_PORT}"
        except Exception as exc:
            log(f"hub scan failed: {exc}")
        with self._lock:
            for side, url in found.items():
                if not self.fixed[side] and self.urls[side] != url:
                    self.urls[side] = url
                    log(f"found {'Mac' if side == 'a' else 'PC'} hub at {url}")
            self._scanned_at = time.monotonic()
            self._scanning = False

    def hand_deck_to(self, side: str) -> bool:
        """Ask that computer to take the deck. False when it does not answer."""
        url = self.url(side)
        host = url.split("//", 1)[-1].split(":", 1)[0] if url else ""
        if not host or probe_hub(host) != side:
            self.rescan(force=True)
            return False
        request = urllib.request.Request(f"{url}/claim", data=b"", method="POST")
        with self._lock:
            if self.token:
                request.add_header("X-Hub-Token", self.token)

        def send() -> None:
            try:
                # The claim detaches the deck from this host; the reply may
                # come long after, or never if the computer is slow.
                with urllib.request.urlopen(request, timeout=180) as response:
                    log(f"claim reply: {response.read(200).decode('utf-8', 'replace')}")
            except Exception as exc:
                log(f"claim request to {url} failed: {exc}")

        threading.Thread(target=send, name="hub-claim", daemon=True).start()
        return True


def send_wol(mac: str, broadcast: str) -> None:
    raw = mac.replace(":", "").replace("-", "")
    if len(raw) != 12:
        raise ValueError("wake address is missing")
    mac_bytes = bytes.fromhex(raw)
    packet = b"\xff" * 6 + mac_bytes * 16
    targets = lan_broadcasts(broadcast)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for address in targets:
                for port in (9, 7):
                    sock.sendto(packet, (address, port))
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
    def __init__(self, config: dict, hubs: HubFinder) -> None:
        self.config = config
        self.hubs = hubs
        self.page = "main"
        self.dirty = False
        self._fetching = False
        self._states_lock = threading.Lock()
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
        """Fetch Home Assistant states off the key loop; sets ``dirty`` on change."""
        if self.page != "ha":
            return
        with self._states_lock:
            if self._fetching or time.monotonic() - self.states_at < 3:
                return
            self._fetching = True
            self.states_at = time.monotonic()
        threading.Thread(target=self._fetch_states, name="ha-states", daemon=True).start()

    def _fetch_states(self) -> None:
        entities = []
        for spec in HA_KEYS.values():
            entity = spec.get("state")
            if entity and entity not in entities:
                entities.append(entity)
        states: dict[str, str] = {}
        try:
            states = ha_states(self.config, entities)
        finally:
            with self._states_lock:
                if states != self.states:
                    self.states = states
                    self.dirty = True
                self.states_at = time.monotonic()
                self._fetching = False

    def image_for(self, spec: dict) -> bytes:
        name = spec["image"]
        entity = spec.get("state")
        if entity:
            with self._states_lock:
                state = self.states.get(entity, "")
            suffix = "on" if entity_is_on(state) else "off"
            return self.images[f"{name}-{suffix}"]
        return self.images[name]

    def paint(self, fd: int) -> None:
        self.dirty = False
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
            label = "Mac" if which == "a" else "PC"
            if self.hubs.hand_deck_to(which):
                return f"hand deck to {label}"
            mac = str(self.config.get(f"wol_{which}") or "")
            broadcast = str(self.config.get("wol_broadcast") or "255.255.255.255")
            send_wol(mac, broadcast)
            return f"wake {label}"
        if spec.get("service"):
            ha_call(self.config, spec["service"], spec.get("entity"), spec.get("data"))
            self.states_at = 0.0
            return spec["service"]
        return ""


def run_deck(path: str, config: dict, hubs: HubFinder) -> None:
    screen = Screen(config, hubs)
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
                screen.refresh_states()
                if screen.dirty:
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
    hubs = HubFinder()
    hubs.configure(load_config())
    hubs.rescan(force=True)
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
        config = load_config()
        hubs.configure(config)
        hubs.rescan()
        try:
            run_deck(path, config, hubs)
        except OSError as exc:
            log(f"deck closed ({exc})")
            time.sleep(0.5)
        except Exception:
            log("standby error:\n" + traceback.format_exc())
            time.sleep(2)


if __name__ == "__main__":
    main()
