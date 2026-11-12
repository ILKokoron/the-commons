"""Unit tests for the security runner's assessment math.

Run directly:  python3 agents/test_security_runner.py
No chain access needed; the epoch records are fabricated in the
exact shape of the contract's get_epoch view.

The claims under test:
- The epoch cap is cap_bps of the settlement snapshot, and the
  snapshot is pool_after + total_paid (proven against v7 room 2
  epoch 0: pool_after 0.0007 + total_paid 0.001 = 0.0017 snapshot,
  cap_bps 5000 -> cap 0.00085, and both reveals, 0.0004 and
  0.0006, sat under it and were paid in full).
- An account is flagged only when its request reaches factor x
  the cap; honest requests under the cap never flag.
- Streaks count consecutive epochs only: a clean epoch in the
  middle resets the count, and re-flagging the same epoch twice
  does not double-count.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import security_runner as sr  # noqa: E402

Y = 10 ** 24


def rec(pool_after, total_paid, reveals):
    return {
        "pool_after": str(pool_after),
        "total_paid": str(total_paid),
        "total_requested": str(sum(a for _, a, _ in reveals)),
        "reveal_count": len(reveals),
        "reveals": [{"account": acc, "amount": str(a), "paid": str(p)}
                    for acc, a, p in reveals],
    }


# The real v7 room 2 epoch 0 numbers.
real = rec(700000000000000000000, 1000000000000000000000,
           [("exam3", 400000000000000000000, 400000000000000000000),
            ("exam2", 600000000000000000000, 600000000000000000000)])
assert sr.epoch_cap_yocto(real, 5000) == 850000000000000000000
assert sr.over_requests(real, 5000, 10) == {}

# A wild over-request: pool snapshot 0.01 NEAR, cap 50% = 0.005,
# factor 10 -> the line is 0.05 NEAR requested.
wild = rec(5 * 10 ** 21, 5 * 10 ** 21,
           [("greedy", 5 * Y, 5 * 10 ** 21),
            ("honest", 10 ** 21, 10 ** 21)])
assert sr.over_requests(wild, 5000, 10) == {"greedy": 5 * Y}

# Just under the line does not flag: 9.9x the cap.
under = rec(5 * 10 ** 21, 5 * 10 ** 21,
            [("almost", 495 * 10 ** 20, 5 * 10 ** 21)])
assert sr.over_requests(under, 5000, 10) == {}

# An empty pool epoch (cap 0) flags nobody.
assert sr.over_requests(rec(0, 0, []), 5000, 10) == {}

# Streaks: three consecutive flagged epochs reach a streak of 3;
# a clean epoch resets; the same epoch counted twice counts once.
s = {}
sr.update_streaks(s, 0, {"greedy": 1})
sr.update_streaks(s, 1, {"greedy": 1})
assert [e for e in s["greedy"]["epochs"]] == [0, 1]
sr.update_streaks(s, 1, {"greedy": 1})
assert s["greedy"]["epochs"] == [0, 1]
sr.update_streaks(s, 2, {"greedy": 1})
assert len(s["greedy"]["epochs"]) == 3
sr.update_streaks(s, 3, {})
assert s == {}
sr.update_streaks(s, 4, {"greedy": 1})
assert s["greedy"]["epochs"] == [4]

# record_confirm / record_filed / is_designated_filer: a failed
# call is never remembered as done (regression: the old code
# appended unconditionally, so one failed confirm or report left
# the target hanging forever).
st = {"confirmed": [], "filed": {}}
assert sr.record_confirm(st, 7, True) is True
assert st["confirmed"] == [7]
assert sr.record_confirm(st, 7, True) is True
assert st["confirmed"] == [7]  # no duplicate
assert sr.record_confirm(st, 8, False) is False
assert st["confirmed"] == [7]  # failed confirm retries next scan
assert sr.record_filed(st, "greedy",
                       {"ok": True, "return_value": 3}) == 3
assert st["filed"] == {"greedy": 3}
assert sr.record_filed(st, "other",
                       {"ok": False, "return_value": None}) is None
assert "other" not in st["filed"]
assert sr.record_filed(st, "third",
                       {"ok": True, "return_value": None}) is None
assert "third" not in st["filed"]

# Designated filer: only the named instance files. When the roster
# cannot be read (designated None), nobody files.
assert sr.is_designated_filer("exam1.testnet", "exam1.testnet") is True
assert sr.is_designated_filer("exam1.testnet", "judge1.testnet") is False
assert sr.is_designated_filer(None, "exam1.testnet") is False

print("security runner tests passed")
