#!/bin/bash
# Hermes cron --script wrapper: stdout is injected into the agent prompt.
# Install: cp to ~/.hermes/scripts/mushroom_brief.sh && chmod 700
REPO=/home/ihor.travkin/mushroom-alerts
export MUSHROOM_DB="$REPO/state.sqlite"
export MUSHROOM_LOCATIONS="$REPO/locations.yaml"
cd "$REPO" || exit 1
exec "$REPO/.venv/bin/python" -m mushroom_alerts brief 2>&1
