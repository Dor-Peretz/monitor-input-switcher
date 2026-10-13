"""Bootstrap Home Assistant credentials into the Stream Deck HA plugin.

The Elgato plugin stores global settings in the Windows Credential Store.
Until those are set once, HA keys on the PC profile flash/alert and do nothing.

This script injects a one-shot bootstrap into the installed plugin bundle so the
first launch saves serverUrl + accessToken via setGlobalSettings, then reconnects.
Credentials are read from a local file (never committed):

  %LOCALAPPDATA%\\Elgato\\StreamDeck\\de.perdoctus.streamdeck.homeassistant\\connection.json

Or from HA_BASE_URL / HA_TOKEN in the environment / jarvis .env.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

PLUGIN_DIR = (
    Path.home()
    / "AppData/Roaming/Elgato/StreamDeck/Plugins"
    / "de.perdoctus.streamdeck.homeassistant.sdPlugin"
)
PLUGIN_JS = PLUGIN_DIR / "assets" / "plugin-frRiVCDK.js"
CONN_DIR = (
    Path.home()
    / "AppData/Local/Elgato/StreamDeck"
    / "de.perdoctus.streamdeck.homeassistant"
)
CONN_FILE = CONN_DIR / "connection.json"
MARKER = "/*HA_CONN_BOOTSTRAP*/"
JARVIS_ENV = Path.home() / "Projects/jarvis/.env"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def resolve_connection() -> tuple[str, str]:
    if CONN_FILE.is_file():
        data = json.loads(CONN_FILE.read_text(encoding="utf-8"))
        url = str(data.get("serverUrl") or "").strip()
        token = str(data.get("accessToken") or "").strip()
        if url and token:
            return url, token

    env = {**_load_dotenv(JARVIS_ENV), **os.environ}
    url = (env.get("HA_BASE_URL") or env.get("HASS_URL") or "").strip().rstrip("/")
    token = (env.get("HA_TOKEN") or env.get("HASS_TOKEN") or "").strip()
    if not url or not token:
        raise SystemExit(
            f"Missing HA connection. Write {CONN_FILE} with "
            '{"serverUrl":"http://...:8123","accessToken":"..."} '
            "or set HA_BASE_URL and HA_TOKEN."
        )
    return url, token


def write_connection_file(url: str, token: str) -> None:
    CONN_DIR.mkdir(parents=True, exist_ok=True)
    CONN_FILE.write_text(
        json.dumps({"serverUrl": url, "accessToken": token}, indent=2) + "\n",
        encoding="utf-8",
    )


def quit_streamdeck() -> None:
    subprocess.run(
        ["taskkill", "/IM", "StreamDeck.exe", "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    time.sleep(2)


def start_streamdeck() -> None:
    exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
        "Elgato/StreamDeck/StreamDeck.exe"
    )
    if exe.is_file():
        subprocess.Popen([str(exe)], close_fds=True)


def patch_plugin_js(url: str, token: str) -> None:
    if not PLUGIN_JS.is_file():
        raise SystemExit(f"HA plugin not installed: {PLUGIN_JS}")

    text = PLUGIN_JS.read_text(encoding="utf-8")
    # Remove a previous bootstrap if present.
    text = re.sub(
        re.escape(MARKER) + r".*?" + re.escape(MARKER),
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )

    needle = (
        'M.value.on("globalsettings",A=>{M.value.log("Got global settings.");'
        "const V=JSON.stringify(A),L=d1.migrate(A);"
    )
    if needle not in text:
        raise SystemExit("Could not find globalsettings handler to patch")

    # Embed credentials only in the installed AppData plugin copy (not in git).
    bootstrap = (
        f"{MARKER}"
        "A=(A&&typeof A==='object'?A:{});"
        f"if(!A.serverUrl||!A.accessToken){{A={{...A,serverUrl:{json.dumps(url)},"
        f"accessToken:{json.dumps(token)}}}}};"
        f"{MARKER}"
    )
    replacement = (
        'M.value.on("globalsettings",A=>{M.value.log("Got global settings.");'
        + bootstrap
        + "const V=JSON.stringify(A),L=d1.migrate(A);"
        # Always persist after bootstrap so Credential Store gets the token.
        + "M.value.saveGlobalSettings(L);"
        # Skip the original conditional save — already saved above. Keep migrate compare noop.
        + "void V;"
    )
    # The original continues with: JSON.stringify(L)!==V&&M.value.saveGlobalSettings(L),o.value=L;...
    # Replace through the conditional save so we don't double-call awkwardly.
    full_needle = (
        'M.value.on("globalsettings",A=>{M.value.log("Got global settings.");'
        "const V=JSON.stringify(A),L=d1.migrate(A);"
        "JSON.stringify(L)!==V&&M.value.saveGlobalSettings(L),"
    )
    full_replacement = (
        'M.value.on("globalsettings",A=>{M.value.log("Got global settings.");'
        + bootstrap
        + "const V=JSON.stringify(A),L=d1.migrate(A);"
        + "M.value.saveGlobalSettings(L),"
    )
    if full_needle not in text:
        raise SystemExit("Could not find full globalsettings save sequence to patch")

    text = text.replace(full_needle, full_replacement, 1)
    PLUGIN_JS.write_text(text, encoding="utf-8")
    print(f"patched {PLUGIN_JS}")


def strip_bootstrap() -> None:
    """Remove embedded credentials after Credential Store has them."""
    if not PLUGIN_JS.is_file():
        return
    text = PLUGIN_JS.read_text(encoding="utf-8")
    if MARKER not in text:
        return
    text = re.sub(
        re.escape(MARKER) + r".*?" + re.escape(MARKER),
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    forced = (
        "const V=JSON.stringify(A),L=d1.migrate(A);M.value.saveGlobalSettings(L),"
    )
    original = (
        "const V=JSON.stringify(A),L=d1.migrate(A);"
        "JSON.stringify(L)!==V&&M.value.saveGlobalSettings(L),"
    )
    if forced in text:
        text = text.replace(forced, original, 1)
    PLUGIN_JS.write_text(text, encoding="utf-8")
    print(f"stripped bootstrap from {PLUGIN_JS}")


def main() -> None:
    url, token = resolve_connection()
    # Normalize ws URLs the same way the plugin migrate does.
    url = (
        url.replace("ws://", "http://")
        .replace("wss://", "https://")
        .removesuffix("/api/websocket")
        .rstrip("/")
    )
    write_connection_file(url, token)
    print(f"connection file: {CONN_FILE}")
    print(f"serverUrl: {url}")
    print(f"accessToken: length={len(token)}")

    quit_streamdeck()
    try:
        patch_plugin_js(url, token)
        start_streamdeck()
        print("Stream Deck restarted — waiting for plugin to save global settings…")
        time.sleep(6)
        quit_streamdeck()
    finally:
        # The patched JS holds the token in plain text; never leave it behind.
        strip_bootstrap()
    start_streamdeck()
    print("Done. HA keys should work; token is in the Windows Credential Store.")


if __name__ == "__main__":
    main()
