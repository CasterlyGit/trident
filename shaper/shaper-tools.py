#!/usr/bin/env python3
"""trident SHAPER — PreToolUse hook on Agent|Workflow (DESIGN.md §3.4).

The load-bearing primitive: the ONE verified mechanical spawn lever is rewriting
`AgentInput.model` via `updatedInput` (enum sonnet|opus|haiku — verified against
CC 2.1.163 sdk-tools.d.ts at build time, 2026-06-05).

- Agent: requested tier > wallet ceiling → updatedInput with model lowered. Never raise.
  Missing model → leave (inherits parent; ceiling still binds the child's own spawns).
- Depth: hook payload transcript_path containing "/subagents/" → this session IS a
  subagent → ceiling = tier_ceiling_child; else tier_ceiling_spawn.
- Width: flock'd counter increment on Agent allow. Over fanout_max → still ALLOW
  (advisory dimension; envelope shows breach next turn). Over 2× → hard DENY (runaway).
- Workflow: DENY when speculative=false AND input is explicitly speculative, or runaway.
  Reason strings always teach the cheaper shape.
- Ported claudeignore-guard rules live behind TRIDENT_READGUARD=1 (activated post-soak
  when claudeignore-guard.sh is unwired; dormant during parallel soak to avoid doubles).

Fail OPEN: wallet stale/missing or any error → allow untouched (advisory-only).
"""

import fcntl
import json
import os
import re
import sys
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
WALLET = os.path.join(STATE, "wallet.json")
COUNTERS = os.path.join(STATE, "trident-counters.json")
LOCK = os.path.join(STATE, "trident-counters.lock")
TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
SPECULATIVE_RE = re.compile(r"\bspeculative|just in case|in parallel speculat", re.I)


def out(obj):
    print(json.dumps(obj))
    sys.exit(0)


def allow():
    sys.exit(0)  # empty output = allow untouched


def decision(permission, reason, updated_input=None):
    h = {"hookEventName": "PreToolUse", "permissionDecision": permission,
         "permissionDecisionReason": reason}
    if updated_input is not None:
        h["updatedInput"] = updated_input
    out({"hookSpecificOutput": h})


def read_wallet():
    try:
        if time.time() - os.path.getmtime(WALLET) > 120:
            return None  # stale → advisory-only
        with open(WALLET) as f:
            w = json.load(f)
        return w if w.get("contract_ok") else None
    except Exception:
        return None


def bump_counter(sid, delta=1):
    """flock(LOCK_EX) read-modify-write on trident-counters.json. Returns inflight."""
    try:
        with open(LOCK, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                with open(COUNTERS) as f:
                    c = json.load(f)
            except Exception:
                c = {"spawns_inflight": 0, "by_session": {}}
            c["spawns_inflight"] = max(0, c.get("spawns_inflight", 0) + delta)
            bs = c.setdefault("by_session", {})
            bs[sid] = max(0, bs.get(sid, 0) + delta)
            if bs[sid] == 0:
                del bs[sid]
            c["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            tmp = f"{COUNTERS}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(c, f)
            os.replace(tmp, COUNTERS)
            return c["spawns_inflight"]
    except Exception:
        return 0


def read_rules(tool, tool_input):
    """claudeignore-guard.sh rules, ported verbatim (active only when TRIDENT_READGUARD=1)."""
    waste = ("node_modules", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
             "Pipfile.lock", "poetry.lock", "Cargo.lock", "go.sum", ".next/", "dist/",
             "__pycache__", ".pyc", ".min.js", ".min.css", ".map", ".parquet", ".pkl",
             ".bin", ".whl", ".egg-info")
    if tool == "Bash":
        cmd = tool_input.get("command", "")
        if re.search(r"(package-lock\.json|yarn\.lock|pnpm-lock|Pipfile\.lock|poetry\.lock|Cargo\.lock|go\.sum)", cmd):
            decision("deny", "Lockfile reads waste tokens. Use package.json or Cargo.toml instead.")
        allow()
    path = tool_input.get("file_path") or tool_input.get("pattern") or ""
    path = os.path.expanduser(path)
    if not path:
        allow()
    for p in waste:
        if p in path:
            decision("deny", f"Token-wasteful file ({p}) — use a summary or parent config instead.")
    if tool == "Read" and os.path.isfile(path) and \
            not tool_input.get("offset") and not tool_input.get("limit"):
        size = os.path.getsize(path)
        if size > 15000:
            decision("deny", f"File is {size // 1024}KB — use offset+limit to read only the "
                             f"section you need (~{size // 4} tokens if read whole).")
    allow()


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    sid = payload.get("session_id", "")

    if tool not in ("Agent", "Task", "Workflow"):
        if os.environ.get("TRIDENT_READGUARD") == "1" and tool in ("Read", "Glob", "WebFetch", "Bash"):
            read_rules(tool, tool_input)
        allow()

    w = read_wallet()
    if not w:
        allow()  # mint down/degraded → advisory-only, never block work

    blk = w["derived"]["per_pin"].get(sid) or w["derived"]["default"]
    is_subagent = "/subagents/" in (payload.get("transcript_path") or "")
    ceiling = blk["tier_ceiling_child"] if is_subagent else blk["tier_ceiling_spawn"]
    fanout_max = blk.get("fanout_max", 12)

    try:
        with open(COUNTERS) as f:
            inflight = json.load(f).get("spawns_inflight", 0)
    except Exception:
        inflight = 0

    if tool == "Workflow":
        if inflight >= fanout_max * 2:
            decision("deny", f"trident: over width budget (W≤{fanout_max}, {inflight} in flight). "
                             "Split into fewer agents or run inline.")
        if not blk.get("speculative", True):
            text = json.dumps({k: tool_input.get(k) for k in ("script", "args", "name")})
            if SPECULATIVE_RE.search(text or ""):
                decision("deny", "trident: speculative work is off at this lever "
                                 f"(L={blk['L_eff']}). Run the committed shape inline, or pin "
                                 "this terminal (`throttle pin 100`) to bypass.")
        allow()

    # Agent / Task
    if inflight >= fanout_max * 2:
        decision("deny", f"trident: over width budget (W≤{fanout_max}, {inflight} in flight). "
                         "Batch into fewer agents or run inline.")

    bump_counter(sid, +1)  # width accounting (advisory dimension; settle decrements)

    model = tool_input.get("model")
    if model in TIER_RANK and TIER_RANK[model] > TIER_RANK[ceiling]:
        updated = dict(tool_input)
        updated["model"] = ceiling
        decision("allow",
                 f"trident: model {model}→{ceiling} "
                 f"({'child' if is_subagent else 'spawn'} ceiling at L={blk['L_eff']})",
                 updated)
    allow()


if __name__ == "__main__":
    main()
