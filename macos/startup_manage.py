"""Install or remove a macOS LaunchAgent so the menu bar app runs at login."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

APP_LABEL = "com.monitorinputswitcher.tray"


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return _launch_agents_dir() / f"{APP_LABEL}.plist"


def _python_executable() -> str:
    return sys.executable or "python3"


def _plist_contents() -> str:
    python = _xml_escape(_python_executable())
    menu_bar = _xml_escape(str(app_dir() / "menu_bar_app.py"))
    working_dir = _xml_escape(str(app_dir()))
    log_out = _xml_escape(str(app_dir() / "tray.out.log"))
    log_err = _xml_escape(str(app_dir() / "tray.err.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{APP_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{menu_bar}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""


def is_startup_enabled() -> bool:
    return plist_path().exists()


def enable_startup() -> None:
    """Install the LaunchAgent; launchd picks it up at the next login.

    It is deliberately not loaded now: with RunAtLoad, ``launchctl load``
    would start a second copy of the app alongside the one already running.
    """
    directory = _launch_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{APP_LABEL}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(_plist_contents())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, plist_path())
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def disable_startup() -> None:
    plist_path().unlink(missing_ok=True)
    # launchd sets XPC_SERVICE_NAME to the job label; removing our own job
    # would SIGTERM this process.
    if os.environ.get("XPC_SERVICE_NAME") == APP_LABEL:
        return
    try:
        subprocess.run(
            ["launchctl", "remove", APP_LABEL],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
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
