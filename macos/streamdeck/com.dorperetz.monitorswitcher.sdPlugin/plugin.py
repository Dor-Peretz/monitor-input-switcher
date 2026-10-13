#!/usr/bin/env python3
"""Stream Deck plugin for Monitor Input Switcher.

PC Switch only changes monitor inputs. Mac / PC is a separate key: it hands
this Stream Deck to the other computer and leaves the monitors alone.
"""

from __future__ import annotations

import argparse
import base64
import json
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
# Mac helpers live in macos/. Windows helpers live in the repo root, two
# levels above macos/streamdeck/<this plugin>.
APP_DIR = PLUGIN_DIR.parents[2] if sys.platform == "win32" else PLUGIN_DIR.parents[1]
sys.path.insert(0, str(APP_DIR))

from config_store import load_config  # noqa: E402
from ddc_monitor import (  # noqa: E402
    DDCError,
    enumerate_monitors,
    input_name,
    parse_input_value,
    pc_switch_headline,
    read_active_pc,
    read_last_input,
    resolve_monitor,
    toggle_input,
    toggle_pc_group,
)
from sdclient import StreamDeckClient  # noqa: E402
from switch_progress import (  # noqa: E402
    begin_progress,
    end_progress,
    other_pc_name,
    read_progress,
    switch_title,
)

ACTION_PC_SWITCH = "com.dorperetz.monitorswitcher.pcswitch"
ACTION_INPUT = "com.dorperetz.monitorswitcher.input"
ACTION_DECK = "com.dorperetz.monitorswitcher.deck"
ACTION_WAKE = "com.dorperetz.monitorswitcher.wake"

LOG_FILE = APP_DIR / "streamdeck.log"
REFRESH_SECONDS = 1.5
# Must stay well under streamdeck_hub._ELGATO_STALE.
ELGATO_NOTE_SECONDS = 5.0
# Stream Deck Mobile (3) and the virtual Stream Deck (11) are not the USB deck.
_SOFTWARE_DECK_TYPES = {3, 11}
# Enumeration shells out to m1ddc, so keep it off the refresh path.
MONITOR_CACHE_SECONDS = 30.0


