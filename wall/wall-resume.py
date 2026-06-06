#!/usr/bin/env python3
"""trident WALL — resume tick.

When an armed limit-wall.json exists and the 5h window has reset
(now >= resets_at + buffer), respawn one fresh terminal per checkpointed
session — `wall-spawn.sh <handoff-id> <cwd>` — then archive the manifest
to limit-wall-done-<fired_at>.json. Old windows are never touched
(window-safety): resume always means NEW windows beside them.

Postpone rule: if the wallet is fresh but still shows the window exhausted
(telemetry lag — keepalive refreshes headers every ~3 min), wait. Force
through anyway WALL_FORCE_AFTER_S past resets_at (fail open).

Env (all optional, sandbox-friendly):
  TRIDENT_STATE_DIR     state dir                        (~/.claude/state)
  WALL_RESUME_BUFFER_S  wait after resets_at             (60)
  WALL_FORCE_AFTER_S    resume regardless of telemetry   (900)
  WALL_SPAWN_CMD        spawner                          (wall-spawn.sh)
  WALL_MAX_SPAWNS       cap on respawned terminals       (6)
  WALL_SPAWN_STAGGER_S  pause between spawns             (2)
"""

import json
import os
import subprocess
import sys
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
WALLET = os.path.join(STATE, "wallet.json")
MANIFEST = os.path.join(STATE, "limit-wall.json")
SPAWN_CMD = os.environ.get("WALL_SPAWN_CMD") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wall-spawn.sh")
BUFFER_S = int(os.environ.get("WALL_RESUME_BUFFER_S", "60"))
FORCE_AFTER_S = int(os.environ.get("WALL_FORCE_AFTER_S", "900"))
MAX_SPAWNS = int(os.environ.get("WALL_MAX_SPAWNS", "6"))
STAGGER_S = float(os.environ.get("WALL_SPAWN_STAGGER_S", "2"))
WALLET_STALE_S = 120


def _now():
    return int(time.time())


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


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
    os.rename(tmp, path)


def _window_recovered(threshold_pct, resets_at, now):
    """True unless a FRESH wallet still shows the 5h window exhausted.
    Stale/missing wallet → trust resets_at alone (fail open)."""
    wallet = _read_json(WALLET)
    if not wallet:
        return True
    try:
        if now - os.path.getmtime(WALLET) > WALLET_STALE_S:
            return True
    except OSError:
        return True
    left = ((wallet.get("telemetry") or {}).get("five_hour") or {}).get("left_pct")
    if left is None:
        return True
    if left > threshold_pct:
        return True
    return now >= resets_at + FORCE_AFTER_S  # telemetry lag — force eventually


def main():
    now = _now()
    m = _read_json(MANIFEST)
    if not m or m.get("status") != "armed":
        return 0
    resets_at = m.get("resets_at") or 0
    if now < resets_at + BUFFER_S:
        return 0
    if not _window_recovered(m.get("threshold_pct", 2.0), resets_at, now):
        print("[wall] reset passed but telemetry still exhausted — postponing",
              file=sys.stderr)
        return 0

    seen, spawned = set(), []
    for s in m.get("sessions", []):
        if len(spawned) >= MAX_SPAWNS:
            break
        key = s.get("hf_id")
        if not key or key in seen:
            continue
        seen.add(key)
        cwd = s.get("cwd") or os.path.expanduser("~")
        try:
            r = subprocess.run([SPAWN_CMD, key, cwd],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
        except Exception as e:
            ok, r = False, None
            print(f"[wall] spawn failed for {key}: {e}", file=sys.stderr)
        spawned.append({"hf_id": key, "cwd": cwd, "ok": ok})
        if STAGGER_S > 0:
            time.sleep(STAGGER_S)

    m["status"] = "resumed"
    m["resumed_at"] = now
    m["resumed_iso"] = _iso(now)
    m["spawned"] = spawned
    archive = os.path.join(STATE, f"limit-wall-done-{m.get('fired_at', now)}.json")
    _atomic_write(archive, m)
    try:
        os.remove(MANIFEST)
    except OSError:
        pass
    n_ok = sum(1 for s in spawned if s["ok"])
    print(f"[wall] resumed {n_ok}/{len(spawned)} session(s) → {archive}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
