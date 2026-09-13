#!/bin/bash
# Hermes cron --script wrapper (Friday 19:00): stdout is injected into the prompt.
# The weekend plan is always sent; the script still says so in the block.
# Install: cp to ~/.hermes/profiles/family/scripts/ && chmod 700
REPO=/home/ihor.travkin/mushroom-alerts
MAPS="$REPO/maps"
export MUSHROOM_DB="$REPO/state.sqlite"
export MUSHROOM_LOCATIONS="$REPO/locations.yaml"
cd "$REPO" || exit 1
# The map is an attachment: if the directory cannot be made, the block is
# still printed and the run is unaffected -- hence no `|| exit`.
mkdir -p "$MAPS" 2>/dev/null
exec "$REPO/.venv/bin/python" -m mushroom_alerts brief --mode weekend --map-dir "$MAPS" 2>&1
