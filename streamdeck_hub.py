#!/usr/bin/env python3
"""Hand the shared Stream Deck to whichever PC the desk just switched to.

The deck stays plugged into an always-on VirtualHere server (proxmox2).
The Mac / PC key hands the deck to the other computer. Monitor switching
is separate and does not move the deck.

When one computer turns off, the one that is still on takes the deck.
When both are off, the one that wakes up takes it. Wake-on-LAN addresses
are learned from the other computer while it is on.

No extra packages. The other machine is reached with a small HTTP listener
that this module starts inside the menu-bar app, the tray app, and the
Stream Deck plugin.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "streamdeck-hub.log"
LOG_MAX_BYTES = 1_000_000
_log_lock = threading.Lock()
_DEVICE_LINE = re.compile(
    r"^\s*(\*?)-->\s+(?P<name>.+?)\s+\((?P<address>[^)]+)\)(?P<rest>.*)$"
)
_MAC_CLIENT = (
    "/Applications/VirtualHereUniversal.app/Contents/MacOS/VirtualHereUniversal"
)

_started = False
_start_lock = threading.Lock()
_client_lock = threading.RLock()
_suspend_follow = False
_suspend_follow_until = 0.0
# A suspend that Windows cancels sends no resume event.
_SUSPEND_FOLLOW_MAX = 120.0
# macOS waits up to 30 s for IOAllowPowerChange; Windows gives about 2 s.
_SLEEP_HANDOFF_WAIT = 25.0 if sys.platform == "darwin" else 5.0
_CLAIM_BACKOFF_MAX = 60.0
_restart_threads: list[threading.Thread] = []
_MAC_ADDRESS = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_WINDOWS_MAC = re.compile(
    r"(?<![0-9A-Fa-f-])(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f-])"
)
_CLIENT_TIMEOUT = 20
_CLAIM_ATTEMPTS = 5
_CLAIM_RETRY_WINDOW = 30.0
# A claim attempt makes up to five VirtualHere calls and may first wait for
# another call to release _client_lock. No attempt starts after the window.
_PEER_CLAIM_TIMEOUT = _CLAIM_RETRY_WINDOW + 6 * _CLIENT_TIMEOUT + 10
_PEER_RELEASE_TIMEOUT = 30
_HANDOFF_SETTLE = 30.0
_MAX_BODY = 4096
# The plugin records whether Elgato shows a Stream Deck. A deck VirtualHere
# gives this computer that Elgato never shows has dead keys, so the watchdog
# restarts Elgato and then hands the deck back to the standby screen.
ELGATO_FILE = Path(__file__).resolve().parent / "elgato_deck.json"
_ELGATO_STALE = 20.0
_WATCHDOG_GRACE = 25.0
_WATCHDOG_RETRY = 30.0
_WATCHDOG_RESTARTS = 2
_AUTO_CLAIM_COOLDOWN = 600.0
# kIOMessage* from IOKit/pwr_mgt/IOPMLibDefs.h
_IO_CAN_SLEEP = 0xE0000270
_IO_WILL_SLEEP = 0xE0000280
_IO_WILL_NOT_SLEEP = 0xE0000290
_IO_HAS_POWERED_ON = 0xE0000300
_PBT_APMSUSPEND = 0x0004
_PBT_APMRESUMEAUTOMATIC = 0x0012


def _log(message: str) -> None:
    with _log_lock:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_suffix(".1.log"))
        except OSError:
            pass
        try:
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass


def _run_hidden(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a helper without flashing a Windows console or GUI window."""
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.run(argv, **kwargs)


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
        "follow_power": bool(raw.get("follow_power", True)),
        "wol_broadcast": str(raw.get("wol_broadcast") or "").strip(),
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
                "in_use": "in-use" in rest.lower() or "in use" in rest.lower(),
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
    with _client_lock:
        return _run_client_locked(binary, command)


def _run_client_locked(binary: str, command: str) -> str:
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
        result = _run_hidden(argv, timeout=_CLIENT_TIMEOUT)
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


