#!/usr/bin/env python3
"""trident MINT — Segment 1 daemon (DESIGN.md §3.1).

Single writer of ~/.claude/state/wallet.json (atomic tmp+rename). All curve math lives
here (via formulas.py); every consumer reads pre-baked values and does zero math.

Modes:
  mint.py --tick                one pricing tick (read telemetry → write wallet)
  mint.py --daemon              tick loop every 30s (launchd KeepAlive)
  mint.py --ingest -            statusline push: stdin JSON → guarded telemetry write
                                (absorbs capture-rate-limits.sh: NULL guard + monotonic
                                stale-value guard kept verbatim) → status string → tick

Fail direction: OPEN. mint down → wallet stale → consumers degrade to advisory-only.
Never blocks work. Lever intent is owned by the throttle CLI (burn-throttle.json);
mint only READS it — no code path here may write `pins` (anti-theater bright line).
"""

import glob
import json
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import formulas

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
WALLET = os.path.join(STATE, "wallet.json")
COUNTERS = os.path.join(STATE, "trident-counters.json")
THROTTLE = os.path.join(STATE, "burn-throttle.json")
PAUSED = os.path.join(STATE, "trident-paused")
RL_CAP = 50
TICK_S = 30
ACTIVE_WINDOW_S = 120
DEAD_PIN_S = 24 * 3600
SCHEMA_VERSION = 1


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


# ---------------------------------------------------------------- telemetry

