"""Unit tests for the phase safety margin (server._margin_secs).

Run directly:  python3 mcp/test_margin.py
No chain access needed; importing the server module is side-effect
free apart from resolving the near CLI path.

The margin contract under test:
- With no explicit COMMONS_COMMIT_MIN_MARGIN_SECS, the margin
  scales with the commit phase: a quarter of the phase, capped at
  45 s and floored at 10 s. A 120 s epoch (60 s commit phase) gets
  15 s, so the usable commit window is 45 s instead of the 15 s a
  fixed 45 s margin left (the short-epoch squeeze an outside
  operator's daemon hit in 2026-11).
- An explicit environment value always wins, including 0.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

# Dynamic mode: quarter of the phase, cap 45, floor 10.
server.COMMIT_MARGIN_FROM_ENV = False
CASES = {15: 10, 20: 10, 30: 10, 40: 10, 60: 15, 100: 25,
         150: 37, 180: 45, 300: 45, 600: 45, 3600: 45}
for half, want in CASES.items():
    got = server._margin_secs(half)
    assert got == want, f"half={half}: got {got}, want {want}"
    assert got <= half, f"half={half}: margin {got} exceeds the phase"

# Explicit environment value always wins.
server.COMMIT_MARGIN_FROM_ENV = True
for fixed in (0, 5, 45, 90):
    server.COMMIT_MIN_MARGIN_SECS = fixed
    for half in (15, 60, 600):
        got = server._margin_secs(half)
        assert got == fixed, f"env={fixed}, half={half}: got {got}"

print("margin tests passed")
