<h1 align="center">🔱 trident</h1>

<p align="center">
  <b>A self-tuning token-burn governor for AI coding agents.</b><br>
  Every AI request — terminal, IDE, headless <code>claude -p</code>, subagents, raw SDK —
  routed through three god-routers that price it, shape it, and settle it,<br>
  re-fitted up to twice per budget window by a Fable-class policy brain.
</p>

<p align="center">
  <a href="https://casterlygit.github.io/trident/">Live demo</a> ·
  <a href="./ARCHITECTURE.md">Architecture</a> ·
  <a href="./DESIGN.md">Design spec</a>
</p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-4%20waves%20%2B%20brain%20live-00e5ff">
  <img alt="tests" src="https://img.shields.io/badge/tests-80%2F80%20green-22c55e">
  <img alt="surfaces" src="https://img.shields.io/badge/governs-6%20surfaces-7c3aed">
  <img alt="brain" src="https://img.shields.io/badge/brain-%E2%89%A42%20fires%2F5h%20window-f472b6">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-555">
</p>

---

## What it is

Long-running AI agents burn tokens unevenly: a single careless fan-out, an over-eager
`opus` spawn, or a session that runs past its compact window can torch a 5-hour budget in
minutes. **trident** sits underneath every surface that can spend, and makes the budget a
*money supply* governed by one lever (0–100).

```
                ┌──────────────────────────────────────────────┐
 burn digest ─▶ │  BRAIN    re-fits it  → clamped policy overlay│  claude-fable-5 · ≤2 fires / 5h window
 (slow loop)    └──────────────────────────────────────────────┘
                ┌──────────────────────────────────────────────┐
 telemetry ───▶ │  MINT     prices it   → atomic wallet.json    │  the single source of truth
 (every 30s)    └──────────────────────────────────────────────┘
                ┌──────────────────────────────────────────────┐
 every spawn ─▶ │  SHAPER   shapes it   → model/width/envelope  │  rewrites over-ceiling spawns
                └──────────────────────────────────────────────┘
                ┌──────────────────────────────────────────────┐
 every stop ──▶ │  LEDGER   settles it  → counters + antibodies │  learns what to block next time
                └──────────────────────────────────────────────┘
```

The three prongs are why it's called trident. The brain is not a fourth prong — it's the
hand that periodically re-grips the other three.

## The brain (new)

The fast path is 100% mechanical — sub-ms hook reads of pre-baked curves. The **brain**
sits above it: at most twice per 5-hour window it reads a compact burn digest (telemetry
history ring, in-flight counters, a deterministic wall-ETA forecast) and asks
`claude-fable-5` for a routing policy — *"burn is 30pp/h with 4 idle windows: narrow the
fan-out, bias one tier up, compact earlier."* The policy is **clamped in code**
(tier ±1, bounded multipliers), expires on its own, applies only to the un-pinned default
lane, and can never touch verification floors. Every fire lands in an audit log with its
rationale and cost; `no_change` verdicts *delete* the overlay. Missing, expired, or
malformed policy → pure mechanical curves. Kill switch: `trident brain off`.

Deterministic forecasting rides along free: MINT fits the burn slope every tick and the
posture line warns `🔮 wall in ~2h10m at current burn` — before the wall, not after.

## The bright line

> **Routing only. The lever never trades away correctness, tests, or scope.**

Turning the lever down means *"spend fewer tokens for the same answer"* — a cheaper model
for a mechanical spawn, a tighter fan-out, an earlier handoff — never a worse answer. No
traceback stripping, no dropped context, no blocking a first read.

## The three segments

| Segment | Role | Mechanism |
|---|---|---|
| **MINT** | Prices the budget | launchd daemon, sole writer of atomic `wallet.json`; headroom = `min(5h, 7d, -p pool)`; lever `L_eff = min(global, pin)`. Hooks become sub-ms reads. |
| **SHAPER** | Shapes each spawn | `PreToolUse` rewrite of `AgentInput.model` (the one verified mechanical lever) + tier-descent cascade + per-turn `[TRIDENT …]` envelope + env-stamp for headless + an SSE-transparent proxy floor. |
| **LEDGER** | Settles + learns | `Stop` **and** `SubagentStop` settlement, cross-surface "antibody" memory (tighter budget → higher confidence to block), ROI-capped evolution so learning never locks itself out. |

## Honest limits

trident is mechanically ruthless where the Claude Code binary allows it, and openly
advisory where it doesn't:

- **Model rewrite on spawn** — ✅ mechanical (`updatedInput` enum is writable).
- **Fan-out width / thinking budget** — ❌ no hook field exists → counted + advisory only.
- **Main-loop model mid-session** — ❌ immutable; governed only via envelope + earlier compact/handoff.
- **`--bare` / untrusted / `disableAllHooks`** — survive only through the `ANTHROPIC_BASE_URL` proxy floor.

≈98% of real spend is sequential main-loop burn, which no per-call hook can govern — so
LEDGER's learning exists to close the advisory gap over time. See
[ARCHITECTURE.md](./ARCHITECTURE.md) for the full verified-mechanics table.

## Everything fails open

Every segment degrades to a no-op rather than blocking a call. A dead MINT daemon falls
back to legacy posture; a dead proxy routes direct; a missing envelope behaves as AMBER.
trident can never be the reason a request fails.

## Layout

```
mint/      mint.py daemon · formulas.py · canary.py        → wallet.json
shaper/    shaper-prompt.sh · shaper-tools.py · env-stamp.sh
ledger/    ledger-settle.py · ledger-filter.sh · antibodies.py
proxy/     trident-proxy.py   (SSE-transparent floor on :8742)
bin/       trident            (CLI: lever, pins, proxy on|off, posture)
launchd/   com.casterly.trident-mint · com.casterly.trident-proxy
tests/     test_wave1.py · test_wave234.py                 (34 tests)
```

## Usage

```bash
trident                 # show live posture: lever, headroom, 🟢/🟡/🔴
trident set 55          # set the global lever (0–100)
trident pin 30          # per-terminal bypass valve (human-only)
trident proxy on        # attach the hook-independent SSE floor
python3 -m pytest tests/ -q
```

## Roadmap

- [ ] Soak the legacy guards in parallel ~1 week, then flip the guard-owner flag and delete the 9 shimmed scripts.
- [ ] Wave 4b — proxy floor for interactive shells.
- [ ] TC0 traffic-class detection heuristic.
- [ ] Antibodies from observe-only → enforcing after the 2026-07-05 confidence window.

## License

MIT © CasterlyGit
