"""Shared "switch in progress" flag for the Stream Deck keys and the menu bar.

A Mac/PC handoff or a desk-wide input switch can take several seconds. The
process doing the switch writes this file when it starts and removes it when
the switch finishes. The plugin paints a running timer from the file, and the
menu bar app shows the same timer, including when a hotkey started the switch.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

# Longer than the slowest deck handoff: the peer /claim timeout plus the
# ownership re-check in streamdeck_hub.toggle_deck_owner.
MAX_AGE_SECONDS = 200.0


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
    """Publish that a switch has started. Returns a token for :func:`end_progress`.

    Never raises. "" means no timer was published.
    """
    # A frozen build's __file__ is in PyInstaller's temp dir, which nothing reads.
    if _frozen():
        return ""
    token = _unique()
    tmp: Path | None = None
    try:
        payload = {
            "target": target,
            "kind": kind,
            "origin": origin,
            "started": time.time(),
            "token": token,
        }
        path = progress_path()
        tmp = path.with_name(f"{path.name}.{_unique()}.tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        _retry(lambda: os.replace(tmp, path))
    except Exception:
        if tmp is not None:
            _discard(tmp)
        return ""
    return token


def end_progress(token: str | None = None) -> None:
    """Remove the flag. A token only clears the switch that created it."""
    if _frozen():
        return
    path = progress_path()
    if not token:
        _discard(path)
        return
    # Move the flag aside before checking it, so a newer switch's flag that
    # lands in the meantime is never the one deleted.
    tomb = path.with_name(f"{path.name}.{_unique()}.end")
    try:
        _retry(lambda: os.replace(path, tomb))
    except OSError:
        return
    current = _read_raw(tomb)
    if current is not None and str(current.get("token") or "") != token:
        _restore(tomb, path)
        return
    _discard(tomb)


def read_progress(max_age: float = MAX_AGE_SECONDS) -> dict | None:
    """Current switch, or None. A stale flag is removed."""
    if _frozen():
        return None
    data = _read_raw(progress_path())
    if data is None:
        return None
    stale_token = str(data.get("token") or "") or None
    try:
        started = float(data["started"])
    except (KeyError, TypeError, ValueError):
        end_progress(stale_token)
        return None
    age = time.time() - started
    if age < -1 or age > max_age:
        end_progress(stale_token)
        return None
    data["elapsed"] = max(0, int(age))
    return data


_RETRIES = 5
_RETRY_DELAY = 0.05


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _unique() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex}"


def _retry(operation) -> None:
    # Windows refuses to replace or delete a file another process has open,
    # and the plugin reads this one every half second.
    for attempt in range(_RETRIES):
        try:
            operation()
            return
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_DELAY)


def _discard(path: Path) -> None:
    try:
        _retry(lambda: path.unlink(missing_ok=True))
    except OSError:
        pass


def _restore(tomb: Path, path: Path) -> None:
    """Put another switch's flag back, unless a newer flag already took its place."""
    try:
        os.link(tomb, path)
    except FileExistsError:
        pass
    except OSError:
        if not path.exists():
            try:
                _retry(lambda: os.replace(tomb, path))
                return
            except OSError:
                pass
    _discard(tomb)


def _read_raw(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
