"""Waves 2–4 smoke tests — SHAPER hooks, LEDGER settle/antibodies, proxy rewrite logic.

Isolated TRIDENT_STATE_DIR; never touches real ~/.claude/state.
    python3 tests/test_wave234.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHAPER_PROMPT = os.path.join(ROOT, "shaper", "shaper-prompt.sh")
SHAPER_TOOLS = os.path.join(ROOT, "shaper", "shaper-tools.py")
SETTLE = os.path.join(ROOT, "ledger", "ledger-settle.py")
sys.path.insert(0, os.path.join(ROOT, "ledger"))
sys.path.insert(0, os.path.join(ROOT, "proxy"))


def make_wallet(d, global_l=45, pins=None, fanout=3, spawn="sonnet", child="haiku"):
    blk = {"L_eff": global_l, "M": 0.3, "H": 25.0, "tier_ceiling_spawn": spawn,
           "tier_ceiling_child": child, "fanout_max": fanout, "thinking_budget": 1000,
           "inject_cap_tokens": 1900, "verify_min": 1, "speculative": False,
           "compact_scale": 0.65, "roi_min": 4.0,
           "envelope": f"[TRIDENT L={global_l} H=25 tier≤{spawn} W≤{fanout} think≤1k inject≤1.9k V≥1 spec=off]",
           "posture": ["line1", "line2"]}
    pin_blocks = {}
    for sid in (pins or {}):
        pb = dict(blk)
        pb.update(L_eff=100, tier_ceiling_spawn="opus", tier_ceiling_child="sonnet",
                  envelope="[TRIDENT L=100 ...]")
        pin_blocks[sid] = pb
    w = {"schema_version": 1, "contract_ok": True,
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "telemetry": {"five_hour": {"left_pct": 50, "resets_at": int(time.time()) + 7200},
                       "active_sessions": 1, "stale_s": 5},
         "lever": {"global": global_l, "pins": pins or {}, "pinned_count": len(pins or {}),
                   "theater_warning": False},
         "derived": {"default": blk, "per_pin": pin_blocks},
         "headless": {"model": "claude-sonnet-4-6", "envelope": "[TRIDENT TC2 ...]"}}
    path = os.path.join(d, "wallet.json")
    with open(path, "w") as f:
        json.dump(w, f)
    return path


class TestShaperPrompt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-t2-")
        make_wallet(self.dir)
        self.env = {**os.environ, "TRIDENT_STATE_DIR": self.dir,
                    "TRIDENT_GUARD_MUTE": os.path.join(self.dir, "nonexistent-mute")}

    def _run(self, payload):
        t0 = time.time()
        r = subprocess.run(["bash", SHAPER_PROMPT], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env)
        return r, (time.time() - t0) * 1000

    def test_envelope_and_cap_and_latency(self):
        r, ms = self._run({"session_id": "sess1", "prompt": "hi"})
        self.assertIn("[TRIDENT L=45", r.stdout)
        self.assertEqual(open(os.path.join(self.dir, "trident-inject-cap-sess1")).read(), "1900")
        self.assertLess(ms, 250)  # <100ms budget; CI slack for cold jq

    def test_stale_wallet_silent(self):
        w = os.path.join(self.dir, "wallet.json")
        os.utime(w, (time.time() - 300, time.time() - 300))
        r, _ = self._run({"session_id": "sess1", "prompt": "hi"})
        self.assertEqual(r.stdout, "")

    def test_pin_gets_pin_envelope(self):
        make_wallet(self.dir, pins={"pinned-sid": 100})
        r, _ = self._run({"session_id": "pinned-sid", "prompt": "hi"})
        self.assertIn("L=100", r.stdout)

    def test_guard_fires_only_when_trident_owns(self):
        # heavy session telemetry
        with open(os.path.join(self.dir, "rate-limits-sess1.json"), "w") as f:
            json.dump({"context_window": {"total_input_tokens": 999999}}, f)
        r, _ = self._run({"session_id": "sess1", "prompt": "hi"})
        self.assertNotIn("COMPACT-GUARD", r.stdout)  # owner defaults to shed
        with open(os.path.join(self.dir, "trident-guard-owner"), "w") as f:
            f.write("trident")
        r, _ = self._run({"session_id": "sess1", "prompt": "hi"})
        self.assertIn("COMPACT-GUARD", r.stdout)
        self.assertIn("shed-ho-sess1", r.stdout)


class TestShaperTools(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-t3-")
        make_wallet(self.dir, pins={"pin100": 100})
        self.env = {**os.environ, "TRIDENT_STATE_DIR": self.dir}

    def _run(self, payload):
        r = subprocess.run([sys.executable, SHAPER_TOOLS], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def _agent(self, model=None, sid="s1", transcript="/x/main.jsonl", tool="Agent"):
        ti = {"description": "t", "prompt": "p"}
        if model:
            ti["model"] = model
        return {"session_id": sid, "transcript_path": transcript,
                "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti}

    def test_opus_lowered_to_spawn_ceiling(self):
        out = self._run(self._agent("opus"))
        h = out["hookSpecificOutput"]
        self.assertEqual(h["permissionDecision"], "allow")
        self.assertEqual(h["updatedInput"]["model"], "sonnet")

    def test_subagent_depth_forces_child_ceiling(self):
        out = self._run(self._agent("sonnet", transcript="/x/subagents/child.jsonl"))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")

    def test_pinned_terminal_spawns_opus_untouched(self):
        out = self._run(self._agent("opus", sid="pin100"))
        self.assertIsNone(out)  # no rewrite — allow untouched

    def test_missing_model_left_alone_but_counted(self):
        out = self._run(self._agent(None))
        self.assertIsNone(out)
        c = json.load(open(os.path.join(self.dir, "trident-counters.json")))
        self.assertGreaterEqual(c["spawns_inflight"], 1)

    def test_runaway_guard_denies_at_2x(self):
        with open(os.path.join(self.dir, "trident-counters.json"), "w") as f:
            json.dump({"spawns_inflight": 6, "by_session": {"s1": 6}}, f)  # 2×3
        out = self._run(self._agent("haiku"))
        h = out["hookSpecificOutput"]
        self.assertEqual(h["permissionDecision"], "deny")
        self.assertIn("width budget", h["permissionDecisionReason"])

    def test_speculative_workflow_denied_when_off(self):
        p = self._agent(tool="Workflow")
        p["tool_input"] = {"script": "speculative demo run", "name": "x"}
        out = self._run(p)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stale_wallet_allows_untouched(self):
        w = os.path.join(self.dir, "wallet.json")
        os.utime(w, (time.time() - 300, time.time() - 300))
        self.assertIsNone(self._run(self._agent("opus")))

    def test_never_raises_tier(self):
        out = self._run(self._agent("haiku"))
        self.assertIsNone(out)  # haiku ≤ sonnet ceiling → untouched


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-t4-")
        make_wallet(self.dir)
        self.env = {**os.environ, "TRIDENT_STATE_DIR": self.dir}

    def test_subagent_stop_decrements_and_records(self):
        with open(os.path.join(self.dir, "trident-counters.json"), "w") as f:
            json.dump({"spawns_inflight": 2, "by_session": {"s1": 2}}, f)
        tp = os.path.join(self.dir, "child.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"message": {"model": "claude-haiku-4-5",
                                            "usage": {"output_tokens": 432}}}) + "\n")
        payload = {"session_id": "s1", "hook_event_name": "SubagentStop",
                   "agent_transcript_path": tp}
        r = subprocess.run([sys.executable, SETTLE], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        c = json.load(open(os.path.join(self.dir, "trident-counters.json")))
        self.assertEqual(c["spawns_inflight"], 1)
        recs = [json.loads(l) for l in open(os.path.join(self.dir, "session-stats.jsonl"))]
        spawn = [x for x in recs if x.get("kind") == "spawn"][0]
        self.assertEqual(spawn["output"], 432)
        self.assertIn("haiku", spawn["model"])

    def test_stop_writes_settlement(self):
        payload = {"session_id": "s1", "hook_event_name": "Stop", "transcript_path": ""}
        r = subprocess.run([sys.executable, SETTLE], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = [json.loads(l) for l in open(os.path.join(self.dir, "session-stats.jsonl"))]
        st = [x for x in recs if x.get("kind") == "settlement"][0]
        self.assertEqual(st["planned"]["fanout_max"], 3)


class TestAntibodies(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-t5-")
        os.environ["TRIDENT_STATE_DIR"] = self.dir
        global antibodies
        import importlib
        import antibodies
        importlib.reload(antibodies)

    def test_waste_mints_and_stays_observe_first_month(self):
        stats = os.path.join(self.dir, "session-stats.jsonl")
        with open(stats, "w") as f:
            f.write(json.dumps({"kind": "spawn", "session_id": "s1",
                                "model": "claude-opus-4-8", "output": 120}) + "\n")
        settlement = {"planned": {"fanout_max": 3}, "actual": {"spawns": 8}}
        for i in range(5):
            antibodies.observe(f"s{i}", settlement, stats)
        store = json.load(open(os.path.join(self.dir, "antibodies.json")))
        self.assertIn("fanout_waste:over2x", store)
        ab = store["fanout_waste:over2x"]
        self.assertGreaterEqual(ab["confirmations"], 3)
        self.assertEqual(ab["state"], "observe")  # hard-coded until OBSERVE_UNTIL

    def test_tolerance_suppresses(self):
        with open(os.path.join(self.dir, "antibody-tolerance.json"), "w") as f:
            json.dump(["fanout_waste:over2x"], f)
        self.assertTrue(antibodies.is_tolerated("fanout_waste:over2x"))


class TestProxyLogic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-t6-")
        os.environ["TRIDENT_STATE_DIR"] = self.dir
        make_wallet(self.dir)  # headless model sonnet
        global proxy
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "trident_proxy", os.path.join(ROOT, "proxy", "trident-proxy.py"))
        proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy)

    def test_rewrite_above_ceiling(self):
        body = json.dumps({"model": "claude-opus-4-8", "messages": []}).encode()
        new, orig, final = proxy.maybe_rewrite(body)
        self.assertEqual(orig, "claude-opus-4-8")
        self.assertEqual(final, "claude-sonnet-4-6")
        self.assertIn(b"claude-sonnet-4-6", new)

    def test_at_or_below_ceiling_untouched(self):
        for m in ("claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
            body = json.dumps({"model": m}).encode()
            _, orig, final = proxy.maybe_rewrite(body)
            self.assertEqual(orig, final)

    def test_stale_wallet_no_ceiling(self):
        w = os.path.join(self.dir, "wallet.json")
        os.utime(w, (time.time() - 300, time.time() - 300))
        body = json.dumps({"model": "claude-opus-4-8"}).encode()
        _, orig, final = proxy.maybe_rewrite(body)
        self.assertEqual(orig, final)  # fail open

    def test_parse_usage_from_sse_tail(self):
        tail = (b'event: message_delta\ndata: {"type":"message_delta","usage":'
                b'{"output_tokens":512,"input_tokens":9}}\n\n')
        u = proxy.parse_usage(tail)
        self.assertEqual(u["output_tokens"], 512)


if __name__ == "__main__":
    unittest.main(verbosity=1)
