#!/usr/bin/env python3
"""Settings UI for configuring monitor input hotkeys and PC switching."""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from copy import deepcopy
from tkinter import messagebox, ttk

from config_store import (
    INPUT_CHOICES,
    input_label,
    input_value_from_label,
    load_config,
    save_config,
)
from ddc_monitor import (
    MonitorInfo,
    enumerate_monitors,
    input_name,
    read_active_pc,
    resolve_monitor,
    switch_monitors_to_inputs,
    toggle_input,
    write_active_pc,
)
from version import __app_name__, __version__
from hardware_info import (
    DELL_DISPLAY_MANAGER_URL,
    controllable_reason,
    enrich_monitor,
    get_pnp_monitors,
    open_device_manager_monitors,
    open_driver_page,
)
from hotkey_service import HotkeyService, format_hotkey

COLORS = {
    "bg": "#e8eef5",
    "header": "#0f172a",
    "header_muted": "#94a3b8",
    "header_text": "#f8fafc",
    "card": "#ffffff",
    "border": "#d0d7e2",
    "accent": "#2563eb",
    "ok_bg": "#dcfce7",
    "ok_fg": "#166534",
    "warn_bg": "#ffedd5",
    "warn_fg": "#9a3412",
    "muted": "#64748b",
    "text": "#0f172a",
    "log_bg": "#0f172a",
    "log_fg": "#e2e8f0",
}


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


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
        self.root.title(f"{__app_name__} v{__version__}")
        self.root.geometry("980x740")
        self.root.minsize(840, 600)
        self.root.configure(bg=COLORS["bg"])

        self.service = HotkeyService() if not settings_only else None
        self.monitors: list[MonitorInfo] = []
        self.monitor_extra: dict[str, dict] = {}
        self.pnp_monitors = []
        self.config = load_config()
        self.monitor_rows: list[dict] = []
        self.pc_rows: list[dict] = []
        self._busy = False

        self._apply_theme()
        self._build_layout()
        self.refresh_monitors()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.settings_only:
            self.root.after(200, self._poll_service)

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["card"])
        style.configure("Header.TFrame", background=COLORS["header"])
        style.configure(
            "Header.TLabel",
            background=COLORS["header"],
            foreground=COLORS["header_text"],
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "HeaderSub.TLabel",
            background=COLORS["header"],
            foreground=COLORS["header_muted"],
            font=("Segoe UI", 10),
        )
        style.configure("Card.TLabel", background=COLORS["card"], foreground=COLORS["text"])
        style.configure(
            "Muted.TLabel",
            background=COLORS["card"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 9),
        )
        style.configure("Title.TLabel", background=COLORS["card"], font=("Segoe UI", 11, "bold"))
        style.configure("App.TNotebook", background=COLORS["bg"])
        style.configure("TNotebook.Tab", padding=(14, 8), font=("Segoe UI", 10))
        style.configure("Accent.TButton", padding=(10, 6))
        style.configure("TLabelframe", background=COLORS["bg"])
        style.configure("TLabelframe.Label", background=COLORS["bg"], font=("Segoe UI", 10, "bold"))

    def _build_layout(self) -> None:
        header = tk.Frame(self.root, bg=COLORS["header"])
        header.pack(fill="x")
        inner = ttk.Frame(header, style="Header.TFrame", padding=(20, 16))
        inner.pack(fill="x")
        ttk.Label(inner, text=__app_name__, style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            inner,
            text="Toggle inputs with a hotkey, or switch a whole desk between two PCs.",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        notebook = ttk.Notebook(self.root, style="App.TNotebook")
        notebook.pack(fill="both", expand=True, padx=16, pady=(14, 8))

        self.monitor_tab = ttk.Frame(notebook, padding=12, style="App.TFrame")
        self.pc_tab = ttk.Frame(notebook, padding=12, style="App.TFrame")
        self.drivers_tab = ttk.Frame(notebook, padding=12, style="App.TFrame")
        notebook.add(self.monitor_tab, text="  Per monitor  ")
        notebook.add(self.pc_tab, text="  PC switch  ")
        notebook.add(self.drivers_tab, text="  Drivers  ")

        self._build_monitor_tab()
        self._build_pc_tab()
        self._build_drivers_tab()

        footer = tk.Frame(self.root, bg=COLORS["bg"])
        footer.pack(fill="x", padx=16, pady=(0, 8))

        ttk.Button(footer, text="Refresh monitors", command=self.refresh_monitors).pack(
            side="left"
        )
        ttk.Button(footer, text="Save", command=self.save, style="Accent.TButton").pack(
            side="left", padx=(8, 0)
        )
        if self.settings_only:
            ttk.Label(
                footer,
                text="Hotkeys run from the tray. Save to apply.",
                foreground=COLORS["muted"],
                background=COLORS["bg"],
            ).pack(side="left", padx=(12, 0))
            self.start_button = None
            self.stop_button = None
        else:
            self.start_button = ttk.Button(
                footer, text="Start hotkeys", command=self.start_hotkeys
            )
            self.start_button.pack(side="left", padx=(8, 0))
            self.stop_button = ttk.Button(
                footer, text="Stop hotkeys", command=self.stop_hotkeys, state="disabled"
            )
            self.stop_button.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(
            value="Background hotkeys active" if self.settings_only else "Ready"
        )
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="right")

        log_frame = ttk.LabelFrame(self.root, text="Activity", padding=8)
        log_frame.pack(fill="both", expand=False, padx=16, pady=(0, 14))
        self.log_text = tk.Text(
            log_frame,
            height=6,
            wrap="word",
            bg=COLORS["log_bg"],
            fg=COLORS["log_fg"],
            insertbackground=COLORS["log_fg"],
            relief="flat",
            font=("Consolas", 9),
            padx=8,
            pady=6,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _build_monitor_tab(self) -> None:
        ttk.Label(
            self.monitor_tab,
            text="Enable a monitor, pick a hotkey, then choose the two inputs to toggle.",
            foreground=COLORS["muted"],
            background=COLORS["bg"],
        ).pack(anchor="w", pady=(0, 8))

        container = ttk.Frame(self.monitor_tab, style="App.TFrame")
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0, bg=COLORS["bg"], bd=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.monitor_rows_frame = ttk.Frame(canvas, style="App.TFrame")
        window_id = canvas.create_window((0, 0), window=self.monitor_rows_frame, anchor="nw")
        self.monitor_rows_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._bind_mousewheel(canvas, self.monitor_rows_frame)

    def _build_pc_tab(self) -> None:
        top = tk.Frame(
            self.pc_tab,
            bg=COLORS["card"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        top.pack(fill="x")
        inner = ttk.Frame(top, style="Card.TFrame", padding=14)
        inner.pack(fill="x")

        ttk.Label(inner, text="Desk profiles", style="Title.TLabel").pack(anchor="w")
        names = ttk.Frame(inner, style="Card.TFrame")
        names.pack(fill="x", pady=(10, 0))
        ttk.Label(names, text="PC A", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.pc_a_name = ttk.Entry(names, width=24)
        self.pc_a_name.grid(row=0, column=1, padx=(8, 24), sticky="w")
        ttk.Label(names, text="PC B", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        self.pc_b_name = ttk.Entry(names, width=24)
        self.pc_b_name.grid(row=0, column=3, padx=(8, 0), sticky="w")

        hotkey_row = ttk.Frame(inner, style="Card.TFrame")
        hotkey_row.pack(fill="x", pady=(12, 0))
        self.pc_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            hotkey_row, text="Enable group hotkey", variable=self.pc_enabled
        ).pack(side="left")
        ttk.Label(hotkey_row, text="Hotkey", style="Card.TLabel").pack(
            side="left", padx=(16, 6)
        )
        self.pc_hotkey = HotkeyCapture(hotkey_row, width=22)
        self.pc_hotkey.pack(side="left")

        actions = ttk.Frame(inner, style="Card.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Switch all to A", command=lambda: self._switch_group("a")).pack(
            side="left"
        )
        ttk.Button(actions, text="Switch all to B", command=lambda: self._switch_group("b")).pack(
            side="left", padx=(8, 0)
        )
        self.active_pc_var = tk.StringVar(value="Active PC: unknown")
        ttk.Label(actions, textvariable=self.active_pc_var, style="Muted.TLabel").pack(
            side="left", padx=(16, 0)
        )

        ttk.Label(
            self.pc_tab,
            text="Include each monitor and map which input belongs to PC A vs PC B.",
            foreground=COLORS["muted"],
            background=COLORS["bg"],
        ).pack(anchor="w", pady=(12, 8))

        self.pc_rows_frame = ttk.Frame(self.pc_tab, style="App.TFrame")
        self.pc_rows_frame.pack(fill="both", expand=True)

    def _build_drivers_tab(self) -> None:
        intro = ttk.Frame(self.drivers_tab, style="App.TFrame")
        intro.pack(fill="x", pady=(0, 8))
        ttk.Label(
            intro,
            text=(
                "If a screen shows as Generic PnP, install the manufacturer driver "
                "and enable DDC/CI in the monitor OSD."
            ),
            foreground=COLORS["muted"],
            background=COLORS["bg"],
            wraplength=900,
        ).pack(anchor="w")

        buttons = ttk.Frame(intro, style="App.TFrame")
        buttons.pack(fill="x", pady=(10, 0))
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

        self.drivers_frame = ttk.Frame(self.drivers_tab, style="App.TFrame")
        self.drivers_frame.pack(fill="both", expand=True, pady=(12, 0))

    def _bind_mousewheel(self, canvas: tk.Canvas, inner: ttk.Frame) -> None:
        def on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def bind(_event=None):
            canvas.bind_all("<MouseWheel>", on_wheel)

        def unbind(_event=None):
            canvas.unbind_all("<MouseWheel>")

        inner.bind("<Enter>", bind)
        inner.bind("<Leave>", unbind)

    def _input_combobox(self, parent, value: int) -> ttk.Combobox:
        labels = [label for label, _code in INPUT_CHOICES]
        combo = ttk.Combobox(parent, values=labels, state="readonly", width=22)
        combo.set(input_label(value))
        return combo

    def _badge(self, parent, text: str, ok: bool) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=COLORS["ok_bg"] if ok else COLORS["warn_bg"],
            fg=COLORS["ok_fg"] if ok else COLORS["warn_fg"],
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=2,
        )

    def _card(self, parent) -> tuple[tk.Frame, ttk.Frame]:
        shell = tk.Frame(
            parent,
            bg=COLORS["card"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        shell.pack(fill="x", pady=(0, 10))
        body = ttk.Frame(shell, style="Card.TFrame", padding=12)
        body.pack(fill="x")
        return shell, body

    def refresh_monitors(self) -> None:
        try:
            self.monitors = enumerate_monitors()
        except OSError as exc:
            messagebox.showerror("Refresh failed", str(exc))
            return
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
        self.active_pc_var.set(self._active_pc_text())
        self._set_status(
            f"Found {len(self.monitors)} display(s), {len(controllable)} switchable."
        )

    def _active_pc_text(self) -> str:
        active = read_active_pc()
        pc_a = self.pc_a_name.get().strip() or "PC 1"
        pc_b = self.pc_b_name.get().strip() or "PC 2"
        name = pc_a if active == "a" else pc_b
        return f"Remembered PC: {name}"

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
            model = extra.get("detected_model") or monitor.description
            current = input_name(monitor.current_input)

            _shell, body = self._card(self.monitor_rows_frame)

            title = ttk.Frame(body, style="Card.TFrame")
            title.pack(fill="x")
            enabled_default = switchable and saved_entry.get("enabled", False)
            enabled = tk.BooleanVar(value=enabled_default)
            enable_box = ttk.Checkbutton(title, text="Enable", variable=enabled)
            enable_box.pack(side="left")
            if not switchable:
                enable_box.configure(state="disabled")

            ttk.Label(
                title,
                text=f"[{monitor.position}]  {model}",
                style="Title.TLabel",
            ).pack(side="left", padx=(8, 8))
            self._badge(
                title,
                "SWITCHABLE" if switchable else "NOT SWITCHABLE",
                switchable,
            ).pack(side="right")

            ttk.Label(
                body,
                text=(
                    f"{monitor.width}×{monitor.height} at ({monitor.left}, {monitor.top})"
                    f"{' · primary' if monitor.is_primary else ''} · current input: {current}"
                ),
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(6, 8))

            controls = ttk.Frame(body, style="Card.TFrame")
            controls.pack(fill="x")
            ttk.Label(controls, text="Hotkey", style="Card.TLabel").pack(side="left")
            hotkey = HotkeyCapture(controls, width=18)
            hotkey.pack(side="left", padx=(6, 14))
            if saved_entry.get("hotkey"):
                hotkey.set_value(saved_entry["hotkey"])

            ttk.Label(controls, text="Input A", style="Card.TLabel").pack(side="left")
            input_a = self._input_combobox(controls, saved_entry.get("input_a", 0x0F))
            input_a.pack(side="left", padx=(6, 14))

            ttk.Label(controls, text="Input B", style="Card.TLabel").pack(side="left")
            input_b = self._input_combobox(controls, saved_entry.get("input_b", 0x1B))
            input_b.pack(side="left", padx=(6, 14))

            test_btn = ttk.Button(
                controls,
                text="Test toggle",
                command=lambda row_index=len(self.monitor_rows): self._test_toggle(row_index),
            )
            test_btn.pack(side="right")

            if not switchable:
                hotkey.configure(state="disabled")
                input_a.configure(state="disabled")
                input_b.configure(state="disabled")
                test_btn.configure(state="disabled")

            ttk.Label(body, text=reason, style="Muted.TLabel", wraplength=860).pack(
                anchor="w", pady=(8, 0)
            )

            if extra.get("driver_url"):
                ttk.Button(
                    body,
                    text=f"Download driver ({extra.get('detected_model', 'Dell')})",
                    command=lambda url=extra["driver_url"]: open_driver_page(url),
                ).pack(anchor="w", pady=(8, 0))

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
                background=COLORS["bg"],
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

            _shell, body = self._card(self.pc_rows_frame)
            title = ttk.Frame(body, style="Card.TFrame")
            title.pack(fill="x")

            enabled_default = switchable and saved_entry.get("enabled", True)
            enabled = tk.BooleanVar(value=enabled_default)
            include_box = ttk.Checkbutton(title, text="Include", variable=enabled)
            include_box.pack(side="left")
            if not switchable:
                include_box.configure(state="disabled")

            ttk.Label(
                title,
                text=f"[{monitor.position}]  {model}",
                style="Title.TLabel",
            ).pack(side="left", padx=(8, 8))
            self._badge(
                title,
                "SWITCHABLE" if switchable else "NOT SWITCHABLE",
                switchable,
            ).pack(side="right")

            controls = ttk.Frame(body, style="Card.TFrame")
            controls.pack(fill="x", pady=(8, 0))
            ttk.Label(controls, text="Input for PC A", style="Card.TLabel").pack(side="left")
            input_a = self._input_combobox(controls, saved_entry.get("input_a", 0x0F))
            input_a.pack(side="left", padx=(6, 16))
            ttk.Label(controls, text="Input for PC B", style="Card.TLabel").pack(side="left")
            input_b = self._input_combobox(controls, saved_entry.get("input_b", 0x1B))
            input_b.pack(side="left", padx=(6, 0))

            if not switchable:
                input_a.configure(state="disabled")
                input_b.configure(state="disabled")

            ttk.Label(
                body,
                text=controllable_reason(monitor, extra),
                style="Muted.TLabel",
                wraplength=860,
            ).pack(anchor="w", pady=(8, 0))

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
                background=COLORS["bg"],
            ).pack(anchor="w")
            return

        for pnp in self.pnp_monitors:
            _shell, body = self._card(self.drivers_frame)
            ttk.Label(
                body,
                text=pnp.detected_model or pnp.name,
                style="Title.TLabel",
            ).pack(anchor="w")
            driver_status = (
                "Generic Windows driver"
                if pnp.uses_generic_driver
                else "Manufacturer driver detected"
            )
            ttk.Label(body, text=f"Windows name: {pnp.name}", style="Muted.TLabel").pack(
                anchor="w", pady=(4, 0)
            )
            ttk.Label(
                body,
                text=f"Hardware ID: {pnp.hardware_id or 'unknown'} · {driver_status}",
                style="Muted.TLabel",
            ).pack(anchor="w")
            if pnp.driver_notes:
                ttk.Label(body, text=pnp.driver_notes, style="Muted.TLabel", wraplength=820).pack(
                    anchor="w", pady=(4, 0)
                )

            row = ttk.Frame(body, style="Card.TFrame")
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
                    style="Muted.TLabel",
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

    def _live_monitor(self, monitor: MonitorInfo) -> MonitorInfo | None:
        live = enumerate_monitors()
        return resolve_monitor(
            live, {"device": monitor.device_name, "monitor": monitor.position}
        )

    def _run_ddc(self, title: str, work) -> None:
        if self._busy:
            self._append_log("Wait — a switch is already in progress.")
            return
        self._busy = True
        self._set_status(f"{title}...")

        def runner():
            try:
                lines = work()
            except Exception as exc:
                lines = [f"Error: {exc}"]

            def done():
                self._busy = False
                for line in lines:
                    self._append_log(line)
                self.active_pc_var.set(self._active_pc_text())
                self._set_status("Done." if lines else "Ready")

            self.root.after(0, done)

        threading.Thread(target=runner, daemon=True).start()

    def _test_toggle(self, row_index: int) -> None:
        row = self.monitor_rows[row_index]

        def work():
            monitor = self._live_monitor(row["monitor"])
            if monitor is None or not monitor.controllable:
                return ["Test failed: monitor not controllable right now."]
            previous, new = toggle_input(
                monitor,
                self._combo_value(row["input_a"], 0x0F),
                self._combo_value(row["input_b"], 0x1B),
                monitor.device_name,
            )
            prev_label = input_name(previous) if previous >= 0 else "unknown"
            return [
                f"Test [{monitor.position}] {monitor.description}: "
                f"{prev_label} -> {input_name(new)}"
            ]

        self._run_ddc("Testing toggle", work)

    def _switch_group(self, target_pc: str) -> None:
        input_key = "input_a" if target_pc == "a" else "input_b"
        name = (
            (self.pc_a_name.get().strip() or "PC 1")
            if target_pc == "a"
            else (self.pc_b_name.get().strip() or "PC 2")
        )

        def work():
            live = enumerate_monitors()
            assignments = []
            for row in self.pc_rows:
                if not row["enabled"].get() or not row.get("switchable"):
                    continue
                monitor = resolve_monitor(
                    live,
                    {
                        "device": row["monitor"].device_name,
                        "monitor": row["monitor"].position,
                    },
                )
                if monitor is None:
                    continue
                assignments.append(
                    (monitor, self._combo_value(row[input_key], 0x0F if target_pc == "a" else 0x1B))
                )
            if not assignments:
                return ["No included switchable monitors."]
            lines, successes, failures = switch_monitors_to_inputs(live, assignments)
            if successes and not failures:
                write_active_pc(target_pc)
            lines.insert(0, f"Switch to {name}: {successes} ok, {failures} failed.")
            return lines

        self._run_ddc(f"Switching to {name}", work)

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
            # Keep Mac/PC Stream Deck handoff settings across UI saves.
            "streamdeck_hub": deepcopy(
                self.config.get("streamdeck_hub") or {}
            ),
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
            self.active_pc_var.set(self._active_pc_text())

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

    _enable_dpi_awareness()
    root = tk.Tk()
    MonitorSwitcherUI(root, settings_only=args.settings_only)
    root.mainloop()


if __name__ == "__main__":
    main()
