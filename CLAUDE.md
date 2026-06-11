# trident — 3-segment token-burn governor

OS-wide god-router for every AI request: **MINT** (pricing daemon, single writer of
`~/.claude/state/wallet.json`) → **SHAPER** (per-request mechanical routing: PreToolUse model rewrite,
tier-descent cascade, env-stamp for headless, ANTHROPIC_BASE_URL proxy floor) → **LEDGER**
(Stop+SubagentStop settlement, antibody memory, evolve-v2 ROI gate). Above them the **BRAIN**
(`brain/brain.py`, claude-fable-5, ≤2 fires/5h window): re-fits routing via a clamped expiring
`trident-policy.json` overlay; deterministic burn forecast + `trident-history.jsonl` ring feed it.

Read `ARCHITECTURE.md` before touching anything — it encodes the verified-mechanics table
(what Claude Code hooks can and cannot enforce) and the bright line (routing never cuts correctness).

## Invariants
- Lever resolution: `L_eff = pin if pinned else global` — pins are explicit per-terminal bypasses (human-set ONLY, never scripted); global governs all unpinned/new terminals. MINT owns resolution; consumers never re-derive. Theater guard: 📌 pin visibility everywhere + `theater_warning` + dead-pin reaping.
- L=100 = true free lane (zero lever damping). L=0 = minimum viable shape, never a halt.
- **Posture is ROUTING guidance, never a work-stop.** GREEN/AMBER/🔴RED change the *shape* (model tier, fan-out width, inline-vs-spawn), never *whether the task gets done*. RED ≠ stop — it means "keep working, cheapest sufficient shape." The emitted RED tag in `mint/mint.py:render_posture` and `~/.claude/scripts/budget-posture.sh` MUST carry this clarifier; never reword it back into something that reads as "halt / don't do it."
- Mechanical: spawn model tier, injection bytes, compact threshold, ROI admission. Advisory: width, thinking, voters (floor 1). Never claim advisory things are mechanical.
- Output filter never strips tracebacks/test output/diffs. Injection never drops the active-handoff chunk.
- Hooks must be sub-ms reads of wallet.json — all computation lives in the MINT daemon.
- Fail open: trident degrading must never block work.
- Tiers are now 4: haiku→sonnet→opus→fable (fable band = score≥0.9, free lane + near-full H_eff; ceiling is permissive, never forces fable).
- BRAIN rails live in `formulas.validate_policy` (code, not prompt): tier_bias ±1, bounded multipliers, `verify_min`/`roi_min`/L/H not overlay-addressable. Overlay applies to the DEFAULT block only — pins are human-sovereign. Brain spawns from `--daemon` ticks ONLY (never --tick/ingest/tests). Kill: `trident brain off`.
- **Human lever is sovereign over the brain.** `formulas.lever_stance(global)` (bands: `<40` tighten / `≥70` release / else neutral). In **tighten**, `validate_policy` forces the policy purely subtractive — volume knobs (width/think/inject) capped at neutral, compact earlier-only, spec can't be forced on; tier reshaping (narrow+smart) stays free. Re-applied EVERY read, so a throttle move re-bounds a live overlay instantly with no re-fire. A manual move ≥15pts is also a first-class brain fire trigger. The clamp can only pull toward the mechanical baseline, never reverse the developer's chosen direction.

## State
- Replaces 17 scattered pieces (list + migration waves in ARCHITECTURE.md).
- `~/.claude/scripts/throttle` (v2, global+pins) is owned by another workstream — coordinate before editing.
