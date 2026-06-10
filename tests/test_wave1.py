"""Wave 1 smoke tests — formulas endpoints + mint tick + ingest guards + pin resolution.

Runs against a temp TRIDENT_STATE_DIR; never touches the real ~/.claude/state.
    python3 tests/test_wave1.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mint"))
import formulas  # noqa: E402

MINT = os.path.join(ROOT, "mint", "mint.py")


class TestFormulas(unittest.TestCase):
    def test_lever_endpoints(self):
        self.assertEqual(formulas.M(100), 1.0)
        self.assertEqual(formulas.M(0), 0.0)
        self.assertAlmostEqual(formulas.M(50), 0.354, places=3)

    def test_tier_table_calibration(self):
        # ARCHITECTURE lever table: L=100+H≥90→fable, L=63→sonnet, L=30→sonnet, L=0→haiku (H full)
        for L, want in [(100, "fable"), (63, "sonnet"), (30, "sonnet"), (0, "haiku")]:
            b = formulas.derived_block(L, 100, 1)
            self.assertEqual(b["tier_ceiling_spawn"], want, f"L={L}")
        # fable band is narrow: free lane but headroom dampened (2 windows) → opus
        self.assertEqual(formulas.derived_block(100, 100, 2)["tier_ceiling_spawn"], "opus")
        # depth cascade
        self.assertEqual(formulas.tier_child("fable"), "opus")
        self.assertEqual(formulas.tier_child("opus"), "sonnet")
        self.assertEqual(formulas.tier_child("sonnet"), "haiku")
        self.assertEqual(formulas.tier_child("haiku"), "haiku")

    def test_floors_never_zero(self):
        b = formulas.derived_block(0, 5, 8)  # worst case
        self.assertEqual(b["fanout_max"], 1)
        self.assertEqual(b["verify_min"], 1)        # routing/rigor boundary
        self.assertFalse(b["speculative"])
        self.assertEqual(b["roi_min"], 6.0)          # capped — learning never locks out
        self.assertEqual(b["tier_ceiling_spawn"], "haiku")

    def test_free_lane(self):
        b = formulas.derived_block(100, 100, 1)
        self.assertEqual(b["M"], 1.0)
        self.assertEqual(b["fanout_max"], 12)
        self.assertEqual(b["thinking_budget"], 32000)
        self.assertEqual(b["inject_cap_tokens"], 4000)

    def test_imminence_bonus(self):
        self.assertEqual(formulas.headroom_raw(40, 90, None, 1000), 80)   # +40 <30m
        self.assertEqual(formulas.headroom_raw(40, 90, None, 3000), 60)   # +20 <60m
        self.assertEqual(formulas.headroom_raw(90, 95, None, 1000), 100)  # cap

    def test_inject_matches_design_example(self):
        # DESIGN §2: L=50 → inject_cap ≈ 2150
        self.assertAlmostEqual(formulas.inject_cap_tokens(formulas.M(50)), 2145, delta=20)


class TestMint(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trident-test-")
        self.env = {**os.environ, "TRIDENT_STATE_DIR": self.dir}
        self.sid = "aaaa1111-test"

    def _write_session(self, sid, used=51, resets_in=3600 * 2, mtime=None):
        p = os.path.join(self.dir, f"rate-limits-{sid}.json")
        with open(p, "w") as f:
            json.dump({"captured_at": "x", "session_id": sid, "rate_limits": {
                "five_hour": {"used_percentage": used, "resets_at": int(time.time()) + resets_in},
                "seven_day": {"used_percentage": 24, "resets_at": int(time.time()) + 86400},
            }}, f)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def _tick(self):
        r = subprocess.run([sys.executable, MINT, "--tick"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.load(open(os.path.join(self.dir, "wallet.json")))

    def test_tick_writes_wallet(self):
        self._write_session(self.sid)
        w = self._tick()
        self.assertTrue(w["contract_ok"])
        self.assertEqual(w["schema_version"], 1)
        self.assertAlmostEqual(w["telemetry"]["five_hour"]["left_pct"], 49.0)
        self.assertIn("envelope", w["derived"]["default"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "trident-counters.json")))

    def test_pin_resolution_and_per_pin_block(self):
        self._write_session(self.sid)
        with open(os.path.join(self.dir, "burn-throttle.json"), "w") as f:
            json.dump({"global": 40, "pins": {self.sid: 100}}, f)
        w = self._tick()
        self.assertEqual(w["lever"]["global"], 40)
        self.assertIn(self.sid, w["derived"]["per_pin"])
        self.assertEqual(w["derived"]["per_pin"][self.sid]["L_eff"], 100)
        self.assertEqual(w["derived"]["default"]["L_eff"], 40)
        self.assertTrue(w["lever"]["theater_warning"])  # 1 pinned / 1 active ≥ 50%

    def test_legacy_flat_throttle_shape(self):
        self._write_session(self.sid)
        with open(os.path.join(self.dir, "burn-throttle.json"), "w") as f:
            json.dump({"set_at": "x", "throttle": 45}, f)
        w = self._tick()
        self.assertEqual(w["lever"]["global"], 45)
        self.assertEqual(w["lever"]["pins"], {})

    def test_dead_pin_reaped_from_view(self):
        self._write_session(self.sid)
        with open(os.path.join(self.dir, "burn-throttle.json"), "w") as f:
            json.dump({"global": 40, "pins": {"dead-sid-no-file": 100}}, f)
        w = self._tick()
        self.assertEqual(w["derived"]["per_pin"], {})
        self.assertEqual(w["lever"]["pinned_count"], 0)
        # bright line: mint never writes the throttle file
        st = json.load(open(os.path.join(self.dir, "burn-throttle.json")))
        self.assertIn("dead-sid-no-file", st["pins"])

    def test_pause_leaves_wallet_stale(self):
        self._write_session(self.sid)
        self._tick()
        first = os.path.getmtime(os.path.join(self.dir, "wallet.json"))
        open(os.path.join(self.dir, "trident-paused"), "w").close()
        subprocess.run([sys.executable, MINT, "--tick"], env=self.env, capture_output=True)
        self.assertEqual(os.path.getmtime(os.path.join(self.dir, "wallet.json")), first)

    def _ingest(self, payload):
        return subprocess.run([sys.executable, MINT, "--ingest", "-"], env=self.env,
                              input=json.dumps(payload), capture_output=True, text=True)

    def test_ingest_null_guard(self):
        self._write_session(self.sid, used=51)
        self._tick()
        r = self._ingest({"session_id": self.sid, "rate_limits": None})
        self.assertEqual(r.returncode, 0)
        # shared file not created/overwritten by a null payload
        shared = os.path.join(self.dir, "rate-limits.json")
        self.assertFalse(os.path.exists(shared))

    def test_ingest_monotonic_guard(self):
        resets = int(time.time()) + 7200
        hi = {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 60, "resets_at": resets}}}
        lo = {"session_id": "s2", "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": resets}}}
        self._ingest(hi)
        self._ingest(lo)  # stale echo — must NOT lower shared
        shared = json.load(open(os.path.join(self.dir, "rate-limits.json")))
        self.assertEqual(shared["rate_limits"]["five_hour"]["used_percentage"], 60)
        # per-session file written regardless (own-context ground truth)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "rate-limits-s2.json")))

    def test_no_telemetry_fails_open(self):
        w = self._tick()
        self.assertFalse(w["contract_ok"])
        self.assertIn("envelope", w["derived"]["default"])  # still routes, advisory-only


if __name__ == "__main__":
    unittest.main(verbosity=2)
