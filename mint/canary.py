"""trident canary — contract drift detector (DESIGN.md §3.9).

Runs on mint startup + whenever the CC version string changes (cached in
trident-canary.json). Any failure → contract_ok=false in wallet + one macOS
notification + posture shows the degrade. Consumers all fail to advisory-only;
work continues.
"""

import glob
import json
import os
import subprocess
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
CANARY = os.path.join(STATE, "trident-canary.json")
SDK_TOOLS = "/usr/local/lib/node_modules/@anthropic-ai/claude-code/sdk-tools.d.ts"
TRIDENT = os.path.expanduser("~/Documents/Dev/trident")

# Frozen PreToolUse payload fixture — if our own shaper can't parse this shape,
# either the shape or the shaper drifted.
HOOK_FIXTURE = {"session_id": "fixture", "transcript_path": "/tmp/x.jsonl",
                "hook_event_name": "PreToolUse", "tool_name": "Agent",
                "tool_input": {"description": "x", "prompt": "y", "model": "opus"}}


def cc_version():
    try:
        return subprocess.run(["claude", "--version"], capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return None


def check_statusline_schema():
    """Newest telemetry still has rate_limits.five_hour.used_percentage."""
    files = sorted(glob.glob(os.path.join(STATE, "rate-limits-*.json")),
                   key=os.path.getmtime, reverse=True)
    for f in files[:3]:
        try:
            d = json.load(open(f))
            if d["rate_limits"]["five_hour"]["used_percentage"] is not None:
                return True
        except Exception:
            continue
    return not files  # no files at all = indeterminate, don't degrade on absence


def check_agent_model_enum():
    """AgentInput.model still a writable enum carrying sonnet|opus|haiku|fable
    (fable verified in sdk-tools.d.ts 2026-06-10; the wallet may bake a fable
    ceiling, so its removal IS contract drift)."""
    try:
        src = open(SDK_TOOLS).read()
        i = src.index("interface AgentInput")
        chunk = src[i:i + 3000]
        return all(f'"{t}"' in chunk for t in ("sonnet", "opus", "haiku", "fable"))
    except Exception:
        return None  # indeterminate (file moved) — warn, don't degrade


def check_hook_fixture():
    """Our shaper still parses the frozen PreToolUse payload (allow on fixture)."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="trident-canary-") as td:
            # isolated state dir: the fixture run must never bump REAL counters
            r = subprocess.run(
                ["python3", os.path.join(TRIDENT, "shaper", "shaper-tools.py")],
                input=json.dumps(HOOK_FIXTURE), capture_output=True, text=True, timeout=10,
                env={**os.environ, "TRIDENT_STATE_DIR": td})
        return r.returncode == 0
    except Exception:
        return False


def check_hooks_registered():
    try:
        s = json.load(open(os.path.expanduser("~/.claude/settings.json")))
        flat = json.dumps(s.get("hooks", {}))
        return "shaper-tools.py" in flat and "shaper-prompt.sh" in flat \
            and "ledger-settle.py" in flat
    except Exception:
        return False


def notify(msg):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "trident canary"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def run(force=False):
    """Returns (contract_ok: bool, failures: list[str]). Cached per CC version."""
    ver = cc_version()
    cached = None
    try:
        cached = json.load(open(CANARY))
    except Exception:
        pass
    if not force and cached and cached.get("cc_version") == ver and ver is not None:
        return cached.get("contract_ok", True), cached.get("failures", [])

    checks = {
        "statusline_schema": check_statusline_schema(),
        "agent_model_enum": check_agent_model_enum(),
        "hook_fixture": check_hook_fixture(),
        "hooks_registered": check_hooks_registered(),
    }
    failures = [k for k, v in checks.items() if v is False]  # None = indeterminate
    ok = not failures
    out = {"cc_version": ver, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "contract_ok": ok, "checks": {k: v for k, v in checks.items()}, "failures": failures}
    tmp = f"{CANARY}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, CANARY)
    if not ok and (not cached or cached.get("contract_ok", True)):
        notify(f"contract drift: {', '.join(failures)} — trident degraded to advisory-only")
    return ok, failures
