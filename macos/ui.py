#!/usr/bin/env python3
"""Settings UI for configuring monitor input hotkeys and PC switching (macOS)."""

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
from ddc_monitor import DDCError, MonitorInfo, enumerate_monitors, read_active_pc

# NOTE: Global hotkeys are handled by the menu bar app via native Carbon
# (carbon_hotkey.CarbonHotKeyManager). This settings window intentionally does
# NOT run a keyboard listener: pynput crashes on modern macOS, and Carbon
# hotkeys require a Cocoa run loop that Tkinter does not provide. The window
# only edits config and offers a manual "Switch Now" test.

# Tk modifier-state masks on macOS (best-effort; user can re-capture if wrong).
_MASK_SHIFT = 0x0001
_MASK_CONTROL = 0x0004
_MASK_COMMAND = 0x0008
_MASK_OPTION = 0x0010
_MASK_OPTION_ALT = 0x20000

_IGNORE_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L", "Alt_R", "Option_L", "Option_R",
    "Meta_L", "Meta_R", "Super_L", "Super_R",
    "Command", "Caps_Lock",
}

_KEYSYM_ALIASES = {
    "space": "space",
    "Tab": "tab",
    "Return": "enter",
    "Escape": "esc",
}

# Shifted number row -> base digit, so shift+1 is stored as "1" (pynput's
# canonical matching resolves the shifted character back to the base key).
_SHIFT_NUMBER_KEYSYMS = {
    "exclam": "1", "at": "2", "numbersign": "3", "dollar": "4",
    "percent": "5", "asciicircum": "6", "ampersand": "7", "asterisk": "8",
    "parenleft": "9", "parenright": "0",
}

# Punctuation keysym names -> their base character.
_PUNCT_KEYSYMS = {
    "minus": "-", "underscore": "-", "equal": "=", "plus": "=",
    "bracketleft": "[", "braceleft": "[", "bracketright": "]", "braceright": "]",
    "backslash": "\\", "bar": "\\", "semicolon": ";", "colon": ";",
    "apostrophe": "'", "quotedbl": "'", "grave": "`", "asciitilde": "`",
    "comma": ",", "less": ",", "period": ".", "greater": ".",
    "slash": "/", "question": "/",
}


def _normalize_keysym(keysym: str) -> str | None:
    if keysym in _KEYSYM_ALIASES:
        return _KEYSYM_ALIASES[keysym]
    if keysym in _SHIFT_NUMBER_KEYSYMS:
        return _SHIFT_NUMBER_KEYSYMS[keysym]
    if keysym in _PUNCT_KEYSYMS:
        return _PUNCT_KEYSYMS[keysym]
    if len(keysym) == 1:
        return keysym.lower()
    if keysym[0] in "Ff" and keysym[1:].isdigit():
        return keysym.lower()
    return None


def format_event_hotkey(event) -> str | None:
    keysym = event.keysym
    if keysym in _IGNORE_KEYSYMS:
        return None

    modifiers: list[str] = []
    state = event.state
    if state & _MASK_CONTROL:
        modifiers.append("ctrl")
    if state & _MASK_OPTION or state & _MASK_OPTION_ALT:
        modifiers.append("alt")
    if state & _MASK_SHIFT:
        modifiers.append("shift")
    if state & _MASK_COMMAND:
        modifiers.append("cmd")

    # Require at least one modifier so we never register a bare global key
    # that would fire on every keypress.
    if not modifiers:
        return None

    key = _normalize_keysym(keysym)
    if key is None:
        return None

    return "+".join(modifiers + [key])


class HotkeyCapture(ttk.Entry):
    """Entry widget that captures a keyboard shortcut."""

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
        self.insert(0, "Press shortcut\u2026")
        self.select_range(0, tk.END)

    def _stop_capture(self, _event=None) -> None:
        self._capturing = False
        self.configure(state="readonly")
        if self.get() == "Press shortcut\u2026":
            self.delete(0, tk.END)

    def _on_key_press(self, event) -> str:
        if not self._capturing:
            return "break"
        hotkey = format_event_hotkey(event)
        if hotkey is None:
            return "break"
        self.set_value(hotkey)
        self._capturing = False
        return "break"


