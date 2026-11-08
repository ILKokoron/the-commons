#!/usr/bin/env python3
"""The Commons smoke test, resumable: reuses room 0 if its season is
still alive, otherwise creates a fresh room with longer epochs."""
import base64
import hashlib
import json
import subprocess
import time
import urllib.request

NEAR = "/home/hatch/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near"
CONTRACT = "commons.bindingvow1106.testnet"
RPCS = ["https://rpc.testnet.fastnear.com", "https://rpc.testnet.near.org"]
SPONSOR = "humanmarkfund1107.testnet"
FEEDER_A = "judge1.bindingvow1106.testnet"
FEEDER_B = "judge2.bindingvow1106.testnet"
SECURITY = [
    "exam1.bindingvow1106.testnet",
    "exam2.bindingvow1106.testnet",
    "exam3.bindingvow1106.testnet",
]
STAKE = "5000000000000000000000"
YOCTO = 10**24


def call(signer, fn, args, deposit="0 NEAR", gas="30 TeraGas"):
    cmd = [
        NEAR, "contract", "call-function", "as-transaction", CONTRACT, fn,
        "json-args", json.dumps(args), "prepaid-gas", gas,
        "attached-deposit", deposit, "sign-as", signer,
        "network-config", "testnet", "sign-with-keychain", "send",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    if "Error:" in text or out.returncode != 0:
        raise RuntimeError(f"{fn} by {signer} failed:\n{text[-1200:]}")
    txid = ""
    for line in text.splitlines():
        if "Transaction ID" in line:
            txid = line.split(":")[-1].strip()
    print(f"  ok {fn} by {signer} tx={txid}", flush=True)
    return txid


def view(fn, args):
    payload = {
        "jsonrpc": "2.0", "id": "1", "method": "query",
        "params": {
            "request_type": "call_function", "finality": "final",
            "account_id": CONTRACT, "method_name": fn,
            "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
        },
    }
    last = None
    for attempt in range(6):
        rpc = RPCS[attempt % len(RPCS)]
        try:
            req = urllib.request.Request(
                rpc, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            raw = bytes(data["result"]["result"])
            return json.loads(raw) if raw else None
        except Exception as e:  # transient RPC failures are normal
            last = e
            time.sleep(1.5)
    raise RuntimeError(f"view {fn} failed: {last}")


def wait_until(pred, desc, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
        info = view("current_epoch", {"room_id": ROOM_ID})
        if pred(info):
            return info
        time.sleep(2)
    raise RuntimeError(f"timeout waiting for {desc}")


def reasoning_record(room_id, epoch, agent, strategy, text, amount):
    room = view("get_room", {"room_id": room_id})
    record = {
        "room_id": room_id, "epoch": epoch, "agent": agent,
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pool_observed": room["pool_balance"],
        "reserve_observed": room["refill_reserve"],
        "others_last_epoch": [], "strategy": strategy,
        "reasoning": text, "intended_amount": amount,
    }
    canonical = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    return record, hashlib.sha256(canonical.encode()).hexdigest()


# ---- pick or create the room -------------------------------------------
cfg = view("get_config", {})
ROOM_ID = None
EPOCH_SECS = 60
for rid in range(cfg["rooms_created"]):
    r = view("get_room", {"room_id": rid})
    info = view("current_epoch", {"room_id": rid})
    if not r["ended"] and not info["season_over"] and r["name"] == "smoke-room":
        ROOM_ID = rid
        EPOCH_SECS = r["epoch_secs"]
        print(f"resuming room {rid} at epoch {info['epoch']} phase {info['phase']}", flush=True)
        break
if ROOM_ID is None:
    print("creating a fresh room (epoch_secs=120)", flush=True)
    call(SPONSOR, "create_room", {
        "name": "smoke-room", "stake": STAKE, "capacity": 10,
        "seed": "80000000000000000000000",
        "refill_per_epoch": "4000000000000000000000",
        "cap_bps": 1000, "epoch_secs": 120, "season_epochs": 3,
        "data_exclusivity_days": 30,
    }, deposit="0.09 NEAR")
    ROOM_ID = view("get_config", {})["rooms_created"] - 1
    EPOCH_SECS = 120

room = view("get_room", {"room_id": ROOM_ID})
print(f"room {ROOM_ID}: pool={room['pool_balance']} reserve={room['refill_reserve']}", flush=True)

# ---- register ------------------------------------------------------------
for agent, fn in [(FEEDER_A, "register_agent"), (FEEDER_B, "register_agent")] + \
                 [(s, "register_security") for s in SECURITY]:
    existing = view("get_agent", {"room_id": ROOM_ID, "account": agent})
    if existing:
        print(f"  already registered: {agent}", flush=True)
    else:
        call(agent, fn, {"room_id": ROOM_ID}, deposit="0.005 NEAR")
room = view("get_room", {"room_id": ROOM_ID})
assert room["feeders_count"] == 2 and room["security_count"] == 3, room
print(f"registered: feeders={room['feeders_count']} security={room['security_count']}", flush=True)

# ---- choose the play epoch ------------------------------------------------
info = view("current_epoch", {"room_id": ROOM_ID})
if info["phase"] == "commit" and info["seconds_into_epoch"] <= EPOCH_SECS // 6:
    PLAY = info["epoch"]
else:
    PLAY = info["epoch"] + 1
assert PLAY <= 2, f"no playable epoch left: {info}"
print(f"playing epoch {PLAY}", flush=True)

# ---- commits ---------------------------------------------------------------
wait_until(lambda i: i["epoch"] == PLAY and i["phase"] == "commit", "commit phase")
plans = {}
for agent, amount, strategy, text in [
    (FEEDER_A, "50000000000000000000000", "greedy",
     "Take the maximum the cap allows while the pool is young."),
    (FEEDER_B, "2000000000000000000000", "restrained",
     "Take a small sip so the pool survives the season."),
]:
    record, rhash = reasoning_record(ROOM_ID, PLAY, agent, strategy, text, amount)
    salt = f"salt-{agent.split('.')[0]}-e{PLAY}"
    commitment = hashlib.sha256(f"{amount}|{rhash}|{salt}".encode()).hexdigest()
    path = f"/home/hatch/workspace/the-commons/demo-logs/room{ROOM_ID}-epoch{PLAY}-{agent.split('.')[0]}.json"
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    plans[agent] = (amount, rhash, salt, path)
    call(agent, "commit", {"room_id": ROOM_ID, "commitment": commitment})

# ---- settle earlier epochs --------------------------------------------------
room = view("get_room", {"room_id": ROOM_ID})
for e in range(room["next_epoch_to_settle"], PLAY):
    call(SPONSOR, "settle_epoch", {"room_id": ROOM_ID, "epoch": e})

# ---- reveals ------------------------------------------------------------------
wait_until(lambda i: i["epoch"] == PLAY and i["phase"] == "reveal", "reveal phase")
for agent, (amount, rhash, salt, path) in plans.items():
    call(agent, "reveal", {
        "room_id": ROOM_ID, "amount": amount, "reasoning_hash": rhash,
        "log_ref": path.split("/")[-1], "salt": salt,
    })

# ---- settle the play epoch and verify -----------------------------------------
wait_until(lambda i: i["epoch"] > PLAY or i["season_over"], "epoch end")
call(SPONSOR, "settle_epoch", {"room_id": ROOM_ID, "epoch": PLAY})
epoch = view("get_epoch", {"room_id": ROOM_ID, "epoch": PLAY})
room = view("get_room", {"room_id": ROOM_ID})
print(json.dumps(epoch, indent=2), flush=True)

seed_pool = 16 * 10**21          # 0.016 NEAR initial pool
refill = 4 * 10**21              # 0.004 NEAR per settled epoch
snapshot = seed_pool + refill * (PLAY + 1)
cap = snapshot * 1000 // 10000
expected_a = min(50 * 10**21, cap)
expected_b = min(2 * 10**21, cap)
paid = {r["account"]: int(r["paid"]) for r in epoch["reveals"]}
assert paid[FEEDER_A] == expected_a, (paid, expected_a)
assert paid[FEEDER_B] == expected_b, (paid, expected_b)
assert int(epoch["pool_after"]) == snapshot - expected_a - expected_b, epoch
print(f"pool_after={room['pool_balance']} total_paid_out={room['total_paid_out']}", flush=True)
print("SMOKE TEST PASSED", flush=True)
