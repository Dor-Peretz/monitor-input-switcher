"""Windows DDC/CI monitor control via dxva2.dll."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.ReleaseMutex.restype = wintypes.BOOL
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]

VCP_INPUT_SELECT = 0x60
_SET_DELAYS = (0.08, 0.16, 0.28, 0.45, 0.7)
_MULTI_MONITOR_GAP = 0.22
_DDC_MUTEX_NAME = "Local\\MonitorInputSwitcherDDC"
_STATE_MUTEX_NAME = "Local\\MonitorInputSwitcherState"
_WAIT_MS = 8000
_STATE_WAIT_MS = 3000
_STATE_READ_DELAYS = (0.02, 0.05, 0.1, 0.2)
_STATE_REPLACE_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.35)
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_ddc_lock = threading.RLock()
_ddc_mutex = kernel32.CreateMutexW(None, False, _DDC_MUTEX_NAME)
_state_lock = threading.RLock()
_state_mutex = kernel32.CreateMutexW(None, False, _STATE_MUTEX_NAME)


@contextmanager
def _named_mutex(lock, mutex, wait_ms: int, timeout_message: str):
    """Hold a thread lock plus a named Win32 mutex; both are reentrant per thread."""
    with lock:
        if mutex:
            wait_result = kernel32.WaitForSingleObject(mutex, wait_ms)
            if wait_result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                raise OSError(timeout_message)
        try:
            yield
        finally:
            if mutex:
                kernel32.ReleaseMutex(mutex)


@contextmanager
def _ddc_session():
    """Serialize DDC/CI across threads and between tray + settings processes."""
    with _named_mutex(
        _ddc_lock, _ddc_mutex, _WAIT_MS, "Timed out waiting for another switch to finish"
    ):
        yield


@contextmanager
def _state_session():
    """Serialize state-file read-modify-write across threads and processes.

    Acquired while _ddc_session is held (set_input_source), so never enter
    _ddc_session from inside this one.
    """
    with _named_mutex(
        _state_lock,
        _state_mutex,
        _STATE_WAIT_MS,
        "Timed out waiting for another process to update the switcher state",
    ):
        yield

INPUT_SOURCE_NAMES: dict[int, str] = {
    0x01: "VGA",
    0x02: "Analog-2",
    0x03: "DVI-1",
    0x04: "DVI-2",
    0x0F: "DisplayPort-1",
    0x10: "DisplayPort-2",
    0x11: "HDMI-1",
    0x12: "HDMI-2",
    0x1B: "USB-C",
}


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR,
    wintypes.LPDWORD,
]
dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR,
    wintypes.DWORD,
    ctypes.POINTER(PHYSICAL_MONITOR),
]
dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL
dxva2.DestroyPhysicalMonitors.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(PHYSICAL_MONITOR),
]
# wintypes.BYTE is signed; the Win32 BYTE VCP code is unsigned.
dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE,
    ctypes.c_ubyte,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
]
dxva2.SetVCPFeature.restype = wintypes.BOOL
dxva2.SetVCPFeature.argtypes = [
    wintypes.HANDLE,
    ctypes.c_ubyte,
    wintypes.DWORD,
]

MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    wintypes.LPRECT,
    wintypes.LPARAM,
)
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    wintypes.LPRECT,
    MONITORENUMPROC,
    wintypes.LPARAM,
]


@dataclass
class MonitorInfo:
    index: int
    position: str
    device_name: str
    description: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool
    hmonitor: int
    controllable: bool
    current_input: int | None = None

    @property
    def handle(self) -> int | None:
        """Back-compat alias: a live physical handle is opened per DDC call."""
        return self.hmonitor if self.controllable else None

    @property
    def label(self) -> str:
        primary = " [primary]" if self.is_primary else ""
        return (
            f"{self.position}: {self.description} "
            f"({self.width}x{self.height} at {self.left},{self.top}){primary}"
        )


def _decode_vcp_input(raw: int) -> int | None:
    """Decode VCP 0x60 value; Windows often returns packed (mh << 8) | ml."""
    if raw < 0:
        return None
    if raw <= 0xFF:
        return raw
    high = (raw >> 8) & 0xFF
    low = raw & 0xFF
    if low in INPUT_SOURCE_NAMES:
        return low
    if high in INPUT_SOURCE_NAMES:
        return high
    if low:
        return low
    if high:
        return high
    return None


def _format_input(value: int | None) -> str:
    if value is None:
        return "unknown"
    name = INPUT_SOURCE_NAMES.get(value)
    if name:
        return f"{name} (0x{value:02X})"
    return f"0x{value:02X}"


class DDCError(RuntimeError):
    """Raised when a DDC/CI call fails. Matches the macOS module."""


def parse_input_value(value: int | str) -> int:
    """Parse a DDC input code that may be an int, decimal string, or 0x hex."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    return int(text)


