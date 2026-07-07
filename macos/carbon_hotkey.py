"""Native macOS global hotkeys via Carbon's RegisterEventHotKey.

This replaces the pynput-based listener, which crashes on modern macOS
(Sequoia): pynput's keyboard listener calls a Text Input Source Manager API
(``TSMGetInputSourceProperty``) from a background thread, and macOS now aborts
any process that touches those APIs off the main thread
(``dispatch_assert_queue`` -> ``SIGTRAP``).

Carbon's ``RegisterEventHotKey`` avoids all of that:

* It is delivered through the main Carbon/Cocoa run loop (the one rumps'
  ``NSApplication`` already runs), so no background-thread text-input calls.
* It does **not** require Accessibility or Input Monitoring permission.

We bind Carbon.framework via ``ctypes`` (standard library) so no new
third-party dependency is introduced.

The public API mirrors ``hotkey_service.HotkeyService`` so it is a drop-in
replacement: ``start(config)`` / ``stop()`` / ``running`` / ``poll_events()`` /
``poll_errors()``.

IMPORTANT: ``start()`` / ``stop()`` must be called from the main thread (the
thread that runs the Cocoa event loop). In the menu bar app that is guaranteed
because they run inside ``MenuBarApp.__init__`` / callbacks.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import queue
import threading
from typing import Callable

from config_store import load_config
from ddc_monitor import enumerate_monitors
from hotkey_service import (
    HotkeyAction,
    build_registered_hotkeys,
    parse_hotkey_spec,
)

# --------------------------------------------------------------------------- #
# Carbon.framework bindings (ctypes)
# --------------------------------------------------------------------------- #

_carbon_path = ctypes.util.find_library("Carbon")
if _carbon_path is None:  # pragma: no cover - Carbon ships with macOS
    raise ImportError("Carbon.framework not found (is this macOS?).")
_carbon = ctypes.CDLL(_carbon_path)

OSStatus = ctypes.c_int32
OSType = ctypes.c_uint32
UInt32 = ctypes.c_uint32


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", OSType), ("eventKind", UInt32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", OSType), ("id", UInt32)]


# OSStatus (*)(EventHandlerCallRef, EventRef, void *userData)
_HANDLER_FUNC = ctypes.CFUNCTYPE(
    OSStatus, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

_carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
_carbon.GetApplicationEventTarget.argtypes = []

_carbon.InstallEventHandler.restype = OSStatus
_carbon.InstallEventHandler.argtypes = [
    ctypes.c_void_p,                 # inTarget
    _HANDLER_FUNC,                   # inHandler
    UInt32,                          # inNumTypes
    ctypes.POINTER(_EventTypeSpec),  # inList
    ctypes.c_void_p,                 # inUserData
    ctypes.c_void_p,                 # outRef (nullable)
]

_carbon.RegisterEventHotKey.restype = OSStatus
_carbon.RegisterEventHotKey.argtypes = [
    UInt32,                              # inHotKeyCode
    UInt32,                              # inHotKeyModifiers
    _EventHotKeyID,                      # inHotKeyID (by value)
    ctypes.c_void_p,                     # inTarget
    UInt32,                              # inOptions
    ctypes.POINTER(ctypes.c_void_p),     # outRef
]

_carbon.UnregisterEventHotKey.restype = OSStatus
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]

_carbon.GetEventParameter.restype = OSStatus
_carbon.GetEventParameter.argtypes = [
    ctypes.c_void_p,  # inEvent
    OSType,           # inName
    OSType,           # inDesiredType
    ctypes.c_void_p,  # outActualType (nullable)
    UInt32,           # inBufferSize
    ctypes.c_void_p,  # outActualSize (nullable)
    ctypes.c_void_p,  # outData
]


def _fourcc(code: str) -> int:
    return (
        (ord(code[0]) << 24)
        | (ord(code[1]) << 16)
        | (ord(code[2]) << 8)
        | ord(code[3])
    )


_K_EVENT_CLASS_KEYBOARD = _fourcc("keyb")
_K_EVENT_HOTKEY_PRESSED = 6
_K_EVENT_PARAM_DIRECT_OBJECT = _fourcc("----")
_TYPE_EVENT_HOTKEY_ID = _fourcc("hkid")
_SIGNATURE = _fourcc("MISw")  # Monitor Input Switcher

# Carbon modifier bit masks (Events.h).
_CARBON_MODS = {
    "cmd": 0x0100,    # cmdKey
    "shift": 0x0200,  # shiftKey
    "alt": 0x0800,    # optionKey
    "ctrl": 0x1000,   # controlKey
}

# US-layout virtual key codes (Carbon HIToolbox / Events.h kVK_*).
_VIRTUAL_KEYS: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16,
    "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24,
    "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32,
    "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "enter": 36, "tab": 48, "space": 49, "esc": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105,
    "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "f20": 90,
}


def hotkey_to_carbon(hotkey: str) -> tuple[int, int]:
    """Convert a stored hotkey (e.g. 'ctrl+shift+cmd+w') to (keycode, mods).

    Raises ValueError if the key has no known macOS virtual keycode.
    """
    modifiers, key = parse_hotkey_spec(hotkey)
    if key not in _VIRTUAL_KEYS:
        raise ValueError(
            f"Key '{key}' in hotkey '{hotkey}' has no known macOS keycode."
        )
    keycode = _VIRTUAL_KEYS[key]
    mods = 0
    for mod in modifiers:
        mods |= _CARBON_MODS.get(mod, 0)
    return keycode, mods


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class CarbonHotKeyManager:
    """Register global hotkeys with Carbon and run their actions off-thread.

    The Carbon event handler fires on the main run loop; it only enqueues a job.
    The blocking DDC subprocess work runs on a worker thread so it can never
    stall the run loop (and, unlike pynput, there is no CGEventTap timeout to
    trip).
    """

    def __init__(self) -> None:
        self._event_queue: queue.Queue[list[str]] = queue.Queue()
        self._error_queue: queue.Queue[str] = queue.Queue()
        self._job_queue: queue.Queue[HotkeyAction] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_stop: threading.Event | None = None
        self._running = False

        # Carbon state.
        self._handler_ref = None  # kept alive: the CFUNCTYPE trampoline
        self._handler_installed = False
        self._hotkey_refs: list[ctypes.c_void_p] = []
        self._actions: dict[int, HotkeyAction] = {}
        self._next_id = 1

    @property
    def running(self) -> bool:
        return self._running

    # -- lifecycle -------------------------------------------------------- #

    def start(self, config: dict | None = None) -> None:
        self.stop()
        config = config or load_config()

        try:
            monitors = enumerate_monitors()
        except Exception as exc:
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

        self._ensure_handler_installed()

        registered_any = False
        for item in registered:
            try:
                keycode, mods = hotkey_to_carbon(item.hotkey)
            except ValueError as exc:
                self._error_queue.put(str(exc))
                continue
            if self._register_one(keycode, mods, item.action):
                registered_any = True
            else:
                self._error_queue.put(
                    f"Failed to register hotkey '{item.hotkey}' "
                    "(already taken by another app?)."
                )

        if not registered_any:
            self._error_queue.put("No valid hotkeys could be registered.")
            return

        stop_event = threading.Event()
        self._worker_stop = stop_event
        self._worker = threading.Thread(
            target=self._worker_loop, args=(stop_event,), daemon=True
        )
        self._worker.start()
        self._running = True

    def stop(self) -> None:
        if self._worker_stop is not None:
            self._worker_stop.set()
        for ref in self._hotkey_refs:
            try:
                _carbon.UnregisterEventHotKey(ref)
            except Exception:
                pass
        self._hotkey_refs.clear()
        self._actions.clear()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=1.0)
        self._worker = None
        self._worker_stop = None
        self._running = False

    # -- Carbon wiring ---------------------------------------------------- #

    def _ensure_handler_installed(self) -> None:
        if self._handler_installed:
            return
        # Keep a strong reference to the trampoline for the process lifetime.
        self._handler_ref = _HANDLER_FUNC(self._on_hotkey_event)
        spec = _EventTypeSpec(
            _K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED
        )
        target = _carbon.GetApplicationEventTarget()
        status = _carbon.InstallEventHandler(
            target, self._handler_ref, 1, ctypes.byref(spec), None, None
        )
        if status != 0:
            raise RuntimeError(
                f"InstallEventHandler failed (OSStatus {status})."
            )
        self._handler_installed = True

    def _register_one(
        self, keycode: int, mods: int, action: HotkeyAction
    ) -> bool:
        hotkey_id = self._next_id
        self._next_id += 1
        ref = ctypes.c_void_p()
        hk_id = _EventHotKeyID(signature=_SIGNATURE, id=hotkey_id)
        target = _carbon.GetApplicationEventTarget()
        status = _carbon.RegisterEventHotKey(
            keycode, mods, hk_id, target, 0, ctypes.byref(ref)
        )
        if status != 0 or not ref.value:
            return False
        self._hotkey_refs.append(ref)
        self._actions[hotkey_id] = action
        return True

    def _on_hotkey_event(self, _next, event, _user) -> int:
        # Runs on the main thread. Keep it minimal: identify the hotkey and
        # enqueue its action for the worker thread.
        try:
            hk_id = _EventHotKeyID()
            status = _carbon.GetEventParameter(
                event,
                _K_EVENT_PARAM_DIRECT_OBJECT,
                _TYPE_EVENT_HOTKEY_ID,
                None,
                ctypes.sizeof(hk_id),
                None,
                ctypes.byref(hk_id),
            )
            if status == 0:
                action = self._actions.get(hk_id.id)
                if action is not None:
                    self._job_queue.put(action)
        except Exception as exc:  # never let an exception cross into C
            self._error_queue.put(f"Hotkey dispatch error: {exc}")
        return 0  # noErr

    # -- worker + polling ------------------------------------------------- #

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
