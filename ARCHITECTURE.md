# trident — the 3-segment token-burn governor

> Every AI request, on every surface, routed through three god-routers:
> **MINT** prices it · **SHAPER** shapes it · **LEDGER** settles it.
> The lever (0–100) is the money supply. Routing only — correctness, tests, and scope are never on the lever.

---

## Provenance

Synthesized 2026-06-04 from a 5-way architecture tournament (21 agents, 837k tokens):
OS-kernel scheduler · **token economy (winner, 17.2/30)** · network QoS · immune system · air-traffic control,
each attacked by 3 adversarial judges (enforceability / UX-invisibility / evolvability) + a completeness critic.
0/5 designs survived judging intact — this synthesis is built only from **mechanics the judges verified against the
Claude Code binary**, plus the best graft from every loser.

### Verified mechanics (what the binary actually permits)

| Mechanism | Status | Evidence |
|---|---|---|
| PreToolUse rewrite of `AgentInput.model` via `updatedInput` | ✅ MECHANICAL | `updatedInput` ×219 in binary; `model` is a writable enum (`sonnet\|opus\|haiku\|fable` — fable verified in sdk-tools.d.ts 2026-06-10) |
| PreToolUse DENY of any tool call (incl. Agent/Workflow) | ✅ MECHANICAL | `permissionDecision` ×32 |
| Fan-out width cap via tool input | ❌ NO FIELD | AgentInput has no concurrency/max_turns field — width is **advisory + counted** |
| Thinking-budget injection into a spawn | ❌ NO FIELD | API-level setting, no hook can touch it — **advisory only** |
| `Stop` for subagents | ❌ WRONG EVENT | Subagents fire **`SubagentStop`** — settlement must wire BOTH |
| Stop blocking | via JSON `decision:block` | not exit-code 2 |
| `--bare` / untrusted workspace / `disableAllHooks` | ⚠️ 3 total hook bypasses | only an `ANTHROPIC_BASE_URL` proxy survives all three |
| Main-loop model mid-session | ❌ IMMUTABLE | fixed at session start; no hook can downclock it |
| Sequential burn (≈98% of real spend) | ❌ UNGOVERNABLE per-call | governed only via earlier compact/handoff + advisory + learning |

Design rule that falls out: **be mechanically ruthless where the binary allows it, brutally honest where it doesn't,
and let LEDGER's learning close the advisory gap over time.**

---

## The three segments

```
                      ┌─────────────────────────────────────────────┐
   telemetry (30s) ──▶│  1. MINT        the single source of truth  │
   throttle lever  ──▶│  daemon · one writer · wallet.json (atomic) │
                      └──────────────────┬──────────────────────────┘
                                         │ sub-ms reads, never compute
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
   ┌────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
   │  2. SHAPER          │   │  2. SHAPER          │   │  3. LEDGER          │
   │  UserPromptSubmit   │   │  PreToolUse         │   │  Stop+SubagentStop  │
   │  inject sizing      │   │  model rewrite      │   │  + PostToolUse      │
   │  compact-guard      │   │  tier cascade       │   │  settle · learn ·   │
   │  envelope stamp     │   │  admission/deny     │   │  antibodies · ROI   │
   └────────────────────┘   └─────────────────────┘   └─────────────────────┘
                                         │
                                         ▼
                      ┌─────────────────────────────────────────────┐
                      │  SHAPER deep floor: ANTHROPIC_BASE_URL proxy│
                      │  (SSE-transparent) — survives --bare,       │
                      │  untrusted workspace, disableAllHooks,      │
                      │  raw-API tools                              │
                      └─────────────────────────────────────────────┘
```

### Segment 1 — MINT (price discovery; the only writer)

**Responsibility:** convert raw telemetry + the lever into a single atomic `~/.claude/state/wallet.json`
that every other component reads in sub-milliseconds. Nothing else touches telemetry. Ever.

- **Lives at:** `mint.py` — launchd daemon (folds in statusline events + 30s tick). Statusline hook becomes a
  pipe into mint, not a writer.
- **Absorbs (of the 17):** capture-rate-limits.sh · budget-posture.sh (becomes thin `mint posture` read) ·
  throttle CLI state · token_watch.py · usage-ledger aggregation.
