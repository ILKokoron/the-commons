#!/usr/bin/env python3
"""The Commons demo agent runner.

One process per agent account. The runner talks to the game ONLY
through The Commons MCP server over stdio (the same door an external
operator's agent uses), which proves the MCP is a real participation
door and not an internal shortcut.

Usage:
  COMMONS_ACCOUNT_ID=judge1.bindingvow1106.testnet \
    python3 runner.py --room 1 --strategy greedy

Strategies:
  greedy     request an amount so large the cap always binds
  restrained request refill_per_epoch divided by the feeder count
  adaptive   greedy while the pool is healthy, restrained once the
             pool falls under 60 percent of its starting level
  random     uniform between 0 and the estimated cap (control group)

Loop (per SCHEME section 7): poll commons_current_epoch; commit in
the first half of the epoch; reveal in the second half; settle any
finished epoch nobody has settled yet (keeper behaviour); stop when
the season is over and every epoch is settled.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "mcp", "server.py")
HUGE = str(10**27)  # 1000 NEAR in yocto: the cap always binds


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def decide(strategy, room, epoch):
    """Return (amount_yocto_str, reasoning_text)."""
    pool = int(room["pool_balance"])
    reserve = int(room["refill_reserve"])
    refill = int(room["refill_per_epoch"])
    feeders = max(1, int(room["feeders_count"]))
    cap_bps = int(room["cap_bps"])
    total_paid = int(room["total_paid_out"])
    if strategy == "greedy":
        return HUGE, ("Pool is there to drink. Requesting far above the "
                      "cap and taking whatever the cap allows.")
    if strategy == "restrained":
        amt = refill // feeders
        return str(amt), (f"Sipping exactly my share of the refill "
                          f"({refill} yocto split by {feeders} feeders) "
                          "so the pool outlives the season.")
    if strategy == "adaptive":
        seed_est = pool + reserve + total_paid
        initial_pool = seed_est // 5  # pool starts at 20 percent of seed
        if initial_pool > 0 and pool * 5 < initial_pool * 3:
            amt = refill // feeders
            return str(amt), (f"Pool is thin ({pool} yocto, under 60 "
                              "percent of its starting level). Switching "
                              "to a restrained sip of the refill share.")
        return HUGE, (f"Pool is healthy ({pool} yocto). Drinking deep "
                      "while it lasts.")
    if strategy == "random":
        cap_est = (pool + refill) * cap_bps // 10000
        amt = random.randint(0, max(0, cap_est))
        return str(amt), (f"Control group: drew a uniform random amount "
                          f"between 0 and the estimated cap {cap_est}.")
    raise ValueError(f"unknown strategy {strategy}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, required=True)
    ap.add_argument("--strategy", required=True,
                    choices=["greedy", "restrained", "adaptive", "random"])
    ap.add_argument("--account", default=os.environ.get("COMMONS_ACCOUNT_ID", ""))
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args()
    if not args.account:
        print("COMMONS_ACCOUNT_ID (or --account) is required", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["COMMONS_ACCOUNT_ID"] = args.account
    params = StdioServerParameters(
        command=sys.executable, args=[SERVER], env=env)

    committed = set()
    revealed = set()
    started = time.time()

    async def call(session, tool, arguments):
        result = await session.call_tool(tool, arguments)
        texts = [c.text for c in result.content if getattr(c, "text", None)]
        if not texts:
            return {"ok": False, "error": "empty tool result"}
        try:
            return json.loads(texts[0])
        except Exception:
            return {"ok": False, "error": f"unparseable tool result: {texts[0][:200]}"}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log(f"runner up: account={args.account} room={args.room} "
                f"strategy={args.strategy}")
            while True:
                try:
                    info_r = await call(session, "commons_current_epoch",
                                        {"room_id": args.room})
                    room_r = await call(session, "commons_get_room",
                                        {"room_id": args.room})
                    if not info_r.get("ok") or not room_r.get("ok"):
                        log(f"read failed: {info_r} {room_r}")
                        await asyncio.sleep(args.poll)
                        continue
                    info = info_r["info"]
                    room = room_r["room"]
                    epoch = int(info["epoch"])
                    season_epochs = int(room["season_epochs"])
                    season_over = bool(info["season_over"])
                    next_settle = int(room["next_epoch_to_settle"])

                    # keeper: settle every finished, unsettled epoch
                    last_finished = season_epochs - 1 if season_over else epoch - 1
                    for e in range(next_settle, last_finished + 1):
                        res = await call(session, "commons_settle",
                                         {"room_id": args.room, "epoch": e})
                        if res.get("ok"):
                            rec = res.get("epoch_record") or {}
                            log(f"settled epoch {e}: total_paid="
                                f"{rec.get('total_paid')} pool_after="
                                f"{rec.get('pool_after')}")
                        else:
                            log(f"settle epoch {e} not done by me: "
                                f"{res.get('error', '')[:120]}")
                            break

                    if season_over and next_settle >= season_epochs:
                        log("season over and fully settled; runner done")
                        break
                    if time.time() - started > (season_epochs + 3) * int(room["epoch_secs"]) + 300:
                        log("safety timeout reached; runner stopping")
                        break
                    if season_over:
                        await asyncio.sleep(args.poll)
                        continue

                    if info["phase"] == "commit" and epoch not in committed:
                        amount, text = decide(args.strategy, room, epoch)
                        res = await call(session, "commons_commit", {
                            "room_id": args.room, "amount": amount,
                            "reasoning": text, "strategy": args.strategy})
                        if res.get("ok"):
                            committed.add(epoch)
                            log(f"epoch {epoch}: committed amount={amount} "
                                f"tx={res.get('tx_id')}")
                        else:
                            log(f"epoch {epoch}: commit failed: "
                                f"{res.get('error', '')[:160]}")
                    elif info["phase"] == "reveal" and epoch in committed \
                            and epoch not in revealed:
                        res = await call(session, "commons_reveal",
                                         {"room_id": args.room})
                        if res.get("ok"):
                            revealed.add(epoch)
                            log(f"epoch {epoch}: revealed tx={res.get('tx_id')}")
                        else:
                            log(f"epoch {epoch}: reveal failed: "
                                f"{res.get('error', '')[:160]}")
                except Exception as e:
                    log(f"loop error: {e}")
                await asyncio.sleep(args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
