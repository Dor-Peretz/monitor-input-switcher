"""Global hotkey listener for monitor input switching (macOS build).

Uses a raw pynput keyboard.Listener with a hand-rolled hotkey matcher (see
_HotkeyMatcher). pynput's built-in GlobalHotKeys is unreliable on macOS with
the Command key and multi-modifier combos, so we track pressed keys ourselves.

macOS permissions: the process running this needs **Input Monitoring** (and
usually **Accessibility**) permission (System Settings -> Privacy & Security)
for global hotkeys to fire.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from pynput import keyboard

from config_store import load_config
from ddc_monitor import (
    MonitorInfo,
    enumerate_monitors,
    find_monitor,
    input_name,
    parse_input_value,
    resolve_monitor,
    toggle_input,
    toggle_pc_group,
)

# Re-exported for callers (e.g. monitor_switcher). Canonical impl lives in
# ddc_monitor to avoid duplication.
__all__ = ["parse_input_value"]

# Modifier aliases accepted in a stored hotkey string.
_MODIFIER_ALIASES = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "option": "<alt>",
    "opt": "<alt>",
    "shift": "<shift>",
    "cmd": "<cmd>",
    "command": "<cmd>",
    "super": "<cmd>",
    "meta": "<cmd>",
    "win": "<cmd>",
}

_FUNCTION_KEYS = {f"f{n}": f"<f{n}>" for n in range(1, 21)}
_NAMED_KEYS = {
    "space": "<space>",
    "tab": "<tab>",
    "enter": "<enter>",
    "return": "<enter>",
    "esc": "<esc>",
    "escape": "<esc>",
}

# Tolerate punctuation spelled out by name (e.g. hand-edited or cross-platform
# configs) by mapping to the literal character pynput expects.
_PUNCT_NAMES = {
    "minus": "-", "underscore": "-", "equal": "=", "plus": "+",
    "comma": ",", "period": ".", "slash": "/", "semicolon": ";",
    "apostrophe": "'", "grave": "`", "backslash": "\\",
    "bracketleft": "[", "bracketright": "]",
}

# --------------------------------------------------------------------------- #
# Manual hotkey matcher
#
# pynput's GlobalHotKeys is unreliable on macOS with the Command key and
# multi-modifier combos (it silently fails to fire even though every key event
# is delivered). We instead track pressed keys ourselves on a raw
# keyboard.Listener, which is rock solid.
# --------------------------------------------------------------------------- #

_MODIFIER_TOKENS = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd",
}

_NAMED_SPEC = {
    "space": "space", "tab": "tab",
    "enter": "enter", "return": "enter",
    "esc": "esc", "escape": "esc",
}

# US-layout shifted punctuation -> base key, so a shifted press matches the
# base key stored in the hotkey (e.g. shift+1 reported as "!").
_REVERSE_SHIFT = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}


def _pynput_modifier_map() -> dict:
    """Map every pynput modifier Key variant to a canonical token."""
    mapping: dict = {}
    groups = {
        "ctrl": ("ctrl", "ctrl_l", "ctrl_r"),
        "alt": ("alt", "alt_l", "alt_r", "alt_gr"),
        "shift": ("shift", "shift_l", "shift_r"),
        "cmd": ("cmd", "cmd_l", "cmd_r"),
    }
    for token, names in groups.items():
        for name in names:
            key = getattr(keyboard.Key, name, None)
            if key is not None:
                mapping[key] = token
    return mapping


_MODIFIER_KEYS = _pynput_modifier_map()


def parse_hotkey_spec(hotkey: str) -> tuple[frozenset[str], str]:
    """Parse a stored hotkey like 'ctrl+shift+cmd+w' into (modifiers, key)."""
    parts = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
    if not parts:
        raise ValueError("Hotkey cannot be empty.")

    modifiers: set[str] = set()
    for part in parts[:-1]:
        token = _MODIFIER_TOKENS.get(part)
        if token is None:
            raise ValueError(f"Unknown modifier '{part}' in hotkey '{hotkey}'.")
        modifiers.add(token)

    key_part = parts[-1]
    if key_part in _FUNCTION_KEYS:
        key = key_part
    elif key_part in _NAMED_SPEC:
        key = _NAMED_SPEC[key_part]
    elif key_part in _PUNCT_NAMES:
        key = _PUNCT_NAMES[key_part]
    elif len(key_part) == 1:
        key = key_part
    else:
        raise ValueError(f"Unknown key '{key_part}' in hotkey '{hotkey}'.")

    return frozenset(modifiers), key


def _main_key_token(key) -> str | None:
    if isinstance(key, keyboard.Key):
        return key.name
    char = getattr(key, "char", None)
    if char:
        if char in _REVERSE_SHIFT:
            return _REVERSE_SHIFT[char]
        return char.lower()
    return None


class _HotkeyMatcher:
    def __init__(
        self, specs: list[tuple[frozenset[str], str, Callable[[], None]]]
    ) -> None:
        self._specs = specs
        self._mods: set[str] = set()
        self._down: set[str] = set()

    def on_press(self, key) -> None:
        token = _MODIFIER_KEYS.get(key)
        if token is not None:
            self._mods.add(token)
            return
        main = _main_key_token(key)
        if main is None or main in self._down:
            return  # ignore auto-repeat while key is held
        self._down.add(main)
        current = frozenset(self._mods)
        for mods, key_str, handler in self._specs:
            if key_str == main and mods == current:
                handler()

    def on_release(self, key) -> None:
        token = _MODIFIER_KEYS.get(key)
        if token is not None:
            self._mods.discard(token)
            return
        main = _main_key_token(key)
        if main is not None:
            self._down.discard(main)


@dataclass
class HotkeyAction:
    kind: str
    label: str
    callback: Callable[[], list[str]]


@dataclass
class RegisteredHotkey:
    hotkey: str
    action: HotkeyAction


def to_pynput_hotkey(hotkey: str) -> str:
    """Convert a stored hotkey like 'cmd+shift+1' to pynput's format."""
    parts = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
    if not parts:
        raise ValueError("Hotkey cannot be empty.")

    tokens: list[str] = []
    key_part = parts[-1]
    for part in parts[:-1]:
        alias = _MODIFIER_ALIASES.get(part)
        if alias is None:
            raise ValueError(f"Unknown modifier '{part}' in hotkey '{hotkey}'.")
        if alias not in tokens:
            tokens.append(alias)

    key_token = _key_token(key_part, hotkey)
    tokens.append(key_token)
    return "+".join(tokens)