def input_name(value: int | None) -> str:
    if value is None or value < 0:
        return "unknown"
    return INPUT_SOURCE_NAMES.get(value, f"0x{value:02X}")


@contextmanager
def _physical_monitors(hmonitor: int):
    count = wintypes.DWORD()
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
        wintypes.HMONITOR(hmonitor), ctypes.byref(count)
    ):
        yield ()
        return
    if count.value == 0:
        yield ()
        return

    physical_array = (PHYSICAL_MONITOR * count.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(
        wintypes.HMONITOR(hmonitor), count.value, physical_array
    ):
        yield ()
        return
    try:
        yield physical_array
    finally:
        dxva2.DestroyPhysicalMonitors(count.value, physical_array)


def _get_vcp_input(handle) -> int | None:
    current = wintypes.DWORD()
    maximum = wintypes.DWORD()
    vcp_type = wintypes.DWORD()
    ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
        handle,
        VCP_INPUT_SELECT,
        ctypes.byref(vcp_type),
        ctypes.byref(current),
        ctypes.byref(maximum),
    )
    if not ok:
        return None
    return _decode_vcp_input(int(current.value))


def _set_vcp_input(handle, value: int) -> bool:
    ctypes.set_last_error(0)
    return bool(
        dxva2.SetVCPFeature(
            handle,
            VCP_INPUT_SELECT,
            value,
        )
    )


def _read_from_hmonitor(hmonitor: int) -> tuple[str, bool, int | None]:
    description = "Unknown monitor"
    controllable = False
    current = None
    with _physical_monitors(hmonitor) as physicals:
        if not physicals:
            return description, False, None
        for physical in physicals:
            text = physical.szPhysicalMonitorDescription.strip()
            if text:
                description = text
            # Some GPU drivers return a NULL physical-monitor HANDLE even when
            # VCP 0x60 still works for this HMONITOR. Probe VCP either way.
            handle = physical.hPhysicalMonitor
            current = _get_vcp_input(handle)
            if current is not None or handle not in (None, 0):
                controllable = True
            if current is not None:
                break
    return description, controllable, current


def get_input_source(monitor: MonitorInfo) -> int | None:
    with _ddc_session():
        _desc, _ok, current = _read_from_hmonitor(monitor.hmonitor)
        return current


def _try_set_on_hmonitor(hmonitor: int, value: int) -> bool:
    with _physical_monitors(hmonitor) as physicals:
        if not physicals:
            return False
        for physical in physicals:
            handle = physical.hPhysicalMonitor
            _get_vcp_input(handle)
            time.sleep(0.04)
            if _set_vcp_input(handle, value):
                return True
        return False


def set_input_source(monitor: MonitorInfo, value: int, state_key: str | None = None) -> None:
    key = state_key or monitor.device_name
    last_error = 0
    with _ddc_session():
        for attempt, delay in enumerate(_SET_DELAYS, start=1):
            if _try_set_on_hmonitor(monitor.hmonitor, value):
                _write_last_input(key, value)
                return
            last_error = ctypes.get_last_error()
            time.sleep(delay)
    raise OSError(
        last_error,
        f"SetVCPFeature failed after {len(_SET_DELAYS)} attempts "
        f"({monitor.position}: {monitor.description})",
    )


def switch_monitors_to_inputs(
    monitors: list[MonitorInfo],
    assignments: list[tuple[MonitorInfo, int]],
) -> tuple[list[str], int, int]:
    """Set explicit inputs on multiple monitors.

    Returns (status lines, successes, failures).
    """
    lines: list[str] = []
    successes = 0
    failures = 0
    for index, (monitor, target) in enumerate(assignments):
        if index:
            time.sleep(_MULTI_MONITOR_GAP)
        if not monitor.controllable:
            lines.append(f"[{monitor.position}] skipped (not controllable)")
            failures += 1
            continue
        try:
            set_input_source(monitor, target, monitor.device_name)
            successes += 1
            lines.append(
                f"[{monitor.position}] {monitor.description} -> {input_name(target)}"
            )
        except OSError as exc:
            failures += 1
            lines.append(f"[{monitor.position}] failed: {exc}")
    return lines, successes, failures


def apply_named_layout(config: dict, name: str = "office") -> list[str]:
    """Set the named desk layout on whatever monitors this computer currently drives."""
    layout = (config.get("layouts") or {}).get(name)
    if not isinstance(layout, dict):
        raise ValueError(f"Unknown layout '{name}'")
    entries = layout.get("assignments") or []
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"Layout '{name}' has no assignments")

    monitors = enumerate_monitors()
    assignments: list[tuple[MonitorInfo, int]] = []
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        monitor = resolve_monitor(monitors, entry)
        label = str(entry.get("monitor") or entry.get("device") or "?")
        if monitor is None:
            lines.append(f"[{label}] not connected to this computer")
            continue
        try:
            target = parse_input_value(entry["input"])
        except (KeyError, TypeError, ValueError):
            lines.append(f"[{label}] missing input code")
            continue
        assignments.append((monitor, target))

    switched, _ok, _fail = switch_monitors_to_inputs(monitors, assignments)
    return lines + switched