- **Owns the headroom formula:**
  `H = min(5h_left%, 7d_left%, pool_left%) × imminence_bonus` then `H_eff = min(H, L_effective) / sqrt(active_sessions)`
  - imminence bonus: +40pp if reset <30m, +20pp if <60m (4/5 designs independently reinvented this — keep it).
  - **7-day window and the post-2026-06-15 separate `-p` Agent-SDK pool are first-class inputs** — a session can
    be 5h-green and 7d-red; MINT takes the min. (No tournament design did this; the critic demanded it.)
- **Owns lever resolution — pins are bypass valves (Tarun's intent, 2026-06-05):**
  `L_eff(session) = pins[sid] if pinned else global`. A pin deliberately routes that one terminal
  independently of global (e.g. global=50 but THIS terminal explicitly granted 100); global rules every
  unpinned and every NEW terminal. The failure mode is therefore not precedence but **silent theater**
  (everything pinned → lever looks on, governs nothing — observed live during the tournament). Guards:
  pins are **human-set only** (no script/hook may ever auto-pin); posture + statusline always show
  `📌 pinned N/M`; MINT raises `theater_warning` when pinned sessions are the majority of active burn;
  pins of dead sessions are reaped with their stale telemetry; `throttle unpin-all` exists.
- **Owns the flock'd counters:** in-flight spawn count + per-session spend ledger, `flock`-protected —
  kills the TOCTOU race where N parallel PreToolUse hooks all read stale count=0 and all pass.
- **Why a daemon:** the current UserPromptSubmit chain already measures ~3.1s (Python cold-start + vector inject)
  against a 2s budget → hooks **time out and fail OPEN on exactly the heavy turns they must govern**.
  All computation moves into MINT; per-turn hooks become a one-line read of pre-computed values.
- **Lever response:** computes money supply `M = (L_eff/100)^1.5` (convex — drops below 50 bite hard) and
  pre-bakes every downstream value (tier thresholds, widths, caps) into wallet.json so consumers do zero math.

### Segment 2 — SHAPER (per-request routing broker; the enforcement arm)

**Responsibility:** every request, at the moment it becomes concrete (a prompt, a tool call, an API call),
gets its shape rewritten to what the wallet affords. Mechanical where possible, stamped-advisory where not.

- **Lives at:** three thin probes + one deep floor:
  1. `shaper-prompt` (UserPromptSubmit) — sizes the `<shed-context>` injection from `wallet.inject_cap` (hard,
     it controls the bytes it emits), fires compact-guard at wallet-scaled thresholds, stamps a one-line
     **envelope** into context: `[TRIDENT L=63 tier≤sonnet W≤4 think≤8k V≥1]` — the advisory contract the
     model reads instead of running budget-posture.sh.
  2. `shaper-tools` (PreToolUse) — **the load-bearing primitive** (all 5 designs converged on it):
     rewrites `Agent`/`Workflow` `model` fields via `updatedInput`; enforces the **tier-descent cascade**
     (ATC's graft, expressed through the only writable lever): depth 0 may be Opus → children forced ≤Sonnet →
     grandchildren forced Haiku, ceilings from wallet. Checks MINT's flock'd width counter (best-effort mechanical).
     Can DENY a Workflow/Agent call outright when wallet says the bid is unaffordable — with a one-line reason the
     model can act on (split the task / lower the shape). Absorbs claudeignore-guard's wasteful-read denies.
  3. `env-stamp` — wraps every headless spawn (workflow-watcher, conductor, cron): injects
     `TRIDENT_ENVELOPE=<json>` + model selection into the child env **before** `claude -p` launches, so headless
     is governed even though it has no interactive prompt hook. Never `--bare` without a stamp.
  4. **`trident-proxy` (the deep floor)** — local `ANTHROPIC_BASE_URL` proxy, SSE-transparent passthrough whose
     ONLY power is a model-id ceiling (rewrite `opus→sonnet→haiku` per wallet tier) + spend metering.
     It is the one surface that survives `--bare`, untrusted workspaces, `disableAllHooks`, and raw-SDK tools
     sharing the account pool. Fail-open by design: proxy down → env var unset → direct API (governance degrades,
     work never stops).
- **Absorbs:** compact-guard.sh · inject.sh sizing · S1 governor.py fanout_scale · token-gate skill's
  mechanical half · claudeignore-guard.sh · approver's budget overlap (approver keeps autonomy logic,
  loses budget logic; it may approve but never un-DENY).
- **Traffic classes (TTC's graft)** — class sets the *floor*, lever sets the *ceiling*:
  - **TC0 interactive-debug** (you, mid-bug): floor Sonnet + inject never below active-handoff chunk. Never starved.
  - **TC1 standard** (coding, terminal/VS Code): full lever curves.
  - **TC2 background** (workflow-watcher, conductor, cron): shaped first, hardest.
  - **TC3 speculative** (shed-learn, evolve, demos): first thing gated to zero as M falls.
- **Lever response:** applies wallet's pre-baked curves (below). At L=100: **true free lane** — no lever damping,
  no rewrite, envelope says `FULL`; only the real budget H still matters. At L=0: defined floor, not a halt —
  Haiku-only, W=1, thinking off, inject = active-handoff-only. Work continues at minimum viable shape.

### Segment 3 — LEDGER (settlement, memory, self-improvement)

**Responsibility:** measure what actually happened, reconcile it against what SHAPER stamped, and make the
whole system smarter — the advisory gap closes over time because LEDGER remembers.

- **Lives at:** `ledger-settle` on **Stop AND SubagentStop** (the event no design wired — without it, fan-out
  accounting silently dies) + `ledger-filter` on PostToolUse (noise strip; **never truncates real content** —
  see bright line) + the antibody store.
- **Absorbs:** handoff-writer's threshold learner (composes with `update-guard-thresholds.py`, does not replace) ·
  token-warn.sh · log-session-stats.sh · filter-tool-output.sh · evolve-v2's ROI gate · shed grader's
  budget-awareness · claude-meter/glyph display feeds.
- **Antibody memory (immune graft):** a waste pattern observed once (wide fan-out that returned nothing,
  speculative reread loop, Opus spawn for a lookup) is recorded in `antibodies.json`, keyed
  **cross-surface** (pattern, not session-surface), strengthened on recurrence; after N confirmations + grader
  score it graduates from advisory warning to a mechanical SHAPER deny/downgrade rule.
  **Inverted activation curve (the immune design's fix):** the tighter the budget, the *higher* the confidence
  required to mechanically block — governance must never get trigger-happy exactly when mistakes are costliest.
  Tolerance registry: known-expensive-but-legitimate workflows are whitelisted past antibodies.
- **Self-improvement gating:** evolve-v2 runs only when observed ROI clears `R_min = min(1.2/M, 6)` — the bar
  rises as budget falls but is **capped** so learning is never fully locked out (fixes the
  tighter-budget→can't-learn ratchet the judges caught).
- **Lever response:** settlement always runs (it's cheap and it's the feedback plane). What scales: handoff
  thresholds fire earlier as M falls (the only honest lever on the ungovernable 98%-sequential main loop:
  end heavy sessions earlier, restart clean), display verbosity, learning-run admission.

### The BRAIN (slow loop above the segments — added 2026-06-10)

The fast path stays 100% mechanical. The brain (`brain/brain.py`, claude-fable-5) is a
*policy re-fitter* sitting above it:

- **Cadence:** ≤2 fires per 5h window, spawned detached from MINT `--daemon` ticks only
  (never `--tick`/ingest/tests). Triggers: no live policy + 45min cooldown, or a
  posture-band flip + 20min cooldown. Never below 8% left (don't burn the dregs deciding
  how to save the dregs). Kill switch: `~/.claude/state/trident-brain-off`.
- **Input:** a compact burn digest — wallet, lever, 45min of the `trident-history.jsonl`
  ring (one line per tick, 200KB cap), counters, pace, the deterministic forecast.
- **Output:** strict-JSON knobs → `trident-policy.json`, expiring (≤2h, never past reset).
  MINT folds it into the **default block only — pins are human bypass valves, no machine
  policy may re-route them.** Envelope gains `🧠` while a policy is live.
- **Rails are code, not prompt:** `formulas.validate_policy` is the single gate —
  tier_bias ±1, width 0.5–1.5×, think 0.5–1.5×, inject 0.6–1.25× (floor 800), compact
  0.75–1.25×, spec on/off. `verify_min`/`roi_min`/L/H are not overlay-addressable at
  all. Garbage/expired/missing policy → pure mechanical curves (fail open, like
  everything else).
- **Explainability:** every fire appends digest summary, raw + clamped policy, rationale,
  cost and duration to `trident-brain-audit.jsonl` (`trident brain audit`). An explicit
  `no_change` verdict *deletes* any live overlay — the brain can hand control back.
- **Forecast (deterministic, model-free):** MINT fits burn slope over the history ring
  each tick → `wallet.forecast {burn_pph, eta_exhaust_min, wall_before_reset}`; posture
  shows `🔮 wall in ~2h10m at current burn` when the wall lands inside 8h. The brain
  consumes it; the WALL stays the hard backstop.
- **Why Fable in the loop is not ironic:** ~1 call per 2.5h amortized against every
  routing decision in the window; the call itself runs with `ANTHROPIC_BASE_URL`
  stripped so trident's own proxy ceiling can't rewrite its model out from under it.

---

## Lever semantics v2 (continuous, 0–100)

`L_eff = pin if pinned else global` (pin = explicit per-terminal bypass) · `M = (L_eff/100)^1.5` · `H` from MINT · all values pre-baked into wallet.json (per-pin blocks included).

| Dimension | Formula | L=100 | L=63 | L=30 | L=0 | Enforcement |
|---|---|---|---|---|---|---|
| Spawn model ceiling | tier curve on `M·H` + depth cascade | Fable (H_eff≥90) else Opus | Sonnet (Opus if score>0.95) | Sonnet rare, Haiku default | Haiku | **MECHANICAL** (PreToolUse rewrite + proxy) |
| Fan-out width | `W = max(1, floor(12·M·(H/100)^1.2))` | 12 | 5 | 1–2 | 1 | counted (flock) + advisory + Workflow-DENY |
| Thinking budget | `T = 32k·M·(H/100)²` (quadratic — first luxury shed) | 32k | ~8k | ~1k | 0 | **ADVISORY** (envelope) — no API hook exists |
| Context injection | `I = floor(4000·M^0.6)` (concave — context degrades slowly) | 4000 | 3000 | 1900 | handoff-only | **MECHANICAL** (shaper-prompt emits the bytes) |
| Verification voters | `V = max(1, round(5·M·H/100))` — **floor 1, always** | 3–5 | 2 | 1 | 1 | advisory; the floor is the routing/rigor boundary |
| Speculative work | allowed iff `M·H > 15` | yes | yes | no | no | advisory + TC3 admission deny |
| Compact/handoff threshold | `C = base·(0.5 + M/2)` — fires earlier when tight | base | 0.75× | 0.6× | 0.5× | **MECHANICAL** (guard injection) |
| Output noise filter | noise-strip always; **real content never cut** | — | — | — | — | mechanical, bright-line bounded |
| evolve-v2 ROI bar | `R_min = min(1.2/M, 6)` | 1.2 | 2.4 | 6 | 6 | **MECHANICAL** (LEDGER admission) |

**Endpoint contracts (the two the tournament got wrong):**
- **L=100 = the free lane.** No lever damping anywhere. Only real headroom H still routes (budget reality is not the lever's job to hide).
- **L=0 = minimum viable, never a halt.** Haiku, inline, no thinking, handoff-sized context. The system still answers.

**The bright line (every design violated it; trident's invariant #1):**
routing may change *how* an answer is produced, never *what is true in it*. Mechanically encoded:
- output filter strips ANSI noise/progress spam only — **never tracebacks, never test output, never diffs**
- injection sizing **never drops the active-handoff/project-state chunk** (it is rank-0 by definition, outside top-k)
- speculative gate blocks *re-runs and prefetch*, **never first-reads** of files the task names
- verification floor is 1, not 0

---

## Surface coverage matrix

| Surface | MINT sees it | SHAPER governs it | LEDGER settles it |
|---|---|---|---|
| Terminal interactive | statusline → daemon | prompt-hook + tools-hook + proxy | Stop |
| VS Code extension | same (shared settings.json) | identical path (no special-casing — the binary's real flag is internal; treat identically) | Stop |
| Headless `claude -p` | per-session telemetry + **post-06-15 pool tracked separately** | env-stamp + tools-hook; `--bare` callers → **proxy floor only** | Stop + exit ledger |
| Subagent / Workflow spawns | flock'd spawn counter | tier-descent rewrite at spawn (PreToolUse fires in parent) | **SubagentStop** |
| Raw SDK / MCP / anything else | proxy metering | **proxy model-ceiling only** (honest: that's all anyone can do) | proxy spend log |

---

## Honest limits (stated, not hidden)

1. **~98% of spend is sequential main-loop burn** — no hook can cap it per-call. Trident governs it via:
   envelope advisory (one line, always in context, no posture-script to forget) + earlier compact/handoff when
   tight + LEDGER antibodies converting repeated waste into mechanical rules. That is the whole honest toolkit.
2. **Main-loop model is fixed at session start.** A lever drop mid-session affects spawns now, the main loop
   only at next session. Mitigation: compact-guard fires earlier → heavy sessions end and restart in the new shape.
3. **Telemetry is up to 30s stale; wallet adds ≤1s.** A 100ms 8-wide Opus wave can outrun settlement once.
   It cannot do it twice — the flock'd counter catches in-flight, antibodies catch the pattern.
4. **Hooks can be bypassed** (`--bare`, trust, disableAllHooks). The proxy floor turns "bypassed" into
   "model-capped + metered" instead of "invisible".

## Failure-mode defenses (from the critic's unaddressed list)

- **TOCTOU spawn races** → flock'd counter in MINT, the single writer.
- **Fail-open hook timeouts** → daemon pre-computes; hooks are sub-ms reads. A hook that still fails fails open
  (work > governance), but the steady-state cause (3.1s Python chains) is gone.
- **CC version drift** → `trident canary`: MINT validates the statusline/AgentInput/hook contract on every CC
  update (version string change), degrades to advisory-only mode + notifies rather than silently breaking;
  wallet.json carries `schema_version` + `contract_ok: bool` every consumer checks.
- **6-window races** → wallet is the only writer; sessions never read each other's per-session files.
- **Lever pins** → intentional per-terminal bypasses (`L_eff = pin if pinned else global`), resolved ONLY in MINT; theater prevented by visibility (📌 count everywhere), human-only pinning, dead-pin reaping, `theater_warning`.
- **7d/pool blindness** → headroom takes the min of all live windows/pools.
- **June 15 `-p` pool split** → MINT tracks pools separately from day one; headless headroom uses its own pool.

---

## Migration — 4 waves (what dies, what survives)

**Wave 1 — MINT (foundation).** Build `mint.py` daemon + wallet.json + flock counters + pin resolution
(bypass semantics, human-only pins, theater visibility — coordinate with the sibling session that owns
throttle v2 before MINT takes over resolution). Statusline pipes to mint.
Run in parallel with capture-rate-limits.sh for one week; then DELETE capture-rate-limits.sh, token_watch.py.
budget-posture.sh → thin `mint posture`.

**Wave 2 — SHAPER probes.** `shaper-prompt` (absorbs inject sizing + compact-guard; DELETE both after soak) ·
`shaper-tools` (the model-rewrite primitive + cascade + DENY; absorb claudeignore-guard, S1 governor.fanout_scale;
DELETE both after soak) · `env-stamp` into workflow-watcher + conductor launchers (net-new). Update CLAUDE.md
Prime Directive 3: the 4-band table → one line "read the envelope; it is already in your context."
token-gate skill keeps the *consent* role, loses the cost-math role.

**Wave 3 — LEDGER.** `ledger-settle` on Stop+SubagentStop (absorbs token-warn, log-session-stats; composes with
handoff-writer + threshold learner) · `ledger-filter` (absorbs filter-tool-output) · antibodies.json v1
(observe-only month, then graduation enabled) · evolve-v2 ROI gate wiring · claude-meter/glyph read wallet.json.

**Wave 4 — proxy floor + canary.** `trident-proxy` (SSE-transparent, model-ceiling only, fail-open) wired via
ANTHROPIC_BASE_URL in launchd env + shell profile; `trident canary` on CC-update detection. Then the final
deletion pass (winner's step-9 list) — **17 pieces → 3 segments + 1 lever + 1 proxy.**

**Survives, subordinated:** throttle CLI (UI for the lever; MINT owns semantics) · shed memory/inject content
(SHAPER sizes it, shed selects it) · shed learn/grader (LEDGER-aware) · approver (autonomy only, budget logic
deleted) · evolve-v2 (ROI-gated) · CLAUDE.md directives (the soft layer, now one envelope line).

## Open decisions (need Tarun)

1. ~~Pin precedence~~ **RESOLVED 2026-06-05:** pins are intentional per-terminal bypasses; global rules unpinned/new terminals. Anti-theater guards in MINT (see Segment 1). Remaining: coordinate with the throttle-v2 sibling workstream before MINT takes over resolution.
2. Proxy: launch-at-login for all surfaces, or headless-only first?
3. TC0 detection heuristic (interactive-debug floor): tty + recent human turn cadence, or explicit `throttle tc0`?
4. Antibody graduation N (default proposal: 3 confirmations + grader ≥0.7, observe-only first month).
