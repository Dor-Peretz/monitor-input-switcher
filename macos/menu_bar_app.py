#!/usr/bin/env python3
"""Background macOS menu bar app for monitor input switching.

macOS equivalent of the Windows tray_app.py, built on rumps.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from carbon_hotkey import CarbonHotKeyManager
from config_store import config_path, load_config
from ddc_monitor import enumerate_monitors, pc_switch_headline, toggle_pc_group
from hotkey_service import (
    IDLE_TITLE as _IDLE_TITLE,
    begin_progress,
    end_progress,
    menu_title,
    other_pc_name,
    read_progress,
)
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
        super().__init__(_IDLE_TITLE, quit_button=None)  # ⇄ in the menu bar
        # Native Carbon global hotkeys (RegisterEventHotKey). Must be created and
        # start()ed on the main thread, which __init__ / rumps callbacks are.
        self.service = CarbonHotKeyManager()
        self._config_mtime = 0.0
        self._config_error = ""
        self._switch_lock = threading.Lock()
        # rumps.notification must run on the main thread; worker threads queue
        # here and _poll shows them.
        self._notifications: queue.Queue[tuple[str, str]] = queue.Queue()

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
        from streamdeck_hub import ensure_hub_listener

        ensure_hub_listener()

    # -- lifecycle -------------------------------------------------------- #

    def _start_hotkeys(self) -> bool:
        """(Re)register hotkeys from config. Returns False if it can't be read.

        The config mtime is only recorded after a successful load, so a
        half-written file is retried on the next poll.
        """
        path = config_path()
        try:
            existed = path.exists()
            mtime = path.stat().st_mtime if existed else 0.0
            config = load_config()
            if not existed:
                mtime = path.stat().st_mtime
        except Exception as exc:
            message = f"Could not load {path.name}: {exc}"
            if message != self._config_error:
                self._config_error = message
                self._notify("Config error", message)
                log(message)
            return False
        self._config_error = ""
        self._config_mtime = mtime
        self.service.start(config)
        self._report_hotkey_errors()
        return True

    def reload_hotkeys(self, _sender=None) -> None:
        self._config_error = ""
        if self._start_hotkeys():
            self._notify("Monitor Input Switcher", "Hotkeys reloaded.")

    def switch_now(self, _sender=None) -> None:
        # Run the DDC switch on a thread so the blocking subprocess work does
        # not freeze the menu / run loop. The menu bar title counts seconds
        # until that thread clears the progress flag.
        if not self._switch_lock.acquire(blocking=False):
            self._notify("Monitor Input Switcher", "A switch is already running.")
            return
        try:
            threading.Thread(target=self._do_switch, daemon=True).start()
        except Exception as exc:
            self._switch_lock.release()
            log(f"Switch now failed: {exc}")
            self._notify("Switch failed", str(exc))

    def _do_switch(self) -> None:
        token = ""
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
            token = begin_progress(other_pc_name(), "monitors")
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
            ok, headline = pc_switch_headline(target_name, lines)
            self._notify(
                "Monitor Input Switcher" if ok else "Switch incomplete", headline
            )
        except Exception as exc:
            log(f"Switch now failed: {exc}")
            self._notify("Switch failed", str(exc))
        finally:
            end_progress(token)
            self._switch_lock.release()

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
            self._notify("Monitor Input Switcher", "Login item enabled. It takes effect at next login.")
        self._refresh_startup_state()

    def quit_app(self, _sender=None) -> None:
        self.service.stop()
        rumps.quit_application()

    # -- polling ---------------------------------------------------------- #

    @rumps.timer(0.4)
    def _poll(self, _timer) -> None:
        # Each step is isolated so one failure can't stop the timer or skip
        # the others (notifications always drain last).
        for step in (
            self._apply_switch_title,
            self._report_hotkey_errors,
            self._report_hotkey_events,
            self._watch_config,
            self._show_notifications,
        ):
            try:
                step()
            except Exception as exc:
                log(f"Poll error in {step.__name__}: {exc}")

    def _report_hotkey_errors(self) -> None:
        for error in self.service.poll_errors():
            self._notify("Hotkey error", error)
            log(f"Hotkey error: {error}")

    def _report_hotkey_events(self) -> None:
        for event in self.service.poll_events():
            if event:
                self._notify("Monitor Input Switcher", event[0])
            for line in event:
                log(line)

    def _watch_config(self) -> None:
        try:
            mtime = config_path().stat().st_mtime
        except OSError:
            return
        if mtime > self._config_mtime and self._start_hotkeys():
            self._notify("Monitor Input Switcher", "Config changed — reloaded.")

    # -- helpers ---------------------------------------------------------- #

    def _apply_switch_title(self) -> None:
        progress = read_progress()
        if progress is None:
            title = _IDLE_TITLE
        else:
            title = menu_title(
                str(progress.get("target") or ""),
                int(progress.get("elapsed") or 0),
            )
        if self.title != title:
            self.title = title

    def _notify(self, title: str, message: str) -> None:
        self._notifications.put((title, message))

    def _show_notifications(self) -> None:
        while True:
            try:
                title, message = self._notifications.get_nowait()
            except queue.Empty:
                return
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