def _key_token(key_part: str, hotkey: str) -> str:
    if key_part in _FUNCTION_KEYS:
        return _FUNCTION_KEYS[key_part]
    if key_part in _NAMED_KEYS:
        return _NAMED_KEYS[key_part]
    if key_part in _PUNCT_NAMES:
        return _PUNCT_NAMES[key_part]
    if len(key_part) == 1:
        return key_part
    raise ValueError(f"Unknown key '{key_part}' in hotkey '{hotkey}'.")


def build_registered_hotkeys(
    config: dict, monitors: list[MonitorInfo]
) -> list[RegisteredHotkey]:
    registered: list[RegisteredHotkey] = []

    for binding in config.get("monitor_bindings", []):
        if not binding.get("enabled", True):
            continue
        hotkey = binding.get("hotkey", "").strip()
        if not hotkey:
            continue
        if resolve_monitor(monitors, binding) is None:
            continue

        try:
            input_a = parse_input_value(binding.get("input_a"))
            input_b = parse_input_value(binding.get("input_b"))
        except (ValueError, TypeError):
            continue

        def make_monitor_action(
            monitor_binding: dict = binding,
            a: int = input_a,
            b: int = input_b,
        ) -> Callable[[], list[str]]:
            def action() -> list[str]:
                live_monitors = enumerate_monitors()
                selected = resolve_monitor(live_monitors, monitor_binding)
                if selected is None:
                    label = monitor_binding.get("monitor") or monitor_binding.get(
                        "device", "?"
                    )
                    return [f"[{label}] monitor not found"]
                if selected.handle is None:
                    return [f"[{selected.position}] not controllable"]
                previous, new = toggle_input(
                    selected.handle, a, b, selected.device_name
                )
                prev_label = input_name(previous) if previous >= 0 else "unknown"
                return [
                    f"[{selected.position}] {selected.description}: "
                    f"{prev_label} -> {input_name(new)}"
                ]

            return action

        registered.append(
            RegisteredHotkey(
                hotkey=hotkey,
                action=HotkeyAction(
                    kind="monitor",
                    label=(
                        f"[{binding.get('monitor', '?')}] "
                        f"{input_name(input_a)} <-> {input_name(input_b)}"
                    ),
                    callback=make_monitor_action(),
                ),
            )
        )

    pc_switch = config.get("pc_switch", {})
    if pc_switch.get("enabled") and pc_switch.get("hotkey"):
        enabled_monitors = [
            entry
            for entry in pc_switch.get("monitors", [])
            if entry.get("enabled", True)
        ]
        if enabled_monitors:
            pc_a = pc_switch.get("pc_a_name", "PC 1")
            pc_b = pc_switch.get("pc_b_name", "PC 2")
            hotkey = pc_switch["hotkey"]

            def pc_action(
                monitor_entries: list[dict] = enabled_monitors,
                name_a: str = pc_a,
                name_b: str = pc_b,
            ) -> list[str]:
                live_monitors = enumerate_monitors()
                target_name, lines = toggle_pc_group(
                    live_monitors, monitor_entries, name_a, name_b
                )
                return [f"Switched to {target_name}"] + lines

            registered.append(
                RegisteredHotkey(
                    hotkey=hotkey,
                    action=HotkeyAction(
                        kind="pc_switch",
                        label=f"toggle {pc_a} / {pc_b}",
                        callback=pc_action,
                    ),
                )
            )

    return registered


