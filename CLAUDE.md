# trident — 3-segment token-burn governor

OS-wide god-router for every AI request: **MINT** (pricing daemon, single writer of
`~/.claude/state/wallet.json`) → **SHAPER** (per-request mechanical routing: PreToolUse model rewrite,
tier-descent cascade, env-stamp for headless, ANTHROPIC_BASE_URL proxy floor) → **LEDGER**
(Stop+SubagentStop settlement, antibody memory, evolve-v2 ROI gate).

Read `ARCHITECTURE.md` before touching anything — it encodes the verified-mechanics table
(what Claude Code hooks can and cannot enforce) and the bright line (routing never cuts correctness).

## Invariants
- Lever resolution: `L_eff = pin if pinned else global` — pins are explicit per-terminal bypasses (human-set ONLY, never scripted); global governs all unpinned/new terminals. MINT owns resolution; consumers never re-derive. Theater guard: 📌 pin visibility everywhere + `theater_warning` + dead-pin reaping.
- L=100 = true free lane (zero lever damping). L=0 = minimum viable shape, never a halt.
- Mechanical: spawn model tier, injection bytes, compact threshold, ROI admission. Advisory: width, thinking, voters (floor 1). Never claim advisory things are mechanical.
- Output filter never strips tracebacks/test output/diffs. Injection never drops the active-handoff chunk.
- Hooks must be sub-ms reads of wallet.json — all computation lives in the MINT daemon.
- Fail open: trident degrading must never block work.

## State
- Replaces 17 scattered pieces (list + migration waves in ARCHITECTURE.md).
- `~/.claude/scripts/throttle` (v2, global+pins) is owned by another workstream — coordinate before editing.
