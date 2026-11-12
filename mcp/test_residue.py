"""Unit tests for the MCP residue math and the finalize pre-settle
(server._residue_yocto, server.to_yocto empty input,
server.commons_finalize_room).

Run directly:  python3 mcp/test_residue.py
No chain access needed; the finalize cases run against mocked
view / near_call / commons_settle_room functions.

The claims under test:
- A room's residue is pool + refill reserve + the UNSPENT part of
  the Guarantee Fund (gf_remaining), not pool + reserve alone, and
  not the original escrow: feeder top-ups eat the fund as the
  season runs, so a Covered room's residue was understated and its
  "escrowed" fund overstated (round-10 audit finding).
- An empty amount string is rejected with a message that says it
  is empty, not the generic "invalid amount" text.
- Finalize pre-settles a season-over backlog in installments
  before the one-shot finalize call, so a room with many unsettled
  epochs does not revert on the 300 TGas cap; a failed pre-settle
  stops before any finalize is attempted; an already-ended room
  and a mid-season room skip the pre-settle.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

Y = 10 ** 24

# --- _residue_yocto: pool + reserve + gf_remaining ---
room = {"pool_balance": str(Y), "refill_reserve": str(4 * Y),
        "gf_remaining": str(Y // 2)}
assert server._residue_yocto(room) == Y + 4 * Y + Y // 2

# No gf_remaining field (older contract views): pool + reserve only.
room_old = {"pool_balance": str(Y), "refill_reserve": str(4 * Y)}
assert server._residue_yocto(room_old) == 5 * Y

# gf_remaining "0": same as absent.
room_zero = {"pool_balance": str(Y), "refill_reserve": str(4 * Y),
             "gf_remaining": "0"}
assert server._residue_yocto(room_zero) == 5 * Y

# --- to_yocto(""): named as empty, not generic-invalid ---
try:
    server.to_yocto("")
    raise AssertionError("empty amount accepted")
except ValueError as e:
    assert "empty amount" in str(e)

# --- commons_finalize_room pre-settle (mocked chain) ---
_orig_view = server.view
_orig_call = server.near_call
_orig_settle = server.commons_settle_room


def _run_case(room_ended, season_over, settle_script, call_ok=True):
    calls = {"settle": 0, "finalize": 0}
    ended = {"v": room_ended}

    def fake_view(method, args):
        if method == "get_room":
            return {"ended": ended["v"], "pool_balance": "0",
                    "refill_reserve": "0", "gf_remaining": "0",
                    "season_epochs": 3, "next_epoch_to_settle": 0}
        if method == "current_epoch":
            return {"season_over": season_over, "epoch": 3}
        raise AssertionError(method)

    def fake_settle(room_id, max_epochs=100):
        calls["settle"] += 1
        return settle_script[calls["settle"] - 1]

    def fake_call(fn, args, **kw):
        assert fn == "finalize_room"
        calls["finalize"] += 1
        ended["v"] = True
        if call_ok:
            return {"ok": True, "tx_id": "fake-tx"}
        return {"ok": False, "error": "season not over"}

    server.view = fake_view
    server.commons_settle_room = fake_settle
    server.near_call = fake_call
    try:
        return server.commons_finalize_room(9), calls
    finally:
        server.view = _orig_view
        server.near_call = _orig_call
        server.commons_settle_room = _orig_settle


# Season over with a 2-epoch backlog: settle twice (second call
# finds nothing due), then finalize once, pre-settled reported.
res, calls = _run_case(
    False, True,
    [{"ok": True, "settled": [0, 1]}, {"ok": True, "settled": []}])
assert res["ok"] is True
assert res["pre_settled"] == [0, 1]
assert res["pre_settled_count"] == 2
assert calls == {"settle": 2, "finalize": 1}

# Pre-settle failure with nothing settled: finalize is never
# attempted, the settle error is the result.
res, calls = _run_case(
    False, True,
    [{"ok": False, "error": "settle boom", "settled": []}])
assert res["ok"] is False
assert calls == {"settle": 1, "finalize": 0}

# Already ended: no pre-settle, straight to finalize.
res, calls = _run_case(True, True, [])
assert res["ok"] is True
assert "pre_settled" not in res
assert calls == {"settle": 0, "finalize": 1}

# Mid-season: pre-settle skipped, finalize fails as before.
res, calls = _run_case(False, False, [], call_ok=False)
assert res["ok"] is False
assert calls == {"settle": 0, "finalize": 1}

print("residue tests passed")
