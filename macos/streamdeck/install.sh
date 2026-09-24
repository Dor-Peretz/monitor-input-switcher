#!/usr/bin/env bash
# Symlink the Stream Deck plugin into place and restart Stream Deck.
set -euo pipefail

PLUGIN_SRC="$(cd "$(dirname "$0")" && pwd)/com.dorperetz.monitorswitcher.sdPlugin"
PLUGIN_DEST="$HOME/Library/Application Support/com.elgato.StreamDeck/Plugins/com.dorperetz.monitorswitcher.sdPlugin"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This plugin is macOS only." >&2
  exit 1
fi

if [[ ! -d "$PLUGIN_SRC" ]]; then
  echo "Plugin source not found: $PLUGIN_SRC" >&2
  exit 1
fi

python3 "$PLUGIN_SRC/scripts/generate-icons.py" >/dev/null
chmod +x "$PLUGIN_SRC/launch.sh" "$PLUGIN_SRC/plugin.py"

echo "Quitting Stream Deck..."
osascript -e 'quit app "Elgato Stream Deck"' 2>/dev/null || true
sleep 1
killall "Stream Deck" 2>/dev/null || true
sleep 2

mkdir -p "$(dirname "$PLUGIN_DEST")"
rm -rf "$PLUGIN_DEST"
ln -s "$PLUGIN_SRC" "$PLUGIN_DEST"

echo "Installed: $PLUGIN_DEST"
echo "Relaunching Stream Deck..."
open -a "Stream Deck" 2>/dev/null || open -a "Elgato Stream Deck"
echo "Done. Drag 'PC Switch' or 'Toggle Monitor Input' onto a key."