class MonitorSwitcherUI:
    def __init__(self, root: tk.Tk, settings_only: bool = False) -> None:
        self.root = root
        self.settings_only = settings_only
        self.root.title("Monitor Input Switcher")
        self.root.geometry("860x640")
        self.root.minsize(720, 520)

        self.service = None  # hotkeys run in the menu bar app, not here
        self.monitors: list[MonitorInfo] = []
        self.config = load_config()
        self.monitor_rows: list[dict] = []
        self.pc_rows: list[dict] = []

        self._build_layout()
        self.refresh_monitors()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_layout(self) -> None:
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="Configure hotkeys to toggle monitor inputs or switch between two PCs.",
            wraplength=820,
        ).pack(anchor="w")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.monitor_tab = ttk.Frame(notebook, padding=10)
        self.pc_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.monitor_tab, text="Per Monitor")
        notebook.add(self.pc_tab, text="PC Switch (Group)")

        self._build_monitor_tab()
        self._build_pc_tab()

        footer = ttk.Frame(self.root, padding=10)
        footer.pack(fill="x")

        ttk.Button(
            footer, text="Refresh Monitors", command=self.refresh_monitors
        ).pack(side="left")
        ttk.Button(footer, text="Save", command=self.save).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            footer, text="Switch Now (PC)", command=self.switch_now
        ).pack(side="left", padx=(8, 0))
        # Global hotkeys are owned by the menu bar app (native Carbon). This
        # window never runs a keyboard listener, so there is no Start/Stop here.
        ttk.Label(
            footer,
            text="Hotkeys run in the background (menu bar). Save to apply.",
        ).pack(side="left", padx=(8, 0))
        self.start_button = None
        self.stop_button = None

        self.status_var = tk.StringVar(value="Ready")
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
                "All detected displays are listed. Only external DDC/CI monitors "
                "can be configured (the built-in display cannot switch inputs)."
            ),
            wraplength=820,
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
            wraplength=820,
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

    def _input_combobox(self, parent, value: int) -> ttk.Combobox:
        labels = [label for label, _code in INPUT_CHOICES]
        combo = ttk.Combobox(parent, values=labels, state="readonly", width=24)
        combo.set(input_label(value))
        return combo

    def refresh_monitors(self) -> None:
        try:
            self.monitors = enumerate_monitors(with_positions=True)
            error = None
        except DDCError as exc:
            self.monitors = []
            error = str(exc)

        controllable = [m for m in self.monitors if m.controllable]
        self._render_monitor_rows(self.monitors)
        self._render_pc_rows(self.monitors)
        self._load_into_form()
        self.active_pc_var.set(
            f"Active PC: {read_active_pc()} (will flip on next PC hotkey)"
        )
        if error:
            self._set_status(error)
            self._append_log(error)
        else:
            self._set_status(
                f"Found {len(self.monitors)} display(s), "
                f"{len(controllable)} switchable."
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
            saved_entry = (
                saved.get(monitor.device_name) or saved.get(monitor.position, {})
            )
            switchable = monitor.controllable

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

            status = "switchable" if switchable else "not switchable"
            label = f"[{monitor.position}] {monitor.description} \u2014 {status}"
            ttk.Label(row_frame, text=label, width=48).pack(side="left")

            ttk.Label(row_frame, text="Hotkey").pack(side="left", padx=(8, 4))
            hotkey = HotkeyCapture(row_frame, width=18)
            hotkey.pack(side="left")
            if saved_entry.get("hotkey"):
                hotkey.set_value(saved_entry["hotkey"])

            ttk.Label(row_frame, text="Input A").pack(side="left", padx=(12, 4))
            input_a = self._input_combobox(row_frame, saved_entry.get("input_a", 0x0F))
            input_a.pack(side="left")

            ttk.Label(row_frame, text="Input B").pack(side="left", padx=(12, 4))
            input_b = self._input_combobox(row_frame, saved_entry.get("input_b", 0x1B))
            input_b.pack(side="left")

            if not switchable:
                hotkey.configure(state="disabled")
                input_a.configure(state="disabled")
                input_b.configure(state="disabled")

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
                text=(
                    "No displays detected. Make sure m1ddc (Apple Silicon) or "
                    "ddcctl (Intel) is installed via Homebrew."
                ),
                wraplength=800,
            ).pack(anchor="w")

    def _render_pc_rows(self, monitors: list[MonitorInfo]) -> None:
        self.pc_rows = []
        self._clear_frame(self.pc_rows_frame)

        saved = {
            (entry.get("device") or entry.get("monitor", "")): entry
            for entry in self.config.get("pc_switch", {}).get("monitors", [])
        }

        for monitor in monitors:
            saved_entry = (
                saved.get(monitor.device_name) or saved.get(monitor.position, {})
            )
            switchable = monitor.controllable

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
                text=f"[{monitor.position}] {monitor.description} \u2014 {status}",
                width=40,
            ).pack(side="left")

            input_a = self._input_combobox(row_frame, saved_entry.get("input_a", 0x0F))
            input_a.pack(side="left", padx=(0, 8))

            input_b = self._input_combobox(row_frame, saved_entry.get("input_b", 0x1B))
            input_b.pack(side="left")

            if not switchable:
                input_a.configure(state="disabled")
                input_b.configure(state="disabled")

            self.pc_rows.append(
                {
                    "monitor": monitor,
                    "enabled": enabled,
                    "input_a": input_a,
                    "input_b": input_b,
                    "switchable": switchable,
                }
            )

    def _load_into_form(self) -> None:
        pc_switch = self.config.get("pc_switch", {})
        self.pc_a_name.delete(0, tk.END)
        self.pc_a_name.insert(0, pc_switch.get("pc_a_name", "PC 1"))
        self.pc_b_name.delete(0, tk.END)
        self.pc_b_name.insert(0, pc_switch.get("pc_b_name", "PC 2"))
        self.pc_enabled.set(pc_switch.get("enabled", False))
        self.pc_hotkey.set_value(pc_switch.get("hotkey", "cmd+shift+1"))

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
            if row["enabled"].get() and hotkey and hotkey != "Press shortcut\u2026":
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

    def switch_now(self) -> None:
        """Trigger the PC-switch immediately from the current form settings."""
        from ddc_monitor import toggle_pc_group

        try:
            config = self.build_config_from_form()
            pc = config.get("pc_switch", {})
            monitor_cfgs = [
                entry
                for entry in pc.get("monitors", [])
                if entry.get("enabled", True)
            ]
            if not monitor_cfgs:
                self._append_log(
                    "Switch Now: no PC-switch monitors are enabled. "
                    "Enable monitors in the PC Switch tab first."
                )
                return
            monitors = enumerate_monitors()
            target_name, lines = toggle_pc_group(
                monitors,
                monitor_cfgs,
                pc.get("pc_a_name", "PC 1"),
                pc.get("pc_b_name", "PC 2"),
            )
            self._append_log(f"Switch Now -> {target_name}")
            for line in lines:
                self._append_log("  " + line)
            self.active_pc_var.set(f"Active PC: {target_name}")
            self._set_status(f"Switched to {target_name}.")
        except Exception as exc:
            self._append_log(f"Switch Now failed: {exc}")
            messagebox.showerror("Switch failed", str(exc))

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
        help="Open settings UI while hotkeys run from the menu bar app",
    )
    args = parser.parse_args()

    root = tk.Tk()
    style = ttk.Style(root)
    if "aqua" in style.theme_names():
        style.theme_use("aqua")
    MonitorSwitcherUI(root, settings_only=args.settings_only)
    root.mainloop()


if __name__ == "__main__":
    main()
