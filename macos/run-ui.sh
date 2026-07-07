#!/usr/bin/env bash
# Open the settings UI.
cd "$(dirname "$0")" || exit 1
exec python3 ui.py
