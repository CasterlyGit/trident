"""WALL smoke tests — 5h-limit wall detector + resume respawn.

Runs against a temp TRIDENT_STATE_DIR + sandbox HOME + stub spawner;
never touches the real ~/.claude/state or any live session.
    python3 tests/test_wall.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, "wall", "wall-check.py")
RESUME = os.path.join(ROOT, "wall", "wall-resume.py")

SID_A = "aaaa1111-0000-0000-0000-000000000001"
SID_B = "bbbb2222-0000-0000-0000-000000000002"


def transcript_lines(cwd):
    """Minimal JSONL that generate-handoff.py finds 'signal' in."""
    return [
        {"cwd": cwd, "message": {"role": "user", "content": [{"type": "text",
            "text": "build the limit-wall pipeline simulation for trident and make resume work end to end"}]}},
        {"cwd": cwd, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Implemented the wall detector and resume daemon; tests added and everything is green now."},
            {"type": "tool_use", "name": "Write", "input": {"file_path": f"{cwd}/wall.py"}}]}},
    ]


class WallBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trident-wall-test-")
        self.state = os.path.join(self.tmp, "state")
        self.projects = os.path.join(self.tmp, "projects")
        self.home = os.path.join(self.tmp, "home")
        self.proj_a = os.path.join(self.tmp, "proj-a")
        self.proj_b = os.path.join(self.tmp, "proj-b")
        for d in (self.state, self.projects, self.home, self.proj_a, self.proj_b):
            os.makedirs(d)
        # stub spawner: records "hf_id cwd" lines
        self.spawn_log = os.path.join(self.tmp, "spawns.log")
        self.spawn_stub = os.path.join(self.tmp, "spawn-stub.sh")
        with open(self.spawn_stub, "w") as f:
            f.write(f'#!/bin/bash\necho "$1 $2" >> "{self.spawn_log}"\n')
        os.chmod(self.spawn_stub, 0o755)
        self.env = dict(os.environ)
        self.env.update({
            "TRIDENT_STATE_DIR": self.state,
            "WALL_PROJECTS_DIR": self.projects,
            "WALL_HANDOFF_HOME": self.home,
            "WALL_NOTIFY": "0",
            "WALL_SPAWN_CMD": self.spawn_stub,
            "WALL_RESUME_BUFFER_S": "1",
            "WALL_SPAWN_STAGGER_S": "0",
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def write_wallet(self, left_pct, resets_at, mtime=None):
        path = os.path.join(self.state, "wallet.json")
        with open(path, "w") as f:
            json.dump({"telemetry": {"five_hour": {
                "left_pct": left_pct, "resets_at": resets_at}}}, f)
        if mtime:
            os.utime(path, (mtime, mtime))

    def write_session(self, sid, cwd, mtime=None):
        with open(os.path.join(self.state, f"rate-limits-{sid}.json"), "w") as f:
            json.dump({"session_id": sid}, f)
        if mtime:
            p = os.path.join(self.state, f"rate-limits-{sid}.json")
            os.utime(p, (mtime, mtime))
        tdir = os.path.join(self.projects, cwd.replace("/", "-"))
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, f"{sid}.jsonl"), "w") as f:
            for line in transcript_lines(cwd):
                f.write(json.dumps(line) + "\n")

    def run_check(self):
        return subprocess.run([sys.executable, CHECK], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def run_resume(self):
        return subprocess.run([sys.executable, RESUME], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def manifest(self):
        try:
            with open(os.path.join(self.state, "limit-wall.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def spawns(self):
        try:
            with open(self.spawn_log) as f:
                return [ln.split() for ln in f.read().strip().splitlines()]
        except FileNotFoundError:
            return []


class TestWallCheck(WallBase):
    def test_no_fire_above_threshold(self):
        self.write_wallet(left_pct=50.0, resets_at=int(time.time()) + 3600)
        self.write_session(SID_A, self.proj_a)
        self.run_check()
        self.assertIsNone(self.manifest())

    def test_fires_at_threshold_with_sessions_and_handoffs(self):
        resets = int(time.time()) + 90
        self.write_wallet(left_pct=1.4, resets_at=resets)
        self.write_session(SID_A, self.proj_a)
        self.write_session(SID_B, self.proj_b)
        r = self.run_check()
        m = self.manifest()
        self.assertIsNotNone(m, r.stderr)
        self.assertEqual(m["status"], "armed")
        self.assertEqual(m["resets_at"], resets)
        self.assertEqual(len(m["sessions"]), 2)
        by_sid = {s["sid"]: s for s in m["sessions"]}
        self.assertEqual(by_sid[SID_A]["hf_id"], f"shed-ho-{SID_A[:8]}")
        self.assertEqual(by_sid[SID_A]["cwd"], self.proj_a)
        self.assertEqual(by_sid[SID_B]["cwd"], self.proj_b)
        # handoff refreshed into the SANDBOX home, not the real one
        for s in m["sessions"]:
            self.assertTrue(s["handoff"].startswith(self.home), s["handoff"])
            self.assertTrue(os.path.isfile(s["handoff"]))

    def test_stale_wallet_no_fire(self):
        self.write_wallet(left_pct=1.0, resets_at=int(time.time()) + 90,
                          mtime=time.time() - 600)
        self.write_session(SID_A, self.proj_a)
        self.run_check()
        self.assertIsNone(self.manifest())

    def test_stale_session_excluded(self):
        self.write_wallet(left_pct=1.0, resets_at=int(time.time()) + 90)
        self.write_session(SID_A, self.proj_a)
        self.write_session(SID_B, self.proj_b, mtime=time.time() - 7200)
        self.run_check()
        m = self.manifest()
        self.assertEqual([s["sid"] for s in m["sessions"]], [SID_A])

    def test_exclude_list(self):
        self.write_wallet(left_pct=1.0, resets_at=int(time.time()) + 90)
        self.write_session(SID_A, self.proj_a)
        self.write_session(SID_B, self.proj_b)
        with open(os.path.join(self.state, "wall-exclude"), "w") as f:
            f.write(f"# keepalive\n{SID_B[:8]}\n")
        self.run_check()
        m = self.manifest()
        self.assertEqual([s["sid"] for s in m["sessions"]], [SID_A])

    def test_armed_is_idempotent(self):
        self.write_wallet(left_pct=1.0, resets_at=int(time.time()) + 90)
        self.write_session(SID_A, self.proj_a)
        self.run_check()
        first = self.manifest()["fired_at"]
        time.sleep(1.1)
        self.run_check()
        self.assertEqual(self.manifest()["fired_at"], first)


class TestWallResume(WallBase):
    def arm(self, resets_at):
        self.write_wallet(left_pct=1.0, resets_at=resets_at)
        self.write_session(SID_A, self.proj_a)
        self.write_session(SID_B, self.proj_b)
        self.run_check()
        self.assertIsNotNone(self.manifest())

    def test_no_resume_before_reset(self):
        self.arm(int(time.time()) + 3600)
        self.run_resume()
        self.assertEqual(self.manifest()["status"], "armed")
        self.assertEqual(self.spawns(), [])

    def test_resume_after_reset_spawns_all(self):
        self.arm(int(time.time()) - 10)
        self.write_wallet(left_pct=98.0, resets_at=int(time.time()) + 18000)
        r = self.run_resume()
        self.assertIsNone(self.manifest(), r.stderr)  # consumed
        got = {(hf, cwd) for hf, cwd in self.spawns()}
        self.assertEqual(got, {(f"shed-ho-{SID_A[:8]}", self.proj_a),
                               (f"shed-ho-{SID_B[:8]}", self.proj_b)})
        done = [p for p in os.listdir(self.state) if p.startswith("limit-wall-done-")]
        self.assertEqual(len(done), 1)
        with open(os.path.join(self.state, done[0])) as f:
            archived = json.load(f)
        self.assertEqual(archived["status"], "resumed")
        self.assertTrue(all(s["ok"] for s in archived["spawned"]))

    def test_postpone_when_telemetry_still_exhausted(self):
        self.arm(int(time.time()) - 10)
        self.write_wallet(left_pct=0.5, resets_at=int(time.time()) - 10)  # fresh + exhausted
        self.run_resume()
        self.assertEqual(self.manifest()["status"], "armed")
        self.assertEqual(self.spawns(), [])

    def test_force_resume_after_force_window(self):
        self.arm(int(time.time()) - 1000)  # > WALL_FORCE_AFTER_S=900 past reset
        self.write_wallet(left_pct=0.5, resets_at=int(time.time()) - 1000)
        self.run_resume()
        self.assertIsNone(self.manifest())
        self.assertEqual(len(self.spawns()), 2)

    def test_stale_wallet_trusts_resets_at(self):
        self.arm(int(time.time()) - 10)
        self.write_wallet(left_pct=0.5, resets_at=int(time.time()) - 10,
                          mtime=time.time() - 600)  # stale → fail open
        self.run_resume()
        self.assertIsNone(self.manifest())
        self.assertEqual(len(self.spawns()), 2)

    def test_max_spawns_cap(self):
        self.env["WALL_MAX_SPAWNS"] = "1"
        self.arm(int(time.time()) - 10)
        self.write_wallet(left_pct=98.0, resets_at=int(time.time()) + 18000)
        self.run_resume()
        self.assertEqual(len(self.spawns()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
