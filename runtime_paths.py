"""Resolve application, resource, and user-data directories."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    path = base / "MonitorInputSwitcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


def executable_path() -> Path:
    return Path(sys.executable).resolve()


def pythonw_path() -> Path:
    if is_frozen():
        return executable_path()
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def settings_command() -> list[str]:
    if is_frozen():
        return [str(executable_path()), "--settings-only"]
    return [str(pythonw_path()), str(app_dir() / "ui.py"), "--settings-only"]
