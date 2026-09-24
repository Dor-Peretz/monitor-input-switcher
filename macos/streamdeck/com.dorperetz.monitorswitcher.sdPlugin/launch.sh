#!/bin/bash
# Stream Deck launches plugins with a minimal PATH, so locate a usable python3
# the same way ddc_monitor.py locates m1ddc.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for candidate in \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  /usr/bin/python3
do
  if [[ -x "$candidate" ]]; then
    exec "$candidate" "$PLUGIN_DIR/plugin.py" "$@"
  fi
done

echo "No python3 found for Monitor Input Switcher Stream Deck plugin." >&2
exit 1