def _state_path():
    from runtime_paths import is_frozen, user_data_dir

    if is_frozen():
        return user_data_dir() / "switcher_state.json"
    return Path(__file__).with_name(".switcher_state.json")


def _load_state(for_write: bool = False) -> dict:
    """Read the state file, retrying transient sharing and parse failures.

    With for_write, an unreadable file raises instead of returning {} so the
    caller cannot overwrite it; a file that stays unparseable is moved aside
    to ``*.corrupt`` before starting fresh.
    """
    path = _state_path()
    for delay in (*_STATE_READ_DELAYS, None):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            if delay is None:
                if for_write:
                    raise
                return {}
            time.sleep(delay)
            continue
        try:
            data = json.loads(text)
        except ValueError:
            if delay is None:
                if for_write:
                    with suppress(OSError):
                        os.replace(path, path.with_name(path.name + ".corrupt"))
                return {}
            time.sleep(delay)
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _save_state(data: dict) -> None:
    path = _state_path()
    payload = json.dumps(data, indent=2) + "\n"
    with _state_session():
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows refuses the replace while another process has the target open.
            for delay in (*_STATE_REPLACE_DELAYS, None):
                try:
                    os.replace(tmp_path, path)
                    break
                except PermissionError:
                    if delay is None:
                        raise
                    time.sleep(delay)
        finally:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)


@contextmanager
def _edit_state():
    """Yield the state dict for in-place edits; saved atomically on clean exit."""
    with _state_session():
        data = _load_state(for_write=True)
        yield data
        _save_state(data)


def read_active_pc(group_id: str = "default") -> str:
    data = _load_state()
    pc_groups = data.get("pc_groups", {})
    active = pc_groups.get(group_id, "a")
    return "b" if active == "b" else "a"


def write_active_pc(active: str, group_id: str = "default") -> None:
    with _edit_state() as data:
        pc_groups = data.setdefault("pc_groups", {})
        pc_groups[group_id] = "b" if active == "b" else "a"


def toggle_pc_group(
    monitors: list[MonitorInfo],
    monitor_configs: list[dict],
    pc_a_name: str,
    pc_b_name: str,
    group_id: str = "default",
) -> tuple[str, list[str]]:
    """Toggle all configured monitors between PC A and PC B inputs."""
    active = read_active_pc(group_id)
    target_pc = "b" if active == "a" else "a"
    target_name = pc_b_name if target_pc == "b" else pc_a_name
    input_key = "input_b" if target_pc == "b" else "input_a"

    assignments: list[tuple[MonitorInfo, int]] = []
    missing: list[str] = []
    for entry in monitor_configs:
        if not entry.get("enabled", True):
            continue
        monitor = resolve_monitor(monitors, entry)
        if monitor is None:
            missing.append(str(entry.get("monitor") or entry.get("device") or "?"))
            continue
        assignments.append((monitor, int(entry[input_key])))

    lines, successes, failures = switch_monitors_to_inputs(monitors, assignments)
    for name in missing:
        lines.append(f"[{name}] monitor not found")
        failures += 1

    if successes and not failures:
        write_active_pc(target_pc, group_id)
        return target_name, lines

    if successes:
        lines.append(
            f"Partial switch to {target_name} ({successes} ok, {failures} failed). "
            "State was not flipped — press the hotkey again to retry."
        )
        return target_name, lines

    lines.append(
        f"Could not switch to {target_name}. Kept previous PC state so the next "
        "press retries the same target."
    )
    return target_name, lines


def pc_switch_headline(target_name: str, lines: list[str]) -> tuple[bool, str]:
    """``(ok, message)`` for a ``toggle_pc_group`` result."""
    last = lines[-1] if lines else ""
    if last.startswith(("Partial switch", "Could not switch")):
        return False, last
    return True, f"Switched to {target_name}"


def toggle_input(
    monitor: MonitorInfo,
    input_a: int,
    input_b: int,
    state_key: str | None = None,
) -> tuple[int, int]:
    """Toggle between two input sources. Returns (previous, new)."""
    key = state_key or monitor.device_name
    current = get_input_source(monitor)
    last = _read_last_input(key)

    if current == input_a:
        target = input_b
    elif current == input_b:
        target = input_a
    elif last == input_b:
        target = input_a
    elif last == input_a:
        target = input_b
    else:
        target = input_b

    set_input_source(monitor, target, state_key=key)
    previous = current if current is not None else (last if last is not None else -1)
    return previous, target