def _restart_streamdeck_soon() -> None:
    """Quit/reopen Elgato after a VirtualHere claim so it sees the USB device.

    Without this, Windows often keeps showing the old (empty) session until you
    manually restart Stream Deck — which feels like having to reinstall it.

    The restart runs in a separate process so it still finishes when claim was
    handled inside the Stream Deck plugin (killing StreamDeck.exe would otherwise
    kill this Python process mid-restart).
    """
    try:
        if sys.platform == "win32":
            exe = (
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Elgato"
                / "StreamDeck"
                / "StreamDeck.exe"
            )
            if not exe.is_file():
                _log(f"StreamDeck.exe not found at {exe}")
                return
            # Off the request thread: the other computer waits on /claim.
            thread = threading.Thread(
                target=_restart_streamdeck_windows,
                args=(exe,),
                name="streamdeck-restart",
                daemon=True,
            )
            thread.start()
            _restart_threads.append(thread)
        elif sys.platform == "darwin":
            script = (
                'sleep 2; '
                'osascript -e \'quit app "Elgato Stream Deck"\' >/dev/null 2>&1; '
                'sleep 1; '
                'open -a "Elgato Stream Deck"'
            )
            subprocess.Popen(
                ["/bin/sh", "-c", script],
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _log("scheduled Elgato Stream Deck restart after claim")
    except Exception as exc:
        _log(f"Stream Deck restart after claim failed: {exc}")


def _restart_streamdeck_windows(exe: Path) -> None:
    """Kill and relaunch StreamDeck.exe from a process outside our job object.

    The plugin runs inside launch.exe's kill-on-close job, and every child it
    starts joins that job, DETACHED_PROCESS or not. Killing Elgato ends the
    plugin, launch.exe exits, and Windows kills the restart script before it
    relaunches Elgato. A process created through WMI is not in the job.
    """
    folder = str(exe.parent)
    # ping delays work in a hidden console; `timeout` does not. The script runs
    # in `folder`, so `start` finds the exe without a quoted path.
    script = (
        "ping -n 3 127.0.0.1 >nul & "
        f"taskkill /IM {exe.name} /F >nul 2>&1 & "
        "ping -n 2 127.0.0.1 >nul & "
        f"start {exe.name}"
    )

    def quoted(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    command = (
        "$startup = ([wmiclass]'Win32_ProcessStartup').CreateInstance(); "
        "$startup.ShowWindow = 0; "
        "$result = ([wmiclass]'Win32_Process').Create("
        f"{quoted('cmd.exe /c ' + script)}, {quoted(folder)}, $startup); "
        "exit [int]$result.ReturnValue"
    )
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        result = _run_hidden(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            timeout=20,
        )
    except Exception as exc:
        _log(f"Stream Deck restart after claim failed: {exc}")
        return
    if result.returncode != 0:
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        _log(
            f"Stream Deck restart after claim failed (code {result.returncode}). "
            f"{detail}"
        )
        return
    _log("scheduled StreamDeck.exe restart after claim")


def _wait_for_restart(timeout: float) -> None:
    """Let a pending Elgato restart finish before a short-lived process exits."""
    deadline = time.monotonic() + timeout
    for thread in list(_restart_threads):
        thread.join(max(0.0, deadline - time.monotonic()))


def claim_local() -> str:
    """Take the Stream Deck on this computer."""
    settings = hub_settings()
    last_error: Exception | None = None
    started = time.monotonic()
    sent_use = False
    for attempt in range(1, _CLAIM_ATTEMPTS + 1):
        deck = _disable_auto_use(_find_deck(settings), settings)
        if deck["in_use_by_me"] and not deck["auto_use"]:
            if sent_use:
                # USE timed out but went through: still a fresh attach.
                _restart_streamdeck_soon()
                return f"claimed {deck['name']}"
            if elgato_driving() is False:
                _restart_streamdeck_soon()
                return f"already on this computer ({deck['name']}); restarted Elgato"
            return f"already on this computer ({deck['name']})"
        try:
            sent_use = True
            _run_client(f"USE,{deck['address']}", settings)
            deck = _find_deck(settings)
            if deck["in_use_by_me"]:
                # Fresh USB attach: Elgato will not see the deck until restarted.
                _restart_streamdeck_soon()
                return f"claimed {deck['name']}"
            last_error = RuntimeError("USE returned but device is not in use by this computer")
        except Exception as exc:
            last_error = exc
            _log(f"claim attempt {attempt} failed: {exc}")
        if time.monotonic() - started >= _CLAIM_RETRY_WINDOW:
            break
        time.sleep(1.0)
    raise RuntimeError(str(last_error) if last_error else "claim failed")


def release_local() -> str:
    """Give the Stream Deck up so the other computer can take it."""
    settings = hub_settings()
    deck = _disable_auto_use(_find_deck(settings), settings)
    if not deck["in_use_by_me"]:
        return f"already released ({deck['name']})"
    try:
        _run_client(f"STOP USING,{deck['address']}", settings)
    except Exception:
        # VirtualHere often answers late after it has already let go.
        if _find_deck(settings)["in_use_by_me"]:
            raise
        return f"released {deck['name']} (VirtualHere answered late)"
    return f"released {deck['name']}"


SEEN_PEER_FILE = Path(__file__).resolve().parent / "peer_seen.json"
WAKE_FILE = Path(__file__).resolve().parent / "peer_wake.json"
_lan_mac_cache: tuple[float, str] | None = None


def _valid_mac(value: str) -> bool:
    return bool(_MAC_ADDRESS.match(value.strip().lower()))


def _normalize_mac(value: str) -> str:
    text = value.strip().lower().replace("-", ":")
    return text if _valid_mac(text) else ""


_state_lock = threading.RLock()
_STATE_LOCK_FILE = Path(tempfile.gettempdir()) / (
    "streamdeck-hub-"
    + hashlib.sha1(str(Path(__file__).resolve().parent).encode("utf-8")).hexdigest()[:12]
    + ".lock"
)


def _read_json(path: Path) -> dict:
    """Read a state file. A torn or locked file is retried, not treated as absent."""
    for attempt in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if attempt == 4:
                return {}
            time.sleep(0.05)
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _write_json(path: Path, data: dict) -> None:
    """Replace a state file in one step; other processes read it at any time."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data) + "\n")
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses while another process has the file open.
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)


def _lock_handle(handle, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _unlock_handle(handle) -> None:
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _state_locked():
    """Serialize read-modify-write of state files across threads and processes.

    The file lock is best effort: if it cannot be taken in time the update
    still runs under the in-process lock.
    """
    with _state_lock:
        try:
            handle = open(_STATE_LOCK_FILE, "a+b")
        except OSError:
            handle = None
        locked = handle is not None and _lock_handle(handle)
        try:
            yield
        finally:
            if handle is not None:
                if locked:
                    _unlock_handle(handle)
                handle.close()


def resolved_peer_url(settings: dict | None = None) -> str:
    """Configured peer, or the address the other computer last called from."""
    settings = settings or hub_settings()
    if settings["peer_url"]:
        return settings["peer_url"]
    return str(_read_json(SEEN_PEER_FILE).get("peer_url") or "").rstrip("/")


def remember_peer_host(host: str) -> None:
    """The other computer calls us on 8765. Remember it when peer_url is blank."""
    if not host:
        return
    if host.startswith("::ffff:"):
        host = host[7:]
    if host in ("127.0.0.1", "::1"):
        return
    url_host = f"[{host}]" if ":" in host else host
    settings = hub_settings()
    url = f"http://{url_host}:{settings['listen_port']}"
    if settings["peer_url"]:
        return
    with _state_locked():
        current = _read_json(SEEN_PEER_FILE).get("peer_url")
        if current == url:
            return
        _write_json(SEEN_PEER_FILE, {"peer_url": url})
    _log(f"Learned the other computer at {url}")


def _mask_to_prefix(mask: str) -> int | None:
    text = mask.strip().lower()
    if text.startswith("0x"):
        try:
            bits = int(text, 16)
        except ValueError:
            return None
        if bits < 0 or bits > 0xFFFFFFFF:
            return None
        return bin(bits).count("1")
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{text}").prefixlen
    except (ValueError, TypeError):
        return None


def _interface_cidrs() -> list[tuple[str, int]]:
    """Private IPv4 address and prefix for each connected interface."""
    if sys.platform == "darwin":
        result = _run_hidden(["ifconfig"], timeout=5)
        found: list[tuple[str, int]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0] != "inet" or parts[2] != "netmask":
                continue
            prefix = _mask_to_prefix(parts[3])
            if prefix is None:
                continue
            found.append((parts[1], prefix))
        return found
    if sys.platform == "win32":
        result = _run_hidden(
            ["netsh", "interface", "ipv4", "show", "addresses"],
            timeout=8,
        )
        text = result.stdout or ""
        ips = re.findall(r"IP Address:\s+(\d+\.\d+\.\d+\.\d+)", text)
        prefixes = [int(item) for item in re.findall(r"Subnet Prefix:\s+\S+/(\d+)", text)]
        if ips and len(ips) == len(prefixes):
            return list(zip(ips, prefixes))
        return [(ip, 24) for ip in ips]
    return []


def peer_scan_hosts() -> list[str]:
    """Hosts on this computer's private LAN, excluding our own addresses.

    A prefix wider than /24 is narrowed to the /24 that contains our address
    so a key press does not sweep a whole private /16.
    """
    own: set[str] = set()
    networks: set[ipaddress.IPv4Network] = set()
    for ip, prefix in _interface_cidrs():
        try:
            addr = ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        if addr.is_loopback or not addr.is_private:
            continue
        own.add(str(addr))
        bounded = min(max(prefix, 24), 30)
        try:
            networks.add(ipaddress.IPv4Network(f"{addr}/{bounded}", strict=False))
        except ipaddress.NetmaskValueError:
            continue
    hosts: list[str] = []
    seen: set[str] = set()
    for network in sorted(networks, key=lambda item: (item.network_address, item.prefixlen)):
        for host in network.hosts():
            text = str(host)
            if text in own or text in seen:
                continue
            seen.add(text)
            hosts.append(text)
    return hosts


_discover_lock = threading.Lock()


def _probe_hub(host: str, port: int, timeout: float) -> dict | None:
    """Return /health JSON when this host is running the other side's hub.

    No token is sent: any host on the LAN with this port open answers here.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if sock.connect_ex((host, port)) != 0:
            return None
    except OSError:
        return None
    finally:
        sock.close()
    request = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # A hub from before /health existed answers GET with 501 and
        # "Unsupported method" in the body. Anything else on this port is ignored.
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        if exc.code == 501 and "Unsupported method" in body:
            return {"legacy": True}
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("local_pc") or "") not in ("a", "b"):
        return None
    return data


def discover_peer_url(settings: dict | None = None) -> str:
    """Find the other computer on the LAN when peer_url was never set.

    The other side is only remembered after it calls us. Pressing Mac / PC on
    this computer has to be able to go first, so an empty address is resolved
    by asking who on the local network answers the hub health check as the
    other computer.
    """
    settings = settings or hub_settings()
    known = resolved_peer_url(settings)
    if known:
        return known
    other = "b" if settings["local_pc"] == "a" else "a"
    with _discover_lock:
        known = resolved_peer_url(settings)
        if known:
            return known
        hosts = peer_scan_hosts()
        if not hosts:
            _log("No private network to search for the other computer.")
            return ""
        _log(f"Looking for the other computer on the local network ({len(hosts)} addresses).")
        port = int(settings["listen_port"])
        found = ""
        legacy: list[str] = []
        workers = min(32, len(hosts))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            pending = {
                pool.submit(_probe_hub, host, port, 0.3): host for host in hosts
            }
            for future in as_completed(pending):
                host = pending[future]
                try:
                    data = future.result()
                except Exception:
                    continue
                if not data:
                    continue
                if str(data.get("local_pc") or "") == other:
                    found = host
                    break
                if data.get("legacy"):
                    legacy.append(host)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not found and legacy:
            # An older hub cannot say which side it is; it gets no token unasked.
            _log(
                "Found an older hub that cannot identify itself ("
                + ", ".join(legacy)
                + "). Update it, or set streamdeck_hub.peer_url."
            )
        if not found:
            _log("The other computer did not answer on this network.")
            return ""
        remember_peer_host(found)
        return resolved_peer_url(settings)


def known_wake_mac(which: str) -> str:
    saved = _read_json(WAKE_FILE).get("b" if which == "b" else "a")
    return _normalize_mac(str(saved or ""))


def remember_wake_mac(which: str, mac: str) -> None:
    which = "b" if which == "b" else "a"
    mac = _normalize_mac(mac)
    if not mac:
        return
    with _state_locked():
        data = _read_json(WAKE_FILE)
        if data.get(which) == mac:
            return
        data[which] = mac
        _write_json(WAKE_FILE, data)
    _log(f"Learned wake address for {owner_name(which)}")


def local_lan_mac() -> str:
    """MAC of this computer's LAN adapter, for the other side's wake packet."""
    global _lan_mac_cache
    if _lan_mac_cache and time.monotonic() - _lan_mac_cache[0] < 60:
        return _lan_mac_cache[1]
    mac = _read_lan_mac()
    _lan_mac_cache = (time.monotonic(), mac)
    return mac


def _lan_source_ip() -> str:
    """Our address on the route to the other computer, else on the default route.

    Connecting a UDP socket only picks a route; nothing is sent.
    """
    target = "192.0.2.1"
    host = urllib.parse.urlsplit(resolved_peer_url()).hostname or ""
    try:
        ipaddress.IPv4Address(host)
        target = host
    except ValueError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()


def _read_lan_mac() -> str:
    try:
        source = _lan_source_ip()
    except Exception:
        source = ""
    if source in ("0.0.0.0", "127.0.0.1"):
        source = ""
    if sys.platform == "darwin":
        result = _run_hidden(["ifconfig"], timeout=5)
        current = {"mac": "", "ips": []}
        blocks: list[dict] = []
        for line in (result.stdout or "").splitlines():
            if line and not line[0].isspace():
                if current["mac"] or current["ips"]:
                    blocks.append(current)
                current = {"mac": "", "ips": []}
                continue
            stripped = line.strip()
            if stripped.startswith("ether "):
                current["mac"] = _normalize_mac(stripped.split()[1])
            elif stripped.startswith("inet ") and not stripped.startswith("inet6"):
                ip = stripped.split()[1]
                if ip != "127.0.0.1":
                    current["ips"].append(ip)
        if current["mac"] or current["ips"]:
            blocks.append(current)
        for block in blocks:
            if source and block["mac"] and source in block["ips"]:
                return block["mac"]
        for block in blocks:
            if block["mac"] and block["ips"]:
                return block["mac"]
        return ""
    if source:
        result = _run_hidden(["ipconfig", "/all"], timeout=8)
        own_ip = re.compile(rf"(?<![\d.]){re.escape(source)}(?!\d)")
        for block in re.split(r"\n(?=\S)", result.stdout or ""):
            if not own_ip.search(block):
                continue
            match = _WINDOWS_MAC.search(block)
            mac = _normalize_mac(match.group(0)) if match else ""
            if mac:
                return mac
    result = _run_hidden(["getmac", "/fo", "csv", "/v", "/nh"], timeout=8)
    for line in (result.stdout or "").splitlines():
        if "disconnected" in line.lower() or "hardware not present" in line.lower():
            continue
        for part in line.split(","):
            mac = _normalize_mac(part.strip().strip('"'))
            if mac and not mac.startswith("00:00:00"):
                return mac
    return ""


def build_wol_packet(mac: str) -> bytes:
    mac_bytes = bytes.fromhex(_normalize_mac(mac).replace(":", ""))
    if len(mac_bytes) != 6:
        raise ValueError("wake address must be a 6-byte MAC")
    return b"\xff" * 6 + mac_bytes * 16


def wol_broadcasts(settings: dict | None = None) -> list[str]:
    """Every address that can carry a magic packet from this computer.

    Limited broadcast only leaves through the default interface, and a /22
    LAN's directed broadcast is not x.x.x.255, so each interface's real
    broadcast gets a copy too.
    """
    settings = settings or hub_settings()
    found = ["255.255.255.255"]
    extra = str(settings.get("wol_broadcast") or "").strip()
    if extra:
        try:
            extra = str(ipaddress.IPv4Address(extra))
        except ValueError:
            _log(f"Ignoring streamdeck_hub.wol_broadcast {extra!r}: not an IPv4 address")
        else:
            if extra not in found:
                found.append(extra)
    try:
        cidrs = _interface_cidrs()
    except Exception as exc:
        _log(f"Could not list network interfaces for wake: {exc}")
        cidrs = []
    for ip, prefix in cidrs:
        try:
            interface = ipaddress.IPv4Interface(f"{ip}/{prefix}")
        except ValueError:
            continue
        address = interface.ip
        if address.is_loopback or address.is_link_local or not address.is_private:
            continue
        if interface.network.prefixlen >= 31:
            continue
        broadcast = str(interface.network.broadcast_address)
        if broadcast not in found:
            found.append(broadcast)
    return found


def send_wol(mac: str, settings: dict | None = None) -> None:
    packet = build_wol_packet(mac)
    targets = wol_broadcasts(settings)
    sent = 0
    last_error: OSError | None = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for address in targets:
                for port in (9, 7):
                    try:
                        sock.sendto(packet, (address, port))
                        sent += 1
                    except OSError as exc:
                        last_error = exc
    finally:
        sock.close()
    if not sent and last_error is not None:
        raise last_error


def wake_machine(which: str) -> str:
    """Turn on the Mac (``a``) or the PC (``b``) with a wake-on-LAN packet."""
    which = "b" if str(which).strip().lower() == "b" else "a"
    settings = hub_settings()
    if which == settings["local_pc"]:
        return f"{owner_name(which)} is already on"
    mac = known_wake_mac(which)
    if not mac:
        raise RuntimeError(
            f"No wake address for {owner_name(which)} yet. "
            "Leave that computer on once so this one can learn it."
        )
    send_wol(mac, settings)
    note = f"wake sent to {owner_name(which)}"
    _log(note)
    return note


def peer_status(settings: dict | None = None) -> dict | None:
    """Health of the other computer. None when it does not answer.

    An HTTP error still counts as up: an older build has no /health route.
    """
    settings = settings or hub_settings()
    peer = resolved_peer_url(settings)
    if not peer:
        return None
    request = urllib.request.Request(peer + "/health", method="GET")
    token = settings["token"]
    if token:
        request.add_header("X-Hub-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return {"ok": True}
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(raw) if raw else {"ok": True}
    except json.JSONDecodeError:
        return {"ok": True}
    if not isinstance(data, dict):
        return {"ok": True}
    remember_wake_mac(str(data.get("local_pc") or ""), str(data.get("wol_mac") or ""))
    return data


def _suspend_power_follow() -> None:
    global _suspend_follow, _suspend_follow_until
    _suspend_follow_until = time.time() + _SUSPEND_FOLLOW_MAX
    _suspend_follow = True


def _resume_power_follow() -> None:
    global _suspend_follow
    _suspend_follow = False


def _follow_suspended() -> bool:
    global _suspend_follow
    if _suspend_follow and time.time() >= _suspend_follow_until:
        _suspend_follow = False
        _log("Power follow is back on after the sleep handoff.")
    return _suspend_follow


def note_elgato_deck(connected: bool) -> None:
    """Called by the plugin: whether Elgato shows a Stream Deck right now."""
    try:
        _write_json(ELGATO_FILE, {"connected": bool(connected), "at": time.time()})
    except OSError as exc:
        _log(f"Could not record Elgato state: {exc}")


def elgato_driving() -> bool | None:
    """True when Elgato shows the deck, False when not, None when unknown.

    Unknown means the plugin has not reported lately: Elgato is not running,
    or the plugin is not installed on this computer.
    """
    data = _read_json(ELGATO_FILE)
    try:
        at = float(data.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    if not data or time.time() - at > _ELGATO_STALE:
        return None
    return bool(data.get("connected"))


_watchdog = {"plugin_seen": False, "since": 0.0, "restarts": 0, "next": 0.0}
_auto_claim_blocked_until = 0.0


def _watch_elgato(settings: dict) -> None:
    """Never leave the deck on this computer while Elgato does not show it."""
    global _auto_claim_blocked_until
    driving = elgato_driving()
    if driving is not None:
        _watchdog["plugin_seen"] = True
    # Without a plugin report since this process started, a silent Elgato
    # may simply mean the plugin is not installed here; do nothing then.
    if driving or not _watchdog["plugin_seen"]:
        _watchdog.update(since=0.0, restarts=0, next=0.0)
        return
    now = time.monotonic()
    if now < _watchdog["next"]:
        return
    try:
        held = _find_deck(settings)["in_use_by_me"]
    except Exception:
        _watchdog["next"] = now + 30.0
        return
    if not held:
        _watchdog.update(since=0.0, restarts=0, next=now + 8.0)
        return
    if not _watchdog["since"]:
        _watchdog.update(since=now, next=now + _WATCHDOG_GRACE)
        return
    if _watchdog["restarts"] < _WATCHDOG_RESTARTS:
        _watchdog["restarts"] += 1
        _watchdog["next"] = now + _WATCHDOG_RETRY
        _log(
            "Elgato does not show the Stream Deck this computer holds; "
            f"restarting Elgato (try {_watchdog['restarts']})."
        )
        _restart_streamdeck_soon()
        return
    note = release_local()
    _auto_claim_blocked_until = now + _AUTO_CLAIM_COOLDOWN
    _watchdog.update(since=0.0, restarts=0, next=now + 8.0)
    _log(f"Elgato never showed the Stream Deck; gave it back to the standby screen. {note}")


def claim_if_alone() -> str | None:
    """Take the deck when the other computer is off and we do not hold it."""
    if _follow_suspended():
        return None
    if time.monotonic() < _auto_claim_blocked_until:
        return None
    settings = hub_settings()
    if not settings["enabled"] or not settings["follow_power"]:
        return None
    if not resolved_peer_url(settings):
        return None
    if peer_status(settings) is not None:
        return None
    deck = _find_deck(settings)
    if deck["in_use_by_me"]:
        return None
    note = claim_local()
    write_selected(settings["local_pc"])
    _log(f"Other computer is off; took the Stream Deck. {note}")
    return note


def release_for_power_off() -> str:
    """Give the deck to the computer that is still on, before we sleep."""
    _suspend_power_follow()
    settings = hub_settings()
    if not settings["enabled"] or not settings["follow_power"]:
        return "power follow is off"
    if peer_status(settings) is not None:
        local = release_local()
        _status, peer = _post_peer("/claim", _SLEEP_HANDOFF_WAIT)
        note = f"sleep handoff: {local}; {peer}"
    else:
        note = "sleep handoff: " + release_local()
    _log(note)
    return note


def _follow_loop() -> None:
    misses = 0
    announced = False
    next_discover = 0.0
    claim_delay = 0.0
    next_claim = 0.0
    while True:
        time.sleep(4)
        try:
            settings = hub_settings()
            if not settings["enabled"]:
                continue
            try:
                _watch_elgato(settings)
            except Exception as exc:
                _log(f"Elgato watchdog: {exc}")
            if not settings["follow_power"]:
                continue
            if not resolved_peer_url(settings):
                if time.monotonic() >= next_discover:
                    next_discover = time.monotonic() + 30
                    discover_peer_url(settings)
                if not resolved_peer_url(settings):
                    if not announced:
                        _log("Power follow is waiting until the other computer is found on the network.")
                        announced = True
                    continue
            announced = False
            if _follow_suspended():
                continue
            if peer_status(settings) is not None:
                misses = 0
                claim_delay = 0.0
                next_claim = 0.0
                continue
            misses += 1
            if misses < 3 or time.monotonic() < next_claim:
                continue
            try:
                claim_if_alone()
            except Exception as exc:
                claim_delay = min(_CLAIM_BACKOFF_MAX, max(8.0, claim_delay * 2))
                next_claim = time.monotonic() + claim_delay
                _log(f"Power follow: {exc} (next try in {claim_delay:.0f}s)")
                continue
            claim_delay = 0.0
            next_claim = 0.0
        except Exception as exc:
            _log(f"Power follow: {exc}")


def _after_wake() -> None:
    _resume_power_follow()
    time.sleep(3)
    try:
        note = claim_if_alone()
        if note:
            _log(f"Woke up and took the Stream Deck. {note}")
    except Exception as exc:
        _log(f"Wake claim failed: {exc}")


def _sleep_handoff() -> None:
    """Run the sleep handoff, blocking the power callback as long as the OS waits."""
    done = threading.Event()

    def work() -> None:
        try:
            release_for_power_off()
        except Exception as exc:
            _log(f"Sleep handoff failed: {exc}")
        finally:
            done.set()

    threading.Thread(target=work, name="streamdeck-sleep-handoff", daemon=True).start()
    if not done.wait(_SLEEP_HANDOFF_WAIT):
        _log("Sleep handoff still running when the computer went to sleep.")


def _power_callback_mac(message_type: int) -> None:
    if message_type == _IO_WILL_SLEEP:
        _sleep_handoff()
    elif message_type == _IO_WILL_NOT_SLEEP:
        _resume_power_follow()
    elif message_type == _IO_HAS_POWERED_ON:
        threading.Thread(target=_after_wake, name="streamdeck-wake", daemon=True).start()


def _start_mac_power_hook() -> None:
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    callback_type = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
    )
    root_port = 0

    def _callback(_refcon, _service, message_type, message_argument):
        try:
            _power_callback_mac(int(message_type))
        except Exception as exc:
            _log(f"Power callback failed: {exc}")
        if message_type in (_IO_CAN_SLEEP, _IO_WILL_SLEEP):
            iokit.IOAllowPowerChange(root_port, message_argument)

    callback = callback_type(_callback)
    # Keep the callback alive for the process lifetime.
    globals()["_POWER_CALLBACK"] = callback
    port_ref = ctypes.c_void_p()
    notifier = ctypes.c_uint32()
    iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
    iokit.IORegisterForSystemPower.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        callback_type,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
    iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
    root_port = iokit.IORegisterForSystemPower(
        None, ctypes.byref(port_ref), callback, ctypes.byref(notifier)
    )
    if not root_port:
        _log("Could not register for Mac sleep and wake.")
        return
    source = iokit.IONotificationPortGetRunLoopSource(port_ref)
    core.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    core.CFRunLoopAddSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    mode = core.CFStringCreateWithCString(None, b"kCFRunLoopDefaultMode", 0x08000100)
    core.CFRunLoopAddSource(core.CFRunLoopGetCurrent(), source, mode)
    _log("Watching Mac sleep and wake.")
    core.CFRunLoopRun()


def _start_windows_power_hook() -> None:
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p
    )

    def _callback(_context, type_, _setting):
        try:
            if type_ == _PBT_APMSUSPEND:
                _sleep_handoff()
            elif type_ == _PBT_APMRESUMEAUTOMATIC:
                threading.Thread(target=_after_wake, name="streamdeck-wake", daemon=True).start()
        except Exception as exc:
            _log(f"Power callback failed: {exc}")
        return 0

    callback = callback_type(_callback)

    class _Subscribe(ctypes.Structure):
        _fields_ = [("Callback", callback_type), ("Context", ctypes.c_void_p)]

    params = _Subscribe(callback, None)
    globals()["_POWER_CALLBACK"] = callback
    globals()["_POWER_PARAMS"] = params
    # This API lives in user32, not powrprof. Looking it up on the wrong
    # DLL raises and kills the tray before the hub listener stays up.
    user32 = ctypes.WinDLL("user32")
    try:
        register = user32.RegisterSuspendResumeNotification
    except (AttributeError, OSError):
        _log("RegisterSuspendResumeNotification missing; sleep handoff disabled.")
        return
    register.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    register.restype = ctypes.c_void_p
    handle = register(ctypes.byref(params), 2)
    if not handle:
        _log("Could not register for Windows sleep and wake.")
        return
    globals()["_POWER_HANDLE"] = handle
    _log("Watching Windows sleep and wake.")


def start_power_follow() -> None:
    """Claim the deck when we are the computer that is on."""
    threading.Thread(target=_follow_loop, name="streamdeck-power", daemon=True).start()
    if sys.platform == "darwin":
        threading.Thread(
            target=_start_mac_power_hook, name="streamdeck-sleep", daemon=True
        ).start()
    elif sys.platform == "win32":
        _start_windows_power_hook()


def _post_peer(path: str, timeout: float = 8) -> tuple[str, str]:
    """POST to the other computer. Returns ``(status, message)``.

    ``ok``: it answered 200. ``uncertain``: the request went out but no answer
    came back, so it may still be working on it. ``unreachable``, ``failed``
    and ``not configured`` mean it did not or could not act.
    """
    settings = hub_settings()
    peer = resolved_peer_url(settings)
    if not peer:
        return "not configured", "other computer not configured (streamdeck_hub.peer_url)"
    url = peer + path
    request = urllib.request.Request(url, data=b"", method="POST")
    token = settings["token"]
    if token:
        request.add_header("X-Hub-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(OSError):
            detail = exc.read().decode("utf-8", errors="replace").strip()
        return "failed", f"other computer failed ({exc.code}: {detail or exc.reason})"
    except urllib.error.URLError as exc:
        return "unreachable", f"other computer unreachable ({exc.reason})"
    except TimeoutError:
        return "uncertain", f"other computer timed out after {timeout:.0f}s"
    except (OSError, http.client.HTTPException) as exc:
        return "uncertain", f"other computer dropped the connection ({exc!r})"
    return "ok", body or "ok"


def _await_peer_claim(settings: dict, note: str) -> tuple[str, str]:
    """After an unanswered /claim, watch VirtualHere for who ends up with the deck."""
    deadline = time.monotonic() + _HANDOFF_SETTLE
    while True:
        deck = None
        try:
            deck = _find_deck(settings)
        except Exception as exc:
            _log(f"Handoff check failed: {exc}")
        if deck and deck["in_use_by_me"]:
            return "here", f"{note}; the deck is still on this computer"
        if deck and deck["in_use"]:
            return "ok", f"{note}; it has the deck now"
        if time.monotonic() >= deadline:
            return "failed", f"{note}; nobody took the deck"
        time.sleep(2)


SELECTION_FILE = Path(__file__).resolve().parent / "deck_selection.json"


def read_selected() -> str | None:
    owner = str(_read_json(SELECTION_FILE).get("owner") or "")
    return owner if owner in ("a", "b") else None


def write_selected(owner: str) -> None:
    which = "b" if owner == "b" else "a"
    _write_json(SELECTION_FILE, {"owner": which})


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

    Monitor inputs are not touched. The chosen side is saved only after the
    handoff is accepted. A failed press leaves the key on the computer that
    still has the deck. If the other computer goes quiet mid-claim, the deck
    is only taken back once VirtualHere shows that nobody has it.
    """
    settings = hub_settings()
    if not settings["enabled"]:
        raise RuntimeError("Stream Deck hub is off in config.json")
    previous = displayed_owner()
    if target not in ("a", "b"):
        target = "b" if previous == "a" else "a"
    sending_away = target != settings["local_pc"]
    if sending_away and not resolved_peer_url(settings) and not discover_peer_url(settings):
        raise RuntimeError(
            "Could not find the other computer on this network. "
            "Open Stream Deck there, or set streamdeck_hub.peer_url."
        )
    try:
        if sending_away:
            local = release_local()
            status, peer = _post_peer("/claim", _PEER_CLAIM_TIMEOUT)
            if status == "uncertain":
                _log(f"{peer}; checking who has the deck")
                status, peer = _await_peer_claim(settings, peer)
            if status == "here":
                raise RuntimeError(peer)
            if status != "ok":
                try:
                    claim_local()
                except Exception as exc:
                    _log(f"Could not take the deck back: {exc}")
                raise RuntimeError(peer)
        else:
            _status, peer = _post_peer("/release", _PEER_RELEASE_TIMEOUT)
            local = claim_local()
    except Exception:
        write_selected(previous)
        raise
    write_selected(target)
    note = f"{owner_name(target)}: {local}; {peer}"
    _log(note)
    return note


class _HubHandler(BaseHTTPRequestHandler):
    timeout = 5

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health" and not self._token_ok():
            # Discovery asks which side we are before it sends the token.
            identity = {"ok": True, "local_pc": hub_settings()["local_pc"]}
            self._reply(200, json.dumps(identity))
            return
        if not self._authorized():
            return
        if path != "/health":
            self._reply(404, "not found")
            return
        self._note_caller()
        settings = hub_settings()
        body = json.dumps(
            {
                "ok": True,
                "local_pc": settings["local_pc"],
                "wol_mac": local_lan_mac(),
            }
        )
        self._reply(200, body)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        body = self._read_body()
        if body is None:
            return
        self._note_caller()
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/claim":
                message = claim_local()
                write_selected(hub_settings()["local_pc"])
            elif path == "/release":
                message = release_local()
            elif path == "/layout":
                message = _apply_hub_layout(body)
            else:
                self._reply(404, "not found")
                return
        except Exception as exc:
            _log(f"{path} failed: {exc}")
            self._reply(500, str(exc))
            return
        _log(f"{path} -> {message}")
        self._reply(200, message)

    def _token_ok(self) -> bool:
        settings = hub_settings()
        token = settings["token"]
        got = self.headers.get("X-Hub-Token", "")
        return not token or hmac.compare_digest(got, token)

    def _authorized(self) -> bool:
        if not self._token_ok():
            self._reply(403, "forbidden")
            return False
        return True

    def _read_body(self) -> bytes | None:
        """Request body, or None after replying 400/413 to a bad one."""
        raw = (self.headers.get("Content-Length") or "").strip()
        if not raw:
            return b""
        try:
            length = int(raw)
        except ValueError:
            length = -1
        if length < 0:
            self._reply(400, "bad Content-Length")
            return None
        if length > _MAX_BODY:
            self._reply(413, "request body too large")
            return None
        if length == 0:
            return b""
        try:
            return self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return None

    def _note_caller(self) -> None:
        try:
            host = self.client_address[0]
        except (IndexError, TypeError):
            return
        remember_peer_host(str(host))

    def _reply(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        _log(fmt % args)


def _apply_hub_layout(body: bytes) -> str:
    name = "office"
    if body:
        raw = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("name"):
            name = str(payload["name"]).strip() or "office"
    from config_store import load_config
    from ddc_monitor import apply_named_layout

    lines = apply_named_layout(load_config(), name)
    return "; ".join(lines) or f"layout {name} applied"


class _HubServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets a second process bind the same port.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


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
            server = _HubServer(
                (settings["listen_host"], settings["listen_port"]),
                _HubHandler,
            )
        except OSError as exc:
            _log(f"Hub listener not started ({exc}). Another process has the port.")
            return
        _started = True
        threading.Thread(target=server.serve_forever, name="streamdeck-hub", daemon=True).start()
        try:
            start_power_follow()
        except Exception as exc:
            _log(f"Power follow not started: {exc}")
        _log(
            f"Hub listener on {settings['listen_host']}:{settings['listen_port']} "
            f"(this computer is {settings['label_a'] if settings['local_pc'] == 'a' else settings['label_b']})"
        )


def main() -> int:
    """`python3 streamdeck_hub.py claim|release|status` for a manual check."""
    command = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    target = sys.argv[2] if len(sys.argv) > 2 else ""
    if command == "claim":
        print(claim_local())
        _wait_for_restart(30)
    elif command == "release":
        print(release_local())
    elif command == "wake":
        print(wake_machine(target or "b"))
    elif command == "status":
        settings = hub_settings()
        shown = dict(settings, token="***" if settings["token"] else "")
        print(json.dumps({
            "settings": shown,
            "peer_url": resolved_peer_url(settings),
            "wake": {"a": known_wake_mac("a"), "b": known_wake_mac("b")},
            "devices": parse_list(_run_client("LIST", settings)),
        }, indent=2))
    else:
        print("Use claim, release, wake, or status.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
