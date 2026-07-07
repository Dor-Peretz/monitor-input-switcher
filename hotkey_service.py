"""Global hotkey listener for monitor input switching."""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from config_store import load_config
from ddc_monitor import (
    MonitorInfo,
    enumerate_monitors,
    find_monitor,
    input_name,
    resolve_monitor,
    toggle_input,
    toggle_pc_group,
)

user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

VK_NAMES: dict[str, int] = {
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(d): ord("0") + d for d in range(0, 10)},
    "space": 0x20,
    "tab": 0x09,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}

VK_TO_NAME = {code: name for name, code in VK_NAMES.items()}


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


@dataclass
class HotkeyAction:
    kind: str
    label: str
    callback: Callable[[], list[str]]


@dataclass
class RegisteredHotkey:
    hotkey: str
    action: HotkeyAction


def parse_hotkey(hotkey: str) -> tuple[int, int]:
    parts = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
    if not parts:
        raise ValueError("Hotkey cannot be empty.")

    modifiers = 0
    key_part = parts[-1]
    for part in parts[:-1]:
        if part in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif part == "alt":
            modifiers |= MOD_ALT
        elif part == "shift":
            modifiers |= MOD_SHIFT
        elif part in {"win", "super", "meta"}:
            modifiers |= MOD_WIN
        else:
            raise ValueError(f"Unknown modifier '{part}' in hotkey '{hotkey}'.")

    vk = VK_NAMES.get(key_part)
    if vk is None:
        raise ValueError(f"Unknown key '{key_part}' in hotkey '{hotkey}'.")
    return modifiers, vk


def format_hotkey(modifiers: int, vk: int) -> str:
    parts: list[str] = []
    if modifiers & MOD_CONTROL:
        parts.append("ctrl")
    if modifiers & MOD_ALT:
        parts.append("alt")
    if modifiers & MOD_SHIFT:
        parts.append("shift")
    if modifiers & MOD_WIN:
        parts.append("win")
    parts.append(VK_TO_NAME.get(vk, f"vk{vk}"))
    return "+".join(parts)


def parse_input_value(value: int | str) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    return int(text)


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

        input_a = parse_input_value(binding["input_a"])
        input_b = parse_input_value(binding["input_b"])

        def make_monitor_action(
            monitor_binding: dict = binding,
            a: int = input_a,
            b: int = input_b,
        ) -> Callable[[], list[str]]:
            def action() -> list[str]:
                live_monitors = enumerate_monitors()
                selected_monitor = resolve_monitor(live_monitors, monitor_binding)
                if selected_monitor is None:
                    label = monitor_binding.get("monitor") or monitor_binding.get(
                        "device", "?"
                    )
                    return [f"[{label}] monitor not found"]
                if selected_monitor.handle is None:
                    return [f"[{selected_monitor.position}] not controllable"]
                previous, new = toggle_input(
                    selected_monitor.handle,
                    a,
                    b,
                    selected_monitor.device_name,
                )
                prev_label = input_name(previous) if previous >= 0 else "unknown"
                return [
                    f"[{selected_monitor.position}] {selected_monitor.description}: "
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
                    live_monitors,
                    monitor_entries,
                    name_a,
                    name_b,
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
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._event_queue: queue.Queue[list[str]] = queue.Queue()
        self._error_queue: queue.Queue[str] = queue.Queue()
        self._registered: list[tuple[int, HotkeyAction]] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, config: dict | None = None) -> None:
        if self._running:
            self.stop()
        config = config or load_config()
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(config,),
            daemon=True,
        )
        self._thread.start()
        self._ready_event.wait(timeout=2.0)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
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

    def _run_loop(self, config: dict) -> None:
        try:
            monitors = enumerate_monitors()
            if not any(monitor.controllable for monitor in monitors):
                self._error_queue.put("No DDC/CI monitors detected.")
                self._ready_event.set()
                return

            hotkeys = build_registered_hotkeys(config, monitors)
            if not hotkeys:
                self._error_queue.put("No enabled hotkey bindings found.")
                self._ready_event.set()
                return

            registered: list[tuple[int, HotkeyAction, str]] = []
            for index, item in enumerate(hotkeys, start=1):
                modifiers, vk = parse_hotkey(item.hotkey)
                if not user32.RegisterHotKey(None, index, modifiers, vk):
                    self._error_queue.put(
                        f"Could not register hotkey '{item.hotkey}'."
                    )
                    for reg_id, _, _ in registered:
                        user32.UnregisterHotKey(None, reg_id)
                    self._ready_event.set()
                    return
                registered.append((index, item.action, item.hotkey))

            self._registered = [(item[0], item[1]) for item in registered]
            self._running = True
            self._ready_event.set()

            msg = MSG()
            while not self._stop_event.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY:
                        action = next(
                            item[1]
                            for item in self._registered
                            if item[0] == msg.wParam
                        )
                        try:
                            lines = action.callback()
                            self._event_queue.put(lines)
                        except OSError as exc:
                            self._event_queue.put([f"Error: {exc}"])
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.05)
        finally:
            for reg_id, _ in self._registered:
                user32.UnregisterHotKey(None, reg_id)
            self._registered = []
            self._running = False
            self._ready_event.set()


def run_toggle_cli(
    monitors: list[MonitorInfo],
    monitor_selector: str,
    input_a: int,
    input_b: int,
) -> None:
    monitor = find_monitor(monitors, monitor_selector)
    if not monitor.controllable or monitor.handle is None:
        raise ValueError(
            f"Monitor [{monitor.position}] {monitor.description} is not DDC/CI controllable."
        )
    previous, new = toggle_input(
        monitor.handle,
        input_a,
        input_b,
        monitor.device_name,
    )
    prev_label = input_name(previous) if previous >= 0 else "unknown"
    print(
        f"[{monitor.position}] {monitor.description}: "
        f"{prev_label} -> {input_name(new)}"
    )