def log(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def is_hardware_deck(info: object) -> bool:
    """A physical Stream Deck in Elgato's device info, not Mobile or virtual."""
    if not isinstance(info, dict):
        return False
    try:
        kind = int(info.get("type", 0))
    except (TypeError, ValueError):
        kind = 0
    return kind not in _SOFTWARE_DECK_TYPES


class MonitorCache:
    """Cached monitor enumeration, refreshed on a TTL or after a switch."""

    def __init__(self) -> None:
        self._monitors: list = []
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> list:
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < MONITOR_CACHE_SECONDS
            if self._monitors and fresh and not force:
                return self._monitors
        monitors = enumerate_monitors()
        with self._lock:
            self._monitors = monitors
            self._fetched_at = time.monotonic()
        return monitors

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_at = 0.0


def binding_label(binding: dict) -> str:
    name = binding.get("device") or binding.get("monitor") or "monitor"
    try:
        input_a = input_name(parse_input_value(binding.get("input_a")))
        input_b = input_name(parse_input_value(binding.get("input_b")))
    except (TypeError, ValueError):
        return name
    return f"{name} ({input_a} \u21c4 {input_b})"


def binding_id(binding: dict) -> str:
    return (binding.get("device") or binding.get("monitor") or "").strip()


def toggle_bindings(config: dict) -> list[dict]:
    """Monitors a Toggle key can flip.

    Per-monitor hotkey rows come first. A display that only lives on the PC
    Switch tab (the second settings screen) is included too, so it can have
    its own toggle key without a separate hotkey.
    """
    seen: set[str] = set()
    bindings: list[dict] = []

    def add(binding: dict) -> None:
        ident = binding_id(binding).lower()
        if not ident or ident in seen:
            return
        seen.add(ident)
        bindings.append(binding)

    for binding in config.get("monitor_bindings", []):
        if binding.get("enabled", True):
            add(binding)

    for binding in config.get("pc_switch", {}).get("monitors", []):
        if binding.get("enabled", True):
            add(binding)

    return bindings


def find_binding(config: dict, target: str) -> dict | None:
    return lookup_binding(toggle_bindings(config), target)


def lookup_binding(bindings: list[dict], target: str) -> dict | None:
    target = (target or "").strip().lower()
    if not target:
        return bindings[0] if bindings else None
    for binding in bindings:
        if binding_id(binding).lower() == target:
            return binding
    for binding in bindings:
        if (binding.get("monitor") or "").strip().lower() == target:
            return binding
    return None


# Mac is always the blue key. The other computer is always the green key.
# State 0 is Mac, state 1 is the PC. Both states are painted with that same
# image so a press cannot flip the color.
_POSITION_RANK = {"only": 0, "left": 0, "center": 1, "right": 2}
_IMAGE_URIS: dict[str, str] = {}


def image_uri(name: str) -> str:
    cached = _IMAGE_URIS.get(name)
    if cached is not None:
        return cached
    raw = (PLUGIN_DIR / "imgs" / f"{name}.png").read_bytes()
    uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    _IMAGE_URIS[name] = uri
    return uri


def image_for(action: str, state: int) -> str:
    mac = state == 0
    if action in (ACTION_DECK, ACTION_WAKE):
        return "deck-mac" if mac else "deck-pc"
    if action == ACTION_PC_SWITCH:
        return "pc-a" if mac else "pc-b"
    return "input-a" if mac else "input-b"


def spatial_bindings(bindings: list[dict]) -> list[dict]:
    """Left-to-right desk order, using the positions saved in config."""

    def sort_key(binding: dict) -> tuple:
        position = (binding.get("monitor") or "").strip().lower()
        return (_POSITION_RANK.get(position, 9), binding_id(binding).lower())

    return sorted(bindings, key=sort_key)


def reconcile_monitor_targets(
    keys: list[dict],
    bindings: list[dict],
) -> list[tuple[dict, str]]:
    """Point mirrored monitor keys at the screen on the same side.

    ``keys`` items have ``column``, ``row``, and ``target``. Left-to-right on
    the Stream Deck follows left-to-right on the desk. A pairing that already
    matches is left alone. A mirrored pairing, or several keys aimed at one
    monitor, is rewritten.
    """
    placed = [
        key
        for key in keys
        if key.get("column") is not None and key.get("row") is not None
    ]
    ordered = spatial_bindings(bindings)
    if len(placed) < 2 or len(ordered) < 2:
        return []

    placed.sort(key=lambda key: (key["column"], key["row"], key.get("target") or ""))
    count = min(len(placed), len(ordered))
    placed = placed[:count]
    ordered = ordered[:count]
    desired = [binding_id(binding) for binding in ordered]
    current = []
    for key in placed:
        found = lookup_binding(bindings, key.get("target") or "")
        current.append(binding_id(found) if found else "")

    mirrored = current == list(reversed(desired))
    collapsed = any(not item for item in current) or len(set(current)) < len(current)
    if current == desired or not (mirrored or collapsed):
        return []
    return [(key, target) for key, target in zip(placed, desired) if key.get("target") != target]


class Plugin:
    def __init__(self, client: StreamDeckClient) -> None:
        self.client = client
        self.monitors = MonitorCache()
        self.contexts: dict[str, dict] = {}
        self.signatures: dict[str, str] = {}
        self.jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._stop = threading.Event()
        self._paint_lock = threading.RLock()
        # Keys currently showing the in-progress timer. Refresh must not
        # paint the settled title over them.
        self._elgato_devices: set[str] = set()
        self._elgato_noted_at = 0.0
        self._progress_contexts: set[str] = set()
        self._progress_imaged: set[str] = set()
        self._progress_state: dict[str, int] = {}
        # Keys with a press queued or running. Another press on the same key
        # before that finishes is dropped.
        self._queued: set[str] = set()
        self._queued_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------- #

    def start_workers(self) -> None:
        threading.Thread(target=self._worker_loop, daemon=True).start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        threading.Thread(
            target=self._progress_loop, name="switch-timer", daemon=True
        ).start()

    def stop(self) -> None:
        self._stop.set()

    # -- incoming events -------------------------------------------------- #

    def on_message(self, message: dict) -> None:
        event = message.get("event")
        context = message.get("context")
        action = message.get("action")
        payload = message.get("payload") or {}
        if event in ("deviceDidConnect", "deviceDidDisconnect"):
            device = str(message.get("device") or "")
            if event == "deviceDidConnect" and is_hardware_deck(message.get("deviceInfo")):
                self._elgato_devices.add(device)
            else:
                self._elgato_devices.discard(device)
            log(f"Elgato {event[6:]}: {len(self._elgato_devices)} deck(s)")
            self._note_elgato(force=True)
            return
        if event == "keyDown":
            if context:
                self._remember_key(context, action, payload)
                with self._queued_lock:
                    if context in self._queued:
                        log("Ignored repeat press while that key is still switching")
                        return
                    self._queued.add(context)
                self.jobs.put((context, payload.get("settings") or {}))
            return

        if event in ("willAppear", "didReceiveSettings"):
            if not context:
                return
            self._remember_key(context, action, payload)
            self.signatures.pop(context, None)
            self._reconcile_monitor_keys()
            self.refresh_context(context)
            return

        if event == "willDisappear":
            self.contexts.pop(context, None)
            self.signatures.pop(context, None)
            return

        if event == "sendToPlugin":
            if payload.get("event") == "getTargets" and context:
                self.send_targets(context)
            return

    def send_targets(self, context: str) -> None:
        config = load_config()
        targets = [
            {"id": binding_id(binding), "label": binding_label(binding)}
            for binding in toggle_bindings(config)
        ]
        self.client.send(
            "sendToPropertyInspector",
            context=context,
            payload={"event": "targets", "targets": targets},
        )

    # -- key rendering ---------------------------------------------------- #

    def _deck_view(self) -> tuple[int, str]:
        from streamdeck_hub import displayed_owner, owner_name

        try:
            owner = displayed_owner()
        except Exception as exc:
            log(f"Deck status failed: {exc}")
            return 0, "Deck"
        return (0 if owner == "a" else 1), owner_name(owner)

    def _wake_view(self, settings: dict) -> tuple[int, str]:
        from streamdeck_hub import owner_name

        target = str((settings or {}).get("target") or "a").strip().lower()
        if target not in ("a", "b"):
            target = "a"
        state = 0 if target == "a" else 1
        return state, f"Turn on\n{owner_name(target)}"

    def _pc_switch_view(self) -> tuple[int, str]:
        config = load_config()
        pc_switch = config.get("pc_switch", {})
        name_a = pc_switch.get("pc_a_name", "PC 1")
        name_b = pc_switch.get("pc_b_name", "PC 2")
        active = read_active_pc()
        return (0, name_a) if active == "a" else (1, name_b)

    def _input_view(self, settings: dict) -> tuple[int, str]:
        config = load_config()
        binding = find_binding(config, settings.get("target", ""))
        if binding is None:
            return 0, "Not set"

        state_key = binding.get("device") or ""
        if not state_key:
            monitor = resolve_monitor(self.monitors.get(), binding)
            state_key = monitor.device_name if monitor else ""

        position = (binding.get("monitor") or "").strip()
        label = position.capitalize() if position else (binding.get("device") or "monitor")
        last = read_last_input(state_key) if state_key else None
        if last is None:
            return 0, f"{label}\n\u2014"

        try:
            input_b = parse_input_value(binding.get("input_b"))
        except (TypeError, ValueError):
            input_b = None
        # input A belongs to the Mac (blue). input B belongs to the PC (green).
        state = 1 if input_b is not None and last == input_b else 0
        return state, f"{label}\n{input_name(last)}"

    def refresh_context(self, context: str) -> None:
        # The view can shell out to m1ddc, so it is built outside the paint
        # lock. The timer check is repeated under the lock before painting.
        entry = self.contexts.get(context)
        if not entry or context in self._progress_contexts:
            return
        state, title = self._view_for(entry)
        action = entry.get("action") or ACTION_INPUT
        image = image_for(action, state)
        signature = f"{state}|{title}|{image}"
        with self._paint_lock:
            if context in self._progress_contexts or context not in self.contexts:
                return
            if self.signatures.get(context) == signature:
                return
            self.signatures[context] = signature
            self._send_key_face(context, state, title, action)

    def _view_for(self, entry: dict) -> tuple[int, str]:
        try:
            if entry["action"] == ACTION_PC_SWITCH:
                state, title = self._pc_switch_view()
            elif entry["action"] == ACTION_DECK:
                state, title = self._deck_view()
            elif entry["action"] == ACTION_WAKE:
                state, title = self._wake_view(entry["settings"])
            else:
                state, title = self._input_view(entry["settings"])
        except (DDCError, OSError, ValueError) as exc:
            state, title = 0, "Error"
            log(f"Refresh failed: {exc}")
        return state, title

    def refresh_all(self) -> None:
        for context in list(self.contexts):
            self.refresh_context(context)

    def _progress_loop(self) -> None:
        """Tick the on-key timer until the switch that published it finishes."""
        while not self._stop.wait(0.5):
            try:
                self._paint_progress()
            except Exception as exc:  # keep the countdown alive no matter what
                log(f"Switch timer error: {exc}")

    def _paint_progress(self) -> None:
        with self._paint_lock:
            progress = read_progress()
            if not progress:
                stale = self._clear_progress_faces()
            else:
                contexts = self._contexts_for_progress(progress)
                title = switch_title(
                    str(progress.get("target") or ""),
                    int(progress.get("elapsed") or 0),
                )
                seen = set(contexts)
                stale = [item for item in self._progress_contexts if item not in seen]
                self._progress_contexts = seen
                for context in stale:
                    self._forget_progress_face(context)
                for context in contexts:
                    self._paint_progress_key(context, title)
        for context in stale:
            self.refresh_context(context)

    def _clear_progress_faces(self) -> list[str]:
        """Drop every timer face. Returns the keys that need a settled face."""
        stale = list(self._progress_contexts)
        self._progress_contexts = set()
        self._progress_imaged.clear()
        self._progress_state.clear()
        for context in stale:
            self.signatures.pop(context, None)
        return stale

    def _forget_progress_face(self, context: str) -> None:
        self._progress_imaged.discard(context)
        self._progress_state.pop(context, None)
        self.signatures.pop(context, None)

    def _contexts_for_progress(self, progress: dict) -> list[str]:
        kind = str(progress.get("kind") or "")
        origin = str(progress.get("origin") or "")
        if kind == "input":
            return [origin] if origin in self.contexts else []
        if kind == "deck":
            actions = {ACTION_DECK}
        elif kind == "monitors":
            actions = {ACTION_PC_SWITCH, ACTION_INPUT}
        else:
            return []
        return [
            context
            for context, entry in self.contexts.items()
            if entry.get("action") in actions
        ]

    def _paint_progress_key(self, context: str, title: str) -> None:
        entry = self.contexts.get(context)
        if not entry:
            return
        action = entry.get("action") or ACTION_INPUT
        if context not in self._progress_state:
            self._progress_state[context] = self._shown_state(context)
        state = self._progress_state[context]
        signature = f"busy|{title}|{state}|{action}"
        if self.signatures.get(context) == signature:
            return
        if context not in self._progress_imaged:
            self._send_key_face(context, state, title, action)
            self._progress_imaged.add(context)
        else:
            self._send_titles(context, title)
        self.signatures[context] = signature

    def _shown_state(self, context: str) -> int:
        signature = self.signatures.get(context) or ""
        if signature.startswith("busy|"):
            state = signature.rsplit("|", 2)
            if len(state) == 3 and state[1].isdigit():
                return int(state[1])
        head = signature.split("|", 1)[0]
        if head.isdigit():
            return int(head)
        return 0

    def _start_progress(self, context: str, action: str, settings: dict) -> str:
        try:
            target, kind = self._progress_label(action, settings)
        except Exception as exc:
            log(f"Switch label failed: {exc}")
            target = ""
            kind = "deck" if action == ACTION_DECK else "monitors"
        origin = context if kind == "input" else ""
        token = begin_progress(target, kind, origin=origin)
        log(f"Switching to {target or kind}")
        try:
            self._paint_progress()
        except Exception as exc:
            log(f"Switch timer failed: {exc}")
        return token

    def _progress_label(self, action: str, settings: dict) -> tuple[str, str]:
        if action == ACTION_DECK:
            from streamdeck_hub import displayed_owner, owner_name

            previous = displayed_owner()
            target = "b" if previous == "a" else "a"
            return owner_name(target), "deck"
        if action == ACTION_PC_SWITCH:
            return other_pc_name(), "monitors"
        return self._input_target_name(settings), "input"

    def _input_target_name(self, settings: dict) -> str:
        """Input A is the Mac (blue). Input B is the PC (green)."""
        config = load_config()
        binding = find_binding(config, settings.get("target", ""))
        if binding is None:
            return ""
        pc_switch = config.get("pc_switch", {}) or {}
        name_a = str(pc_switch.get("pc_a_name") or "Mac")
        name_b = str(pc_switch.get("pc_b_name") or "PC")
        state_key = binding.get("device") or ""
        last = read_last_input(state_key) if state_key else None
        try:
            input_a = parse_input_value(binding.get("input_a"))
        except (TypeError, ValueError):
            input_a = None
        # toggle_input moves A -> B, B -> A, and an unknown input to B.
        if last is None or (input_a is not None and last == input_a):
            return name_b
        return name_a

    def _finish_progress(self, token: str) -> None:
        end_progress(token)
        self._paint_progress()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            self._note_elgato()
            try:
                self.refresh_all()
            except Exception as exc:  # keep the key display alive no matter what
                log(f"Refresh loop error: {exc}")

    def load_devices(self, info: str) -> None:
        """Decks Elgato already showed when it started this plugin (``-info``)."""
        try:
            devices = json.loads(info or "{}").get("devices") or []
        except (ValueError, AttributeError):
            devices = []
        for device in devices:
            if isinstance(device, dict) and is_hardware_deck(device):
                self._elgato_devices.add(str(device.get("id") or ""))

    def _note_elgato(self, force: bool = False) -> None:
        """Tell the hub whether Elgato shows a deck; its watchdog acts on this."""
        now = time.monotonic()
        if not force and now - self._elgato_noted_at < ELGATO_NOTE_SECONDS:
            return
        self._elgato_noted_at = now
        try:
            from streamdeck_hub import note_elgato_deck

            note_elgato_deck(bool(self._elgato_devices))
        except Exception as exc:
            log(f"Could not record Elgato devices: {exc}")

    # -- switching -------------------------------------------------------- #

    def _worker_loop(self) -> None:
        # One worker thread, so this plugin never runs two switches at once.
        while not self._stop.is_set():
            try:
                context, settings = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                ran = self._run_job(context, settings)
            finally:
                with self._queued_lock:
                    self._queued.discard(context)
            if not ran:
                continue
            try:
                self.monitors.invalidate()
                with self._paint_lock:
                    if context not in self._progress_contexts:
                        self.signatures.pop(context, None)
                self.refresh_all()
            except Exception as exc:  # keep the worker alive no matter what
                log(f"Refresh after switch failed: {exc}")

    def _run_job(self, context: str, settings: dict) -> bool:
        entry = self.contexts.get(context)
        if not entry:
            return False
        action = entry.get("action")
        settings = settings or entry.get("settings") or {}
        token = ""
        if action in (ACTION_PC_SWITCH, ACTION_DECK, ACTION_INPUT):
            try:
                token = self._start_progress(context, action, settings)
            except Exception as exc:
                log(f"Switch timer failed: {exc}")
        try:
            if action == ACTION_PC_SWITCH:
                self._do_pc_switch()
            elif action == ACTION_DECK:
                self._do_deck_switch()
            elif action == ACTION_WAKE:
                self._do_wake(settings)
            else:
                self._do_input_toggle(settings)
            self.client.send("showOk", context=context)
        except Exception as exc:
            log(f"Switch failed: {exc}")
            self.client.send("showAlert", context=context)
        finally:
            if token:
                try:
                    self._finish_progress(token)
                except Exception as exc:
                    log(f"Switch timer failed: {exc}")
        return True

    def _remember_key(self, context: str | None, action: str | None, payload: dict) -> None:
        if not context:
            return
        previous = self.contexts.get(context, {})
        coords = payload.get("coordinates") or {}
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            settings = previous.get("settings") or {}
        self.contexts[context] = {
            "action": action or previous.get("action"),
            "settings": settings,
            "column": coords.get("column", previous.get("column")),
            "row": coords.get("row", previous.get("row")),
        }

    def _reconcile_monitor_keys(self) -> None:
        """If the two monitor keys are swapped, aim each one at its own screen."""
        config = load_config()
        bindings = toggle_bindings(config)
        keys = []
        for context, entry in self.contexts.items():
            if entry.get("action") != ACTION_INPUT:
                continue
            settings = entry.get("settings") or {}
            keys.append(
                {
                    "context": context,
                    "column": entry.get("column"),
                    "row": entry.get("row"),
                    "target": settings.get("target") or "",
                }
            )
        for key, target in reconcile_monitor_targets(keys, bindings):
            context = key["context"]
            entry = self.contexts.get(context)
            if not entry:
                continue
            settings = dict(entry.get("settings") or {})
            settings["target"] = target
            entry["settings"] = settings
            self.signatures.pop(context, None)
            self.client.send("setSettings", context=context, payload=settings)
            log(f"Monitor key -> {target}")

    def _send_key_face(self, context: str, state: int, title: str, action: str) -> None:
        """Paint Mac blue and PC green on both states, so a press cannot flip it."""
        uri = image_uri(image_for(action, state))
        for face in (0, 1):
            for target in (0, 2):
                self.client.send(
                    "setImage",
                    context=context,
                    payload={"image": uri, "target": target, "state": face},
                )
                self.client.send(
                    "setTitle",
                    context=context,
                    payload={"title": title, "target": target, "state": face},
                )
        self.client.send("setState", context=context, payload={"state": state})

    def _send_titles(self, context: str, title: str) -> None:
        """Update the timer text without resending the key image."""
        for face in (0, 1):
            for target in (0, 2):
                self.client.send(
                    "setTitle",
                    context=context,
                    payload={"title": title, "target": target, "state": face},
                )

    def _do_deck_switch(self) -> None:
        from streamdeck_hub import displayed_owner, toggle_deck_owner

        previous = displayed_owner()
        target = "b" if previous == "a" else "a"
        # The key keeps its current icon and shows the timer until this
        # returns. Refresh after the handoff paints whoever actually has it.
        note = toggle_deck_owner(target)
        log(f"Deck switch -> {note}")

    def _do_wake(self, settings: dict) -> None:
        from streamdeck_hub import wake_machine

        target = str((settings or {}).get("target") or "a").strip().lower()
        note = wake_machine("b" if target == "b" else "a")
        log(f"Wake -> {note}")

    def _do_pc_switch(self) -> None:
        config = load_config()
        pc_switch = config.get("pc_switch", {})
        entries = [
            entry
            for entry in pc_switch.get("monitors", [])
            if entry.get("enabled", True)
        ]
        if not entries:
            raise ValueError("No PC-switch monitors configured.")
        target_name, lines = toggle_pc_group(
            self.monitors.get(force=True),
            entries,
            pc_switch.get("pc_a_name", "PC 1"),
            pc_switch.get("pc_b_name", "PC 2"),
        )
        if not lines:
            raise ValueError("None of the configured monitors are connected.")
        log(f"PC switch -> {target_name}")
        for line in lines:
            log("  " + line)
        ok, headline = pc_switch_headline(target_name, lines)
        if not ok:
            raise ValueError(headline)

    def _do_input_toggle(self, settings: dict) -> None:
        config = load_config()
        binding = find_binding(config, settings.get("target", ""))
        if binding is None:
            raise ValueError("No monitor selected for this key.")

        monitor = resolve_monitor(self.monitors.get(force=True), binding)
        if monitor is None:
            raise ValueError(f"Monitor '{binding_id(binding)}' not connected.")
        if monitor.handle is None or not monitor.controllable:
            raise ValueError(f"Monitor '{monitor.description}' is not DDC/CI controllable.")

        input_a = parse_input_value(binding.get("input_a"))
        input_b = parse_input_value(binding.get("input_b"))
        # Windows toggle_input takes MonitorInfo; macOS takes the DDC handle.
        target = monitor if sys.platform == "win32" else monitor.handle
        previous, new = toggle_input(target, input_a, input_b, monitor.device_name)
        previous_label = input_name(previous) if previous >= 0 else "unknown"
        log(f"[{monitor.position}] {monitor.description}: {previous_label} -> {input_name(new)}")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-port", dest="port", type=int, required=True)
    parser.add_argument("-pluginUUID", dest="plugin_uuid", required=True)
    parser.add_argument("-registerEvent", dest="register_event", required=True)
    parser.add_argument("-info", dest="info", default="")
    args, _ = parser.parse_known_args()

    client = StreamDeckClient(args.port, args.plugin_uuid, args.register_event)
    plugin = Plugin(client)
    plugin.load_devices(args.info)
    try:
        client.connect()
        plugin.start_workers()
        from streamdeck_hub import ensure_hub_listener

        ensure_hub_listener()
        client.run(plugin.on_message)
    except Exception:
        log("Fatal error:\n" + traceback.format_exc())
        return 1
    finally:
        plugin.stop()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
