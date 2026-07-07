#!/usr/bin/env python3
"""Simple UI for configuring monitor input hotkeys and PC switching."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from config_store import (
    INPUT_CHOICES,
    input_label,
    input_value_from_label,
    load_config,
    save_config,
)
from ddc_monitor import MonitorInfo, enumerate_monitors, read_active_pc
from hardware_info import (
    DELL_DISPLAY_MANAGER_URL,
    controllable_reason,
    enrich_monitor,
    get_pnp_monitors,
    open_device_manager_monitors,
    open_driver_page,
)
from hotkey_service import HotkeyService, format_hotkey


class HotkeyCapture(ttk.Entry):
    """Entry widget that captures a keyboard shortcut."""

    IGNORE_KEYS = {
        "Shift_L",
        "Shift_R",
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Meta_L",
        "Meta_R",
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._capturing = False
        self.bind("<FocusIn>", self._start_capture)
        self.bind("<FocusOut>", self._stop_capture)
        self.bind("<KeyPress>", self._on_key_press)
        self.configure(state="readonly")

    def set_value(self, value: str) -> None:
        self.configure(state="normal")
        self.delete(0, tk.END)
        if value:
            self.insert(0, value)
        self.configure(state="readonly")

    def _start_capture(self, _event=None) -> None:
        self._capturing = True
        self.configure(state="normal")
        self.delete(0, tk.END)
        self.insert(0, "Press shortcut...")
        self.select_range(0, tk.END)

    def _stop_capture(self, _event=None) -> None:
        self._capturing = False
        self.configure(state="readonly")
        if self.get() == "Press shortcut...":
            self.delete(0, tk.END)

    def _on_key_press(self, event) -> str:
        if not self._capturing:
            return "break"

        if event.keysym in self.IGNORE_KEYS:
            return "break"

        modifiers = 0
        if event.state & 0x0004:
            modifiers |= 0x0002
        if event.state & 0x0008 or event.state & 0x20000:
            modifiers |= 0x0001
        if event.state & 0x0001:
            modifiers |= 0x0004

        hotkey = format_hotkey(modifiers, event.keycode)
        self.set_value(hotkey)
        self._capturing = False
        return "break"


class MonitorSwitcherUI:
    def __init__(self, root: tk.Tk, settings_only: bool = False) -> None:
        self.root = root
        self.settings_only = settings_only
        self.root.title("Monitor Input Switcher")
        self.root.geometry("920x680")
        self.root.minsize(760, 560)

        self.service = HotkeyService() if not settings_only else None
        self.monitors: list[MonitorInfo] = []
        self.monitor_extra: dict[str, dict] = {}
        self.pnp_monitors = []
        self.config = load_config()
        self.monitor_rows: list[dict] = []
        self.pc_rows: list[dict] = []

        self._build_layout()
        self.refresh_monitors()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.settings_only:
            self.root.after(200, self._poll_service)

    def _build_layout(self) -> None:
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="Configure hotkeys to toggle monitor inputs or switch between two PCs.",
            wraplength=860,
        ).pack(anchor="w")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.monitor_tab = ttk.Frame(notebook, padding=10)
        self.pc_tab = ttk.Frame(notebook, padding=10)
        self.drivers_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.monitor_tab, text="Per Monitor")
        notebook.add(self.pc_tab, text="PC Switch (Group)")
        notebook.add(self.drivers_tab, text="Drivers")

        self._build_monitor_tab()
        self._build_pc_tab()
        self._build_drivers_tab()

        footer = ttk.Frame(self.root, padding=10)
        footer.pack(fill="x")

        ttk.Button(footer, text="Refresh Monitors", command=self.refresh_monitors).pack(
            side="left"
        )
        ttk.Button(footer, text="Save", command=self.save).pack(side="left", padx=(8, 0))
        if self.settings_only:
            ttk.Label(
                footer,
                text="Hotkeys run in the background (system tray). Save to apply.",
            ).pack(side="left", padx=(8, 0))
            self.start_button = None
            self.stop_button = None
        else:
            self.start_button = ttk.Button(
                footer, text="Start Hotkeys", command=self.start_hotkeys
            )
            self.start_button.pack(side="left", padx=(8, 0))
            self.stop_button = ttk.Button(
                footer, text="Stop Hotkeys", command=self.stop_hotkeys, state="disabled"
            )
            self.stop_button.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(
            value="Background hotkeys active" if self.settings_only else "Ready"
        )
        ttk.Label(footer, textvariable=self.status_var).pack(side="right")

        log_frame = ttk.LabelFrame(self.root, text="Activity", padding=8)
        log_frame.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=6, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _build_monitor_tab(self) -> None:
        toolbar = ttk.Frame(self.monitor_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(
            toolbar,
            text=(
                "All detected displays are listed. Only switchable monitors can be "
                "configured — others show why they are unavailable."
            ),
            wraplength=860,
        ).pack(anchor="w")

        container = ttk.Frame(self.monitor_tab)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.monitor_rows_frame = ttk.Frame(canvas)
        self.monitor_rows_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.monitor_rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_pc_tab(self) -> None:
        top = ttk.LabelFrame(self.pc_tab, text="PC Profiles", padding=10)
        top.pack(fill="x")

        names = ttk.Frame(top)
        names.pack(fill="x")
        ttk.Label(names, text="PC A name").grid(row=0, column=0, sticky="w")
        self.pc_a_name = ttk.Entry(names, width=24)
        self.pc_a_name.grid(row=0, column=1, padx=(8, 20), sticky="w")
        ttk.Label(names, text="PC B name").grid(row=0, column=2, sticky="w")
        self.pc_b_name = ttk.Entry(names, width=24)
        self.pc_b_name.grid(row=0, column=3, padx=(8, 0), sticky="w")

        hotkey_row = ttk.Frame(top)
        hotkey_row.pack(fill="x", pady=(10, 0))
        self.pc_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            hotkey_row, text="Enable PC switch hotkey", variable=self.pc_enabled
        ).pack(side="left")
        ttk.Label(hotkey_row, text="Hotkey").pack(side="left", padx=(16, 6))
        self.pc_hotkey = HotkeyCapture(hotkey_row, width=24)
        self.pc_hotkey.pack(side="left")

        self.active_pc_var = tk.StringVar(value="Active PC: unknown")
        ttk.Label(top, textvariable=self.active_pc_var).pack(anchor="w", pady=(10, 0))

        ttk.Label(
            self.pc_tab,
            text=(
                "Assign which input each monitor uses for PC A and PC B. "
                "The hotkey switches every included monitor to the other PC at once."
            ),
            wraplength=860,
        ).pack(anchor="w", pady=(10, 8))

        headers = ttk.Frame(self.pc_tab)
        headers.pack(fill="x")
        for text, width in [
            ("Include", 8),
            ("Monitor", 34),
            ("Input for PC A", 22),
            ("Input for PC B", 22),
        ]:
            ttk.Label(headers, text=text, width=width).pack(side="left", padx=(0, 6))

        self.pc_rows_frame = ttk.Frame(self.pc_tab)
        self.pc_rows_frame.pack(fill="both", expand=True)

    def _build_drivers_tab(self) -> None:
        intro = ttk.Frame(self.drivers_tab)
        intro.pack(fill="x", pady=(0, 8))
        ttk.Label(
            intro,
            text=(
                "Windows hardware detection and Dell driver links. "
                "Install the monitor driver if a screen shows as Generic PnP, "
                "then enable DDC/CI in the monitor menu."
            ),
            wraplength=860,
        ).pack(anchor="w")

        buttons = ttk.Frame(intro)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(
            buttons,
            text="Open Device Manager",
            command=open_device_manager_monitors,
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="Dell Display Manager",
            command=lambda: open_driver_page(DELL_DISPLAY_MANAGER_URL),
        ).pack(side="left", padx=(8, 0))

        self.drivers_frame = ttk.Frame(self.drivers_tab)
        self.drivers_frame.pack(fill="both", expand=True)

    def _input_combobox(self, parent, value: int) -> ttk.Combobox:
        labels = [label for label, _code in INPUT_CHOICES]
        combo = ttk.Combobox(parent, values=labels, state="readonly", width=24)
        combo.set(input_label(value))
        return combo

    def refresh_monitors(self) -> None:
        self.monitors = enumerate_monitors()
        self.pnp_monitors = get_pnp_monitors()
        self.monitor_extra = {
            monitor.device_name: enrich_monitor(monitor, self.pnp_monitors)
            for monitor in self.monitors
        }
        controllable = [monitor for monitor in self.monitors if monitor.controllable]
        self._render_monitor_rows(self.monitors)
        self._render_pc_rows(self.monitors)
        self._render_drivers_tab()
        self._load_into_form()
        self.active_pc_var.set(
            f"Active PC: {read_active_pc()} (will flip on next PC hotkey)"
        )
        self._set_status(
            f"Found {len(self.monitors)} display(s), {len(controllable)} switchable."
        )

    def _clear_frame(self, frame: ttk.Frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _render_monitor_rows(self, monitors: list[MonitorInfo]) -> None:
        self.monitor_rows = []
        self._clear_frame(self.monitor_rows_frame)

        saved = {
            (entry.get("device") or entry.get("monitor", "")): entry
            for entry in self.config.get("monitor_bindings", [])
        }

        for monitor in monitors:
            extra = self.monitor_extra.get(monitor.device_name, {})
            saved_entry = saved.get(monitor.device_name) or saved.get(monitor.position, {})
            switchable = monitor.controllable
            reason = controllable_reason(monitor, extra)

            block = ttk.Frame(self.monitor_rows_frame, padding=(0, 8))
            block.pack(fill="x")

            row_frame = ttk.Frame(block)
            row_frame.pack(fill="x")

            enabled_default = switchable and saved_entry.get("enabled", False)
            enabled = tk.BooleanVar(value=enabled_default)
            enable_box = ttk.Checkbutton(row_frame, variable=enabled)
            enable_box.pack(side="left", padx=(0, 8))
            if not switchable:
                enable_box.configure(state="disabled")

            model = extra.get("detected_model") or monitor.description
            status = "switchable" if switchable else "not switchable"
            label = f"[{monitor.position}] {model} — {status}"
            ttk.Label(row_frame, text=label, width=52).pack(side="left")

            ttk.Label(row_frame, text="Hotkey").pack(side="left", padx=(8, 4))
            hotkey = HotkeyCapture(row_frame, width=18)
            hotkey.pack(side="left")
            if saved_entry.get("hotkey"):
                hotkey.set_value(saved_entry["hotkey"])

            ttk.Label(row_frame, text="Input A").pack(side="left", padx=(12, 4))
            input_a = self._input_combobox(
                row_frame, saved_entry.get("input_a", 0x0F)
            )
            input_a.pack(side="left")

            ttk.Label(row_frame, text="Input B").pack(side="left", padx=(12, 4))
            input_b = self._input_combobox(
                row_frame, saved_entry.get("input_b", 0x1B)
            )
            input_b.pack(side="left")

            if not switchable:
                hotkey.configure(state="disabled")
                input_a.configure(state="disabled")
                input_b.configure(state="disabled")

            detail = ttk.Label(block, text=reason, wraplength=860, foreground="#555")
            detail.pack(anchor="w", padx=(28, 0))

            if extra.get("driver_url"):
                driver_row = ttk.Frame(block)
                driver_row.pack(anchor="w", padx=(28, 0), pady=(4, 0))
                ttk.Button(
                    driver_row,
                    text=f"Download driver ({extra.get('detected_model', 'Dell')})",
                    command=lambda url=extra["driver_url"]: open_driver_page(url),
                ).pack(side="left")

            self.monitor_rows.append(
                {
                    "monitor": monitor,
                    "enabled": enabled,
                    "hotkey": hotkey,
                    "input_a": input_a,
                    "input_b": input_b,
                    "switchable": switchable,
                }
            )

        if not monitors:
            ttk.Label(
                self.monitor_rows_frame,
                text="No displays detected.",
            ).pack(anchor="w")

    def _render_pc_rows(self, monitors: list[MonitorInfo]) -> None:
        self.pc_rows = []
        self._clear_frame(self.pc_rows_frame)

        saved = {
            (entry.get("device") or entry.get("monitor", "")): entry
            for entry in self.config.get("pc_switch", {}).get("monitors", [])
        }

        for monitor in monitors:
            extra = self.monitor_extra.get(monitor.device_name, {})
            saved_entry = saved.get(monitor.device_name) or saved.get(monitor.position, {})
            switchable = monitor.controllable
            model = extra.get("detected_model") or monitor.description

            block = ttk.Frame(self.pc_rows_frame, padding=(0, 8))
            block.pack(fill="x")

            row_frame = ttk.Frame(block)
            row_frame.pack(fill="x")

            enabled_default = switchable and saved_entry.get("enabled", True)
            enabled = tk.BooleanVar(value=enabled_default)
            include_box = ttk.Checkbutton(row_frame, variable=enabled)
            include_box.pack(side="left", padx=(0, 8))
            if not switchable:
                include_box.configure(state="disabled")

            status = "switchable" if switchable else "not switchable"
            ttk.Label(
                row_frame,
                text=f"[{monitor.position}] {model} — {status}",
                width=40,
            ).pack(side="left")

            input_a = self._input_combobox(
                row_frame, saved_entry.get("input_a", 0x0F)
            )
            input_a.pack(side="left", padx=(0, 8))

            input_b = self._input_combobox(
                row_frame, saved_entry.get("input_b", 0x1B)
            )
            input_b.pack(side="left")

            if not switchable:
                input_a.configure(state="disabled")
                input_b.configure(state="disabled")

            ttk.Label(
                block,
                text=controllable_reason(monitor, extra),
                wraplength=860,
                foreground="#555",
            ).pack(anchor="w", padx=(28, 0))

            self.pc_rows.append(
                {
                    "monitor": monitor,
                    "enabled": enabled,
                    "input_a": input_a,
                    "input_b": input_b,
                    "switchable": switchable,
                }
            )

    def _render_drivers_tab(self) -> None:
        self._clear_frame(self.drivers_frame)

        if not self.pnp_monitors:
            ttk.Label(
                self.drivers_frame,
                text="Could not query Windows for monitor hardware.",
            ).pack(anchor="w")
            return

        for pnp in self.pnp_monitors:
            block = ttk.LabelFrame(
                self.drivers_frame,
                text=pnp.detected_model or pnp.name,
                padding=10,
            )
            block.pack(fill="x", pady=(0, 8))

            driver_status = (
                "Generic Windows driver"
                if pnp.uses_generic_driver
                else "Manufacturer driver detected"
            )
            ttk.Label(block, text=f"Windows name: {pnp.name}").pack(anchor="w")
            ttk.Label(block, text=f"Hardware ID: {pnp.hardware_id or 'unknown'}").pack(
                anchor="w"
            )
            ttk.Label(block, text=f"Driver status: {driver_status}").pack(anchor="w")
            if pnp.driver_notes:
                ttk.Label(block, text=pnp.driver_notes, wraplength=820).pack(
                    anchor="w", pady=(4, 0)
                )

            row = ttk.Frame(block)
            row.pack(anchor="w", pady=(8, 0))
            if pnp.driver_url:
                ttk.Button(
                    row,
                    text="Download Dell driver",
                    command=lambda url=pnp.driver_url: open_driver_page(url),
                ).pack(side="left")
            else:
                ttk.Label(
                    row,
                    text="No known Dell driver mapping for this hardware ID.",
                ).pack(side="left")

    def _load_into_form(self) -> None:
        pc_switch = self.config.get("pc_switch", {})
        self.pc_a_name.delete(0, tk.END)
        self.pc_a_name.insert(0, pc_switch.get("pc_a_name", "PC 1"))
        self.pc_b_name.delete(0, tk.END)
        self.pc_b_name.insert(0, pc_switch.get("pc_b_name", "PC 2"))
        self.pc_enabled.set(pc_switch.get("enabled", False))
        self.pc_hotkey.set_value(pc_switch.get("hotkey", "ctrl+shift+1"))

    def _combo_value(self, combo: ttk.Combobox, fallback: int) -> int:
        value = input_value_from_label(combo.get())
        return fallback if value is None else value

    def build_config_from_form(self) -> dict:
        monitor_bindings = []
        for row in self.monitor_rows:
            if not row.get("switchable", True):
                continue
            monitor: MonitorInfo = row["monitor"]
            hotkey = row["hotkey"].get().strip()
            if row["enabled"].get() and hotkey and hotkey != "Press shortcut...":
                monitor_bindings.append(
                    {
                        "enabled": True,
                        "monitor": monitor.position,
                        "device": monitor.device_name,
                        "hotkey": hotkey,
                        "input_a": self._combo_value(row["input_a"], 0x0F),
                        "input_b": self._combo_value(row["input_b"], 0x1B),
                    }
                )

        pc_monitors = []
        for row in self.pc_rows:
            monitor = row["monitor"]
            if not row.get("switchable", True):
                continue
            pc_monitors.append(
                {
                    "enabled": row["enabled"].get(),
                    "monitor": monitor.position,
                    "device": monitor.device_name,
                    "input_a": self._combo_value(row["input_a"], 0x0F),
                    "input_b": self._combo_value(row["input_b"], 0x1B),
                }
            )

        return {
            "version": 2,
            "monitor_bindings": monitor_bindings,
            "pc_switch": {
                "enabled": self.pc_enabled.get(),
                "hotkey": self.pc_hotkey.get().strip(),
                "pc_a_name": self.pc_a_name.get().strip() or "PC 1",
                "pc_b_name": self.pc_b_name.get().strip() or "PC 2",
                "monitors": pc_monitors,
            },
        }

    def save(self) -> None:
        try:
            self.config = self.build_config_from_form()
            save_config(self.config)
            self._append_log("Configuration saved.")
            if self.settings_only:
                self._append_log("Background app will reload hotkeys automatically.")
            self._set_status("Saved.")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def start_hotkeys(self) -> None:
        try:
            self.config = self.build_config_from_form()
            save_config(self.config)
            self.service.start(self.config)
            if not self.service.running:
                error = "Failed to start hotkeys."
                errors = self.service.poll_errors()
                if errors:
                    error = errors[0]
                messagebox.showerror("Start failed", error)
                return
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self._set_status("Hotkeys running.")
            self._append_log("Hotkey listener started.")
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))

    def stop_hotkeys(self) -> None:
        self.service.stop()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._set_status("Hotkeys stopped.")
        self._append_log("Hotkey listener stopped.")

    def _poll_service(self) -> None:
        for error in self.service.poll_errors():
            self._append_log(f"Error: {error}")
            self.stop_hotkeys()
            messagebox.showerror("Hotkey error", error)

        for event in self.service.poll_events():
            for line in event:
                self._append_log(line)
            active = read_active_pc()
            pc_a = self.pc_a_name.get().strip() or "PC 1"
            pc_b = self.pc_b_name.get().strip() or "PC 2"
            active_name = pc_a if active == "a" else pc_b
            self.active_pc_var.set(f"Active PC: {active_name}")

        self.root.after(200, self._poll_service)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _on_close(self) -> None:
        if self.service:
            self.service.stop()
        self.root.destroy()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Monitor input switcher settings UI")
    parser.add_argument(
        "--settings-only",
        action="store_true",
        help="Open settings UI while hotkeys run from the system tray app",
    )
    args = parser.parse_args()

    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    MonitorSwitcherUI(root, settings_only=args.settings_only)
    root.mainloop()


if __name__ == "__main__":
    main()
