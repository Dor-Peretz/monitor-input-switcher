"""Global hotkey listener for monitor input switching."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from config_store import load_config
from ddc_monitor import (
    MonitorInfo,
    enumerate_monitors,
    find_monitor,
    input_name,
    parse_input_value,
    pc_switch_headline,
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
MOD_NOREPEAT = 0x4000

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


def build_registered_hotkeys(
    config: dict, _monitors: list[MonitorInfo] | None = None
) -> list[RegisteredHotkey]:
    registered: list[RegisteredHotkey] = []

    for binding in config.get("monitor_bindings", []):
        if not binding.get("enabled", True):
            continue
        hotkey = binding.get("hotkey", "").strip()
        if not hotkey:
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
                if not selected_monitor.controllable:
                    return [f"[{selected_monitor.position}] not controllable"]
                previous, new = toggle_input(
                    selected_monitor,
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
                _ok, headline = pc_switch_headline(target_name, lines)
                return [headline] + lines

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


_SWITCH_PROGRESS_DIR = (
    Path(__file__).resolve().parent
    / "macos"
    / "streamdeck"
    / "com.dorperetz.monitorswitcher.sdPlugin"
)
_switch_progress_module = None
_switch_progress_unavailable = False


def _switch_progress():
    global _switch_progress_module, _switch_progress_unavailable
    if _switch_progress_module is not None or _switch_progress_unavailable:
        return _switch_progress_module
    try:
        import switch_progress
    except ImportError:
        folder = str(_SWITCH_PROGRESS_DIR)
        if folder not in sys.path:
            # Appended, not prepended, so plugin files never shadow app modules.
            sys.path.append(folder)
        try:
            import switch_progress
        except ImportError:
            _switch_progress_unavailable = True
            return None
    _switch_progress_module = switch_progress
    return switch_progress


def _begin_hotkey_progress(action: HotkeyAction) -> str:
    module = _switch_progress()
    if module is None:
        return ""
    try:
        return module.begin_hotkey_progress(action.kind)
    except Exception:
        return ""


def _end_hotkey_progress(token: str) -> None:
    module = _switch_progress()
    if module is None:
        return
    try:
        module.end_progress(token)
    except Exception:
        pass


class _ListenerRun:
    """State owned by one listener thread, so a restart never touches it."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.registered: list[tuple[int, HotkeyAction]] = []
        self.running = False
        self.thread: threading.Thread | None = None


class HotkeyService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run: _ListenerRun | None = None
        self._worker: threading.Thread | None = None
        self._action_queue: queue.Queue[HotkeyAction] = queue.Queue()
        self._event_queue: queue.Queue[list[str]] = queue.Queue()
        self._error_queue: queue.Queue[str] = queue.Queue()

    @property
    def running(self) -> bool:
        run = self._run
        return run is not None and run.running

    def start(self, config: dict | None = None) -> None:
        config = config or load_config()
        with self._lock:
            self._stop_locked()
            self._ensure_worker()
            run = _ListenerRun()
            run.thread = threading.Thread(
                target=self._run_loop,
                args=(config, run),
                name="hotkey-listener",
                daemon=True,
            )
            self._run = run
            run.thread.start()
            run.ready_event.wait(timeout=2.0)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        run = self._run
        self._run = None
        if run is None:
            return
        run.stop_event.set()
        if run.thread and run.thread.is_alive():
            run.thread.join(timeout=2)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._work_loop,
            name="hotkey-actions",
            daemon=True,
        )
        self._worker.start()

    def _work_loop(self) -> None:
        # One worker for the service lifetime keeps DDC switches serialized,
        # even across listener restarts.
        while True:
            action = self._action_queue.get()
            token = _begin_hotkey_progress(action)
            try:
                self._event_queue.put(action.callback())
            except Exception as exc:
                self._event_queue.put([f"Error: {exc}"])
            finally:
                if token:
                    _end_hotkey_progress(token)

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

    def _run_loop(self, config: dict, run: _ListenerRun) -> None:
        # RegisterHotKey(None, ...) binds to the calling thread's message
        # queue, so registration, the message loop and UnregisterHotKey must
        # all stay on this thread.
        try:
            hotkeys = build_registered_hotkeys(config)
            if not hotkeys:
                self._error_queue.put("No enabled hotkey bindings found.")
                return

            for index, item in enumerate(hotkeys, start=1):
                modifiers, vk = parse_hotkey(item.hotkey)
                if not user32.RegisterHotKey(None, index, modifiers | MOD_NOREPEAT, vk):
                    self._error_queue.put(
                        f"Could not register hotkey '{item.hotkey}'."
                    )
                    return
                run.registered.append((index, item.action))

            run.running = True
            run.ready_event.set()

            msg = MSG()
            while not run.stop_event.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY:
                        action = next(
                            (
                                item[1]
                                for item in run.registered
                                if item[0] == msg.wParam
                            ),
                            None,
                        )
                        if action is not None:
                            self._action_queue.put(action)
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.05)
        except Exception as exc:
            self._error_queue.put(str(exc) or exc.__class__.__name__)
        finally:
            for reg_id, _ in run.registered:
                user32.UnregisterHotKey(None, reg_id)
            run.registered.clear()
            run.running = False
            run.ready_event.set()


def run_toggle_cli(
    monitors: list[MonitorInfo],
    monitor_selector: str,
    input_a: int,
    input_b: int,
) -> None:
    monitor = find_monitor(monitors, monitor_selector)
    if not monitor.controllable:
        raise ValueError(
            f"Monitor [{monitor.position}] {monitor.description} is not DDC/CI controllable."
        )
    previous, new = toggle_input(
        monitor,
        input_a,
        input_b,
        monitor.device_name,
    )
    prev_label = input_name(previous) if previous >= 0 else "unknown"
    print(
        f"[{monitor.position}] {monitor.description}: "
        f"{prev_label} -> {input_name(new)}"
    )
