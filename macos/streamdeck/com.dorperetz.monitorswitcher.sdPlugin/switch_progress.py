"""Shared "switch in progress" flag for the Stream Deck keys and the menu bar.

A Mac/PC handoff or a desk-wide input switch can take several seconds. The
process doing the switch writes this file when it starts and removes it when
the switch finishes. The plugin paints a running timer from the file, and the
menu bar app shows the same timer, including when a hotkey started the switch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_AGE_SECONDS = 90.0


def progress_path() -> Path:
    # this file -> plugin dir -> streamdeck -> macos
    return Path(__file__).resolve().parents[2] / ".switch_progress.json"


def compact_name(target: str) -> str:
    name = (target or "").split("(", 1)[0].strip()
    if len(name) > 12:
        name = name[:12].rstrip()
    return name


def switch_title(target: str, elapsed: int) -> str:
    """Key title while a switch is still running."""
    seconds = max(0, int(elapsed))
    name = compact_name(target)
    if name:
        return f"to {name}\n{seconds}s"
    return f"Switching\n{seconds}s"


def menu_title(target: str, elapsed: int) -> str:
    """Menu bar title while a switch is still running."""
    seconds = max(0, int(elapsed))
    name = compact_name(target)
    if name:
        return f"\u21c6 {name} {seconds}s"
    return f"\u21c6 {seconds}s"


def other_pc_name() -> str:
    """Name of the computer a PC-switch will move the monitors to."""
    from config_store import load_config
    from ddc_monitor import read_active_pc

    pc = load_config().get("pc_switch", {}) or {}
    if read_active_pc() == "a":
        return str(pc.get("pc_b_name") or "PC")
    return str(pc.get("pc_a_name") or "Mac")


def begin_hotkey_progress(kind: str) -> str:
    """Start the timer for a hotkey. Returns "" when this hotkey is not a switch."""
    try:
        if kind == "pc_switch":
            return begin_progress(other_pc_name(), "monitors")
        if kind == "monitor":
            return begin_progress("", "input")
    except Exception:
        return ""
    return ""


def begin_progress(target: str, kind: str, origin: str = "") -> str:
    """Publish that a switch has started. Returns a token for :func:`end_progress`."""
    token = f"{os.getpid()}-{time.time():.6f}"
    payload = {
        "target": target,
        "kind": kind,
        "origin": origin,
        "started": time.time(),
        "token": token,
    }
    path = progress_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return token


def end_progress(token: str | None = None) -> None:
    """Remove the flag. A token only clears the switch that created it."""
    path = progress_path()
    if token:
        current = _read_raw()
        if current is not None and str(current.get("token") or "") != token:
            return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_progress(max_age: float = MAX_AGE_SECONDS) -> dict | None:
    """Current switch, or None. A stale flag is removed."""
    data = _read_raw()
    if data is None:
        return None
    try:
        started = float(data["started"])
    except (KeyError, TypeError, ValueError):
        end_progress()
        return None
    age = time.time() - started
    if age < -1 or age > max_age:
        end_progress()
        return None
    data["elapsed"] = max(0, int(age))
    return data


def _read_raw() -> dict | None:
    try:
        data = json.loads(progress_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
