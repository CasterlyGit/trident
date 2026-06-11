"""Brain wave tests — fable tier, policy overlay clamps/bright-line, burn forecast,
brain gating, and mint tick integration.

Isolated TRIDENT_STATE_DIR; never touches real ~/.claude/state, never calls a model
(TRIDENT_BRAIN_DISARM=1 + tick without brain_ok are both exercised).
    python3 tests/test_brain.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINT = os.path.join(ROOT, "mint", "mint.py")
sys.path.insert(0, os.path.join(ROOT, "mint"))
sys.path.insert(0, os.path.join(ROOT, "brain"))
import formulas  # noqa: E402
import brain  # noqa: E402


def fresh_policy_doc(now, **knobs):
    return {"schema_version": 1, "generated_at": "x", "expires_at": now + 3600,
            "policy": knobs, "rationale": "test"}


class TestPolicyValidation(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())

    def test_clamps(self):
        p = formulas.validate_policy(
            fresh_policy_doc(self.now, tier_bias=-5, width_mult=3.0, inject_mult=0.1,
                             think_mult=0.9, compact_bias=2.0), self.now)
        self.assertEqual(p["tier_bias"], -1)
        self.assertEqual(p["width_mult"], 1.5)
        self.assertEqual(p["inject_mult"], 0.6)
        self.assertEqual(p["think_mult"], 0.9)
        self.assertEqual(p["compact_bias"], 1.25)

    def test_expired_wrong_schema_empty_all_none(self):
        doc = fresh_policy_doc(self.now, width_mult=0.8)
        doc["expires_at"] = self.now - 1
        self.assertIsNone(formulas.validate_policy(doc, self.now))
        doc = fresh_policy_doc(self.now, width_mult=0.8)
        doc["schema_version"] = 99
        self.assertIsNone(formulas.validate_policy(doc, self.now))
        self.assertIsNone(formulas.validate_policy(fresh_policy_doc(self.now), self.now))
        self.assertIsNone(formulas.validate_policy({"policy": "garbage"}, self.now))
        self.assertIsNone(formulas.validate_policy(None, self.now))

    def test_garbage_knobs_dropped_not_fatal(self):
        p = formulas.validate_policy(
            fresh_policy_doc(self.now, width_mult="lol", think_mult=0.7,
                             spec_override="yes-string"), self.now)
        self.assertEqual(p, {"think_mult": 0.7})  # garbage dropped, bool-only override

    def test_spec_override_bool(self):
        p = formulas.validate_policy(
            fresh_policy_doc(self.now, spec_override=False), self.now)
        self.assertEqual(p, {"spec_override": False})

    def test_lever_stance_bands(self):
        for g in (0, 25, 39):
            self.assertEqual(formulas.lever_stance(g), "tighten", g)
        for g in (40, 55, 69):
            self.assertEqual(formulas.lever_stance(g), "neutral", g)
        for g in (70, 100):
            self.assertEqual(formulas.lever_stance(g), "release", g)
        self.assertEqual(formulas.lever_stance(None), "neutral")     # fail open
        self.assertEqual(formulas.lever_stance("garbage"), "neutral")

    def test_tighten_strips_expansion_keeps_reshape(self):
        # Human in CRAWL (lever 20): volume inflation + forced spec are removed,
        # but tier reshaping (narrow+smart) survives the clamp.
        doc = fresh_policy_doc(self.now, tier_bias=1, width_mult=1.4, think_mult=1.3,
                               inject_mult=1.2, compact_bias=1.2, spec_override=True)
        p = formulas.validate_policy(doc, self.now, lever_global=20)
        self.assertEqual(p["tier_bias"], 1)            # reshape stays legal
        for k in ("width_mult", "think_mult", "inject_mult", "compact_bias"):
            self.assertNotIn(k, p)                     # capped to 1.0 → pruned as no-op
        self.assertNotIn("spec_override", p)           # can't force speculation on

    def test_tighten_passes_conserving_knobs_through(self):
        doc = fresh_policy_doc(self.now, width_mult=0.6, compact_bias=0.8, spec_override=False)
        p = formulas.validate_policy(doc, self.now, lever_global=10)
        self.assertEqual(p, {"width_mult": 0.6, "compact_bias": 0.8, "spec_override": False})

    def test_release_and_legacy_unconstrained(self):
        doc = fresh_policy_doc(self.now, width_mult=1.4, spec_override=True)
        self.assertEqual(formulas.validate_policy(doc, self.now, 80)["width_mult"], 1.4)
        self.assertTrue(formulas.validate_policy(doc, self.now, 80)["spec_override"])
        self.assertEqual(formulas.validate_policy(doc, self.now)["width_mult"], 1.4)  # 2-arg

    def test_noop_policy_pruned_to_none(self):
        self.assertIsNone(formulas.validate_policy(
            fresh_policy_doc(self.now, width_mult=1.0, tier_bias=0), self.now))


class TestApplyPolicy(unittest.TestCase):
    def setUp(self):
        self.block = formulas.derived_block(63, 100, 1)  # sonnet-band block

    def test_bright_line_untouched(self):
        out = formulas.apply_policy(self.block, {"tier_bias": -1, "width_mult": 0.5,
                                                 "think_mult": 0.5, "inject_mult": 0.6})
        for k in ("verify_min", "roi_min", "L_eff", "M", "H"):
            self.assertEqual(out[k], self.block[k], k)

    def test_tier_bias_and_child_recompute(self):
        up = formulas.apply_policy(self.block, {"tier_bias": 1})
        self.assertEqual(up["tier_ceiling_spawn"], "opus")
        self.assertEqual(up["tier_ceiling_child"], "sonnet")
        down = formulas.apply_policy(self.block, {"tier_bias": -1})
        self.assertEqual(down["tier_ceiling_spawn"], "haiku")
        self.assertEqual(down["tier_ceiling_child"], "haiku")

    def test_floors_and_marker(self):
        out = formulas.apply_policy(self.block, {"width_mult": 0.5, "inject_mult": 0.6})
        self.assertGreaterEqual(out["fanout_max"], 1)
        self.assertGreaterEqual(out["inject_cap_tokens"], formulas.INJECT_FLOOR)
        self.assertTrue(out["brain"])
        self.assertIn("🧠", out["envelope"])
        self.assertNotIn("🧠", self.block["envelope"])  # original untouched (pure)

    def test_fable_reachable_only_via_bias_from_opus(self):
        opus_block = formulas.derived_block(100, 100, 2)  # opus band
        self.assertEqual(opus_block["tier_ceiling_spawn"], "opus")
        up = formulas.apply_policy(opus_block, {"tier_bias": 1})
        self.assertEqual(up["tier_ceiling_spawn"], "fable")
        self.assertEqual(up["tier_ceiling_child"], "opus")


class TestForecast(unittest.TestCase):
    def test_linear_burn(self):
        now = 1_000_000
        pts = [(now + i * 60, 90 - i * 0.5) for i in range(30)]  # 0.5pp/min = 30pp/h
        f = formulas.burn_forecast(pts, 75.0, secs_to_reset=4 * 3600)
        self.assertAlmostEqual(f["burn_pph"], 30.0, delta=0.5)
        self.assertAlmostEqual(f["eta_exhaust_min"], 150, delta=5)
        self.assertTrue(f["wall_before_reset"])

    def test_idle_no_eta(self):
        now = 1_000_000
        pts = [(now + i * 60, 90.0) for i in range(30)]
        f = formulas.burn_forecast(pts, 90.0, secs_to_reset=3600)
        self.assertIsNone(f["eta_exhaust_min"])
        self.assertFalse(f["wall_before_reset"])

    def test_insufficient_data(self):
        self.assertIsNone(formulas.burn_forecast([(0, 90), (60, 89)], 89, 3600))
        pts = [(i, 90) for i in range(5)]  # 5 points but 4s span
        self.assertIsNone(formulas.burn_forecast(pts, 90, 3600))


def make_brain_wallet(left=50, l_eff=50, h=44, resets_in=7200):
    now = int(time.time())
    return {"contract_ok": True,
            "telemetry": {"five_hour": {"left_pct": left, "resets_at": now + resets_in},
                          "active_sessions": 1},
            "derived": {"default": {"L_eff": l_eff, "H": h, "tier_ceiling_spawn": "sonnet",
                                    "fanout_max": 3, "verify_min": 1}}}


class TestBrainGating(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-brain-")
        # Re-point every state path the brain touches at the temp dir.
        for attr in ("WALLET", "POLICY", "HISTORY", "COUNTERS", "BRAIN_STATE",
                     "AUDIT", "LOCK", "OFF_FLAG", "LOG"):
            setattr(brain, attr, os.path.join(self.dir, os.path.basename(getattr(brain, attr))))
        self.now = int(time.time())

    def test_fires_when_no_policy_and_cold(self):
        ok, reason = brain.should_fire(make_brain_wallet(), {}, self.now)
        self.assertTrue(ok)
        self.assertEqual(reason, "no-live-policy")

    def test_off_flag_blocks(self):
        open(brain.OFF_FLAG, "w").close()
        ok, reason = brain.should_fire(make_brain_wallet(), {}, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "off-flag")

    def test_dregs_blocks(self):
        ok, reason = brain.should_fire(make_brain_wallet(left=5), {}, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "dregs")

    def test_degraded_wallet_blocks(self):
        w = make_brain_wallet()
        w["contract_ok"] = False
        self.assertFalse(brain.should_fire(w, {}, self.now)[0])
        self.assertFalse(brain.should_fire(None, {}, self.now)[0])

    def test_window_cap(self):
        w = make_brain_wallet()
        reset_at = w["telemetry"]["five_hour"]["resets_at"]
        state = {"fires": [{"ts": 1, "reset_at": reset_at},
                           {"ts": 2, "reset_at": reset_at}],
                 "last_fire_ts": 0}
        ok, reason = brain.should_fire(w, state, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "window-cap")
        # previous window's fires don't count
        state = {"fires": [{"ts": 1, "reset_at": 123}, {"ts": 2, "reset_at": 123}]}
        self.assertTrue(brain.should_fire(w, state, self.now)[0])

    def test_cooldown_then_band_change(self):
        w = make_brain_wallet()
        state = {"last_fire_ts": self.now - 600, "last_band": "AMBER", "fires": []}
        ok, reason = brain.should_fire(w, state, self.now)
        self.assertFalse(ok)  # 10min ago < both cooldowns
        state["last_fire_ts"] = self.now - 1500
        w["derived"]["default"]["H"] = 90  # AMBER → GREEN flip
        w["derived"]["default"]["L_eff"] = 100
        ok, reason = brain.should_fire(w, state, self.now)
        self.assertTrue(ok)
        self.assertIn("band-change", reason)

    def test_lever_move_triggers(self):
        w = make_brain_wallet()
        w["lever"] = {"global": 25}
        state = {"last_fire_ts": self.now - 1500, "last_band": "AMBER",
                 "last_lever": 100, "fires": []}
        ok, reason = brain.should_fire(w, state, self.now)
        self.assertTrue(ok)
        self.assertIn("lever-move", reason)
        # inside the band cooldown (10min), the move does not re-fire
        state["last_fire_ts"] = self.now - 600
        self.assertFalse(brain.should_fire(w, state, self.now)[0])
        # a sub-threshold nudge is not a trigger
        state["last_fire_ts"] = self.now - 1500
        w["lever"]["global"] = 95
        self.assertNotIn("lever-move", brain.should_fire(w, state, self.now)[1])

    def test_inflight_lock_blocks(self):
        open(brain.LOCK, "w").close()
        ok, reason = brain.should_fire(make_brain_wallet(), {}, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "inflight")

    def test_extract_json_fenced(self):
        v = brain._extract_json('```json\n{"policy": {}, "no_change": true, "rationale": "r"}\n```')
        self.assertTrue(v["no_change"])
        v = brain._extract_json('prose before {"policy": {"width_mult": 0.8}} after')
        self.assertEqual(v["policy"]["width_mult"], 0.8)


class TestMintIntegration(unittest.TestCase):
    """Real `mint.py --tick` subprocess against a temp state dir. Never spawns the
    brain (no brain_ok in --tick) — double-guarded with TRIDENT_BRAIN_DISARM."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-mint-brain-")
        self.env = {**os.environ, "TRIDENT_STATE_DIR": self.dir,
                    "TRIDENT_BRAIN_DISARM": "1"}
        self.now = int(time.time())

    def _telemetry(self, left=50):
        rec = {"captured_at": "x", "rate_limits": {
            "five_hour": {"used_percentage": 100 - left, "resets_at": self.now + 7200},
            "seven_day": {"used_percentage": 10, "resets_at": self.now + 86400}}}
        with open(os.path.join(self.dir, "rate-limits.json"), "w") as f:
            json.dump(rec, f)

    def _tick(self):
        r = subprocess.run([sys.executable, MINT, "--tick"], capture_output=True,
                           text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.dir, "wallet.json")) as f:
            return json.load(f)

    def test_policy_overlay_applied_to_default_only(self):
        self._telemetry()
        with open(os.path.join(self.dir, "burn-throttle.json"), "w") as f:
            json.dump({"global": 63, "pins": {"sid-pinned": 100}}, f)
        # live pin needs a fresh session telemetry file
        with open(os.path.join(self.dir, "rate-limits-sid-pinned.json"), "w") as f:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 50}}}, f)
        with open(os.path.join(self.dir, "trident-policy.json"), "w") as f:
            json.dump(fresh_policy_doc(self.now, width_mult=0.5, tier_bias=-1), f)
        w = self._tick()
        self.assertTrue(w["brain"]["policy_active"])
        d = w["derived"]["default"]
        self.assertTrue(d.get("brain"))
        self.assertIn("🧠", d["envelope"])
        self.assertEqual(d["tier_ceiling_spawn"], "haiku")  # sonnet biased down
        pinned = w["derived"]["per_pin"]["sid-pinned"]
        self.assertNotIn("🧠", pinned["envelope"])  # pins are human-sovereign
        self.assertNotIn("brain", pinned)

    def test_lever_tightens_live_policy_on_read(self):
        # Brain wrote an expansionary overlay; the developer then throttles into
        # CRAWL. The next tick must re-clamp it — no brain re-fire required.
        self._telemetry()
        with open(os.path.join(self.dir, "burn-throttle.json"), "w") as f:
            json.dump({"global": 20, "pins": {}}, f)
        with open(os.path.join(self.dir, "trident-policy.json"), "w") as f:
            json.dump(fresh_policy_doc(self.now, width_mult=1.5, spec_override=True,
                                       tier_bias=-1), f)
        d = self._tick()["derived"]["default"]
        self.assertFalse(d["speculative"])    # spec_override=True stripped under tighten
        self.assertIn("🧠", d["envelope"])     # tier_bias survives → overlay still live

    def test_expired_policy_ignored(self):
        self._telemetry()
        doc = fresh_policy_doc(self.now, width_mult=0.5)
        doc["expires_at"] = self.now - 10
        with open(os.path.join(self.dir, "trident-policy.json"), "w") as f:
            json.dump(doc, f)
        w = self._tick()
        self.assertFalse(w["brain"]["policy_active"])
        self.assertNotIn("🧠", w["derived"]["default"]["envelope"])

    def test_fable_headless_model_at_free_lane(self):
        self._telemetry(left=100)
        with open(os.path.join(self.dir, "burn-throttle.json"), "w") as f:
            json.dump({"global": 100, "pins": {}}, f)
        w = self._tick()
        self.assertEqual(w["derived"]["default"]["tier_ceiling_spawn"], "fable")
        self.assertEqual(w["headless"]["model"], "claude-fable-5")

    def test_history_ring_and_forecast_in_wallet(self):
        # Pre-seed 30min of descending history, then tick: forecast must appear.
        hist = os.path.join(self.dir, "trident-history.jsonl")
        with open(hist, "w") as f:
            for i in range(30):
                f.write(json.dumps({"ts": self.now - 1800 + i * 60,
                                    "l5": 80 - i * 0.5}) + "\n")
        self._telemetry(left=65)
        w = self._tick()
        self.assertIn("forecast", w)
        self.assertGreater(w["forecast"]["burn_pph"], 0)
        # tick appended today's sample to the ring
        with open(hist) as f:
            self.assertEqual(len(f.readlines()), 31)