def _read_last_input(state_key: str) -> int | None:
    data = _load_state()
    monitor_inputs = data.get("monitor_inputs", {})
    if isinstance(monitor_inputs, dict) and state_key in monitor_inputs:
        try:
            return int(monitor_inputs[state_key])
        except (TypeError, ValueError):
            return None

    legacy = data.get(state_key)
    if legacy is not None:
        try:
            return int(legacy)
        except (TypeError, ValueError):
            return None
    return None


def read_last_input(state_key: str) -> int | None:
    """Last input value written for a monitor, or None if never switched."""
    return _read_last_input(state_key)


def _write_last_input(state_key: str, value: int) -> None:
    with _edit_state() as data:
        monitor_inputs = data.setdefault("monitor_inputs", {})
        monitor_inputs[state_key] = value


def _position_name(index: int, total: int) -> str:
    if total == 1:
        return "only"
    if total == 2:
        return "left" if index == 0 else "right"
    if total == 3:
        return ("left", "center", "right")[index]
    return f"screen-{index + 1}"


def enumerate_monitors(controllable_only: bool = False) -> list[MonitorInfo]:
    entries: list[tuple[MONITORINFOEXW, int]] = []

    @MONITORENUMPROC
    def callback(hmonitor, _hdc, _rect, _lparam):
        if not hmonitor:
            return True
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        entries.append((info, int(hmonitor)))
        return True

    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError(ctypes.get_last_error(), "EnumDisplayMonitors failed")

    entries.sort(key=lambda item: (item[0].rcMonitor.left, item[0].rcMonitor.top))
    total = len(entries)
    result: list[MonitorInfo] = []

    for index, (info, hmonitor) in enumerate(entries):
        rect = info.rcMonitor
        with _ddc_session():
            description, controllable, current = _read_from_hmonitor(hmonitor)
        monitor = MonitorInfo(
            index=index,
            position=_position_name(index, total),
            device_name=info.szDevice,
            description=description,
            left=rect.left,
            top=rect.top,
            width=rect.right - rect.left,
            height=rect.bottom - rect.top,
            is_primary=bool(info.dwFlags & 1),
            hmonitor=hmonitor,
            controllable=controllable,
            current_input=current,
        )
        if controllable_only and not monitor.controllable:
            continue
        result.append(monitor)

    return result


def find_monitor(monitors: list[MonitorInfo], selector: str) -> MonitorInfo:
    monitor = resolve_monitor(monitors, {"monitor": selector})
    if monitor is None:
        available = ", ".join(sorted({m.position for m in monitors}))
        raise ValueError(f"Monitor '{selector}' not found. Available positions: {available}")
    return monitor


def resolve_monitor(monitors: list[MonitorInfo], entry: dict) -> MonitorInfo | None:
    device = (entry.get("device") or "").strip().lower()
    if device:
        for monitor in monitors:
            if monitor.device_name.lower() == device:
                return monitor
        # Config from Mac uses model names like "DELL P2721Q"; match description.
        for monitor in monitors:
            desc = monitor.description.lower()
            if device == desc or device in desc or desc.startswith(device):
                return monitor

    selector = (entry.get("monitor") or "").strip().lower()
    if not selector:
        return None

    for monitor in monitors:
        if selector in {
            monitor.position.lower(),
            str(monitor.index),
            monitor.device_name.lower(),
            monitor.description.lower(),
        }:
            return monitor
        if selector and selector in monitor.description.lower():
            return monitor

    aliases = {
        "primary": lambda m: m.is_primary,
        "main": lambda m: m.is_primary,
    }
    predicate = aliases.get(selector)
    if predicate:
        matches = [m for m in monitors if predicate(m)]
        if len(matches) == 1:
            return matches[0]
    return None


def print_monitor_list(monitors: list[MonitorInfo]) -> None:
    if not monitors:
        print("No displays detected.")
        return

    controllable = [monitor for monitor in monitors if monitor.controllable]
    print(f"Found {len(monitors)} display(s), {len(controllable)} controllable via DDC/CI:\n")

    for monitor in monitors:
        current = monitor.current_input
        status = "switchable" if monitor.controllable else "not switchable (no DDC/CI handle)"
        print(f"  [{monitor.position}] {monitor.description} - {status}")
        print(f"    Device: {monitor.device_name}")
        print(f"    Layout: {monitor.width}x{monitor.height} at ({monitor.left}, {monitor.top})")
        print(f"    Current input: {_format_input(current)}")
        print()

    if not controllable:
        print("Enable DDC/CI in each monitor's OSD menu, then run 'list' again.")
