"""Unit tests for the feeder runner's refill estimate
(runner._current_refill).

Run directly:  python3 agents/test_runner.py
No chain access needed.

The rule under test: the refill that moves for the epoch being
decided is the base decayed once per epoch before it, because
settlement is strictly in order, so by the time epoch E settles,
epochs 0..E-1 have all settled. A settle backlog does NOT change
that: the chain's current_refill (decayed only by the epochs
settled so far) is what moves for the next UNSETTLED epoch, not
for the epoch being decided, so using it (or the settled count)
here would overestimate the refill. A round-10 audit claim said
the opposite; this test locks in the correct direction.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner  # noqa: E402


def _room(mode, base, decay=5000, floor="0", settled=None):
    r = {"refill_per_epoch": str(base), "refill_mode": mode,
         "decay_bps": decay, "refill_floor": floor}
    if settled is not None:
        r["next_epoch_to_settle"] = settled
    return r


# Flat rooms always move the base refill.
assert runner._current_refill(_room("Flat", 10000), 7) == 10000

# Decaying with decay 5000 (halves each epoch): base 10000,
# epoch 3 -> 10000 -> 5000 -> 2500 -> 1250.
assert runner._current_refill(_room("Decaying", 10000), 3) == 1250
assert runner._current_refill(_room("Decaying", 10000), 0) == 10000

# The floor holds: decay cannot push the refill below it.
assert runner._current_refill(
    _room("Decaying", 10000, floor="3000"), 3) == 3000

# Backlog case: only 2 epochs settled, deciding epoch 5. The
# refill that will move when epoch 5 settles is still the base
# decayed 5 times (1250 -> 625 -> 312), because epochs 2, 3 and 4
# settle before it, in order.
assert runner._current_refill(
    _room("Decaying", 10000, settled=2), 5) == 312

print("runner tests passed")
