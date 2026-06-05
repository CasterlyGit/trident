#!/usr/bin/env bash
# trident SHAPER — headless spawn wrapper (DESIGN.md §3.5)
# Usage: env-stamp.sh claude -p "..."   (workflow-watcher + conductor launchers)
#
# Injects the ONE mechanical main-loop lever that exists — model choice at session
# start — plus TRIDENT_ENVELOPE env. Never adds --bare itself; if the caller passes
# --bare the stamp still applies (env + --model survive; hooks don't — proxy floor
# covers the rest). Fail open: missing/stale wallet → exec untouched.
set -u

STATE="${TRIDENT_STATE_DIR:-$HOME/.claude/state}"
WALLET="$STATE/wallet.json"

if [ ! -f "$WALLET" ] || [ $(( $(date +%s) - $(stat -f %m "$WALLET" 2>/dev/null || echo 0) )) -gt 120 ]; then
  exec "$@"
fi

MODEL=$(jq -r '.headless.model // empty' "$WALLET" 2>/dev/null)
ENVELOPE=$(jq -r '.headless.envelope // empty' "$WALLET" 2>/dev/null)
export TRIDENT_ENVELOPE="$ENVELOPE"

# Proxy floor (Wave 4a, headless only): point at the proxy ONLY when it is
# actually alive and not switched off — a dead proxy with the env set would
# break every API call, so health-gate here (the SPOF mitigation, §3.8).
if [ ! -f "$STATE/trident-proxy-off" ] && nc -z 127.0.0.1 8742 2>/dev/null; then
  export ANTHROPIC_BASE_URL="http://127.0.0.1:8742"
fi

# Append --model only for a claude invocation that doesn't already pin one.
HAS_MODEL=0
IS_CLAUDE=0
case "$(basename "${1:-}")" in claude|claude.exe) IS_CLAUDE=1;; esac
for a in "$@"; do [ "$a" = "--model" ] && HAS_MODEL=1; done

if [ "$IS_CLAUDE" = "1" ] && [ "$HAS_MODEL" = "0" ] && [ -n "$MODEL" ]; then
  exec "$@" --model "$MODEL"
fi
exec "$@"
