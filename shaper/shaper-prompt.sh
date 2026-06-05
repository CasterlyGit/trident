#!/usr/bin/env bash
# trident SHAPER — UserPromptSubmit hook (DESIGN.md §3.3)
# Hard latency budget <100ms: jq reads of wallet.json ONLY — no python on this path.
#
# 1. Emit the [TRIDENT ...] envelope line (always, one line).
# 2. Write this session's inject cap → consumed by shed inject.sh as SHED_MAX_INJECT
#    (shed keeps SELECTION, trident owns SIZE; rank-0 handoff chunk exempt via top_k≥1).
# 3. Compact-guard: ONLY when trident owns guard duty (post-soak flag
#    ~/.claude/state/trident-guard-owner == "trident"); during soak shed's
#    compact-guard.sh keeps it and this path is dormant.
#
# Fail open everywhere: stale/missing wallet → exit 0 silent (advisory-only).
set -u

STATE="${TRIDENT_STATE_DIR:-$HOME/.claude/state}"
WALLET="$STATE/wallet.json"
input="$(cat)"

# Headless workers must never see guard text (verified 2026-06-04: hijacks task
# output). Envelope is still useful routing context — emit it; skip guard later.
WORKER=0
if [ "${SHED_ORIGIN:-}" = "phone" ] || [ -n "${CONDUCTOR_WORKER:-}" ]; then WORKER=1; fi

[ -f "$WALLET" ] || exit 0
NOW=$(date +%s)
AGE=$(( NOW - $(stat -f %m "$WALLET" 2>/dev/null || echo 0) ))
[ "$AGE" -le 120 ] || exit 0   # wallet stale → mint down → degrade silently

SID=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
BLK=$(jq -c --arg sid "$SID" '.derived.per_pin[$sid] // .derived.default' "$WALLET" 2>/dev/null) || exit 0
[ -n "$BLK" ] || exit 0

ENVELOPE=$(printf '%s' "$BLK" | jq -r '.envelope // empty')
[ -n "$ENVELOPE" ] && printf '%s\n' "$ENVELOPE"

# Inject sizing (MECHANICAL): per-session cap file; shed's inject.sh exports it
# as SHED_MAX_INJECT (top_k = max(1, tokens/1000) — floor 1 keeps rank-0 handoff).
CAP=$(printf '%s' "$BLK" | jq -r '.inject_cap_tokens // empty')
if [ -n "$CAP" ] && [ -n "$SID" ]; then
  printf '%s' "$CAP" > "$STATE/trident-inject-cap-$SID" 2>/dev/null || true
fi

# ---- compact-guard (dormant during soak; shed wording kept verbatim) ----
[ "$WORKER" = "1" ] && exit 0
OWNER=$(cat "$STATE/trident-guard-owner" 2>/dev/null || echo shed)
[ "$OWNER" = "trident" ] || exit 0
[ -f "${TRIDENT_GUARD_MUTE:-/tmp/shed-guard-mute}" ] && exit 0

SCALE=$(printf '%s' "$BLK" | jq -r '.compact_scale // 1')
TH="$HOME/.shed/state/guard-thresholds.json"
SOFT=$(jq -r '.soft_tokens // 100000' "$TH" 2>/dev/null || echo 100000)
STRONG=$(jq -r '.strong_tokens // 130000' "$TH" 2>/dev/null || echo 130000)
# scale thresholds by compact_scale (awk: float-safe, no python)
SOFT=$(awk -v t="$SOFT" -v s="$SCALE" 'BEGIN{printf "%d", t*s}')
STRONG=$(awk -v t="$STRONG" -v s="$SCALE" 'BEGIN{printf "%d", t*s}')

RLS="$STATE/rate-limits-$SID.json"
TOKENS=$(jq -r '.context_window.total_input_tokens // 0' "$RLS" 2>/dev/null || echo 0)
HF_ID="shed-ho-${SID:0:8}"

if [ "$TOKENS" -gt "$STRONG" ] 2>/dev/null; then
  printf '\n[COMPACT-GUARD: ~%dK tokens. SESSION IS HEAVY. Before doing anything else, tell the user this session is at ~%dK tokens, show them their handoff ID in backticks like `%s` so they can triple-click to copy it, and suggest they type /fresh. Do not start implementing until they respond.]\n' \
    "$(( TOKENS / 1000 ))" "$(( TOKENS / 1000 ))" "$HF_ID"
elif [ "$TOKENS" -gt "$SOFT" ] 2>/dev/null; then
  printf '\n[COMPACT-GUARD: ~%dK tokens. Mention to the user at the end of your response that /fresh would save tokens. Show handoff ID in backticks like `%s` so they can triple-click to select the whole thing for the new session.]\n' \
    "$(( TOKENS / 1000 ))" "$HF_ID"
fi
exit 0