class TestShaperFableRewrite(unittest.TestCase):
    """A fable-requesting spawn is mechanically lowered to the wallet ceiling."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-shaper-fable-")
        self.env = {**os.environ, "TRIDENT_STATE_DIR": self.dir}
        blk = {"L_eff": 45, "H": 25.0, "tier_ceiling_spawn": "sonnet",
               "tier_ceiling_child": "haiku", "fanout_max": 3, "thinking_budget": 1000,
               "inject_cap_tokens": 1900, "verify_min": 1, "speculative": False,
               "compact_scale": 0.65, "roi_min": 4.0, "envelope": "[TRIDENT ...]",
               "posture": ["l1", "l2"]}
        w = {"schema_version": 1, "contract_ok": True,
             "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "telemetry": {"five_hour": {"left_pct": 50}, "active_sessions": 1},
             "lever": {"global": 45, "pins": {}, "pinned_count": 0,
                       "theater_warning": False},
             "derived": {"default": blk, "per_pin": {}},
             "headless": {"model": "claude-sonnet-4-6", "envelope": "x"}}
        with open(os.path.join(self.dir, "wallet.json"), "w") as f:
            json.dump(w, f)

    def test_fable_request_lowered_to_ceiling(self):
        payload = {"session_id": "s1", "transcript_path": "/tmp/t.jsonl",
                   "tool_name": "Agent",
                   "tool_input": {"description": "x", "prompt": "y", "model": "fable"}}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "shaper", "shaper-tools.py")],
                           input=json.dumps(payload), capture_output=True, text=True,
                           env=self.env, timeout=10)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"]["model"], "sonnet")


if __name__ == "__main__":
    unittest.main(verbosity=2)
