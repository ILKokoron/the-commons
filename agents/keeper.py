#!/usr/bin/env python3
"""Standalone keeper for The Commons (Wave 3).

The keeper is the platform's settlement janitor, separate from any
player's runner: it watches one room on one contract and calls
settle_epoch for every epoch whose time has come, in order, until
the season is fully settled. Settlement is permissionless on the
contract, so the keeper's account needs nothing but a little gas;
it never commits, reveals, or touches anyone's money beyond making
due payouts land.

All timing comes from the contract's own clock (commons_current_epoch
and the room's start_ns / epoch_secs), never from the local clock:
near a season boundary the two can disagree by tens of seconds, and
the chain is the one that decides.

Usage:
  COMMONS_CONTRACT=commonsv7.bindingvow1106.testnet \
  COMMONS_ACCOUNT_ID=exam1.bindingvow1106.testnet \
  python3 agents/keeper.py --room 2 [--once] [--poll 10]

Default is daemon mode: poll, settle what is due, and exit once the
room reads ended or fully settled. --once settles everything
currently due and exits.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "..", "mcp", "client.py")
PYTHON = os.path.join(HERE, "..", "venv", "bin", "python")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable


def _near_str(yocto):
    """Format a yocto integer as an exact NEAR decimal string."""
    try:
        y = int(yocto)
    except (TypeError, ValueError):
        return "?"
    whole, frac = divmod(y, 10**24)
    if not frac:
        return f"{whole} NEAR"
    return f"{whole}.{str(frac).zfill(24).rstrip('0')} NEAR"


def call(tool, args):
    """One one-shot MCP client call. Returns the tool result dict."""
    try:
        out = subprocess.run(
            [PYTHON, CLIENT, tool, json.dumps(args)],
            capture_output=True, text=True, timeout=240,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": "client call timed out after 240s"}
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"ok": False, "error": f"unparseable client output: "
                                      f"{out.stdout[-300:]} {out.stderr[-300:]}"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, required=True)
    ap.add_argument("--once", action="store_true",
                    help="settle everything currently due, then exit")
    ap.add_argument("--poll", type=float, default=10.0)
    args = ap.parse_args()
    if not os.environ.get("COMMONS_ACCOUNT_ID"):
        print("COMMONS_ACCOUNT_ID is required (the settling account; "
              "any funded account works, settlement is permissionless)",
              file=sys.stderr)
        return 1
    contract = os.environ.get("COMMONS_CONTRACT", "(registry default)")
    log(f"keeper up: contract={contract} room={args.room} "
        f"settler={os.environ['COMMONS_ACCOUNT_ID']} once={args.once}")

    while True:
        room_res = call("commons_get_room", {"room_id": args.room})
        info_res = call("commons_current_epoch", {"room_id": args.room})
        if not room_res.get("ok") or not info_res.get("ok"):
            log(f"read failed (room ok={room_res.get('ok')}, epoch "
                f"ok={info_res.get('ok')}); retrying next poll")
            if args.once:
                return 1
            time.sleep(args.poll)
            continue
        room = room_res["room"]
        info = info_res["info"]
        nxt = int(room["next_epoch_to_settle"])
        season_epochs = int(room["season_epochs"])
        if not room["started"]:
            log("room not started yet (recruiting); nothing to settle")
            if args.once:
                return 0
            time.sleep(args.poll)
            continue
        # The contract's current epoch index: an epoch is due when
        # its index is below the current epoch (it has ended), or
        # when the season is over and it is below season_epochs.
        cur = int(info.get("epoch_raw", info["epoch"]))
        due_upto = min(cur, season_epochs) if not info["season_over"] \
            else season_epochs
        if nxt >= season_epochs:
            log(f"room fully settled ({nxt}/{season_epochs}); keeper done")
            return 0
        if nxt < due_upto:
            res = call("commons_settle", {"room_id": args.room, "epoch": nxt})
            if res.get("ok"):
                rec = res.get("epoch_record") or {}
                log(f"settled epoch {nxt}: total_paid="
                    f"{_near_str(rec.get('total_paid'))} "
                    f"tx={res.get('tx_id')}")
                # Loop immediately: more epochs may be due (catch-up).
                continue
            # Benign races: someone else settled first, or the
            # chain clock has not quite reached the epoch end.
            log(f"settle epoch {nxt} not landed: "
                f"{(res.get('error') or '')[:160]}")
            if args.once:
                return 0
            # Back off after a failed attempt instead of spinning:
            # a persistent failure (for example a storage wall)
            # must not become an unspaced loop of doomed calls.
            time.sleep(args.poll)
            continue
        if args.once:
            log(f"nothing due (next={nxt}, current epoch={cur}, "
                f"season_over={info['season_over']})")
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
