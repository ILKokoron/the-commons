#!/usr/bin/env python3
"""Live smoke for The Commons v3 on commonsv3.bindingvow1106.testnet.

One clean room, every number verified by chain read-back or by
balance deltas measured across the exact call that moves the money
(accounts that do not sign in that window have exact deltas).

Room: Wild, stake 0.001, capacity 5, seed 0.02, refill 0.001,
cap 50 percent, 3 epochs of 120 s. Sponsor exam4 (topped up from
the faucet-funded parent first if it cannot cover the deposit plus
the CLI upfront). Security exam1, exam2, exam3 (clean season, no
reports). Feeder A judge1 requests 0 every epoch (the dust case).
Feeder B judge2 requests 0.002 every epoch.

Expected math (yocto-exact):
- GF = 5 x 3 x 63300000000000000000 x 3 / 2 = 1424250000000000000000.
- Create deposit = fee 0.01 + seed 0.02 + GF = 31424250000000000000000.
- Retainer reserve = min(fee, 5 x 0.002) = the whole 0.01 fee, so
  the owner receives 0 at create and 0.004 at finalize (leftover
  after 3 x 0.002 retainers).
- Pool path: snapshots 0.005, 0.004, 0.003; B is paid 0.002, 0.002,
  0.0015 (total 0.0055); A is paid 0. Pool after 0.0015, reserve
  after 0.013.
- A top-up at finalize = 3 x G + eps = 1189900000000000000000,
  exactly. B top-up = 0 (payouts exceed 3 x G + eps).
- GF after finalize = 234350000000000000000.
- Residue = pool + reserve + GF remainder = 14734350000000000000000.
- Conservation: payouts 0.0055 + pool 0.0015 + reserve 0.013 = seed
  0.02; top-up + GF remainder = GF; retainers 0.006 + owner
  leftover 0.004 + owner at create 0 = fee 0.01.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = "/home/hatch/workspace/the-commons"
CLIENT = [f"{ROOT}/venv/bin/python", f"{ROOT}/mcp/client.py"]
NEAR = "/home/hatch/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near"
CONTRACT = "commonsv3.bindingvow1106.testnet"
PARENT = "bindingvow1106.testnet"  # contract owner
SPONSOR = "exam4.bindingvow1106.testnet"
SECS = [f"exam{i}.bindingvow1106.testnet" for i in (1, 2, 3)]
A = "judge1.bindingvow1106.testnet"
B = "judge2.bindingvow1106.testnet"
RECORDS = f"{ROOT}/contract-v3/records-smoke"
RPCS = ["https://rpc.testnet.fastnear.com", "https://rpc.testnet.near.org"]
Y = 10**24
G = 63300000000000000000
EPS = 10**21
RET = 2 * 10**21
FEE = 10**22
GF = 1424250000000000000000
DEPOSIT = FEE + 2 * 10**22 + GF
A_TOPUP = 3 * G + EPS
GF_AFTER = GF - A_TOPUP
B_PAID = [2 * 10**21, 2 * 10**21, 15 * 10**20]
RESIDUE = 14734350000000000000000

txids = {}


def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    last = None
    for attempt in range(6):
        url = RPCS[attempt % len(RPCS)]
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise RuntimeError(f"rpc failed: {last}")


def balance(account):
    data = rpc("query", {"request_type": "view_account", "finality": "final",
                         "account_id": account})
    return int(data["result"]["amount"])


def view(fn, args):
    data = rpc("query", {
        "request_type": "call_function", "finality": "final",
        "account_id": CONTRACT, "method_name": fn,
        "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
    })
    raw = bytes(data["result"]["result"])
    return json.loads(raw) if raw else None


def mcp(account, tool, args, expect_ok=True):
    env = dict(os.environ)
    env["COMMONS_CONTRACT"] = CONTRACT
    env["COMMONS_RECORDS_DIR"] = RECORDS
    env.pop("COMMONS_STORE_URL", None)
    env.pop("COMMONS_STORE_TOKEN", None)
    if account:
        env["COMMONS_ACCOUNT_ID"] = account
    else:
        env.pop("COMMONS_ACCOUNT_ID", None)
    out = subprocess.run(CLIENT + [tool, json.dumps(args)], env=env,
                         capture_output=True, text=True, timeout=300)
    text = out.stdout
    start = text.find("{")
    res = json.loads(text[start:])
    if expect_ok and not res.get("ok"):
        raise RuntimeError(f"{tool} as {account} failed: {json.dumps(res)[:800]}")
    if res.get("tx_id"):
        txids[f"{tool}:{account}"] = res["tx_id"]
    return res


def check(name, cond, detail=""):
    print(f"  {'ok' if cond else 'FAIL'} {name} {detail}", flush=True)
    if not cond:
        raise SystemExit(f"SMOKE FAILED at {name}: {detail}")


# 0. Preconditions: v3 deployed, sponsor funded.
cfg = view("get_config", {})
check("v3 config", cfg and cfg.get("gas_per_epoch") == str(G), json.dumps(cfg))
if balance(SPONSOR) < DEPOSIT + 45 * 10**21:
    out = subprocess.run(
        [NEAR, "tokens", PARENT, "send-near", SPONSOR, "0.05 NEAR",
         "network-config", "testnet", "sign-with-keychain", "send"],
        capture_output=True, text=True, timeout=300)
    txt = out.stdout + out.stderr
    txid = ""
    for line in txt.splitlines():
        if "Transaction ID" in line:
            txid = line.split(":")[-1].strip()
    txids["sponsor_topup"] = txid
    print(f"  sponsor topped up 0.05 NEAR from parent tx={txid}", flush=True)
check("sponsor funded", balance(SPONSOR) > DEPOSIT + 40 * 10**21,
      f"bal={balance(SPONSOR)}")

# 1. Create the room; the owner must receive fee - reserve = 0.
owner_before = balance(PARENT)
res = mcp(SPONSOR, "commons_create_room", {
    "name": "v3-smoke", "stake": "0.001", "capacity": 5, "seed": "0.02",
    "refill_per_epoch": "0.001", "cap_bps": 5000, "epoch_secs": 120,
    "season_epochs": 3, "data_exclusivity_days": 30, "regime": "Wild",
})
room_id = res["room_id"]
check("mcp guarantee_fund", res.get("guarantee_fund") == str(GF),
      res.get("guarantee_fund", "missing"))
room = view("get_room", {"room_id": room_id})
check("room gf fields", room["guarantee_fund"] == str(GF)
      and room["gf_remaining"] == str(GF), json.dumps(room)[:200])
check("retainer reserve is whole fee", room["retainer_reserve"] == str(FEE))
check("owner got 0 at create", balance(PARENT) - owner_before == 0,
      f"delta={balance(PARENT) - owner_before}")
check("pool and reserve", room["pool_balance"] == str(4 * 10**21)
      and room["refill_reserve"] == str(16 * 10**21))

# 2. Seat security and feeders, then start.
for s in SECS:
    mcp(s, "commons_register_security", {"room_id": room_id})
for f in (A, B):
    mcp(f, "commons_register_agent", {"room_id": room_id})
mcp(SPONSOR, "commons_start_room", {"room_id": room_id})
room = view("get_room", {"room_id": room_id})
check("room started", room["started"] is True)
start_ns = int(room["start_ns"])

# 3. Play the season.
def wait_phase(epoch, phase, deadline_s=240):
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        info = mcp(None, "commons_current_epoch", {"room_id": room_id})
        if info["epoch"] == epoch and info["phase"] == phase:
            return
        if info["epoch"] > epoch or info.get("season_over"):
            raise SystemExit(f"missed {phase} phase of epoch {epoch}: {info}")
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {phase} of epoch {epoch}")


for e in range(3):
    wait_phase(e, "commit")
    procs = []
    for acct, amount in ((A, "0"), (B, "0.002")):
        env = dict(os.environ)
        env["COMMONS_CONTRACT"] = CONTRACT
        env["COMMONS_RECORDS_DIR"] = RECORDS
        env["COMMONS_ACCOUNT_ID"] = acct
        env.pop("COMMONS_STORE_URL", None)
        env.pop("COMMONS_STORE_TOKEN", None)
        procs.append(subprocess.Popen(
            CLIENT + ["commons_commit", json.dumps({
                "room_id": room_id, "amount": amount,
                "reasoning": f"v3 smoke epoch {e}", "strategy": "smoke"})],
            env=env, stdout=subprocess.PIPE, text=True))
    for p in procs:
        out, _ = p.communicate(timeout=300)
        r = json.loads(out[out.find("{"):])
        check(f"epoch {e} commit", r.get("ok") is True, json.dumps(r)[:300])
        if r.get("tx_id"):
            txids[f"commit:e{e}"] = r["tx_id"]
    wait_phase(e, "reveal")
    mcp(A, "commons_reveal", {"room_id": room_id})
    mcp(B, "commons_reveal", {"room_id": room_id})
    # Wait for the epoch to end, then settle as a keeper.
    while True:
        info = mcp(None, "commons_current_epoch", {"room_id": room_id})
        if info["epoch"] > e or info.get("season_over"):
            break
        time.sleep(3)
    mcp(SECS[0], "commons_settle", {"room_id": room_id, "epoch": e})
    ep = view("get_epoch", {"room_id": room_id, "epoch": e})
    check(f"epoch {e} total paid", ep["total_paid"] == str(B_PAID[e]),
          ep["total_paid"])
    paid = {r["account"]: r["paid"] for r in ep["reveals"]}
    check(f"epoch {e} A paid 0", paid.get(A) == "0", json.dumps(paid))
    check(f"epoch {e} B paid", paid.get(B) == str(B_PAID[e]), json.dumps(paid))
    for f in (A, B):
        ag = view("get_agent", {"room_id": room_id, "account": f})
        check(f"epoch {e} {f} epochs_revealed",
              ag["epochs_revealed"] == e + 1, json.dumps(ag))

# 4. Season end: measure finalize deltas exactly.
while int(time.time() * 10**9) < start_ns + 360 * 10**9:
    time.sleep(5)
room = view("get_room", {"room_id": room_id})
check("pre-finalize gf", room["gf_remaining"] == str(GF))
check("pre-finalize pool/reserve", room["pool_balance"] == str(15 * 10**20)
      and room["refill_reserve"] == str(13 * 10**21))
actors = [A, B] + SECS + [PARENT]
before = {a: balance(a) for a in actors}
mcp(SPONSOR, "commons_finalize_room", {"room_id": room_id})
after = {a: balance(a) for a in actors}
check("A top-up exact", after[A] - before[A] == A_TOPUP,
      f"delta={after[A] - before[A]} want={A_TOPUP}")
check("B top-up zero", after[B] - before[B] == 0,
      f"delta={after[B] - before[B]}")
for s in SECS:
    check(f"{s} retainer", after[s] - before[s] == RET,
          f"delta={after[s] - before[s]}")
check("owner leftover", after[PARENT] - before[PARENT] == FEE - 3 * RET,
      f"delta={after[PARENT] - before[PARENT]}")
room = view("get_room", {"room_id": room_id})
check("gf after finalize", room["gf_remaining"] == str(GF_AFTER),
      room["gf_remaining"])
check("retainer reserve drained", room["retainer_reserve"] == "0")

# 5. Residue to the sponsor.
sponsor_before = balance(SPONSOR)
mcp(SPONSOR, "commons_withdraw_residue", {"room_id": room_id})
sponsor_after = balance(SPONSOR)
room = view("get_room", {"room_id": room_id})
check("room zeroed", room["pool_balance"] == "0"
      and room["refill_reserve"] == "0" and room["gf_remaining"] == "0")
delta = sponsor_after - sponsor_before
check("residue received (minus sponsor gas)",
      RESIDUE - 10**21 < delta <= RESIDUE, f"delta={delta} want~={RESIDUE}")

# 6. Conservation, printed in full.
paid_total = sum(B_PAID)
print(f"seed: paid {paid_total} + pool 1500000000000000000000 + reserve "
      f"13000000000000000000000 = {paid_total + 15 * 10**20 + 13 * 10**21} "
      f"(seed 20000000000000000000000)", flush=True)
print(f"gf: topup {A_TOPUP} + remainder {GF_AFTER} = {A_TOPUP + GF_AFTER} "
      f"(GF {GF})", flush=True)
print(f"fee: retainers {3 * RET} + owner leftover {FEE - 3 * RET} + owner "
      f"at create 0 = {FEE} (fee {FEE})", flush=True)
print("TXIDS " + json.dumps(txids), flush=True)
print("SMOKE VERIFIED", flush=True)
