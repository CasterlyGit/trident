#!/usr/bin/env python3
"""trident LEDGER — Stop AND SubagentStop hook (DESIGN.md §3.6). Same script, two wirings.

SubagentStop: decrement the flock'd spawn counter; append a spawn record to
session-stats.jsonl ({"kind":"spawn", ...} — additive; existing usage-ledger lines
unchanged, evolve-v2 compat preserved).

Stop: settlement — planned (wallet envelope values at stop) vs actual (measured burn
via usage-ledger.py --stop, COMPOSED not reimplemented); feed shed's
update-guard-thresholds.py (composed); antibodies.observe() on the settled facts.

trident NEVER blocks Stop (it's a meter, not a cage) — decision:block is approver's
tool. Silent + exit 0 always: this fires in every session including live shed runs.
"""

import fcntl
import json
import os
import subprocess
import sys
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
COUNTERS = os.path.join(STATE, "trident-counters.json")
LOCK = os.path.join(STATE, "trident-counters.lock")
STATS = os.path.join(STATE, "session-stats.jsonl")
WALLET = os.path.join(STATE, "wallet.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def decrement(sid):
    try:
        with open(LOCK, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            c = _read_json(COUNTERS) or {"spawns_inflight": 0, "by_session": {}}
            c["spawns_inflight"] = max(0, c.get("spawns_inflight", 0) - 1)
            bs = c.setdefault("by_session", {})
            if sid in bs:
                bs[sid] = max(0, bs[sid] - 1)
                if bs[sid] == 0:
                    del bs[sid]
            c["updated_at"] = _now_iso()
            tmp = f"{COUNTERS}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(c, f)
            os.replace(tmp, COUNTERS)
    except Exception:
        pass


def spawn_usage(transcript):
    """Best-effort model+tokens of a finished subagent from its transcript tail."""
    model, out_tokens = None, 0
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, os.path.getsize(transcript) - 65536))
            tail = f.read().decode("utf-8", "replace").splitlines()
        for line in tail:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            if u.get("output_tokens"):
                out_tokens += u["output_tokens"]
            model = msg.get("model") or model
    except Exception:
        pass
    return model, out_tokens


def append_stats(rec):
    try:
        with open(STATS, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def on_subagent_stop(payload):
    sid = payload.get("session_id", "")
    decrement(sid)
    tp = payload.get("agent_transcript_path") or payload.get("transcript_path") or ""
    model, out_tokens = spawn_usage(tp) if tp and os.path.isfile(tp) else (None, 0)
    append_stats({"ts": _now_iso(), "kind": "spawn", "session_id": sid,
                  "model": model, "output": out_tokens,
                  "agent_transcript": os.path.basename(tp) if tp else None})


def on_stop(payload):
    sid = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")

    # 1. Measured burn (COMPOSE usage-ledger; it appends the settlement line itself).
    if transcript and os.path.isfile(transcript):
        try:
            subprocess.run([sys.executable, os.path.expanduser("~/.claude/scripts/usage-ledger.py"),
                            "--stop", transcript, sid],
                           capture_output=True, timeout=10)
        except Exception:
            pass

    # 2. Planned-vs-actual: envelope values at stop vs this session's spawn records.
    w = _read_json(WALLET)
    if w:
        blk = (w.get("derived", {}).get("per_pin", {}).get(sid)
               or w.get("derived", {}).get("default", {}))
        spawns = 0
        try:
            with open(STATS) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("kind") == "spawn" and r.get("session_id") == sid:
                        spawns += 1
        except Exception:
            pass
        settlement = {"ts": _now_iso(), "kind": "settlement", "session_id": sid,
                      "planned": {"L_eff": blk.get("L_eff"), "fanout_max": blk.get("fanout_max"),
                                  "tier_ceiling": blk.get("tier_ceiling_spawn")},
                      "actual": {"spawns": spawns}}
        append_stats(settlement)

        # 3. Antibodies: observe waste patterns (observe-only until config date).
        try:
            import antibodies
            antibodies.observe(sid, settlement, STATS)
        except Exception:
            pass

    # 4. Guard thresholds drift (COMPOSE shed's updater — it owns the learning).
    upd = os.path.expanduser("~/.shed/hooks/update-guard-thresholds.py")
    if os.path.isfile(upd) and transcript:
        try:
            subprocess.run([sys.executable, upd, transcript, sid],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    event = payload.get("hook_event_name", "")
    try:
        if event == "SubagentStop":
            on_subagent_stop(payload)
        else:
            on_stop(payload)
    except Exception:
        pass
    sys.exit(0)  # NEVER block Stop


if __name__ == "__main__":
    main()
