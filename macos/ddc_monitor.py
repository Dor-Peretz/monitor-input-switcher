"""macOS DDC/CI monitor control.

macOS has no built-in DDC/CI API (unlike Windows' dxva2.dll), so this module
shells out to a small external CLI:

* Apple Silicon: ``m1ddc``   (``brew install m1ddc``)
* Intel Macs:    ``ddcctl``  (``brew install ddcctl``)

Important limitation: neither tool can reliably *read* the current input
source over DDC (m1ddc only reads luminance/contrast/volume). Toggling
therefore relies on the persisted state file written every time we switch,
falling back to a sensible default when no state exists.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Common locations for Homebrew/MacPorts CLIs. Needed because launchd starts
# processes with a minimal PATH (no /opt/homebrew/bin), so shutil.which alone
# fails to find m1ddc/ddcctl when the app runs as a LaunchAgent.
_COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")

VCP_INPUT_SELECT = 0x60

# Standard DDC/CI VCP 0x60 input source values (same as the Windows build).
INPUT_SOURCE_NAMES: dict[int, str] = {
    0x01: "VGA",
    0x03: "DVI-1",
    0x04: "DVI-2",
    0x0F: "DisplayPort-1",
    0x10: "DisplayPort-2",
    0x11: "HDMI-1",
    0x12: "HDMI-2",
    0x1B: "USB-C",
}

_BUILTIN_HINTS = ("built-in", "builtin", "color lcd", "retina display", "liquid retina")


class DDCError(RuntimeError):
    """Raised when the underlying DDC CLI is missing or fails."""


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
    handle: int | None  # the CLI display number used for addressing
    controllable: bool

    @property
    def label(self) -> str:
        primary = " [primary]" if self.is_primary else ""
        return f"{self.position}: {self.description}{primary}"


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


def _format_input(value: int | None) -> str:
    if value is None:
        return "unknown"
    name = INPUT_SOURCE_NAMES.get(value)
    return f"{name} (0x{value:02X})" if name else f"0x{value:02X}"


# --------------------------------------------------------------------------- #
# DDC backends
# --------------------------------------------------------------------------- #


class _Backend:
    tool_name = ""
    install_hint = ""

    def path(self) -> str | None:
        found = shutil.which(self.tool_name)
        if found:
            return found
        for directory in _COMMON_BIN_DIRS:
            candidate = os.path.join(directory, self.tool_name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def available(self) -> bool:
        return self.path() is not None

    def list_displays(self) -> list[tuple[int, str]]:
        raise NotImplementedError

    def set_input(self, number: int, value: int) -> None:
        raise NotImplementedError

    def get_input(self, number: int) -> int | None:  # noqa: ARG002
        # Neither m1ddc nor ddcctl expose a reliable input read.
        return None


class _M1DDCBackend(_Backend):
    tool_name = "m1ddc"
    install_hint = "brew install m1ddc"

    def list_displays(self) -> list[tuple[int, str]]:
        out = _run([self.path(), "display", "list"])
        displays: list[tuple[int, str]] = []
        for line in out.splitlines():
            match = re.match(r"\s*\[(\d+)\]\s*(.+?)\s*$", line)
            if not match:
                continue
            number = int(match.group(1))
            name = _clean_name(match.group(2))
            displays.append((number, name))
        return displays

    def set_input(self, number: int, value: int) -> None:
        _run([self.path(), "display", str(number), "set", "input", str(value)])


class _DDCCtlBackend(_Backend):
    tool_name = "ddcctl"
    install_hint = "brew install ddcctl"

    def list_displays(self) -> list[tuple[int, str]]:
        # ddcctl has no clean list command; use system_profiler for names and
        # assume its ordering matches ddcctl's 1-based display indexes.
        names = _system_profiler_display_names()
        if names:
            return [(i + 1, name) for i, name in enumerate(names)]
        # Fall back to whatever ddcctl reports it found.
        out = _run([self.path()], check=False)
        count = 0
        for line in out.splitlines():
            match = re.search(r"(\d+)\s+display", line, re.IGNORECASE)
            if match:
                count = int(match.group(1))
                break
        return [(i + 1, f"Display {i + 1}") for i in range(count)]

    def set_input(self, number: int, value: int) -> None:
        _run([self.path(), "-d", str(number), "-i", str(value)])


def _run(cmd: list[str], check: bool = True) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DDCError(f"Failed to run {cmd[0]}: {exc}") from exc
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise DDCError(f"{Path(cmd[0]).name} failed: {message}")
    return result.stdout


def _clean_name(raw: str) -> str:
    # m1ddc appends parenthetical detail, e.g. "DELL U2720Q (online)".
    name = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    return name or raw.strip()


def _system_profiler_display_names() -> list[str]:
    try:
        out = _run(["system_profiler", "SPDisplaysDataType", "-json"], check=False)
        data = json.loads(out or "{}")
    except (DDCError, json.JSONDecodeError):
        return []

    names: list[str] = []
    for gpu in data.get("SPDisplaysDataType", []):
        for display in gpu.get("spdisplays_ndrvs", []):
            names.append(display.get("_name", "Display"))
    return names


def _select_backend() -> _Backend:
    prefer_m1 = platform.machine().lower() in {"arm64", "aarch64"}
    m1 = _M1DDCBackend()
    ddc = _DDCCtlBackend()
    order = (m1, ddc) if prefer_m1 else (ddc, m1)
    for backend in order:
        if backend.available():
            return backend
    # None installed: return the preferred one so callers can surface a hint.
    return order[0]


def active_backend() -> _Backend:
    return _select_backend()


def _screen_positions() -> list[tuple[str, int, bool]]:
    """Best-effort (name, x, is_primary) list from AppKit, for nicer labels."""
    try:
        from AppKit import NSScreen  # type: ignore

        screens = []
        all_screens = list(NSScreen.screens())
        primary_top = all_screens[0].frame().origin.y if all_screens else None
        for screen in all_screens:
            frame = screen.frame()
            try:
                name = str(screen.localizedName())
            except Exception:
                name = "Display"
            is_primary = frame.origin.y == primary_top and frame.origin.x == 0
            screens.append((name, int(frame.origin.x), is_primary))
        return screens
    except Exception:
        return []


def _position_name(index: int, total: int) -> str:
    if total == 1:
        return "only"
    if total == 2:
        return "left" if index == 0 else "right"
    if total == 3:
        return ("left", "center", "right")[index]
    return f"screen-{index + 1}"


def _is_builtin(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _BUILTIN_HINTS)


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #


def enumerate_monitors(
    controllable_only: bool = False,
    with_positions: bool = False,
) -> list[MonitorInfo]:
    """Enumerate displays via the active DDC backend.

    ``with_positions`` enriches left/right/center labels using AppKit's
    ``NSScreen`` geometry. It is opt-in because AppKit should only be touched
    from a process with a GUI session (the settings UI / menu bar app), never
    from the switching hot path or headless contexts.
    """
    backend = active_backend()
    if not backend.available():
        raise DDCError(
            f"'{backend.tool_name}' is not installed. Install it with: "
            f"{backend.install_hint}"
        )

    raw = backend.list_displays()
    screen_hints = _screen_positions() if with_positions else []

    def hint_for(name: str) -> tuple[int, bool] | None:
        for hint_name, x, is_primary in screen_hints:
            if _names_match(name, hint_name):
                return x, is_primary
        return None

    enriched: list[tuple[int, str, int, bool]] = []
    for order_index, (number, name) in enumerate(raw):
        hint = hint_for(name)
        x = hint[0] if hint else order_index
        is_primary = hint[1] if hint else (order_index == 0)
        enriched.append((number, name, x, is_primary))

    enriched.sort(key=lambda item: item[2])
    total = len(enriched)

    monitors: list[MonitorInfo] = []
    for index, (number, name, x, is_primary) in enumerate(enriched):
        controllable = not _is_builtin(name)
        monitor = MonitorInfo(
            index=index,
            position=_position_name(index, total),
            device_name=name,
            description=name,
            left=x,
            top=0,
            width=0,
            height=0,
            is_primary=is_primary,
            handle=number,
            controllable=controllable,
        )
        if controllable_only and not monitor.controllable:
            continue
        monitors.append(monitor)

    return monitors


def _names_match(a: str, b: str) -> bool:
    a_low = a.strip().lower()
    b_low = b.strip().lower()
    if not a_low or not b_low:
        return False
    return a_low == b_low or a_low in b_low or b_low in a_low


def find_monitor(monitors: list[MonitorInfo], selector: str) -> MonitorInfo:
    monitor = resolve_monitor(monitors, {"monitor": selector})
    if monitor is None:
        available = ", ".join(sorted({m.position for m in monitors}))
        raise ValueError(
            f"Monitor '{selector}' not found. Available positions: {available}"
        )
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
        }:
            return monitor

    if selector in {"primary", "main"}:
        matches = [m for m in monitors if m.is_primary]
        if len(matches) == 1:
            return matches[0]
    return None


# --------------------------------------------------------------------------- #
# Switching
# --------------------------------------------------------------------------- #


def get_input_source(handle: int) -> int | None:  # noqa: ARG001
    # DDC read of the input source is not available on macOS CLIs.
    return None


def set_input_source(handle: int, value: int, state_key: str | None = None) -> None:
    backend = active_backend()
    if not backend.available():
        raise DDCError(
            f"'{backend.tool_name}' is not installed. Install it with: "
            f"{backend.install_hint}"
        )
    backend.set_input(handle, value)
    _write_last_input(state_key or str(handle), value)


def switch_monitors_to_inputs(
    monitors: list[MonitorInfo],
    assignments: list[tuple[MonitorInfo, int]],
) -> list[str]:
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


def toggle_input(
    handle: int,
    input_a: int,
    input_b: int,
    state_key: str | None = None,
) -> tuple[int, int]:
    """Toggle between two input sources. Returns (previous, new).

    Since we cannot read the live input over DDC, the decision is based on the
    last value we wrote (persisted state).
    """
    key = state_key or str(handle)
    last = _read_last_input(key)

    if last == input_a:
        target = input_b
    elif last == input_b:
        target = input_a
    else:
        target = input_b

    set_input_source(handle, target, state_key=key)
    previous = last if last is not None else -1
    return previous, target


# --------------------------------------------------------------------------- #
# Persistent state (last written inputs + active PC group)
# --------------------------------------------------------------------------- #


def _state_path() -> Path:
    return Path(__file__).with_name(".switcher_state.json")


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    _state_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read_last_input(state_key: str) -> int | None:
    data = _load_state()
    monitor_inputs = data.get("monitor_inputs", {})
    if isinstance(monitor_inputs, dict) and state_key in monitor_inputs:
        try:
            return int(monitor_inputs[state_key])
        except (TypeError, ValueError):
            return None
    return None


def _write_last_input(state_key: str, value: int) -> None:
    data = _load_state()
    monitor_inputs = data.setdefault("monitor_inputs", {})
    monitor_inputs[state_key] = value
    _save_state(data)


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
        if entry.get(input_key) is None:
            continue
        try:
            target = parse_input_value(entry[input_key])
        except (ValueError, TypeError):
            continue
        assignments.append((monitor, target))

    lines = switch_monitors_to_inputs(monitors, assignments)
    write_active_pc(target_pc, group_id)
    return target_name, lines


# --------------------------------------------------------------------------- #
# CLI helper
# --------------------------------------------------------------------------- #


def print_monitor_list(monitors: list[MonitorInfo]) -> None:
    if not monitors:
        print("No displays detected.")
        return

    controllable = [m for m in monitors if m.controllable]
    backend = active_backend()
    print(
        f"Found {len(monitors)} display(s), {len(controllable)} controllable via "
        f"{backend.tool_name} (DDC/CI):\n"
    )

    for monitor in monitors:
        status = (
            "switchable"
            if monitor.controllable
            else "not switchable (built-in or unsupported)"
        )
        print(f"  [{monitor.position}] {monitor.description} - {status}")
        print(f"    {backend.tool_name} display #: {monitor.handle}")
        print()

    if not controllable:
        print(
            "No external DDC/CI displays detected. Note: m1ddc does not support "
            "the built-in HDMI port on some Macs."
        )
