"""trident LEDGER — antibodies: learned waste patterns (DESIGN.md §3.7).

Keyed by PATTERN, not surface (cross-surface transfer). Minted/strengthened in
settle when planned-vs-actual shows waste. Library only — called by ledger-settle.

States: observe → advise (envelope mentions) → block (shaper-tools enforces).
Graduation: confirmations ≥3 AND grader_score ≥0.7 → advise; ≥5 AND ≥0.8 → block.
Inverted activation: when M < 0.3, blocking requires ≥0.9 — never trigger-happy
when mistakes are costliest. Everything stays `observe` until OBSERVE_UNTIL.
Tolerance list (~/.claude/state/antibody-tolerance.json, human-edited) bypasses blocks.
"""

import json
import os
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
STORE = os.path.join(STATE, "antibodies.json")
TOLERANCE = os.path.join(STATE, "antibody-tolerance.json")
OBSERVE_UNTIL = "2026-07-05"  # first month: observe-only, hard-coded (DESIGN §3.7)

KINDS = ("fanout_waste", "speculative_rerun", "tier_overkill", "reread_loop")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(store):
    tmp = f"{STORE}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=1)
    os.replace(tmp, STORE)


def _grade(ab):
    """Earned-efficiency grade: evidence breadth + cross-surface transfer."""
    breadth = min(1.0, len(ab.get("evidence", [])) / 5.0)
    surfaces = min(1.0, len(ab.get("surfaces_seen", [])) / 2.0)
    return round(0.7 * breadth + 0.3 * surfaces, 2)


def _state_for(ab, m=1.0):
    if time.strftime("%Y-%m-%d") < OBSERVE_UNTIL:
        return "observe"
    c, g = ab.get("confirmations", 0), ab.get("grader_score", 0)
    block_bar = 0.9 if m < 0.3 else 0.8  # inverted activation
    if c >= 5 and g >= block_bar:
        return "block"
    if c >= 3 and g >= 0.7:
        return "advise"
    return "observe"


def _mint(store, key, kind, run_id, surface):
    ab = store.setdefault(key, {"pattern_key": key, "kind": kind, "evidence": [],
                                "confirmations": 0, "grader_score": 0.0,
                                "state": "observe", "surfaces_seen": []})
    if run_id not in ab["evidence"]:
        ab["evidence"] = (ab["evidence"] + [run_id])[-20:]
        ab["confirmations"] += 1
    if surface and surface not in ab["surfaces_seen"]:
        ab["surfaces_seen"].append(surface)
    ab["grader_score"] = _grade(ab)
    ab["state"] = _state_for(ab)
    return ab


def is_tolerated(key):
    tol = _load(TOLERANCE, [])
    return key in tol if isinstance(tol, list) else key in tol.get("patterns", [])


def observe(sid, settlement, stats_path, surface="terminal"):
    """Mint/strengthen antibodies from one session's settled facts. Silent, fail open."""
    store = _load(STORE, {})
    planned = settlement.get("planned", {})
    actual = settlement.get("actual", {})

    # fanout_waste: spawned far over the planned width (spawned 8, budget 3)
    w_max = planned.get("fanout_max") or 12
    if actual.get("spawns", 0) > 2 * w_max:
        _mint(store, f"fanout_waste:over2x", "fanout_waste", sid, surface)

    # tier_overkill: opus spawn returned <500 output tokens (lookup on a god model)
    try:
        with open(stats_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("kind") == "spawn" and r.get("session_id") == sid
                        and "opus" in (r.get("model") or "")
                        and 0 < r.get("output", 0) < 500):
                    _mint(store, "tier_overkill:opus_tiny_return", "tier_overkill", sid, surface)
                    break
    except Exception:
        pass

    _save(store)


def active(min_state="advise"):
    """Antibodies at/above a state, minus tolerated — read by shaper/envelope."""
    rank = {"observe": 0, "advise": 1, "block": 2}
    store = _load(STORE, {})
    return [ab for k, ab in store.items()
            if rank.get(ab.get("state"), 0) >= rank[min_state] and not is_tolerated(k)]
