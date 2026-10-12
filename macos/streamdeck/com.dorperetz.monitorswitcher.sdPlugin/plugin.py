#!/usr/bin/env python3
"""Stream Deck plugin for Monitor Input Switcher.

PC Switch only changes monitor inputs. Mac / PC is a separate key: it hands
this Stream Deck to the other computer and leaves the monitors alone.
"""

from __future__ import annotations

import argparse
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
    read_active_pc,
    read_last_input,
    resolve_monitor,
    toggle_input,
    toggle_pc_group,
)
from sdclient import StreamDeckClient  # noqa: E402

ACTION_PC_SWITCH = "com.dorperetz.monitorswitcher.pcswitch"
ACTION_INPUT = "com.dorperetz.monitorswitcher.input"
ACTION_DECK = "com.dorperetz.monitorswitcher.deck"

LOG_FILE = APP_DIR / "streamdeck.log"
REFRESH_SECONDS = 1.5
# Enumeration shells out to m1ddc, so keep it off the refresh path.
MONITOR_CACHE_SECONDS = 30.0


def log(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


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
    bindings = toggle_bindings(config)
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


class Plugin:
    def __init__(self, client: StreamDeckClient) -> None:
        self.client = client
        self.monitors = MonitorCache()
        self.contexts: dict[str, dict] = {}
        self.signatures: dict[str, str] = {}
        self.jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------- #

    def start_workers(self) -> None:
        threading.Thread(target=self._worker_loop, daemon=True).start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # -- incoming events -------------------------------------------------- #

    def on_message(self, message: dict) -> None:
        event = message.get("event")
        context = message.get("context")
        action = message.get("action")
        payload = message.get("payload") or {}

        if event in ("willAppear", "didReceiveSettings"):
            if not context:
                return
            self.contexts[context] = {
                "action": action or self.contexts.get(context, {}).get("action"),
                "settings": payload.get("settings") or {},
            }
            self.signatures.pop(context, None)
            self.refresh_context(context)
            return

        if event == "willDisappear":
            self.contexts.pop(context, None)
            self.signatures.pop(context, None)
            return

        if event == "keyDown":
            if context:
                self.jobs.put((context, payload.get("settings") or {}))
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
        from streamdeck_hub import current_owner, owner_name

        try:
            owner = current_owner()
        except Exception as exc:
            log(f"Deck status failed: {exc}")
            return 0, "Deck"
        return (0 if owner == "a" else 1), owner_name(owner)

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

        label = binding.get("device") or binding.get("monitor") or "monitor"
        last = read_last_input(state_key) if state_key else None
        if last is None:
            return 0, f"{label}\n\u2014"

        try:
            input_b = parse_input_value(binding.get("input_b"))
        except (TypeError, ValueError):
            input_b = None
        state = 1 if input_b is not None and last == input_b else 0
        return state, f"{label}\n{input_name(last)}"

    def refresh_context(self, context: str) -> None:
        entry = self.contexts.get(context)
        if not entry:
            return
        try:
            if entry["action"] == ACTION_PC_SWITCH:
                state, title = self._pc_switch_view()
            elif entry["action"] == ACTION_DECK:
                state, title = self._deck_view()
            else:
                state, title = self._input_view(entry["settings"])
        except (DDCError, OSError, ValueError) as exc:
            state, title = 0, "Error"
            log(f"Refresh failed: {exc}")

        signature = f"{state}|{title}"
        if self.signatures.get(context) == signature:
            return
        self.signatures[context] = signature
        self.client.send("setState", context=context, payload={"state": state})
        self.client.send("setTitle", context=context, payload={"title": title, "target": 0})

    def refresh_all(self) -> None:
        for context in list(self.contexts):
            self.refresh_context(context)

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            try:
                self.refresh_all()
            except Exception as exc:  # keep the key display alive no matter what
                log(f"Refresh loop error: {exc}")

    # -- switching -------------------------------------------------------- #

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                context, settings = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            entry = self.contexts.get(context)
            if not entry:
                continue
            try:
                if entry["action"] == ACTION_PC_SWITCH:
                    self._do_pc_switch()
                elif entry["action"] == ACTION_DECK:
                    self._do_deck_switch()
                else:
                    self._do_input_toggle(settings or entry["settings"])
                self.client.send("showOk", context=context)
            except Exception as exc:
                log(f"Switch failed: {exc}")
                self.client.send("showAlert", context=context)
            finally:
                self.monitors.invalidate()
                self.signatures.pop(context, None)
                self.refresh_all()

    def _do_deck_switch(self) -> None:
        from streamdeck_hub import toggle_deck_owner

        note = toggle_deck_owner()
        log(f"Deck switch -> {note}")

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
        previous, new = toggle_input(monitor.handle, input_a, input_b, monitor.device_name)
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
