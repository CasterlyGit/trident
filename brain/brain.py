#!/usr/bin/env python3
"""trident BRAIN — Fable-powered slow-loop policy governor.

The fast path (MINT curves → wallet.json → sub-ms hook reads) stays 100% mechanical.
The brain sits ABOVE it: at most 2 fires per 5h window, it reads a compact burn
digest (wallet + history ring + counters + forecast), asks claude-fable-5 for a
routing policy, and writes a clamped, expiring overlay to trident-policy.json.
MINT folds the overlay into the DEFAULT derived block only — pins stay human-
sovereign, and verify_min/roi_min/L/H are not overlay-addressable at all
(formulas.validate_policy is the single gate; clamps live there).

Modes:
  brain.py --fire     build digest → call Fable → validate → write policy + audit
  brain.py --force    same, bypassing cooldown/window-cap gates (operator use)
  brain.py --status   print brain state + active policy

Invocation: mint.py tick calls maybe_spawn(wallet) → gating is sub-ms file reads;
on a yes it Popens `brain.py --fire` fully detached so pricing never blocks.

Fail direction: OPEN, like everything in trident. Any failure → no policy file
→ MINT runs pure mechanical curves. Kill switch: touch ~/.claude/state/trident-brain-off.
"""

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mint"))
import formulas

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
WALLET = os.path.join(STATE, "wallet.json")
POLICY = os.path.join(STATE, "trident-policy.json")
HISTORY = os.path.join(STATE, "trident-history.jsonl")
COUNTERS = os.path.join(STATE, "trident-counters.json")
BRAIN_STATE = os.path.join(STATE, "trident-brain.json")
AUDIT = os.path.join(STATE, "trident-brain-audit.jsonl")
LOCK = os.path.join(STATE, "trident-brain-inflight")
OFF_FLAG = os.path.join(STATE, "trident-brain-off")
LOG = os.path.join(STATE, "trident-brain.log")

MAX_FIRES_PER_WINDOW = 2
COOLDOWN_S = 2700          # ≥45min between routine fires
BAND_CHANGE_COOLDOWN_S = 1200  # posture-band flip may re-fire after 20min
MIN_LEFT_PCT = 8           # never burn the dregs deciding how to save the dregs
LOCK_FRESH_S = 600
POLICY_TTL_S = 7200
CLAUDE_TIMEOUT_S = 240
MODEL = "claude-fable-5"

PROMPT_TEMPLATE = """You are the trident BRAIN — the slow-loop policy governor of a \
token-budget router on a developer's Mac. The fast path is mechanical; you periodically \
re-fit its routing knobs to the observed burn. You are routing-only: you may change HOW \
work is shaped (model tier, fan-out width, thinking budget, context injection, speculative \
work, compact threshold), NEVER whether it gets done or how rigorously it is verified.

Knobs (all optional; hard-clamped in code — values outside rails are clipped):
  tier_bias      int  -1..1   shift spawn-model ceiling down/up one tier
  width_mult     0.5..1.5     multiplier on max fan-out width
  think_mult     0.5..1.5     multiplier on advisory thinking budget
  inject_mult    0.6..1.25    multiplier on context-injection cap
  compact_bias   0.75..1.25   <1 = compact/handoff fires earlier
  spec_override  true|false   force speculative work on/off

Decision principles:
- Narrow+smart can beat wide+cheap: if burn is high from many parallel agents, consider
  tier_bias +1 with width_mult down, or the reverse when headroom is fat and work is parallel.
- If forecast shows the wall arriving BEFORE the window reset, bias toward conservation
  (width/think down, compact earlier). If the window is fat and idle, release the brakes.
- Sessions count ≠ burn: many idle windows should not starve the one active session.
- A policy expires on its own; prefer modest, explainable adjustments over big swings.

Respond with ONLY a JSON object, no markdown fences, shaped exactly:
{"policy": {<knobs or empty>}, "no_change": <bool>, "rationale": "<one sentence, <=280 chars>"}
Set no_change=true with an empty policy if the mechanical curves are already right.

Burn digest:
"""


