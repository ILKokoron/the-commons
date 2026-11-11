"""Unit tests for the MCP amount parser (server.to_yocto / near_str).

Run directly:  python3 mcp/test_amounts.py
No chain access needed; importing the server module is side-effect
free apart from resolving the near CLI path.

The parser contract under test:
- A string is always a NEAR amount, with or without a decimal
  point: "1", "1.0" and "0.005" are NEAR. A whole-number string is
  never raw yocto (regression test for the room-6 bug, where a
  seed of "1" funded a room with 1 yocto instead of 1 NEAR).
- Raw yocto is accepted only when explicitly marked: the "_yocto"
  suffix on a digit string, or a non-negative Python int (internal
  callers).
- Signs, exponents, floats, empty strings and more than 24 decimal
  places are rejected.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

Y = 10 ** 24

OK_CASES = {
    "1": Y,
    "1.0": Y,
    "12": 12 * Y,
    "0": 0,
    "0.005": 5 * 10 ** 21,
    "0.000000000000000000000001": 1,
    "1_yocto": 1,
    "0_yocto": 0,
    "1500000000000000000000_yocto": 1500000000000000000000,
}
for text, want in OK_CASES.items():
    got = server.to_yocto(text)
    assert got == want, f"to_yocto({text!r}) = {got}, want {want}"

# A Python int is already yocto (internal callers only).
assert server.to_yocto(7) == 7
assert server.to_yocto(0) == 0

BAD_CASES = [
    "-1", "-0.5", "+1", "1e-3", "1.5_yocto", "_yocto", "", "abc",
    "1,5", "1.0000000000000000000000001", "1..0", "0x10", "  ",
]
for text in BAD_CASES:
    try:
        server.to_yocto(text)
    except ValueError:
        pass
    else:
        raise AssertionError(f"to_yocto accepted invalid amount {text!r}")

try:
    server.to_yocto(1.5)
except ValueError:
    pass
else:
    raise AssertionError("to_yocto accepted a float")

try:
    server.to_yocto(True)
except ValueError:
    pass
else:
    raise AssertionError("to_yocto accepted a bool")

# near_str round-trips the values the create-room check reports.
assert server.near_str(Y) == "1"
assert server.near_str(1) == "0.000000000000000000000001"
assert server.near_str(5 * 10 ** 21) == "0.005"

print("amount parser tests passed")
