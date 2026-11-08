#!/usr/bin/env python3
"""Full live smoke for The Commons v3, second attempt with long
epochs (300 s) so the commit window is comfortable. Creates a fresh
room on commonsv3.bindingvow1106.testnet through the MCP, seats
3 security and 2 feeders, starts it, plays 3 epochs (feeder A
judge1 requests 0 every epoch, feeder B judge2 requests 0.002),
settles, finalizes and withdraws residue, verifying every v3
economics leg by chain read-back and exact balance deltas."""
import json
import os
import subprocess
import threading
import time

BASE = "/home/hatch/workspace/the-commons"
CLIENT = [f"{BASE}/venv/bin/python", f"{BASE}/mcp/client.py"]
CONTRACT = "commonsv3.bindingvow1106.testnet"
SPONSOR = "exam4.bindingvow1106.testnet"
A = "judge1.bindingvow1106.testnet"
B = "judge2.bindingvow1106.testnet"
SECS = ["exam1.bindingvow1106.testnet", "exam2.bindingvow1106.testnet",
        "exam3.bindingvow1106.testnet"]
OWNER = "bindingvow1106.testnet"
G = 63300000000000000000
EPS = 1000000000000000000000
RETAINER = 2000000000000000000000
GF = 1424250000000000000000
RPCS = ["https://rpc.testnet.fastnear.com", "https://rpc.testnet.near.org"]
FAIL = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f" {extra}" if extra else ""),
          flush=True)
    if not cond:
        FAIL.append(name)


def mcp(account, tool, args):
    env = dict(os.environ)
    env["COMMONS_CONTRACT"] = CONTRACT
    env["COMMONS_RECORDS_DIR"] = f"{BASE}/records-v3-smoke2"
    env.pop("COMMONS_STORE_URL", None)
    env.pop("COMMONS_STORE_TOKEN", None)
    if account:
        env["COMMONS_ACCOUNT_ID"] = account
    else:
        env.pop("COMMONS_ACCOUNT_ID", None)
    out = subprocess.run(CLIENT + [tool, json.dumps(args)], env=env,
                         capture_output=True, text=True, timeout=300)
    text = out.stdout
    return json.loads(text[text.find("{"):])


def rpc_view(fn, args):
    import base64
    payload = {"jsonrpc": "2.0", "id": "1", "method": "query",
               "params": {"request_type": "call_function", "finality": "final",
                          "account_id": CONTRACT, "method_name": fn,
                          "args_base64": base64.b64encode(
                              json.dumps(args).encode()).decode()}}
    last = None
    for url in RPCS:
        try:
            out = subprocess.run(["curl", "-s", "-m", "20", url,
                                  "-H", "Content-Type: application/json",
                                  "-d", json.dumps(payload)],
                                 capture_output=True, text=True, timeout=30)
            return json.loads(bytes(json.loads(out.stdout)["result"]["result"]))
        except Exception as e:
            last = e
    raise last


def balance(acct):
    payload = {"jsonrpc": "2.0", "id": "1", "method": "query",
               "params": {"request_type": "view_account", "finality": "final",
                          "account_id": acct}}
    for url in RPCS:
        try:
            out = subprocess.run(["curl", "-s", "-m", "20", url,
                                  "-H", "Content-Type: application/json",
                                  "-d", json.dumps(payload)],
                                 capture_output=True, text=True, timeout=30)
            return int(json.loads(out.stdout)["result"]["amount"])
        except Exception:
            pass
    raise RuntimeError(f"balance read failed for {acct}")


def cur(room):
    return rpc_view("current_epoch", {"room_id": room})


def wait_for(room, pred, desc, deadline_s=700):
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        info = cur(room)
        if pred(info):
            return info
        time.sleep(1)
    raise SystemExit(f"timeout waiting: {desc}")


