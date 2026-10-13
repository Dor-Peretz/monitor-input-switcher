#!/usr/bin/env python3
"""Background system-tray app for monitor input switching."""

from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from config_store import config_path, load_config
from hotkey_service import HotkeyService
from startup_manage import disable_startup, enable_startup, is_startup_enabled
from version import __app_name__, __github_url__, __version__

from runtime_paths import app_dir, is_frozen, settings_command, user_data_dir

STATE_DIR = user_data_dir() if is_frozen() else app_dir()
LOG_FILE = STATE_DIR / "tray.log"
LOG_MAX_BYTES = 1_000_000
ICON_FILE = STATE_DIR / "tray_icon.ico"
STARTUP_MARKER = user_data_dir() / "startup_initialized"
TRAY_TITLE = f"{__app_name__} v{__version__}"
MUTEX_NAME = "MonitorInputSwitcherTray"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None
_log_lock = threading.Lock()


def log(message: str) -> None:
    with _log_lock:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_suffix(".1.log"))
        except OSError:
            pass
        try:
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass


def acquire_single_instance() -> bool:
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    last_error = ctypes.get_last_error()
    if not _mutex_handle:
        log(f"CreateMutexW failed (error {last_error}); skipping single-instance check.")
        return True
    if last_error == ERROR_ALREADY_EXISTS:
        log("Another tray instance is already running.")
        return False
    return True


def create_icon_image() -> Image.Image:
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 14, 56, 46), radius=6, fill=(45, 110, 220, 255))
    draw.rectangle((24, 46, 40, 54), fill=(80, 80, 80, 255))
    draw.ellipse((28, 52, 36, 60), fill=(60, 60, 60, 255))
    draw.polygon((18, 24, 30, 24, 24, 18), fill=(180, 220, 255, 255))
    return image


def load_tray_image() -> Image.Image:
    image = create_icon_image()
    if not ICON_FILE.exists():
        rgb = Image.new("RGB", image.size, (255, 255, 255))
        rgb.paste(image, mask=image.split()[3])
        rgb.save(ICON_FILE, format="ICO", sizes=[(64, 64), (32, 32), (16, 16)])
    return Image.open(ICON_FILE)


class TrayApplication:
    def __init__(self) -> None:
        self.service = HotkeyService()
        self.icon: pystray.Icon | None = None
        self._config_mtime = 0.0
        self._config_error: str | None = None
        self._stop_watcher = threading.Event()
        self._startup_enabled = is_startup_enabled()
        self._notify_lock = threading.Lock()
        self._icon_ready = False
        self._pending_notices: list[str] = []

    def run(self) -> None:
        if not acquire_single_instance():
            return

        self._enable_startup_on_first_run()

        self._start_hotkeys()
        try:
            from streamdeck_hub import ensure_hub_listener

            ensure_hub_listener()
        except Exception as exc:
            log(f"Stream Deck hub listener not started: {exc}")
        watcher = threading.Thread(target=self._watch_config, daemon=True)
        watcher.start()

        menu = pystray.Menu(
            pystray.MenuItem("Open settings", self.open_settings, default=True),
            pystray.MenuItem("Reload hotkeys", self.reload_hotkeys),
            pystray.MenuItem(
                "Run at startup",
                self.toggle_startup,
                checked=lambda _item: self._startup_enabled,
            ),
            pystray.MenuItem(
                f"About v{__version__}",
                self.show_about,
            ),
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.icon = pystray.Icon(
            "monitor_input_switcher",
            load_tray_image(),
            TRAY_TITLE,
            menu,
            on_activate=self.open_settings,
        )
        log("Starting tray icon.")
        self.icon.run(self._on_icon_ready)

    def _enable_startup_on_first_run(self) -> None:
        if STARTUP_MARKER.exists():
            return
        try:
            if not is_startup_enabled():
                enable_startup()
                log("Enabled run at startup (first run).")
            self._startup_enabled = True
            STARTUP_MARKER.write_text(f"{__version__}\n", encoding="utf-8")
        except OSError as exc:
            log(f"Could not set up run at startup: {exc}")

    def _on_icon_ready(self, icon: pystray.Icon) -> None:
        icon.visible = True
        with self._notify_lock:
            self._icon_ready = True
            pending = self._pending_notices
            self._pending_notices = []
        if pending:
            self._notify(" | ".join(pending)[:240])
        else:
            self._notify("Running in background. Double-click to open settings.")

    def _start_hotkeys(self, repeat_errors: bool = True) -> bool:
        path = config_path()
        try:
            if path.exists():
                self._config_mtime = path.stat().st_mtime
            config = load_config()
        except Exception as exc:
            error = f"Could not read {path.name}: {exc}"
            if repeat_errors or error != self._config_error:
                log(error)
                self._notify(error)
            self._config_error = error
            return False
        self._config_error = None
        self.service.start(config)
        for error in self.service.poll_errors():
            log(f"Hotkey error: {error}")
            self._notify(f"Hotkey error: {error}")
        return True

    def reload_hotkeys(self, _icon=None, _item=None) -> None:
        if self._start_hotkeys():
            self._notify("Hotkeys reloaded.")

    def open_settings(self, _icon=None, _item=None) -> None:
        subprocess.Popen(
            settings_command(),
            cwd=str(app_dir()),
        )

    def show_about(self, _icon=None, _item=None) -> None:
        self._notify(f"v{__version__} — {__github_url__}")

    def toggle_startup(self, _icon=None, _item=None) -> None:
        if self._startup_enabled:
            disable_startup()
            self._startup_enabled = False
            self._notify("Startup disabled.")
        else:
            enable_startup()
            self._startup_enabled = True
            self._notify("Startup enabled.")
        if self.icon:
            self.icon.update_menu()

    def exit_app(self, _icon=None, _item=None) -> None:
        self._stop_watcher.set()
        self.service.stop()
        if self.icon:
            self.icon.stop()

    def _watch_config(self) -> None:
        path = config_path()
        self._stop_watcher.wait(2)
        while not self._stop_watcher.is_set():
            try:
                self._check_config(path)
                self._report_service()
            except Exception as exc:
                log(f"Config watcher error: {exc}")
            self._stop_watcher.wait(1.5)

    def _check_config(self, path: Path) -> None:
        changed = path.exists() and path.stat().st_mtime > self._config_mtime
        # A failed read (e.g. a half-written file) is retried every tick.
        if changed or self._config_error:
            if self._start_hotkeys(repeat_errors=False):
                self._notify("Hotkeys reloaded.")

    def _report_service(self) -> None:
        for event in self.service.poll_events():
            summary = " | ".join(event)
            log(summary)
            self._notify(summary[:180])
        for error in self.service.poll_errors():
            log(f"Hotkey error: {error}")
            self._notify(f"Hotkey error: {error}")

    def _notify(self, message: str) -> None:
        with self._notify_lock:
            if not self._icon_ready:
                self._pending_notices.append(message)
                return
        if self.icon:
            try:
                self.icon.notify(message, TRAY_TITLE)
            except Exception:
                pass


def main() -> None:
    try:
        TrayApplication().run()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        raise


if __name__ == "__main__":
    main()