def _now():
    return int(time.time())


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_write(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _audit(rec):
    try:
        with open(AUDIT, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _band(block):
    share = min(block["H"], block["L_eff"]) if block["L_eff"] < 100 else block["H"]
    return "GREEN" if share >= 60 else ("AMBER" if share >= 25 else "RED")


def _window_reset_at(wallet):
    return ((wallet.get("telemetry") or {}).get("five_hour") or {}).get("resets_at", 0)


def should_fire(wallet, state, now, force=False):
    """(bool, reason). All gates are cheap dict/file checks."""
    if os.path.exists(OFF_FLAG):
        return False, "off-flag"
    if not wallet or not wallet.get("contract_ok"):
        return False, "wallet-degraded"
    fh = (wallet.get("telemetry") or {}).get("five_hour") or {}
    if fh.get("left_pct") is None:
        return False, "no-telemetry"
    if fh["left_pct"] < MIN_LEFT_PCT:
        return False, "dregs"
    try:
        if now - os.path.getmtime(LOCK) < LOCK_FRESH_S:
            return False, "inflight"
    except OSError:
        pass
    if force:
        return True, "forced"
    reset_at = _window_reset_at(wallet)
    fires = [f for f in state.get("fires", []) if f.get("reset_at") == reset_at]
    if len(fires) >= MAX_FIRES_PER_WINDOW:
        return False, "window-cap"
    last_fire = state.get("last_fire_ts", 0)
    band = _band(wallet["derived"]["default"])
    band_changed = state.get("last_band") and band != state["last_band"]
    policy_doc = _read_json(POLICY)
    policy_live = bool(policy_doc and formulas.validate_policy(policy_doc, now))
    if not policy_live and now - last_fire >= COOLDOWN_S:
        return True, "no-live-policy"
    if band_changed and now - last_fire >= BAND_CHANGE_COOLDOWN_S:
        return True, f"band-change:{state['last_band']}→{band}"
    return False, "cooldown"


def maybe_spawn(wallet):
    """Called from the MINT tick. Gating inline (cheap); the fire is detached."""
    state = _read_json(BRAIN_STATE) or {}
    ok, _reason = should_fire(wallet, state, _now())
    if not ok:
        # Track band transitions even when not firing, so flips are detectable.
        band = _band(wallet["derived"]["default"]) if wallet.get("derived") else None
        if band and band != state.get("last_band"):
            state["last_band"] = band
            _atomic_write(BRAIN_STATE, state)
        return False
    with open(LOG, "a") as log:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--fire"],
            stdout=log, stderr=log, cwd="/",  # cwd=/ → WALL_EXCLUDE_CWDS skips it
            start_new_session=True,
            env={**os.environ, "TRIDENT_BRAIN": "1"},
        )
    return True


# ----------------------------------------------------------------- the fire

def _history_tail(now, span_s=3600, cap=80):
    pts = []
    try:
        with open(HISTORY) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if r.get("ts", 0) >= now - span_s:
                    pts.append(r)
    except OSError:
        return []
    step = max(1, len(pts) // cap)
    return pts[::step][-cap:]


def build_digest(wallet, now):
    default = dict(wallet.get("derived", {}).get("default", {}))
    default.pop("posture", None)
    return {
        "now": _iso(now),
        "telemetry": wallet.get("telemetry"),
        "lever": wallet.get("lever"),
        "derived_default": default,
        "forecast": wallet.get("forecast"),
        "pace": wallet.get("pace"),
        "counters": _read_json(COUNTERS),
        "history_45min": _history_tail(now),
        "previous_policy": _read_json(POLICY),
    }


def _find_claude():
    cand = shutil.which("claude")
    if cand:
        return cand
    for p in ("~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def fire(force=False):
    now = _now()
    wallet = _read_json(WALLET)
    state = _read_json(BRAIN_STATE) or {}
    ok, reason = should_fire(wallet, state, now, force=force)
    if not ok:
        print(f"brain: not firing ({reason})")
        return 0
    claude = _find_claude()
    if not claude:
        _audit({"ts": _iso(now), "ok": False, "error": "claude binary not found"})
        print("brain: claude binary not found")
        return 1
    try:
        with open(LOCK, "w") as f:
            f.write(str(now))

        digest = build_digest(wallet, now)
        prompt = PROMPT_TEMPLATE + json.dumps(digest, indent=1)
        env = {**os.environ, "TRIDENT_BRAIN": "1"}
        # Never let trident's own proxy ceiling rewrite the brain's Fable call.
        env.pop("ANTHROPIC_BASE_URL", None)
        t0 = time.time()
        r = subprocess.run(
            [claude, "-p", prompt, "--model", MODEL, "--output-format", "json"],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S, cwd="/", env=env)
        dur = round(time.time() - t0, 1)
        if r.returncode != 0:
            raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[:300]}")
        wrapper = json.loads(r.stdout)
        verdict = _extract_json(wrapper.get("result", ""))

        reset_at = _window_reset_at(wallet)
        audit_rec = {"ts": _iso(now), "ok": True, "trigger": reason, "duration_s": dur,
                     "model": MODEL, "cost_usd": wrapper.get("total_cost_usd"),
                     "digest_left_pct": digest["telemetry"]["five_hour"]["left_pct"],
                     "raw_policy": verdict.get("policy"),
                     "no_change": bool(verdict.get("no_change")),
                     "rationale": str(verdict.get("rationale", ""))[:280]}

        if verdict.get("no_change") or not verdict.get("policy"):
            # Explicit no-op: mechanical curves are right — drop any live overlay.
            try:
                os.remove(POLICY)
            except OSError:
                pass
            audit_rec["applied"] = None
        else:
            doc = {"schema_version": formulas.POLICY_SCHEMA_VERSION,
                   "generated_at": _iso(now),
                   "expires_at": min(now + POLICY_TTL_S, reset_at) if reset_at > now
                   else now + POLICY_TTL_S,
                   "policy": verdict["policy"],
                   "rationale": audit_rec["rationale"]}
            clamped = formulas.validate_policy(doc, now)
            if clamped:
                _atomic_write(POLICY, doc)
                audit_rec["applied"] = clamped
            else:
                audit_rec.update(ok=False, error="policy failed validation/clamps")

        _audit(audit_rec)
        state.setdefault("fires", []).append({"ts": now, "reset_at": reset_at})
        state["fires"] = state["fires"][-20:]
        state["last_fire_ts"] = now
        state["last_band"] = _band(wallet["derived"]["default"])
        _atomic_write(BRAIN_STATE, state)
        print(f"brain: fired ok in {dur}s — {audit_rec.get('applied') or 'no_change'}")
        return 0
    except Exception as e:
        _audit({"ts": _iso(now), "ok": False, "trigger": reason, "error": str(e)[:300]})
        print(f"brain: fire failed — {e}", file=sys.stderr)
        return 1
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def status():
    state = _read_json(BRAIN_STATE) or {}
    doc = _read_json(POLICY)
    live = formulas.validate_policy(doc, _now()) if doc else None
    print(json.dumps({
        "off": os.path.exists(OFF_FLAG),
        "last_fire": _iso(state["last_fire_ts"]) if state.get("last_fire_ts") else None,
        "fires_recorded": len(state.get("fires", [])),
        "last_band": state.get("last_band"),
        "policy_live": bool(live),
        "policy_clamped": live,
        "policy_doc": doc,
    }, indent=1))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--fire" in args or "--force" in args:
        sys.exit(fire(force="--force" in args))
    status()
