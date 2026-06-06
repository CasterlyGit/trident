#!/usr/bin/env python3
"""trident MINT — pace computation (the sequential-burn valve's brain).

ARCHITECTURE.md marks sequential burn "ungovernable per-call" by hooks — model is
immutable mid-session and every turn re-reads the whole context. The one thing a
hook CAN do without touching correctness is *space turns out*: a bounded sleep in
PreToolUse lowers tokens-per-minute while every session keeps doing exactly the
same work. This module computes WHO sleeps and HOW LONG; the valve hook
(shaper/pace-valve.sh) is a sub-ms wallet read + sleep, per the invariant that
all computation lives in MINT.

Billed weighting matches claude-meter's quota math:
    billed = input + output + cache_creation + 0.1 * cache_read

Control law (proportional shed, converges via the 30s tick feedback loop):
  - global_bpm <= target  → nobody sleeps.
  - fair = target / active_sessions; only sessions ABOVE fair are paced — light
    and interactive sessions never feel the valve.
  - each over-fair session sheds its share of (global - target), proportional to
    its overshare; sleep_s = turn_interval * (k - 1) for slowdown factor k,
    capped at MAX_SLEEP_S (well under the 5-min prompt-cache TTL).

Fail open everywhere: no target file → no pace block → valve no-ops.
"""

import glob
import json
import os

PROJECTS = os.path.expanduser("~/.claude/projects")
TARGET_FILE = os.path.join(
    os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state"),
    "trident-pace-target.json",
)
WINDOW_S = 120          # burn-rate sliding window
TAIL_BYTES = 512 * 1024  # read only the tail of each transcript
MAX_SLEEP_S = 20.0       # hard cap — never approach the 5-min cache TTL
MIN_SLEEP_S = 1.0        # below this, not worth the latency


def read_target():
    """Target billed tokens/min, or None (valve disabled)."""
    try:
        with open(TARGET_FILE) as f:
            t = json.load(f).get("billed_tpm")
        return float(t) if t and float(t) > 0 else None
    except Exception:
        return None


def _billed(usage):
    return (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + int(usage.get("cache_read_input_tokens", 0) * 0.1)
    )


def _tail_lines(path):
    """Last TAIL_BYTES of a transcript, split to lines (first partial line dropped)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # drop partial
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def scan_burn(now, projects=PROJECTS, window_s=WINDOW_S):
    """{sid: {"bpm": billed/min, "turns": n}} over the trailing window.

    Timestamps come from each entry's own ISO field (same source claude-meter
    trusts); files untouched for window_s+60 are skipped without reading.
    """
    from datetime import datetime

    rates = {}
    cutoff_mtime = now - (window_s + 60)
    for path in glob.glob(os.path.join(projects, "*", "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_mtime:
                continue
        except OSError:
            continue
        sid = os.path.basename(path)[:-6]
        billed = 0
        turns = 0
        for line in _tail_lines(path):
            if '"usage"' not in line:
                continue
            try:
                j = json.loads(line)
                ts = j.get("timestamp")
                u = (j.get("message") or {}).get("usage")
                if not ts or not u:
                    continue
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if now - t > window_s:
                continue
            billed += _billed(u)
            turns += 1
        if turns:
            prev = rates.get(sid, {"bpm": 0.0, "turns": 0})
            rates[sid] = {
                "bpm": prev["bpm"] + billed * 60.0 / window_s,
                "turns": prev["turns"] + turns,
            }
    return rates


def assign_sleeps(rates, target_bpm, window_s=WINDOW_S):
    """(global_bpm, {sid: sleep_s}) — proportional shed, over-fair sessions only."""
    global_bpm = sum(r["bpm"] for r in rates.values())
    if not rates or target_bpm <= 0 or global_bpm <= target_bpm:
        return global_bpm, {}
    fair = target_bpm / len(rates)
    over = {sid: r["bpm"] - fair for sid, r in rates.items() if r["bpm"] > fair}
    total_over = sum(over.values())
    if total_over <= 0:
        return global_bpm, {}
    shed_total = global_bpm - target_bpm
    sleeps = {}
    for sid, overshare in over.items():
        r = rates[sid]
        shed = shed_total * (overshare / total_over)
        new_bpm = r["bpm"] - shed
        interval = window_s / r["turns"]  # current seconds between billed turns
        if new_bpm <= 0:
            s = MAX_SLEEP_S
        else:
            s = interval * (r["bpm"] / new_bpm - 1.0)
        s = min(MAX_SLEEP_S, s)
        if s >= MIN_SLEEP_S:
            sleeps[sid] = round(s, 1)
    return global_bpm, sleeps


def pace_block(now):
    """Full wallet["pace"] block, or None when the valve is disabled."""
    target = read_target()
    if target is None:
        return None
    rates = scan_burn(now)
    global_bpm, sleeps = assign_sleeps(rates, target)
    return {
        "target_bpm": int(target),
        "global_bpm": int(global_bpm),
        "window_s": WINDOW_S,
        "sessions": len(rates),
        "sleeps": sleeps,
    }
