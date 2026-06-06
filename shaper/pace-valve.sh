#!/usr/bin/env bash
# trident SHAPER — pace valve (PreToolUse, all tools).
#
# Sequential burn is ungovernable per-call (model immutable, context re-read every
# turn) — but turn CADENCE isn't. MINT computes per-session sleep assignments into
# wallet.json["pace"]["sleeps"] when global billed tok/min exceeds the target in
# trident-pace-target.json; this hook is the sub-ms read + bounded sleep that
# enacts them. Same work, same model, same rigor — over-fair-share sessions just
# space their turns until the pool is back under target. Light sessions are never
# in the sleeps map and pay only the jq read.
#
# Safety rails:
#   - fail OPEN: missing/stale wallet, no pace block, jq error → exit 0 instantly
#   - sleep hard-capped by MINT at 20s (cache TTL is 300s — never at risk)
#   - per-session stamp: at most one sleep per 20s, so parallel/batched tool
#     calls in one turn don't stack sleeps
#   - kill switch: touch ~/.claude/state/trident-pace-off (or delete the target
#     file) — valve goes dormant on the next tick
set -u

STATE="${TRIDENT_STATE_DIR:-$HOME/.claude/state}"
WALLET="$STATE/wallet.json"
[ -f "$STATE/trident-pace-off" ] && { cat >/dev/null; exit 0; }
[ -f "$WALLET" ] || { cat >/dev/null; exit 0; }

SID=$(jq -r '.session_id // empty' 2>/dev/null) || exit 0
[ -n "$SID" ] || exit 0

# Stale wallet (mint down / paused) → fail open. 120s = 4 missed ticks.
NOW=$(date +%s)
W_MTIME=$(stat -f %m "$WALLET" 2>/dev/null || echo 0)
[ $((NOW - W_MTIME)) -gt 120 ] && exit 0

SLEEP_S=$(jq -r --arg sid "$SID" '.pace.sleeps[$sid] // empty' "$WALLET" 2>/dev/null) || exit 0
[ -n "$SLEEP_S" ] || exit 0

# One sleep per 20s per session — batched tool calls must not stack.
STAMP="/tmp/trident-pace-stamp-$SID"
LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
case "$LAST" in (*[!0-9]*|'') LAST=0 ;; esac
[ $((NOW - LAST)) -lt 20 ] && exit 0
echo "$NOW" > "$STAMP" 2>/dev/null

sleep "$SLEEP_S" 2>/dev/null
exit 0
