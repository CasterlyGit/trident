#!/usr/bin/env bash
# trident LEDGER — PostToolUse noise strip (DESIGN.md §3.6), bright-line bounded.
# Absorbs filter-tool-output.sh: COMPOSES the same filter-noise.py (loss-LESS noise
# removal only). Bright line: never strips tracebacks/test output/diffs; never
# touches Read/Grep/Glob (structural output); never replaces output with empty.
# Wired IN PLACE of filter-tool-output.sh (never both — double-filtering).
# Runs in every session incl. live shed — never error visibly, always exit 0.
set -uo pipefail

PAYLOAD=$(cat 2>/dev/null) || exit 0
[ -n "$PAYLOAD" ] || exit 0

TOOL=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null) || exit 0
case "$TOOL" in
  Bash|TaskOutput|WebFetch) ;;   # NEVER Read/Grep/Glob — structural
  *) exit 0 ;;
esac

OUTPUT=$(printf '%s' "$PAYLOAD" | jq -r '.tool_response.output // empty' 2>/dev/null) || exit 0
[ -n "$OUTPUT" ] || exit 0
OUTLEN=${#OUTPUT}
[ "$OUTLEN" -lt 1000 ] && exit 0   # small output: never worth touching

# Bright line, belt-and-suspenders: outputs that look like tracebacks/tests/diffs
# pass through UNTOUCHED even before the filter sees them.
if printf '%s' "$OUTPUT" | head -c 8000 | grep -qE 'Traceback \(most recent call last\)|FAILED|AssertionError|^diff --git|^@@ |^[+-]{3} ' 2>/dev/null; then
  exit 0
fi

FILTERED=$(printf '%s' "$OUTPUT" | python3 "$HOME/.claude/scripts/filter-noise.py" "$OUTLEN" 2>/dev/null)
[ $? -ne 0 ] && exit 0
[ -z "$FILTERED" ] && exit 0       # never replace with empty

jq -cn --arg out "$FILTERED" '{"hookSpecificOutput":{"updatedToolOutput":$out}}' 2>/dev/null || exit 0
