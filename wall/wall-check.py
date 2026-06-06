#!/usr/bin/env python3
"""trident WALL — 5h-limit wall detector (check tick).

When the 5-hour window is nearly exhausted (left_pct <= WALL_THRESHOLD_PCT),
snapshot every active session into $STATE/limit-wall.json:
  - refresh each session's handoff doc straight from its live transcript
    (the exact generate-handoff.py call handoff-writer.sh makes at Stop, so
    even a session idle mid-task gets a current checkpoint)
  - record sid / handoff id / cwd so wall-resume.py can respawn each
    terminal after resets_at passes.

Fail direction: OPEN — stale wallet / missing telemetry → do nothing.
Purely observational: never blocks, signals, or touches running sessions.

Env (all optional, sandbox-friendly):
  TRIDENT_STATE_DIR       state dir                      (~/.claude/state)
  WALL_THRESHOLD_PCT      fire at 5h left% <=            (2.0)
  WALL_SESSION_WINDOW_S   session telemetry freshness    (1800)
  WALL_PROJECTS_DIR       transcript root                (~/.claude/projects)
  WALL_HANDOFF_HOME       HOME for generate-handoff.py   (real HOME)
  WALL_NOTIFY             1 = osascript banner on fire   (1)
"""

import glob
import json
import os
import subprocess
import sys
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
WALLET = os.path.join(STATE, "wallet.json")
MANIFEST = os.path.join(STATE, "limit-wall.json")
EXCLUDE = os.path.join(STATE, "wall-exclude")
PROJECTS = os.environ.get("WALL_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")
GEN_HANDOFF = os.path.expanduser("~/.claude/scripts/generate-handoff.py")
THRESHOLD_PCT = float(os.environ.get("WALL_THRESHOLD_PCT", "2.0"))
SESSION_WINDOW_S = int(os.environ.get("WALL_SESSION_WINDOW_S", "1800"))
WALLET_STALE_S = 120
# cwd-based exclusion (colon-separated). Default "/" skips the tty-keepalive
# session durably — its sid changes on every respawn, so the sid-prefix file
# alone would go stale; nothing else runs claude at /.
EXCLUDE_CWDS = set(filter(None, os.environ.get("WALL_EXCLUDE_CWDS", "/").split(":")))


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


def _excluded_prefixes():
    try:
        with open(EXCLUDE) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except Exception:
        return []


def _transcript_for(sid):
    """Newest transcript JSONL for this session id, or None."""
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl"))
    if not hits:
        return None
    return max(hits, key=lambda p: os.path.getmtime(p))


def _cwd_from_transcript(transcript):
    """Last `cwd` recorded in the transcript; fallback decode of the
    projects dirname (dashes→slashes, ambiguous but better than nothing)."""
    try:
        with open(transcript, "rb") as f:
            tail = f.read()[-65536:].decode("utf-8", "replace")
        for line in reversed(tail.strip().splitlines()):
            try:
                cwd = json.loads(line).get("cwd")
                if cwd and os.path.isdir(cwd):
                    return cwd
            except Exception:
                continue
    except Exception:
        pass
    name = os.path.basename(os.path.dirname(transcript))
    guess = name.replace("-", "/")
    if guess.startswith("/") and os.path.isdir(guess):
        return guess
    return os.path.expanduser("~")


def _refresh_handoff(transcript, sid, hf_id):
    """Re-run the Stop-hook generator so the checkpoint is current, not
    last-turn-end. WALL_HANDOFF_HOME redirects output in sandbox tests."""
    if not (transcript and os.path.isfile(GEN_HANDOFF)):
        return None
    env = dict(os.environ)
    home = os.environ.get("WALL_HANDOFF_HOME")
    if home:
        env["HOME"] = home
    try:
        subprocess.run([sys.executable, GEN_HANDOFF, transcript, sid, hf_id],
                       env=env, capture_output=True, timeout=20)
    except Exception:
        pass
    out = os.path.join(env.get("HOME", os.path.expanduser("~")),
                       ".claude", "state", f"handoff-{hf_id}.md")
    return out if os.path.isfile(out) else None


def _notify(n_sessions, resets_at):
    if os.environ.get("WALL_NOTIFY", "1") != "1":
        return
    msg = (f"5h wall hit — {n_sessions} session(s) checkpointed. "
           f"Auto-resume after {time.strftime('%H:%M', time.localtime(resets_at))}.")
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "trident wall"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def main():
    now = _now()
    wallet = _read_json(WALLET)
    if not wallet:
        return 0
    try:
        if now - os.path.getmtime(WALLET) > WALLET_STALE_S:
            return 0  # mint down → fail open, never wall on dead data
    except OSError:
        return 0

    fh = (wallet.get("telemetry") or {}).get("five_hour") or {}
    left = fh.get("left_pct")
    resets_at = fh.get("resets_at")
    if left is None or not resets_at:
        return 0

    existing = _read_json(MANIFEST)
    if existing and existing.get("status") == "armed":
        return 0  # already armed for this window — idempotent

    if left > THRESHOLD_PCT:
        return 0

    excluded = _excluded_prefixes()
    sessions = []
    for path in glob.glob(os.path.join(STATE, "rate-limits-*.json")):
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age > SESSION_WINDOW_S:
            continue
        rec = _read_json(path) or {}
        sid = rec.get("session_id") or os.path.basename(path)[len("rate-limits-"):-len(".json")]
        if any(sid.startswith(p) for p in excluded):
            continue
        hf_id = f"shed-ho-{sid[:8]}"
        transcript = _transcript_for(sid)
        cwd = _cwd_from_transcript(transcript) if transcript else os.path.expanduser("~")
        if cwd in EXCLUDE_CWDS:
            continue
        handoff = _refresh_handoff(transcript, sid, hf_id)
        sessions.append({"sid": sid, "hf_id": hf_id, "cwd": cwd,
                         "handoff": handoff, "transcript": transcript,
                         "last_seen_s": int(age)})

    if not sessions:
        return 0

    _atomic_write(MANIFEST, {
        "schema_version": 1,
        "status": "armed",
        "fired_at": now,
        "fired_iso": _iso(now),
        "threshold_pct": THRESHOLD_PCT,
        "left_pct": left,
        "resets_at": resets_at,
        "resets_iso": _iso(resets_at),
        "sessions": sessions,
    })
    _notify(len(sessions), resets_at)
    print(f"[wall] armed: {len(sessions)} session(s), resume after {_iso(resets_at)}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
