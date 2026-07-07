#!/usr/bin/env python3
"""Entry point for Monitor Input Switcher (source installs and frozen builds)."""

from __future__ import annotations

import sys

_CLI_COMMANDS = frozenset({"list", "init", "ui", "toggle", "run"})


def main() -> None:
    args = sys.argv[1:]

    if "--settings-only" in args:
        from ui import main as ui_main

        ui_main()
        return

    if args and args[0] in _CLI_COMMANDS:
        from monitor_switcher import main as cli_main

        raise SystemExit(cli_main(args))

    from tray_app import main as tray_main

    tray_main()


if __name__ == "__main__":
    main()
