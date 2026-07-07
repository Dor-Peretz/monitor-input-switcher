"""Install or remove Windows startup registration."""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

from runtime_paths import app_dir, executable_path, is_frozen, pythonw_path, settings_command

APP_NAME = "MonitorInputSwitcher"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if is_frozen():
        return f'"{executable_path()}"'
    tray = app_dir() / "tray_app.py"
    return f'"{pythonw_path()}" "{tray}"'


def is_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def enable_startup() -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())


def disable_startup() -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args or args[0] == "status":
        print("enabled" if is_startup_enabled() else "disabled")
        return 0
    if args[0] == "enable":
        enable_startup()
        print("Startup enabled.")
        return 0
    if args[0] == "disable":
        disable_startup()
        print("Startup disabled.")
        return 0
    print("Usage: startup_manage.py [enable|disable|status]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
