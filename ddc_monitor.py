"""Windows DDC/CI monitor control via dxva2.dll."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.windll.user32
dxva2 = ctypes.windll.dxva2

VCP_INPUT_SELECT = 0x60

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
    handle: int | None
    controllable: bool

    @property
    def label(self) -> str:
        primary = " [primary]" if self.is_primary else ""
        return (
            f"{self.position}: {self.description} "
            f"({self.width}x{self.height} at {self.left},{self.top}){primary}"
        )


def _format_input(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value > 0xFF:
        return f"unavailable (enable DDC/CI in monitor OSD)"
    name = INPUT_SOURCE_NAMES.get(value)
    if name:
        return f"{name} (0x{value:02X})"
    return f"0x{value:02X}"


def input_name(value: int) -> str:
    if value < 0:
        return "unknown"
    return INPUT_SOURCE_NAMES.get(value, f"0x{value:02X}")


def get_input_source(handle: int) -> int | None:
    current = wintypes.DWORD()
    maximum = wintypes.DWORD()
    supported = wintypes.DWORD()
    ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
        wintypes.HANDLE(handle),
        wintypes.BYTE(VCP_INPUT_SELECT),
        ctypes.byref(supported),
        ctypes.byref(current),
        ctypes.byref(maximum),
    )
    if not ok:
        return None
    value = int(current.value)
    if value > 0xFF:
        return None
    return value


def set_input_source(handle: int, value: int, state_key: str | None = None) -> None:
    ok = dxva2.SetVCPFeature(
        wintypes.HANDLE(handle),
        wintypes.BYTE(VCP_INPUT_SELECT),
        wintypes.DWORD(value),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "SetVCPFeature failed")
    _write_last_input(state_key or str(handle), value)


def switch_monitors_to_inputs(
    monitors: list[MonitorInfo],
    assignments: list[tuple[MonitorInfo, int]],
) -> list[str]:
    """Set explicit inputs on multiple monitors. Returns status lines."""
    lines: list[str] = []
    for monitor, target in assignments:
        if not monitor.controllable or monitor.handle is None:
            lines.append(f"[{monitor.position}] skipped (not controllable)")
            continue
        set_input_source(monitor.handle, target, monitor.device_name)
        lines.append(
            f"[{monitor.position}] {monitor.description} -> {input_name(target)}"
        )
    return lines


def _state_path():
    from pathlib import Path

    return Path(__file__).with_name(".switcher_state.json")


def _load_state() -> dict:
    import json

    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    import json

    _state_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _pc_state_path():
    return _state_path()


def read_active_pc(group_id: str = "default") -> str:
    data = _load_state()
    pc_groups = data.get("pc_groups", {})
    active = pc_groups.get(group_id, "a")
    return "b" if active == "b" else "a"


def write_active_pc(active: str, group_id: str = "default") -> None:
    data = _load_state()
    pc_groups = data.setdefault("pc_groups", {})
    pc_groups[group_id] = "b" if active == "b" else "a"
    _save_state(data)


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
    for entry in monitor_configs:
        if not entry.get("enabled", True):
            continue
        monitor = resolve_monitor(monitors, entry)
        if monitor is None:
            continue
        assignments.append((monitor, int(entry[input_key])))

    lines = switch_monitors_to_inputs(monitors, assignments)
    write_active_pc(target_pc, group_id)
    return target_name, lines


def toggle_input(
    handle: int,
    input_a: int,
    input_b: int,
    state_key: str | None = None,
) -> tuple[int, int]:
    """Toggle between two input sources. Returns (previous, new)."""
    key = state_key or str(handle)
    current = get_input_source(handle)
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

    set_input_source(handle, target, state_key=key)
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


def _write_last_input(state_key: str, value: int) -> None:
    data = _load_state()
    monitor_inputs = data.setdefault("monitor_inputs", {})
    monitor_inputs[state_key] = value
    _save_state(data)


def _position_name(index: int, total: int) -> str:
    if total == 1:
        return "only"
    if total == 2:
        return "left" if index == 0 else "right"
    if total == 3:
        return ("left", "center", "right")[index]
    return f"screen-{index + 1}"


def _is_controllable(description: str, handle: int | None) -> bool:
    if not handle:
        return False
    lowered = description.lower()
    if "generic pnp monitor" in lowered:
        return False
    return True


def _read_physical_monitor(
    hmonitor: wintypes.HMONITOR,
) -> tuple[str, int | None]:
    count = wintypes.DWORD()
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)):
        return "Unknown monitor", None
    if count.value == 0:
        return "Unknown monitor", None

    physical_array = (PHYSICAL_MONITOR * count.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(
        hmonitor, count.value, physical_array
    ):
        return "Unknown monitor", None

    physical = physical_array[0]
    description = physical.szPhysicalMonitorDescription.strip() or "Unknown monitor"
    handle_value = physical.hPhysicalMonitor
    if handle_value in (None, 0):
        return description, None
    return description, int(handle_value)


def enumerate_monitors(controllable_only: bool = False) -> list[MonitorInfo]:
    entries: list[tuple[MONITORINFOEXW, str, int | None]] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    def callback(hmonitor, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        description, handle = _read_physical_monitor(hmonitor)
        entries.append((info, description, handle))
        return True

    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError("EnumDisplayMonitors failed")

    entries.sort(key=lambda item: (item[0].rcMonitor.left, item[0].rcMonitor.top))
    total = len(entries)
    result: list[MonitorInfo] = []

    for index, (info, description, handle) in enumerate(entries):
        rect = info.rcMonitor
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
            handle=handle,
            controllable=_is_controllable(description, handle),
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
        if monitor.controllable and monitor.handle is not None:
            current = get_input_source(monitor.handle)
        else:
            current = None

        status = "switchable" if monitor.controllable else "not switchable (no DDC/CI handle)"
        print(f"  [{monitor.position}] {monitor.description} - {status}")
        print(f"    Device: {monitor.device_name}")
        print(f"    Layout: {monitor.width}x{monitor.height} at ({monitor.left}, {monitor.top})")
        print(f"    Current input: {_format_input(current)}")
        print()

    if not controllable:
        print("Enable DDC/CI in each monitor's OSD menu, then run 'list' again.")
