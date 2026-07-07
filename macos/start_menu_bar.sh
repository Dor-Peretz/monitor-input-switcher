#!/usr/bin/env bash
# Launch the background menu bar app.
cd "$(dirname "$0")" || exit 1
exec python3 menu_bar_app.py
