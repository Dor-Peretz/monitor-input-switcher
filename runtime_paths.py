"""Resolve pythonw.exe next to the current Python interpreter."""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def pythonw_path() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return Path(sys.executable)
