#!/bin/bash
# trident WALL daemon loop — detector + resume tick every WALL_TICK_S (30s).
# Run under launchd (com.casterly.trident-wall.plist, KeepAlive) or by hand
# with a sandbox TRIDENT_STATE_DIR for simulation.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICK="${WALL_TICK_S:-30}"

while true; do
  python3 "$DIR/wall-check.py" || true
  python3 "$DIR/wall-resume.py" || true
  sleep "$TICK"
done
