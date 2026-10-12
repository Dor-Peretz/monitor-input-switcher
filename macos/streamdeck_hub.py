#!/usr/bin/env python3
"""Hand the shared Stream Deck to whichever PC the desk just switched to.

The deck stays plugged into an always-on VirtualHere server (proxmox2).
The Mac / PC key hands the deck to the other computer. Monitor switching
is separate and does not move the deck.

No extra packages. The other machine is reached with a small HTTP listener
that this module starts inside the menu-bar app, the tray app, and the
Stream Deck plugin.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "streamdeck-hub.log"
_DEVICE_LINE = re.compile(
    r"^\s*(\*?)-->\s+(?P<name>.+?)\s+\((?P<address>[^)]+)\)(?P<rest>.*)$"
)
_MAC_CLIENT = (
    "/Applications/VirtualHereUniversal.app/Contents/MacOS/VirtualHereUniversal"
)

_started = False
_start_lock = threading.Lock()


def _log(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def hub_settings() -> dict:
    from config_store import load_config

    raw = load_config().get("streamdeck_hub") or {}
    local_pc = str(raw.get("local_pc") or "a").strip().lower()
    if local_pc not in ("a", "b"):
        local_pc = "a"
    try:
        port = int(raw.get("listen_port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    return {
        "enabled": bool(raw.get("enabled")),
        "local_pc": local_pc,
        "listen_host": str(raw.get("listen_host") or "0.0.0.0"),
        "listen_port": port,
        "peer_url": str(raw.get("peer_url") or "").rstrip("/"),
        "token": str(raw.get("token") or ""),
        "client_path": str(raw.get("client_path") or "").strip(),
        "device_match": str(raw.get("device_match") or "Stream Deck"),
        "label_a": str(raw.get("label_a") or "Mac"),
        "label_b": str(raw.get("label_b") or "PC"),
    }


def client_path(settings: dict | None = None) -> str:
    settings = settings or hub_settings()
    configured = settings["client_path"]
    if configured:
        return configured
    if sys.platform == "darwin" and Path(_MAC_CLIENT).is_file():
        return _MAC_CLIENT
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    for candidate in (
        Path(program_files) / "VirtualHere" / "vhui64.exe",
        Path(local) / "VirtualHere" / "vhui64.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "VirtualHere client not found. Set streamdeck_hub.client_path."
    )


def parse_list(text: str) -> list[dict]:
    """Parse `VirtualHere -t LIST` into device records."""
    devices: list[dict] = []
    for line in text.splitlines():
        match = _DEVICE_LINE.match(line)
        if not match:
            continue
        rest = match.group("rest") or ""
        devices.append(
            {
                "name": match.group("name").strip(),
                "address": match.group("address").strip(),
                "auto_use": bool(match.group(1)),
                "in_use_by_me": "in-use by you" in rest.lower(),
            }
        )
    return devices


def _run_client(command: str, settings: dict | None = None) -> str:
    """Run a VirtualHere client API command against the already-running client.

    On Windows the GUI client does not write IPC replies to stdout, so we
    pass ``-r`` and read the reply from a temp file (VirtualHere docs).
    """
    binary = client_path(settings)
    reply_path: Path | None = None
    argv = [binary, "-t", command]
    if sys.platform == "win32":
        handle = tempfile.NamedTemporaryFile(
            prefix="vh-ipc-", suffix=".txt", delete=False
        )
        handle.close()
        reply_path = Path(handle.name)
        argv.extend(["-r", str(reply_path)])
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if reply_path is not None:
            try:
                output = reply_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
        if result.returncode != 0 and not output.startswith("OK"):
            raise RuntimeError(output or f"VirtualHere exited {result.returncode}")
        return output
    finally:
        if reply_path is not None:
            try:
                reply_path.unlink(missing_ok=True)
            except OSError:
                pass


def _find_deck(settings: dict | None = None) -> dict:
    settings = settings or hub_settings()
    devices = parse_list(_run_client("LIST", settings))
    needle = settings["device_match"].lower()
    matches = [device for device in devices if needle in device["name"].lower()]
    if not matches:
        raise RuntimeError(
            f"No '{settings['device_match']}' on the VirtualHere server."
        )
    return matches[0]


def _disable_auto_use(deck: dict, settings: dict) -> dict:
    """Auto-use would grab the deck straight back after we release it."""
    if not deck["auto_use"]:
        return deck
    _run_client(f"AUTO USE DEVICE,{deck['address']}", settings)
    return _find_deck(settings)


def claim_local() -> str:
    """Take the Stream Deck on this computer."""
    settings = hub_settings()
    last_error: Exception | None = None
    for attempt in range(1, 6):
        deck = _disable_auto_use(_find_deck(settings), settings)
        if deck["in_use_by_me"] and not deck["auto_use"]:
            return f"already on this computer ({deck['name']})"
        try:
            _run_client(f"USE,{deck['address']}", settings)
            deck = _find_deck(settings)
            if deck["in_use_by_me"]:
                return f"claimed {deck['name']}"
            last_error = RuntimeError("USE returned but device is not in use by this computer")
        except Exception as exc:
            last_error = exc
            _log(f"claim attempt {attempt} failed: {exc}")
        time.sleep(1.0)
    raise RuntimeError(str(last_error) if last_error else "claim failed")


def release_local() -> str:
    """Give the Stream Deck up so the other computer can take it."""
    settings = hub_settings()
    deck = _disable_auto_use(_find_deck(settings), settings)
    if not deck["in_use_by_me"]:
        return f"already released ({deck['name']})"
    _run_client(f"STOP USING,{deck['address']}", settings)
    return f"released {deck['name']}"


def _post_peer(path: str) -> str:
    settings = hub_settings()
    if not settings["peer_url"]:
        return "other computer not configured (streamdeck_hub.peer_url)"
    url = settings["peer_url"] + path
    request = urllib.request.Request(url, data=b"", method="POST")
    token = settings["token"]
    if token:
        request.add_header("X-Hub-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.URLError as exc:
        return f"other computer unreachable ({exc.reason})"
    return body or "ok"


SELECTION_FILE = Path(__file__).resolve().parent / "deck_selection.json"


def read_selected() -> str | None:
    try:
        data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    owner = str(data.get("owner") or "")
    return owner if owner in ("a", "b") else None


def write_selected(owner: str) -> None:
    which = "b" if owner == "b" else "a"
    SELECTION_FILE.write_text(json.dumps({"owner": which}) + "\n", encoding="utf-8")


def displayed_owner() -> str:
    """Who the Stream Deck UI should show. Falls back to who holds the USB device."""
    selected = read_selected()
    if selected:
        return selected
    try:
        return current_owner()
    except Exception:
        return hub_settings()["local_pc"]


def current_owner() -> str:
    """``a`` when this machine holds the deck, otherwise ``b`` flipped from local_pc."""
    settings = hub_settings()
    deck = _find_deck(settings)
    if deck["in_use_by_me"]:
        return settings["local_pc"]
    return "b" if settings["local_pc"] == "a" else "a"


def owner_name(which: str) -> str:
    settings = hub_settings()
    return settings["label_b"] if which == "b" else settings["label_a"]


def toggle_deck_owner(target: str | None = None) -> str:
    """Hand the Stream Deck to the computer that does not have it.

    Monitor inputs are not touched. The chosen side is saved first so the
    key face can update even when the other computer cannot take the deck.
    If that handoff fails, this machine claims the deck back.
    """
    settings = hub_settings()
    if not settings["enabled"]:
        raise RuntimeError("Stream Deck hub is off in config.json")
    if target not in ("a", "b"):
        target = "b" if displayed_owner() == "a" else "a"
    write_selected(target)
    sending_away = target != settings["local_pc"]
    if sending_away and not settings["peer_url"]:
        raise RuntimeError(
            "Set streamdeck_hub.peer_url to the other computer before handing the deck away."
        )
    if sending_away:
        local = release_local()
        peer = _post_peer("/claim")
        if "unreachable" in peer or "not configured" in peer:
            try:
                claim_local()
            except Exception as exc:
                _log(f"Could not take the deck back: {exc}")
            raise RuntimeError(peer)
    else:
        peer = _post_peer("/release")
        local = claim_local()
    note = f"{owner_name(target)}: {local}; {peer}"
    _log(note)
    return note


class _HubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        settings = hub_settings()
        token = settings["token"]
        got = self.headers.get("X-Hub-Token", "")
        if token and not hmac.compare_digest(got, token):
            self._reply(403, "forbidden")
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/claim":
                message = claim_local()
            elif path == "/release":
                message = release_local()
            else:
                self._reply(404, "not found")
                return
        except Exception as exc:
            _log(f"{path} failed: {exc}")
            self._reply(500, str(exc))
            return
        _log(f"{path} -> {message}")
        self._reply(200, message)

    def _reply(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        _log(fmt % args)


def ensure_hub_listener() -> None:
    """Listen for claim/release from the other computer, once per process."""
    global _started
    settings = hub_settings()
    if not settings["enabled"]:
        return
    with _start_lock:
        if _started:
            return
        try:
            server = ThreadingHTTPServer(
                (settings["listen_host"], settings["listen_port"]),
                _HubHandler,
            )
        except OSError as exc:
            _log(f"Hub listener not started ({exc}). Another process has the port.")
            return
        _started = True
        threading.Thread(target=server.serve_forever, name="streamdeck-hub", daemon=True).start()
        _log(
            f"Hub listener on {settings['listen_host']}:{settings['listen_port']} "
            f"(this computer is {settings['label_a'] if settings['local_pc'] == 'a' else settings['label_b']})"
        )


def main() -> int:
    """`python3 streamdeck_hub.py claim|release|status` for a manual check."""
    command = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if command == "claim":
        print(claim_local())
    elif command == "release":
        print(release_local())
    elif command == "status":
        settings = hub_settings()
        print(json.dumps({"settings": settings, "devices": parse_list(_run_client("LIST", settings))}, indent=2))
    else:
        print("Use claim, release, or status.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
