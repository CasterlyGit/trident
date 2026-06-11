# trident · handoff — the session AI-log

A **handoff doc** lets a brand-new Claude session resume a prior one without
re-reading the whole transcript — it's the single biggest context-restore
token saver in the stack. The goal: a cold session should *feel like it never
left*, reconstructing not just **what changed** but **the flow of how you were
directing it** — an AI log, not a file diff.

## The problem this module fixes

The original generator (`generate-handoff.py`) is a **regex skimmer**. It
captures *words* — one truncated task sentence, one truncated "where we left
off", a file list, the order board — but it never reads the *arc* of the
session. A three-hour design conversation collapsed to ~700 bytes that felt
like "the last two or three messages," because that's literally all the regex
could see. There is no synthesis: a session where you reasoned through *why* a
decision matters, without the literal word "decided", produced **zero** captured
reasoning.

## The fix — two layers

### 1. Deterministic skeleton (`generate-handoff.py`) — instant, free, always on
The facts that must never be wrong, regardless of any model:
- **Boot block** — task, resume pointer, git state, verify command, ruled-out list.
- **Order Ledger** — the CASTER KITCHEN board scraped as the complete order list
  (queued work included; on the board ⇒ in the handoff).
- **Last message (not yet processed)** — the unprocessed tail of your intent.
- **Where We Left Off** — now up to 2 recent substantive turns (was 1, capped at 480c).
- **Recent Exchange** — *new*: the verbatim tail of the last ~6 real turns. The
  cheapest thing that makes a resume feel continuous: the next session reads the
  actual end of the dialogue, not a summary of it.

Widened caps + a slash-command-template guard (so `/loop`/`/goal` scaffolding
never wins the Active Task slot) make the skeleton itself much richer at zero cost.

### 2. Model-written Session Narrative (`handoff-narrate.py`) — the "AI log"
A cheap `claude -p` call reads the **whole transcript** and writes the part regex
can't: *what we're really doing & why · the mental model · decisions + the
reasoning that killed the alternatives · dead-ends and why they failed · current
state and the open thread.* This is what carries **the flow of your direction**.
It's merged into the doc between the Boot block and the Order Ledger (the *why*
sits above the *what*).

## Why this doesn't blow the token budget

Handoffs are rewritten on **every Stop** (~29/day on a busy day). Making each one
a model call would be exactly the waste the budget posture forbids. So the
narrator is **gated hard** in `handoff-writer.sh`:

| Gate | Rule |
|---|---|
| Substantial only | skip unless the session has ≥ `HANDOFF_NARRATE_MIN_TURNS` (default 6) turns |
| Debounced | narrator no-ops (re-merges from a `.narrative.md` sidecar) if the transcript content-hash is unchanged — **no model call** |
| Async | runs backgrounded, never adds latency to the Stop |
| Origin-gated | never for `SHED_ORIGIN=phone` headless runs |
| Cheap tier | `haiku` by default (`HANDOFF_NARRATE_MODEL` to override) |
| Fail-open | if it's skipped or errors, the skeleton handoff stands on its own |

Net effect: a handful of narrations a day (one per *materially-changed* session),
not 29. Each ~48s / a few cents on haiku, saving a full transcript re-read on resume.

## Files

| File | Role |
|---|---|
| `generate-handoff.py` | deterministic skeleton generator (regex) |
| `handoff-narrate.py` | model-written Session Narrative + merge/debounce |
| `handoff-writer.sh` | the Stop hook that wires both, with the gating |

> **Live copies** run from `~/.claude/scripts/` (skeleton + narrator) and
> `~/.shed/hooks/handoff-writer.sh` (hook). The copies here are the versioned
> source of truth — keep them in sync when either changes.

## Knobs

| Env var | Default | Effect |
|---|---|---|
| `HANDOFF_NARRATE` | `1` | set `0` to disable the narrative entirely |
| `HANDOFF_NARRATE_MIN_TURNS` | `6` | min session turns before narrating |
| `HANDOFF_NARRATE_MODEL` | `haiku` | model for the narrative call |
