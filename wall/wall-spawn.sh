#!/bin/bash
# trident WALL — spawn one fresh Terminal window and resume a session by
# handing claude its handoff ID as the initial prompt (argv). No clipboard,
# no synthetic keystrokes, no app activation — immune to the focus-steal /
# dictation race hit in the 2026-06-05 live sim (the old conductor-style
# clipboard paste activated Terminal and swallowed user typing mid-dictation).
# Usage: wall-spawn.sh <handoff-id> [workdir] [profile]
set -euo pipefail

HID="${1:?usage: wall-spawn.sh <handoff-id> [workdir] [profile]}"
WORKDIR="${2:-$HOME}"
PROFILE="${3:-}"
LAUNCH="${WALL_LAUNCH_CMD:-claude}"

# do script WITHOUT activate: window opens behind whatever has focus.
osascript <<APPLESCRIPT
tell application "Terminal"
    set newWin to do script "cd " & quoted form of "$WORKDIR" & " && $LAUNCH " & quoted form of "$HID"
    try
        set custom title of newWin to "wall-resume $HID"
    end try
    if "$PROFILE" is not "" then
        try
            set current settings of newWin to settings set "$PROFILE"
        end try
    end if
end tell
APPLESCRIPT

echo "spawned $LAUNCH with $HID at $WORKDIR (background window, no focus steal)"
