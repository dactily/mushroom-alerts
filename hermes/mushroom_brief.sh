#!/bin/bash
# Hermes cron --script wrapper (daily 08:30): stdout is injected into the prompt.
# The script decides send/silent itself; the agent only re-words the block.
# Install: cp to ~/.hermes/profiles/family/scripts/ && chmod 700
REPO=/home/ihor.travkin/mushroom-alerts
export MUSHROOM_DB="$REPO/state.sqlite"
export MUSHROOM_LOCATIONS="$REPO/locations.yaml"
cd "$REPO" || exit 1
exec "$REPO/.venv/bin/python" -m mushroom_alerts brief --mode daily 2>&1
