#!/usr/bin/env python3
"""Background system-tray app for monitor input switching."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from config_store import config_path, load_config
from hotkey_service import HotkeyService
from startup_manage import disable_startup, enable_startup, is_startup_enabled

from runtime_paths import app_dir
UI_SCRIPT = app_dir() / "ui.py"
LOG_FILE = app_dir() / "tray.log"
ICON_FILE = app_dir() / "tray_icon.ico"
MUTEX_NAME = "MonitorInputSwitcherTray"
_mutex_handle = None


def log(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def acquire_single_instance() -> bool:
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:
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
        self._stop_watcher = threading.Event()
        self._startup_enabled = is_startup_enabled()

    def run(self) -> None:
        if not acquire_single_instance():
            return

        if not is_startup_enabled():
            enable_startup()
            self._startup_enabled = True

        self._start_hotkeys()
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
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.icon = pystray.Icon(
            "monitor_input_switcher",
            load_tray_image(),
            "Monitor Input Switcher",
            menu,
            on_activate=self.open_settings,
        )
        log("Starting tray icon.")
        self.icon.run(self._on_icon_ready)

    def _on_icon_ready(self, icon: pystray.Icon) -> None:
        icon.visible = True
        self._notify("Running in background. Double-click to open settings.")

    def _start_hotkeys(self) -> None:
        config = load_config()
        path = config_path()
        if path.exists():
            self._config_mtime = path.stat().st_mtime
        self.service.start(config)
        for error in self.service.poll_errors():
            self._notify(f"Hotkey error: {error}")

    def reload_hotkeys(self, _icon=None, _item=None) -> None:
        self._start_hotkeys()
        self._notify("Hotkeys reloaded.")

    def open_settings(self, _icon=None, _item=None) -> None:
        subprocess.Popen(
            [sys.executable, str(UI_SCRIPT), "--settings-only"],
            cwd=str(app_dir()),
        )

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
        time.sleep(2)
        if path.exists():
            self._config_mtime = path.stat().st_mtime
        while not self._stop_watcher.is_set():
            if path.exists():
                mtime = path.stat().st_mtime
                if mtime > self._config_mtime:
                    self._config_mtime = mtime
                    self.reload_hotkeys()
            time.sleep(1.5)

    def _notify(self, message: str) -> None:
        if self.icon:
            try:
                self.icon.notify(message, "Monitor Input Switcher")
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
