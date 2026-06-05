# trident — DESIGN.md (execution spec)

> Companion to `ARCHITECTURE.md` (the *why*). This file is the *how*: every component mapped to exact
> paths, schemas, wiring, formulas, and acceptance gates. A fresh session should be able to build trident
> from this file alone. Execute wave by wave; run the acceptance gate before moving on.
>
> **Before any wave: run `~/.claude/scripts/budget-posture.sh` and honor it.** Building the governor must
> not blow the budget the governor exists to protect. Each wave is sized to be finishable inside one session.

---

## 0. Resolved decisions (do not re-litigate)

| Decision | Resolution |
|---|---|
| Pin semantics | **Bypass valves.** `L_eff = pins[sid] if sid in pins else global`. A pinned terminal routes independently (pin may exceed global); global rules every unpinned + every new terminal. |
| Anti-theater | Pins human-set ONLY (no script/hook may write `pins`). `📌 N/M` shown in posture + statusline. MINT sets `theater_warning=true` when pinned sessions ≥50% of *active* sessions. Dead-session pins reaped when their `rate-limits-<sid>.json` is gone/stale >24h. `throttle unpin-all` exists. |
| Segment count | Exactly 3: MINT / SHAPER / LEDGER (+ proxy as SHAPER's deep floor). |
| Lever curve | `M = (L_eff/100)^1.5`. L=100 → free lane (no lever damping). L=0 → minimum viable (Haiku/inline/no-thinking), never a halt. |
| Bright line | Routing never cuts correctness: never strip tracebacks/test output/diffs; never drop the active-handoff chunk; never block first-reads; verification floor = 1. |
| Fail direction | Everything fails OPEN (work > governance). Failures must be *visible* (posture line + `contract_ok=false`), never silent AND consequential. |
| Honesty labels | Mechanical: spawn model tier, injection bytes, compact threshold, ROI admission, proxy ceiling. Advisory: fan-out width (flock-counted), thinking budget, voter count. Never claim otherwise. |

---

## 1. File map

### Created (all new)
```
~/Documents/Dev/trident/
  mint/mint.py                 # Segment 1 daemon
  mint/formulas.py             # single source of all curves (imported by mint only)
  shaper/shaper-prompt.sh      # UserPromptSubmit (thin: jq reads + emit)
  shaper/shaper-tools.py       # PreToolUse rewrite/deny (thin: read wallet, no math)
  shaper/env-stamp.sh          # headless spawn wrapper
  ledger/ledger-settle.py      # Stop + SubagentStop
  ledger/ledger-filter.sh      # PostToolUse noise strip (bright-line bounded)
  ledger/antibodies.py         # pattern store + graduation (library, called by settle)
  proxy/trident-proxy.py       # ANTHROPIC_BASE_URL SSE-transparent model-ceiling proxy
  bin/trident                  # CLI: status | posture | canary | counters | pause
  launchd/com.casterly.trident-mint.plist
  launchd/com.casterly.trident-proxy.plist
  tests/                       # per-component smoke tests (see §7)

~/.claude/state/
  wallet.json                  # MINT's atomic output (schema §2)
  trident-counters.json        # flock'd in-flight counters
  antibodies.json              # LEDGER pattern memory
  trident-canary.json          # contract check result per CC version
```

### Modified
```
~/.claude/settings.json            # hook rewiring (§5)
~/.claude/scripts/throttle         # v3: stays the lever UI; resolution logic REMOVED (MINT owns it)  ⚠ coordinate w/ sibling workstream
~/.claude/scripts/budget-posture.sh# becomes thin `trident posture` shim reading wallet.json
~/.claude/CLAUDE.md                # Prime Directive 3: 4-band table → "read the [TRIDENT …] envelope"
workflow-watcher + conductor launchers  # call env-stamp.sh before claude -p
~/.claude/skills/token-gate/SKILL.md    # keeps consent role; cost math now read from wallet
```

### Deleted (only after the soak called out in each wave)
`capture-rate-limits.sh` · `token_watch.py` · shed `compact-guard.sh` · shed `inject.sh` (sizing half — selection stays in shed) · `claudeignore-guard.sh` · `filter-tool-output.sh` · `token-warn.sh` · shed `governor.py` fanout logic · `log-session-stats.sh` · standalone `guard-thresholds.json`

---

## 2. `wallet.json` schema (the one contract everything reads)

Written atomically (`tmp + rename`) by mint.py ONLY. Consumers do **zero math** — every value pre-baked.

```jsonc
{
  "schema_version": 1,
  "contract_ok": true,              // canary result; false → all consumers degrade to advisory-only
  "updated_at": "2026-06-05T02:10:00Z",
  "telemetry": {
    "five_hour":  { "left_pct": 52.0, "resets_at": 1781234567 },
    "seven_day":  { "left_pct": 71.0, "resets_at": 1781834567 },
    "p_pool":     { "left_pct": null, "active_from": "2026-06-15" },   // headless Agent-SDK pool, post-split
    "active_sessions": 3,
    "stale_s": 12                    // age of newest telemetry; >120 → degrade flag in posture
  },
  "lever": {
    "global": 50,
    "pins": { "<sid>": 100 },        // mirrored from burn-throttle.json (throttle CLI still writes it)
    "pinned_count": 1,
    "theater_warning": false
  },
  "derived": {
    "default": {                     // L_eff = global  → for every unpinned session
      "L_eff": 50, "M": 0.354, "H": 36.7,
      "tier_ceiling_spawn": "sonnet",     // depth-0 session spawning children
      "tier_ceiling_child": "haiku",      // session that is itself a subagent
      "fanout_max": 3,
      "thinking_budget": 4000,            // ADVISORY (envelope only)
      "inject_cap_tokens": 2150,          // MECHANICAL (shaper-prompt emits the bytes)
      "verify_min": 1,
      "speculative": false,
      "compact_scale": 0.68,              // multiplier on shed guard thresholds
      "roi_min": 3.4,
      "envelope": "[TRIDENT L=50 H=37 tier≤sonnet W≤3 think≤4k inject≤2.1k V≥1 spec=off]"
    },
    "per_pin": { "<sid>": { /* same shape, computed at that pin's L_eff */ } }
  },
  "headless": {                      // values env-stamp.sh injects (computed for TC2 at L_eff=global)
    "model": "claude-sonnet-4-6", "envelope": "[TRIDENT TC2 ...]"
  }
}
```

`trident-counters.json` (separate file — hot path, flock'd):
```jsonc
{ "spawns_inflight": 2, "by_session": { "<sid>": 1 }, "updated_at": "..." }
```
Lock protocol: `flock(LOCK_EX)` on `trident-counters.lock`, read-modify-write, release. Increment in
shaper-tools on Agent/Workflow allow; decrement in ledger-settle on SubagentStop; mint.py reconciles
drift every tick (counter > live subagent transcripts → reset to observed).

---

## 3. Component specs

### 3.1 `mint.py` — Segment 1 daemon
- **Trigger:** launchd `com.casterly.trident-mint`, KeepAlive, tick every 30s; ALSO receives statusline
  pushes (statusline command pipes its stdin JSON to `mint.py --ingest -` then prints the status string —
  absorbs `capture-rate-limits.sh`, keep its NULL-guard + monotonic stale-value guard logic verbatim).
- **Algorithm per tick:**
  1. Ingest newest telemetry (statusline push or per-session files); apply both guards.
  2. Read `burn-throttle.json` (throttle CLI remains the *writer* of lever intent).
  3. Reap dead pins + stale `rate-limits-*.json` (>24h), cap 50 (keep capture-rate-limits.sh behavior).
  4. `H_raw = min(5h_left, 7d_left, p_pool_left if active) + imminence_bonus` (+40pp if reset<30m, +20pp if <60m, cap 100).
  5. For `default` (L_eff=global) and each pin: `M=(L_eff/100)^1.5`; `H = min(H_raw, 100)/sqrt(active_sessions)`
     — **except** at L_eff=100: skip lever damping entirely (free lane; only H_raw matters).
  6. Compute every derived value via `formulas.py` (§6). Render envelope strings.
  7. `theater_warning = pinned_active ≥ 0.5 × active_sessions`.
  8. Atomic write wallet.json. Reconcile counters.
- **Failure mode:** daemon down → wallet stale. Every consumer checks `updated_at`; if >120s old, behave
  as `contract_ok=false` (advisory-only, log one posture warning). Never block work.
- **Acceptance gate:** `trident status` shows live wallet <35s old across 2 terminals; pin one terminal →
  its `per_pin` block appears within one tick; kill daemon → consumers visibly degrade, nothing breaks.

### 3.2 `throttle` v3 (coordinate with sibling workstream — they own v2)
- Keeps: `throttle [N|up|down|full|pin|unpin|pins|unpin-all|show]`, writes `burn-throttle.json` only.
- Removes: any resolution/posture logic (MINT owns it). Gauge display now also prints `📌 pinned N/M` and
  `theater_warning` if set. **No code path anywhere else may write `pins`.**

### 3.3 `shaper-prompt.sh` — UserPromptSubmit (replaces compact-guard.sh + inject.sh *sizing*)
- **Hard latency budget: <100ms.** jq reads of wallet.json only; NO python on this path.
- Steps: resolve own sid → pick `derived.per_pin[sid] // derived.default` → 
  1. emit envelope line (always, one line);
  2. compact-guard: compare session tokens (`rate-limits-<sid>.json` context_window) against shed
     `guard-thresholds.json × compact_scale`; if crossed, emit the `[COMPACT-GUARD: …]` block (keep shed's
     existing wording — the ratchet behavior is learned downstream);
  3. injection sizing: export `SHED_MAX_INJECT=<inject_cap_tokens>` consumed by shed's inject (shed keeps
     *selection*, trident owns *size*). **Rank-0 active-handoff chunk is exempt from the cap** (bright line).
- **Acceptance gate:** `time` the hook <100ms; envelope visible in transcript; drop lever → envelope updates
  next turn; heavy session triggers guard earlier at low lever.

### 3.4 `shaper-tools.py` — PreToolUse on `Agent|Workflow` (the load-bearing primitive)
- Read wallet block (own sid) ONCE. Determine depth: hook payload `transcript_path` contains `/subagents/`
  → this session IS a subagent → ceiling = `tier_ceiling_child`; else `tier_ceiling_spawn`.
- For `Agent`: if requested `model` tier > ceiling → return `updatedInput` with `model` lowered to ceiling.
  Never raise. Missing model → leave (inherits parent; ceiling still binds the child's own spawns).
- For `Workflow`: read `meta.phases[].model` hints? NO — out of contract. Instead: DENY with reason when
  `speculative=false` AND prompt/args look speculative (TC3) or when `spawns_inflight ≥ fanout_max × 2`
  (runaway guard). Reason string must tell the model the cheaper shape: `"trident: over width budget
  (W≤3). Split into ≤3 agents or run inline."`
- Width: flock-increment counter; if `spawns_inflight > fanout_max` → still ALLOW (advisory dimension)
  but append warning to `updatedInput.prompt`? NO — do not mutate prompts (correctness risk). Log to
  ledger + envelope shows the breach next turn. Only the runaway guard (2×) hard-denies.
- Absorb claudeignore-guard: port its big-read/repeat-read deny rules verbatim (they're mechanical + safe).
- **Output JSON shape:** verify exact `hookSpecificOutput`/`permissionDecision`/`updatedInput` field names
  against the installed CC version AT BUILD TIME (canary §3.9 does this continuously after).
- **Acceptance gate:** in a live session ask for 5 Opus subagents at lever 40 → transcript shows spawns ran
  Sonnet/Haiku; pinned-100 terminal spawns Opus untouched; subagent session's own spawns forced to Haiku.

### 3.5 `env-stamp.sh` — headless wrapper
- Usage: `env-stamp.sh claude -p "..."` in workflow-watcher + conductor launchers.
- Injects `TRIDENT_ENVELOPE` env + `--model $(wallet headless.model)` onto the command line (the ONE
  mechanical main-loop lever that exists: model choice at session start). Refuses to add `--bare` itself;
  if caller insists on `--bare`, stamp still applies (env + --model survive; hooks don't — proxy floor covers).
- **Acceptance gate:** workflow-watcher build runs Sonnet at lever 50, Opus at lever 100 + GREEN.

### 3.6 `ledger-settle.py` — Stop AND SubagentStop (two hook entries, same script)
- On SubagentStop: decrement flock counter; append spawn record (model, tokens if available) to
  `session-stats.jsonl` (absorbs log-session-stats.sh format — keep fields identical for evolve-v2 compat).
- On Stop: write session settlement (planned-vs-actual: envelope values vs measured burn via
  usage-ledger.py); feed shed's `update-guard-thresholds.py` (COMPOSE — call it, don't reimplement);
  run `antibodies.observe()` (§3.7); token-warn behavior folds in (same thresholds source).
  Handoff-writer stays separate (it's shed's, already works).
- Stop blocking: only the existing approver/shed semantics — trident itself NEVER blocks Stop (it's a
  meter, not a cage). JSON `decision:block` is approver's tool, not ours.
- **Acceptance gate:** after a fan-out session, `session-stats.jsonl` shows N spawn records + 1 settlement;
  counters return to 0; guard thresholds file updated.

### 3.7 `antibodies.py` — learned waste patterns
- Schema per antibody: `{pattern_key, kind: fanout_waste|speculative_rerun|tier_overkill|reread_loop,
  evidence: [run_ids], confirmations, grader_score, state: observe|advise|block, surfaces_seen: [...]}`.
- Keyed by **pattern**, not surface (cross-surface transfer). Minted/strengthened in settle when
  planned-vs-actual shows waste (spawned 8, used 1; Opus spawn returned <500 tokens; same file read 5×…).
- Graduation: `confirmations ≥ 3 AND grader_score ≥ 0.7` → state=advise (envelope mentions it);
  `≥5 AND ≥0.8` → state=block (shaper-tools enforces as deny/downgrade). **Inverted activation:** when
  `M < 0.3`, blocking requires `≥0.9` — never trigger-happy when mistakes are costliest. Tolerance list:
  `~/.claude/state/antibody-tolerance.json`, human-edited, bypasses blocks.
- First month: everything stays `observe` (hard-coded until date in config), then graduation enables.
- **Acceptance gate:** seeded synthetic waste run mints an antibody; 3 repeats graduate it; tolerance entry suppresses it.

### 3.8 `trident-proxy.py` — the hook-independent floor
- aiohttp/raw-asyncio reverse proxy on `127.0.0.1:8742` → `api.anthropic.com`. **SSE-transparent:**
  stream chunks through untouched (no buffering — this is the make-or-break requirement; test with a
  streamed completion before wiring anything).
- Only two powers: (1) rewrite `"model"` in request body when tier > wallet ceiling for the requesting
  class (classify by env header the stamp adds, else default class); (2) append `{ts, model, usage}` to
  `proxy-ledger.jsonl` from response (non-streaming fields or final SSE `message_delta`).
- Wired via `ANTHROPIC_BASE_URL=http://127.0.0.1:8742` in: launchd env for headless daemons FIRST
  (Wave 4a); shell profile for interactive LAST and only after a week of headless soak (Wave 4b — Tarun
  decision pending). Health: launchd KeepAlive; if port dead, env var is harmless? **NO — a dead proxy
  with the env set breaks all API calls.** Mitigation: `trident proxy off` flips a profile include +
  launchd env removal in one command; proxy also self-checks upstream on start and exits non-zero (so
  KeepAlive loops) rather than half-working. Document this as the known SPOF; default posture = headless-only.
- **Acceptance gate:** streamed Opus request at lever 30 arrives at API as Sonnet, streams back smoothly;
  `--bare` claude -p through proxy gets ceilinged; `trident proxy off` restores direct in <5s.

### 3.9 `trident canary` — contract drift detector
- Runs on mint startup + when CC version string changes (`claude --version` cached in canary file).
- Checks: statusline JSON still has `rate_limits.five_hour.used_percentage`; hook payload fixture still
  parses; `AgentInput` schema still has writable `model` enum (probe via `claude --help`/sdk-tools.d.ts
  if present); settings.json hooks still registered.
- Any failure → `contract_ok=false` in wallet + one PushNotification + posture line shows `⚠ trident
  degraded (CC vX.Y drift)`. Consumers all fail to advisory-only. **Acceptance gate:** fake a renamed
  field in a fixture → degrade fires end-to-end, work continues.

---

## 4. Surface routing (who touches a request, in order)

| Surface | Path through trident |
|---|---|
| Terminal / VS Code turn | mint(wallet) → shaper-prompt(envelope+guard+inject-cap) → model works → shaper-tools on each spawn (tier rewrite/deny + counter) → ledger-filter on outputs → ledger-settle on Stop |
| Subagent | parent's shaper-tools shaped its model at spawn → child session detected via transcript_path → child's own spawns ceilinged at `tier_ceiling_child` → SubagentStop settles + decrements |
| Headless `claude -p` | env-stamp (--model + envelope env) → same hooks as terminal (unless --bare) → settle on Stop |
| `--bare` / raw SDK / MCP | proxy floor only: model ceiling + metering. Honest: that is all anyone can do. |

## 5. settings.json wiring (end state)

```jsonc
"statusLine": { "command": "mint.py --ingest-statusline" },      // absorbs capture-rate-limits.sh
"hooks": {
  "UserPromptSubmit": [ shed inject.sh (selection only), trident shaper-prompt.sh ],   // compact-guard.sh REMOVED
  "PreToolUse":       [ shed permit_observe.sh, approver_pretooluse.py, trident shaper-tools.py ],  // claudeignore-guard REMOVED (rules ported)
  "PostToolUse":      [ shed observe.sh, trident ledger-filter.sh ],                   // filter-tool-output REMOVED (absorbed)
  "Stop":             [ shed handoff-writer.sh, shed reflect.sh, approver_stop.py, trident ledger-settle.py ],  // token-warn + log-session-stats REMOVED (absorbed)
  "SubagentStop":     [ trident ledger-settle.py ],                                    // NEW — without this, fan-out accounting dies
  "SessionStart":     [ shed brief.sh, constitution-lint.sh ]                          // unchanged
}
```
Order matters: approver before shaper-tools (approver may approve, must never see post-rewrite input it
can't reason about → actually REVERSED: shaper-tools LAST so approver approves intent, trident shapes it).

## 6. Formulas (single source: `mint/formulas.py`; this table is the spec)

```
M(L)        = (L/100)^1.5                          # L=100 → bypass all lever damping
H           = min(5h, 7d, p_pool) + bonus(reset)   # bonus: +40 if <30m, +20 if <60m
H_eff       = min(H,100) / sqrt(active_sessions)
tier:  opus if complexity>max(0.6·M, .95-gate) … per ARCHITECTURE table; ceilings clamp to enum
W           = max(1, floor(12 · M · (H_eff/100)^1.2))
think       = floor(32000 · M · (H_eff/100)^2)      # ADVISORY
inject      = floor(4000 · M^0.6)                   # MECHANICAL; rank-0 handoff chunk exempt
V           = max(1, round(5 · M · H_eff/100))      # floor 1 ALWAYS
speculative = (M · H_eff) > 15
compact     = 0.5 + M/2                             # threshold multiplier
roi_min     = min(1.2/M, 6)                         # capped — learning never locks out
```

## 7. Build waves + gates (each ends with its component acceptance gates + `tests/` smoke green)

- **Wave 1 — MINT:** mint.py + wallet + counters + throttle-v3 coordination + posture shim. *Soak: 1 week parallel with capture-rate-limits.sh, then delete it + token_watch.py.*
- **Wave 2 — SHAPER probes:** shaper-prompt + shaper-tools + env-stamp + CLAUDE.md PD-3 rewrite (4-band table → envelope line) + token-gate skill update. *Soak: 3 days, then delete compact-guard.sh, claudeignore-guard.sh, governor.py fanout logic, inject sizing env hack.*
- **Wave 3 — LEDGER:** ledger-settle (Stop+SubagentStop) + ledger-filter + antibodies (observe-only) + evolve-v2 ROI wiring + claude-meter/glyph read wallet.json. *Then delete token-warn.sh, filter-tool-output.sh, log-session-stats.sh.*
- **Wave 4 — Floor:** 4a proxy for headless launchd only + canary. 4b (separate Tarun decision): proxy for interactive shells. *Then final deletion pass + update memory + ship per casterly-ship if publishing.*

## 8. Verify-at-build-time list (do NOT trust this doc for these — check the installed CC)

1. Exact PreToolUse output JSON for `updatedInput` (field names/casing) on the installed version.
2. `AgentInput.model` accepted values (enum strings) right now.
3. SubagentStop payload fields (need session id + transcript path of the CHILD).
4. Statusline stdin schema (`rate_limits`, `context_window`, `transcript_path` fields).
5. Whether VS Code sessions carry any distinguishing field (treat identically if not).
6. June-15 `-p` pool: how it appears in telemetry once live (placeholder in schema until observed).
7. Hook timeout defaults per event on installed version (budget shaper-prompt accordingly).
```
