#!/usr/bin/env python3
"""Toggle monitor inputs with global hotkeys (DDC/CI on macOS)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from config_store import DEFAULT_CONFIG, config_path, load_config, save_config
from ddc_monitor import DDCError, enumerate_monitors, print_monitor_list
from hotkey_service import HotkeyService, parse_input_value, run_toggle_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Switch monitor inputs using DDC/CI and global hotkeys (macOS)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=config_path(),
        help="Path to config.json (default: beside this script)",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List detected monitors and current inputs")
    subparsers.add_parser("init", help="Create a default config.json")
    subparsers.add_parser("ui", help="Open the configuration UI")

    toggle_parser = subparsers.add_parser("toggle", help="Toggle one monitor once")
    toggle_parser.add_argument(
        "--monitor",
        default="right",
        help="Monitor position/name (left, right, center, primary, 0, ...)",
    )
    toggle_parser.add_argument(
        "--inputs",
        nargs=2,
        type=parse_input_value,
        metavar=("INPUT_A", "INPUT_B"),
        help="Two DDC input codes to toggle between (decimal or 0x hex)",
    )

    subparsers.add_parser("run", help="Listen for configured hotkeys (default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        if command == "list":
            print_monitor_list(enumerate_monitors())
            return 0

        if command == "init":
            path = args.config
            if path.exists():
                print(f"Config already exists: {path}")
                return 0
            save_config(DEFAULT_CONFIG, path)
            print(f"Created {path}")
            print("Run 'python3 ui.py' to configure hotkeys in the UI.")
            return 0

        if command == "ui":
            from ui import main as ui_main

            ui_main()
            return 0

        config = load_config(args.config)

        if command == "toggle":
            monitors = enumerate_monitors(controllable_only=True)
            binding = next(
                (
                    item
                    for item in config.get("monitor_bindings", [])
                    if item.get("monitor", "").lower() == args.monitor.lower()
                    or item.get("device", "").lower() == args.monitor.lower()
                ),
                None,
            )
            if args.inputs:
                input_a, input_b = args.inputs
            elif binding:
                input_a = parse_input_value(binding["input_a"])
                input_b = parse_input_value(binding["input_b"])
            else:
                parser.error(
                    "Provide --inputs A B or add a binding for this monitor in "
                    "config.json."
                )

            run_toggle_cli(monitors, args.monitor, input_a, input_b)
            return 0

        if command == "run":
            service = HotkeyService()
            service.start(config)
            if not service.running:
                for error in service.poll_errors():
                    print(error)
                return 1

            print("Monitor input switcher running. Press Ctrl+C to stop.\n")
            try:
                while service.running:
                    for error in service.poll_errors():
                        print(error)
                    for event in service.poll_events():
                        for line in event:
                            print(line)
                    time.sleep(0.2)
            except KeyboardInterrupt:
                print("\nStopping.")
            finally:
                service.stop()
            return 0
    except DDCError as exc:
        print(f"Error: {exc}")
        return 1

    parser.error(f"Unknown command: {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
