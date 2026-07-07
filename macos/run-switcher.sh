#!/usr/bin/env bash
# Run the hotkey listener in the terminal (no menu bar).
cd "$(dirname "$0")" || exit 1
exec python3 monitor_switcher.py run
