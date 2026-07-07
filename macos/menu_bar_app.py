#!/usr/bin/env python3
"""Background macOS menu bar app for monitor input switching.

macOS equivalent of the Windows tray_app.py, built on rumps.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from carbon_hotkey import CarbonHotKeyManager
from config_store import config_path, load_config
from ddc_monitor import enumerate_monitors, toggle_pc_group
from startup_manage import disable_startup, enable_startup, is_startup_enabled

APP_DIR = Path(__file__).resolve().parent
UI_SCRIPT = APP_DIR / "ui.py"
LOG_FILE = APP_DIR / "tray.log"


def log(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


class MenuBarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("\u21c6", quit_button=None)  # ⇄ in the menu bar
        # Native Carbon global hotkeys (RegisterEventHotKey). Must be created and
        # start()ed on the main thread, which __init__ / rumps callbacks are.
        self.service = CarbonHotKeyManager()
        self._config_mtime = 0.0

        self.startup_item = rumps.MenuItem(
            "Run at login", callback=self.toggle_startup
        )
        self.menu = [
            rumps.MenuItem("Switch now (PC toggle)", callback=self.switch_now),
            rumps.MenuItem("Open settings\u2026", callback=self.open_settings),
            rumps.MenuItem("Reload hotkeys", callback=self.reload_hotkeys),
            self.startup_item,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self._refresh_startup_state()
        self._start_hotkeys()

    # -- lifecycle -------------------------------------------------------- #

    def _start_hotkeys(self) -> None:
        config = load_config()
        path = config_path()
        if path.exists():
            self._config_mtime = path.stat().st_mtime
        self.service.start(config)
        for error in self.service.poll_errors():
            self._notify("Hotkey error", error)
            log(f"Hotkey error: {error}")

    def reload_hotkeys(self, _sender=None) -> None:
        self._start_hotkeys()
        self._notify("Monitor Input Switcher", "Hotkeys reloaded.")

    def switch_now(self, _sender=None) -> None:
        # Run the DDC switch on a thread so the blocking subprocess work does
        # not freeze the menu / run loop.
        threading.Thread(target=self._do_switch, daemon=True).start()

    def _do_switch(self) -> None:
        try:
            config = load_config()
            pc = config.get("pc_switch", {})
            monitor_cfgs = [
                entry
                for entry in pc.get("monitors", [])
                if entry.get("enabled", True)
            ]
            if not monitor_cfgs:
                self._notify(
                    "Monitor Input Switcher",
                    "No PC-switch monitors configured. Open settings first.",
                )
                return
            monitors = enumerate_monitors()
            target_name, lines = toggle_pc_group(
                monitors,
                monitor_cfgs,
                pc.get("pc_a_name", "PC 1"),
                pc.get("pc_b_name", "PC 2"),
            )
            log(f"Switch now -> {target_name}")
            for line in lines:
                log("  " + line)
            self._notify("Monitor Input Switcher", f"Switched to {target_name}")
        except Exception as exc:
            log(f"Switch now failed: {exc}")
            self._notify("Switch failed", str(exc))

    def open_settings(self, _sender=None) -> None:
        subprocess.Popen(
            [sys.executable, str(UI_SCRIPT), "--settings-only"],
            cwd=str(APP_DIR),
        )

    def _refresh_startup_state(self) -> None:
        self.startup_item.state = 1 if is_startup_enabled() else 0

    def toggle_startup(self, _sender=None) -> None:
        if is_startup_enabled():
            disable_startup()
            self._notify("Monitor Input Switcher", "Login item disabled.")
        else:
            enable_startup()
            self._notify("Monitor Input Switcher", "Login item enabled.")
        self._refresh_startup_state()

    def quit_app(self, _sender=None) -> None:
        self.service.stop()
        rumps.quit_application()

    # -- polling ---------------------------------------------------------- #

    @rumps.timer(0.4)
    def _poll(self, _timer) -> None:
        for error in self.service.poll_errors():
            self._notify("Hotkey error", error)
            log(f"Hotkey error: {error}")

        for event in self.service.poll_events():
            if event:
                self._notify("Monitor Input Switcher", event[0])
            for line in event:
                log(line)

        self._watch_config()

    def _watch_config(self) -> None:
        path = config_path()
        if not path.exists():
            return
        mtime = path.stat().st_mtime
        if mtime > self._config_mtime:
            self._config_mtime = mtime
            self._start_hotkeys()
            self._notify("Monitor Input Switcher", "Config changed — reloaded.")

    # -- helpers ---------------------------------------------------------- #

    def _notify(self, title: str, message: str) -> None:
        try:
            rumps.notification(title, "", message)
        except Exception:
            # Notifications need a bundled/signed app; ignore when unavailable.
            pass


def main() -> None:
    try:
        MenuBarApp().run()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        raise


if __name__ == "__main__":
    main()