def main():
    owner_before = balance(OWNER)
    res = mcp(SPONSOR, "commons_create_room", {
        "name": "v3-smoke-2", "stake": "0.001", "capacity": 5, "seed": "0.02",
        "refill_per_epoch": "0.001", "cap_bps": 5000, "epoch_secs": 300,
        "season_epochs": 3, "data_exclusivity_days": 30, "regime": "Wild",
    })
    check("create ok", res.get("ok") is True, json.dumps(res)[:200])
    room_id = res["room_id"]
    print("room_id:", room_id, flush=True)
    check("mcp guarantee_fund", res.get("guarantee_fund") == str(GF),
          res.get("guarantee_fund", "missing"))
    room = rpc_view("get_room", {"room_id": room_id})
    check("room gf fields", room["guarantee_fund"] == str(GF)
          and room["gf_remaining"] == str(GF))
    check("retainer reserve is whole fee",
          room["retainer_reserve"] == "10000000000000000000000")
    check("owner got 0 at create", balance(OWNER) - owner_before == 0)
    for s in SECS:
        r = mcp(s, "commons_register_security", {"room_id": room_id})
        check(f"register security {s}", r.get("ok") is True)
    for f in (A, B):
        r = mcp(f, "commons_register_agent", {"room_id": room_id})
        check(f"register feeder {f}", r.get("ok") is True)
    r = mcp(SPONSOR, "commons_start_room", {"room_id": room_id})
    check("start room", r.get("ok") is True, json.dumps(r)[:200])

    played = {A: 0, B: 0}
    for e in range(3):
        wait_for(room_id, lambda i: i["epoch"] == e and i["phase"] == "commit"
                 and i["seconds_into_epoch"] <= 30, f"commit window e{e}")
        results = {}

        def do_commit(acct, amount):
            results[acct] = mcp(acct, "commons_commit",
                                {"room_id": room_id, "amount": str(amount),
                                 "reasoning": f"v3 smoke2 epoch {e}"})
        t1 = threading.Thread(target=do_commit, args=(A, 0))
        t2 = threading.Thread(target=do_commit,
                              args=(B, 2000000000000000000000))
        t1.start(); t2.start(); t1.join(); t2.join()
        for acct in (A, B):
            check(f"e{e} commit {acct}", results[acct].get("ok") is True,
                  json.dumps(results[acct])[:200])
        wait_for(room_id, lambda i: i["epoch"] == e and i["phase"] == "reveal",
                 f"reveal phase e{e}")
        for acct in (A, B):
            r = mcp(acct, "commons_reveal", {"room_id": room_id, "epoch": e})
            check(f"e{e} reveal {acct}", r.get("ok") is True,
                  json.dumps(r)[:200])
            if r.get("ok"):
                played[acct] += 1
        wait_for(room_id, lambda i: i["epoch"] > e or i.get("season_over"),
                 f"epoch {e} end")
        r = mcp(SPONSOR, "commons_settle", {"room_id": room_id, "epoch": e})
        check(f"e{e} settle", r.get("ok") is True, json.dumps(r)[:200])
        ep = rpc_view("get_epoch", {"room_id": room_id, "epoch": e})
        print(f"  e{e} epoch record: total_paid={ep.get('total_paid')} "
              f"pool_after={ep.get('pool_after')}", flush=True)
    print("played reveals:", played, flush=True)
    check("A revealed 3", played[A] == 3)
    check("B revealed 3", played[B] == 3)

    ga = rpc_view("get_agent", {"room_id": room_id, "account_id": A})
    gb = rpc_view("get_agent", {"room_id": room_id, "account_id": B})
    print("agent A:", {k: ga[k] for k in ("epochs_revealed", "total_withdrawn")},
          flush=True)
    print("agent B:", {k: gb[k] for k in ("epochs_revealed", "total_withdrawn")},
          flush=True)
    check("A epochs_revealed 3", ga["epochs_revealed"] == 3)
    check("A withdrawn 0", ga["total_withdrawn"] == "0")
    check("B epochs_revealed 3", gb["epochs_revealed"] == 3)
    check("B withdrawn 0.0055", gb["total_withdrawn"] == "5500000000000000000000",
          gb["total_withdrawn"])

    bals = {a: balance(a) for a in [A, B, OWNER] + SECS}
    room = rpc_view("get_room", {"room_id": room_id})
    print("room before finalize:", {k: room[k] for k in
          ("pool_balance", "refill_reserve", "gf_remaining",
           "retainer_reserve", "total_paid_out")}, flush=True)
    r = mcp(SPONSOR, "commons_finalize_room", {"room_id": room_id})
    check("finalize", r.get("ok") is True, json.dumps(r)[:200])
    print("finalize tx:", r.get("tx_id"), flush=True)
    after = {a: balance(a) for a in [A, B, OWNER] + SECS}
    exp_topup_a = 3 * G + EPS
    check("A topup exact", after[A] - bals[A] == exp_topup_a,
          f"delta={after[A]-bals[A]} expected={exp_topup_a}")
    check("B topup zero", after[B] - bals[B] == 0,
          f"delta={after[B]-bals[B]}")
    for s in SECS:
        check(f"retainer {s}", after[s] - bals[s] == RETAINER,
              f"delta={after[s]-bals[s]}")
    check("owner leftover reserve 0.004",
          after[OWNER] - bals[OWNER] == 4000000000000000000000,
          f"delta={after[OWNER]-bals[OWNER]}")
    room = rpc_view("get_room", {"room_id": room_id})
    check("gf_remaining after finalize",
          room["gf_remaining"] == "234350000000000000000",
          room["gf_remaining"])
    sp_before = balance(SPONSOR)
    r = mcp(SPONSOR, "commons_withdraw_residue", {"room_id": room_id})
    check("withdraw residue", r.get("ok") is True, json.dumps(r)[:200])
    print("residue tx:", r.get("tx_id"), flush=True)
    sp_after = balance(SPONSOR)
    print(f"sponsor residue delta (net of gas): {sp_after - sp_before} "
          f"expected gross 14734350000000000000000", flush=True)
    room = rpc_view("get_room", {"room_id": room_id})
    print("room after:", {k: room[k] for k in
          ("pool_balance", "refill_reserve", "gf_remaining", "ended",
           "total_paid_out")}, flush=True)
    check("room ended", room["ended"] is True)
    print("FAILURES:", FAIL if FAIL else "none", flush=True)
    print("SMOKE2 DONE", flush=True)


if __name__ == "__main__":
    main()
