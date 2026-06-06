#!/usr/bin/env python3
"""trident WALL daemon — check + resume tick every WALL_TICK_S (30s).

Python, not bash: launchd runs /usr/bin/python3 directly (mint pattern)
because /bin/bash under launchd cannot read ~/Documents — hit live
2026-06-05 as "Operation not permitted" on every KeepAlive respawn.
"""

import os
import subprocess
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
TICK = int(os.environ.get("WALL_TICK_S", "30"))

while True:
    for script in ("wall-check.py", "wall-resume.py"):
        try:
            subprocess.run([sys.executable, os.path.join(DIR, script)], timeout=300)
        except Exception as e:
            print(f"[walld] {script}: {e}", file=sys.stderr)
    time.sleep(TICK)
