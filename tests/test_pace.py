"""Pace valve brain — billed weighting, burn scan, proportional-shed control law."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mint"))
import pace


def _entry(ts, cr=0, cc=0, inp=0, out=0):
    return json.dumps({
        "timestamp": ts,
        "message": {"usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
        }},
    })


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def test_billed_matches_meter_weighting():
    # claude-meter quota math: in + out + cc + 0.1*cr
    u = {"input_tokens": 100, "output_tokens": 50,
         "cache_creation_input_tokens": 200, "cache_read_input_tokens": 1000}
    assert pace._billed(u) == 100 + 50 + 200 + 100


def test_scan_burn_windows_and_rates(tmp_path):
    now = time.time()
    proj = tmp_path / "-Users-x"
    proj.mkdir()
    lines = [
        _entry(_iso(now - 30), cr=100000, cc=1000, out=500),   # in window
        _entry(_iso(now - 90), cr=100000, cc=1000, out=500),   # in window
        _entry(_iso(now - 600), cr=999999),                    # outside window
        "not json",
    ]
    (proj / "abc12345-0000.jsonl").write_text("\n".join(lines) + "\n")
    rates = pace.scan_burn(now, projects=str(tmp_path), window_s=120)
    assert list(rates) == ["abc12345-0000"]
    r = rates["abc12345-0000"]
    assert r["turns"] == 2
    # 2 * (10000 + 1000 + 500) billed over 120s → *0.5 per minute
    assert abs(r["bpm"] - 2 * 11500 * 0.5) < 1


def test_scan_burn_skips_cold_files(tmp_path):
    proj = tmp_path / "-Users-x"
    proj.mkdir()
    f = proj / "cold.jsonl"
    f.write_text(_entry(_iso(time.time()), cr=100000) + "\n")
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert pace.scan_burn(time.time(), projects=str(tmp_path)) == {}


def test_under_target_nobody_sleeps():
    rates = {"a": {"bpm": 90000, "turns": 10}, "b": {"bpm": 30000, "turns": 5}}
    g, sleeps = pace.assign_sleeps(rates, target_bpm=150000)
    assert g == 120000 and sleeps == {}


def test_over_target_paces_only_over_fair_sessions():
    # whale 100k, mid 60k, light 5k; target 100k, fair ≈ 33.3k
    rates = {
        "whale": {"bpm": 100000, "turns": 12},
        "mid": {"bpm": 60000, "turns": 8},
        "light": {"bpm": 5000, "turns": 3},
    }
    g, sleeps = pace.assign_sleeps(rates, target_bpm=100000)
    assert g == 165000
    assert "light" not in sleeps
    assert set(sleeps) == {"whale", "mid"}
    # whale overshare > mid overshare → whale sleeps longer per its interval
    assert sleeps["whale"] > 0 and sleeps["mid"] > 0
    assert all(s <= pace.MAX_SLEEP_S for s in sleeps.values())


def test_sleep_is_capped():
    rates = {"runaway": {"bpm": 10_000_000, "turns": 100}}
    _, sleeps = pace.assign_sleeps(rates, target_bpm=1000)
    assert sleeps["runaway"] == pace.MAX_SLEEP_S


def test_shed_math_converges_toward_target():
    # Applying the assigned slowdown to each paced session must land ≤ target
    # (cap can leave residual overshoot for extreme cases; not the case here).
    rates = {
        "a": {"bpm": 100000, "turns": 12},
        "b": {"bpm": 80000, "turns": 10},
    }
    target = 120000
    _, sleeps = pace.assign_sleeps(rates, target)
    new_total = 0.0
    for sid, r in rates.items():
        interval = pace.WINDOW_S / r["turns"]
        s = sleeps.get(sid, 0.0)
        new_total += r["bpm"] * interval / (interval + s)
    assert new_total <= target * 1.05  # within rounding of the 0.1s sleep grain


def test_read_target_absent_disables(monkeypatch, tmp_path):
    monkeypatch.setattr(pace, "TARGET_FILE", str(tmp_path / "nope.json"))
    assert pace.read_target() is None
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    monkeypatch.setattr(pace, "TARGET_FILE", str(bad))
    assert pace.read_target() is None


def test_pace_block_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(pace, "TARGET_FILE", str(tmp_path / "nope.json"))
    assert pace.pace_block(time.time()) is None