def _session_files():
    return sorted(
        glob.glob(os.path.join(STATE, "rate-limits-*.json")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )


def _reap_stale_files(files):
    """Stale-file reap (>24h) + hard count cap — capture-rate-limits.sh behavior kept."""
    now = _now()
    keep = []
    for i, f in enumerate(files):
        if now - os.path.getmtime(f) > DEAD_PIN_S or i >= RL_CAP:
            try:
                os.remove(f)
            except OSError:
                pass
        else:
            keep.append(f)
    return keep


def read_telemetry():
    """Best account-level telemetry. The shared rate-limits.json carries the
    monotonic-guarded truth; newest per-session file is the fallback."""
    files = _reap_stale_files(_session_files())
    now = _now()
    active = sum(1 for f in files if now - os.path.getmtime(f) <= ACTIVE_WINDOW_S) or 1

    src, src_mtime = None, 0
    shared = os.path.join(STATE, "rate-limits.json")
    for cand in [shared] + files[:1]:
        d = _read_json(cand)
        if d and d.get("rate_limits", {}).get("five_hour", {}).get("used_percentage") is not None:
            mt = os.path.getmtime(cand)
            if mt > src_mtime:
                src, src_mtime = d, mt

    if not src:
        return {"five_hour": None, "seven_day": None, "active_sessions": active,
                "stale_s": None, "contract_ok": False}

    rl = src["rate_limits"]
    fh = rl.get("five_hour") or {}
    sd = rl.get("seven_day") or {}
    return {
        "five_hour": {"left_pct": round(100 - float(fh.get("used_percentage", 0)), 1),
                      "resets_at": int(fh.get("resets_at", 0))},
        "seven_day": ({"left_pct": round(100 - float(sd.get("used_percentage", 0)), 1),
                       "resets_at": int(sd.get("resets_at", 0))} if sd else None),
        "p_pool": {"left_pct": None, "active_from": "2026-06-15"},
        "active_sessions": active,
        "stale_s": int(now - src_mtime),
        "contract_ok": True,
    }


# ------------------------------------------------------------------- lever

def read_lever():
    """burn-throttle.json — both shapes (legacy flat {throttle:N} and v2 {global,pins})."""
    st = _read_json(THROTTLE)
    clamp = lambda v: max(0, min(100, int(v)))
    if not isinstance(st, dict):
        return {"global": 100, "pins": {}}
    if "throttle" in st and "global" not in st:
        try:
            return {"global": clamp(st["throttle"]), "pins": {}}
        except (TypeError, ValueError):
            return {"global": 100, "pins": {}}
    g = clamp(st.get("global", 100)) if str(st.get("global", 100)).lstrip("-").isdigit() else 100
    pins = st.get("pins", {}) if isinstance(st.get("pins"), dict) else {}
    out = {}
    for sid, v in pins.items():
        try:
            out[sid] = clamp(v)
        except (TypeError, ValueError):
            continue
    return {"global": g, "pins": out}


def live_pins(pins):
    """Reap dead pins from the WALLET VIEW: pin's session file gone or stale >24h.
    burn-throttle.json itself stays untouched — the throttle CLI owns that file."""
    now = _now()
    out, active_pinned = {}, 0
    for sid, val in pins.items():
        f = os.path.join(STATE, f"rate-limits-{sid}.json")
        if not os.path.exists(f) or now - os.path.getmtime(f) > DEAD_PIN_S:
            continue  # dead pin — reaped from view
        out[sid] = val
        if now - os.path.getmtime(f) <= ACTIVE_WINDOW_S:
            active_pinned += 1
    return out, active_pinned


# ----------------------------------------------------------------- posture

def _fmt_reset(secs):
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def render_posture(block, tele, lever, pinned_here):
    """One-line GREEN/AMBER/RED routing posture — same contract as budget-posture.sh."""
    fh = tele.get("five_hour") or {}
    left = fh.get("left_pct")
    if left is None:
        return ["5h budget: no live data — assume AMBER, route conservatively"]
    secs = max(0, fh.get("resets_at", 0) - _now())
    share = block["H"]  # already lever-capped via L_eff? No — H is headroom; fold lever:
    share = min(block["H"], block["L_eff"]) if block["L_eff"] < 100 else block["H"]
    if share >= 60:
        tag = ("🟢 GREEN — full power: ultracode / dynamic-workflow / wide fan-out OK. "
               f"W≤{block['fanout_max']}, V≥{block['verify_min']}, tier≤{block['tier_ceiling_spawn']}.")
    elif share >= 25:
        tag = ("🟡 AMBER — spread: schema+pipeline over barriers, cheapest-sufficient model per leg, "
               f"gate big fan-outs. W≤{block['fanout_max']}, V≥{block['verify_min']}, "
               f"tier≤{block['tier_ceiling_spawn']}.")
    else:
        tag = ("🔴 RED — conserve: inline-first, no speculative work, finish compactly, "
               f"confirm before ANY workflow. W≤{block['fanout_max']}, V≥{block['verify_min']}, "
               f"tier≤{block['tier_ceiling_spawn']}.")
    active = tele["active_sessions"]
    conc = "" if active <= 1 else f" · {active} windows open"
    L = block["L_eff"]
    pin_note = " 📌pinned" if pinned_here else ""
    lev = "" if L >= 100 else f" · 🛒 L={L}{pin_note}"
    theater = " · ⚠ pin-theater" if lever.get("theater_warning") else ""
    stale = ""
    if tele.get("stale_s") is not None and tele["stale_s"] > ACTIVE_WINDOW_S:
        stale = f" · ⚠ telemetry {tele['stale_s']}s stale (advisory-only)"
    lines = [f"5h: {left:.0f}% left · resets {_fmt_reset(secs)}{conc}{lev}{theater}{stale} · {tag}",
             block["envelope"]]
    return lines


# ---------------------------------------------------------------- counters

def reconcile_counters():
    c = _read_json(COUNTERS)
    if not isinstance(c, dict):
        c = {"spawns_inflight": 0, "by_session": {}, "updated_at": _iso()}
        _atomic_write(COUNTERS, c)
        return c
    # Drift reconcile: counters stuck >1h with no update → no live fan-out plausibly remains.
    try:
        upd = time.mktime(time.strptime(c.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, OverflowError):
        upd = 0
    if c.get("spawns_inflight", 0) > 0 and _now() - upd > 3600:
        c = {"spawns_inflight": 0, "by_session": {}, "updated_at": _iso()}
        _atomic_write(COUNTERS, c)
    return c


# ------------------------------------------------------------------ canary

def canary_lite(tele):
    """Wave-1 contract check: statusline schema still yields five_hour.used_percentage.
    Full canary (§3.9, hook fixtures + AgentInput enum) lands in Wave 4."""
    return bool(tele.get("contract_ok"))


# -------------------------------------------------------------------- tick

def tick():
    if os.path.exists(PAUSED):
        return None  # paused: wallet goes stale, consumers visibly degrade — fail open
    tele = read_telemetry()
    lever = read_lever()
    pins, active_pinned = live_pins(lever["pins"])
    active = tele["active_sessions"]
    theater = active_pinned >= max(1, math.ceil(0.5 * active)) and active_pinned > 0
    lever_out = {"global": lever["global"], "pins": pins,
                 "pinned_count": len(pins), "theater_warning": theater}

    fh = tele.get("five_hour") or {}
    sd = tele.get("seven_day") or {}
    secs = max(0, fh.get("resets_at", 0) - _now()) if fh else None
    h_raw = formulas.headroom_raw(
        fh.get("left_pct"), (sd or {}).get("left_pct"), None, secs)

    default = formulas.derived_block(lever["global"], h_raw, active)
    per_pin = {sid: formulas.derived_block(v, h_raw, active) for sid, v in pins.items()}

    contract_ok = canary_lite(tele)
    default["posture"] = render_posture(default, tele, lever_out, False)
    for sid, blk in per_pin.items():
        blk["posture"] = render_posture(blk, tele, lever_out, True)

    # Headless (TC2) runs at L_eff=global, depth 0 → spawn ceiling governs its model.
    headless_model = {"haiku": "claude-haiku-4-5-20251001",
                      "sonnet": "claude-sonnet-4-6",
                      "opus": "claude-opus-4-8"}[default["tier_ceiling_spawn"]]

    wallet = {
        "schema_version": SCHEMA_VERSION,
        "contract_ok": contract_ok,
        "updated_at": _iso(),
        "telemetry": {
            "five_hour": tele.get("five_hour"),
            "seven_day": tele.get("seven_day"),
            "p_pool": tele.get("p_pool"),
            "active_sessions": active,
            "stale_s": tele.get("stale_s"),
        },
        "lever": lever_out,
        "derived": {"default": default, "per_pin": per_pin},
        "headless": {"model": headless_model,
                     "envelope": default["envelope"].replace("[TRIDENT ", "[TRIDENT TC2 ")},
    }
    _atomic_write(WALLET, wallet)
    reconcile_counters()
    return wallet


# ------------------------------------------------------------------ ingest

def ingest(stream):
    """Statusline push — absorbs capture-rate-limits.sh verbatim:
    NULL guard, monotonic stale-value guard, atomic shared + per-session writes,
    then the status string. Ends with a tick so the wallet is never >1 push stale."""
    raw = stream.read()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        print("")
        return
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "last-statusline.json"), "w") as f:
        f.write(raw)

    rl = d.get("rate_limits")
    if rl:  # NULL guard: fresh session pre-API-call feeds rate_limits=null — skip writes
        ts = _iso()
        record = {"captured_at": ts, "session_id": d.get("session_id"),
                  "rate_limits": rl, "model": d.get("model"), "cost": d.get("cost"),
                  "context_window": d.get("context_window")}
        shared = os.path.join(STATE, "rate-limits.json")
        accept = True
        old = _read_json(shared)
        if old:  # monotonic stale-value guard: same window, % only goes up
            ofh = old.get("rate_limits", {}).get("five_hour", {})
            nfh = rl.get("five_hour", {})
            same_window = (ofh.get("resets_at") == nfh.get("resets_at")
                           and ofh.get("resets_at", 0) > _now())
            if same_window and float(nfh.get("used_percentage", 0)) + 0.5 < float(
                    ofh.get("used_percentage", 0)):
                accept = False  # stale per-session echo — ignore
        if accept:
            _atomic_write(shared, record)
        if d.get("session_id"):  # per-session write — always (own-context ground truth)
            _atomic_write(os.path.join(STATE, f"rate-limits-{d['session_id']}.json"), record)

    wallet = tick()

    # Status string for the statusline area.
    out = []
    fh = (rl or {}).get("five_hour", {})
    sd = (rl or {}).get("seven_day", {})
    if fh.get("used_percentage") is not None:
        out.append(f"5h:{round(fh['used_percentage'])}%")
    if sd.get("used_percentage") is not None:
        out.append(f"wk:{round(sd['used_percentage'])}%")
    if wallet:
        sid = d.get("session_id", "")
        blk = wallet["derived"]["per_pin"].get(sid) or wallet["derived"]["default"]
        if blk["L_eff"] < 100:
            pin = "📌" if sid in wallet["derived"]["per_pin"] else ""
            out.append(f"🛒{pin}{blk['L_eff']}")
        if wallet["lever"]["theater_warning"]:
            out.append("⚠📌")
    tp, sid = d.get("transcript_path"), d.get("session_id", "")
    if tp and os.path.isfile(tp):  # live token ledger — compose, fail open
        try:
            led = subprocess.run(
                [sys.executable, os.path.expanduser("~/.claude/scripts/usage-ledger.py"),
                 "--status", tp, sid],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if led:
                out.append(led)
        except Exception:
            pass
    print("  ".join(out))


def main():
    args = sys.argv[1:]
    if "--ingest" in args or "--ingest-statusline" in args:
        ingest(sys.stdin)
    elif "--daemon" in args:
        while True:
            try:
                tick()
            except Exception as e:  # fail open, visibly: stderr → launchd log
                print(f"mint tick failed: {e}", file=sys.stderr)
            time.sleep(TICK_S)
    else:  # --tick (default)
        w = tick()
        print("paused (wallet left stale)" if w is None else f"wallet written {w['updated_at']}")


if __name__ == "__main__":
    main()