class HotkeyService:
    def __init__(self) -> None:
        self._listener: keyboard.GlobalHotKeys | None = None
        self._event_queue: queue.Queue[list[str]] = queue.Queue()
        self._error_queue: queue.Queue[str] = queue.Queue()
        self._job_queue: queue.Queue[HotkeyAction] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_stop: threading.Event | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, config: dict | None = None) -> None:
        self.stop()
        config = config or load_config()

        try:
            monitors = enumerate_monitors()
        except Exception as exc:  # DDCError or unexpected
            self._error_queue.put(str(exc))
            return

        if not any(monitor.controllable for monitor in monitors):
            self._error_queue.put("No controllable DDC/CI monitors detected.")
            return

        try:
            registered = build_registered_hotkeys(config, monitors)
        except Exception as exc:
            self._error_queue.put(f"Failed to build hotkeys: {exc}")
            return
        if not registered:
            self._error_queue.put("No enabled hotkey bindings found.")
            return

        specs: list[tuple[frozenset[str], str, Callable[[], None]]] = []
        for item in registered:
            try:
                mods, key = parse_hotkey_spec(item.hotkey)
            except ValueError as exc:
                self._error_queue.put(str(exc))
                continue
            specs.append((mods, key, self._make_handler(item.action)))

        if not specs:
            self._error_queue.put("No valid hotkeys to register.")
            return

        # Bind a fresh stop event to this worker so that if a previous worker
        # is still finishing a slow switch (past stop()'s join timeout) it can
        # never be revived by a subsequent start().
        stop_event = threading.Event()
        self._worker_stop = stop_event
        self._worker = threading.Thread(
            target=self._worker_loop, args=(stop_event,), daemon=True
        )
        self._worker.start()

        matcher = _HotkeyMatcher(specs)
        try:
            self._listener = keyboard.Listener(
                on_press=matcher.on_press, on_release=matcher.on_release
            )
            self._listener.start()
        except Exception as exc:
            self._error_queue.put(
                f"Could not start hotkey listener: {exc}. Grant Accessibility and "
                "Input Monitoring permission in System Settings."
            )
            self._listener = None
            stop_event.set()
            return

        self._running = True

    def stop(self) -> None:
        if self._worker_stop is not None:
            self._worker_stop.set()
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        self._listener = None
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=1.0)
        self._worker = None
        self._worker_stop = None
        self._running = False

    def poll_events(self) -> list[list[str]]:
        events: list[list[str]] = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return events

    def poll_errors(self) -> list[str]:
        errors: list[str] = []
        while True:
            try:
                errors.append(self._error_queue.get_nowait())
            except queue.Empty:
                break
        return errors

    def _make_handler(self, action: HotkeyAction) -> Callable[[], None]:
        # Keep the pynput event-tap callback near-instant: just enqueue the
        # action. Blocking DDC subprocess work runs on the worker thread so a
        # slow switch can't trip the CGEventTap timeout and drop hotkeys.
        def handler() -> None:
            self._job_queue.put(action)

        return handler

    def _worker_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                action = self._job_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                lines = action.callback()
                self._event_queue.put(lines)
            except Exception as exc:
                self._event_queue.put([f"Error: {exc}"])


def run_toggle_cli(
    monitors: list[MonitorInfo],
    monitor_selector: str,
    input_a: int,
    input_b: int,
) -> None:
    monitor = find_monitor(monitors, monitor_selector)
    if not monitor.controllable or monitor.handle is None:
        raise ValueError(
            f"Monitor [{monitor.position}] {monitor.description} is not "
            "DDC/CI controllable."
        )
    previous, new = toggle_input(
        monitor.handle, input_a, input_b, monitor.device_name
    )
    prev_label = input_name(previous) if previous >= 0 else "unknown"
    print(
        f"[{monitor.position}] {monitor.description}: "
        f"{prev_label} -> {input_name(new)}"
    )
