#!/usr/bin/env python3
"""The Commons smoke test: one full epoch on testnet with two feeders.

Sponsor: humanmarkfund1107.testnet. Feeders: judge1 (greedy) and judge2
(restrained). Security: exam1, exam2, exam3. All sign via the near CLI
keychain. Views go through the FastNEAR RPC mirror.
"""
import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.request

NEAR = "/home/hatch/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near"
CONTRACT = "commons.bindingvow1106.testnet"
RPC = "https://rpc.testnet.fastnear.com"
SPONSOR = "humanmarkfund1107.testnet"
FEEDER_A = "judge1.bindingvow1106.testnet"  # greedy
FEEDER_B = "judge2.bindingvow1106.testnet"  # restrained
SECURITY = [
    "exam1.bindingvow1106.testnet",
    "exam2.bindingvow1106.testnet",
    "exam3.bindingvow1106.testnet",
]
STAKE = "5000000000000000000000"  # 0.005 NEAR


def call(signer, fn, args, deposit="0 NEAR", gas="30 TeraGas"):
    cmd = [
        NEAR, "contract", "call-function", "as-transaction", CONTRACT, fn,
        "json-args", json.dumps(args),
        "prepaid-gas", gas,
        "attached-deposit", deposit,
        "sign-as", signer,
        "network-config", "testnet",
        "sign-with-keychain", "send",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    if "Error" in text or out.returncode != 0:
        raise RuntimeError(f"{fn} by {signer} failed:\n{text[-1500:]}")
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
    req = urllib.request.Request(
        RPC, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    raw = bytes(data["result"]["result"])
    return json.loads(raw) if raw else None


def wait_phase(room_id, epoch, phase, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        info = view("current_epoch", {"room_id": room_id})
        if info["epoch"] == epoch and info["phase"] == phase:
            return info
        if info["epoch"] > epoch or info["season_over"]:
            raise RuntimeError(f"missed {phase} of epoch {epoch}: {info}")
        time.sleep(2)
    raise RuntimeError(f"timeout waiting for epoch {epoch} {phase}")


def reasoning_record(room_id, epoch, agent, strategy, text, amount):
    record = {
        "room_id": room_id,
        "epoch": epoch,
        "agent": agent,
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pool_observed": view("get_room", {"room_id": room_id})["pool_balance"],
        "reserve_observed": view("get_room", {"room_id": room_id})["refill_reserve"],
        "others_last_epoch": [],
        "strategy": strategy,
        "reasoning": text,
        "intended_amount": amount,
    }
    canonical = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    return record, hashlib.sha256(canonical.encode()).hexdigest()


def main():
    print("1. create_room", flush=True)
    call(SPONSOR, "create_room", {
        "name": "smoke-room", "stake": STAKE, "capacity": 10,
        "seed": "80000000000000000000000",
        "refill_per_epoch": "4000000000000000000000",
        "cap_bps": 1000, "epoch_secs": 60, "season_epochs": 3,
        "data_exclusivity_days": 30,
    }, deposit="0.09 NEAR")
    room_id = 0
    room = view("get_room", {"room_id": room_id})
    print(f"  room: pool={room['pool_balance']} reserve={room['refill_reserve']}", flush=True)
    assert room["pool_balance"] == "16000000000000000000000", room
    assert room["refill_reserve"] == "64000000000000000000000", room

    print("2. register feeders and security", flush=True)
    call(FEEDER_A, "register_agent", {"room_id": room_id}, deposit="0.005 NEAR")
    call(FEEDER_B, "register_agent", {"room_id": room_id}, deposit="0.005 NEAR")
    for s in SECURITY:
        call(s, "register_security", {"room_id": room_id}, deposit="0.005 NEAR")
    room = view("get_room", {"room_id": room_id})
    assert room["feeders_count"] == 2 and room["security_count"] == 3, room
    print(f"  feeders={room['feeders_count']} security={room['security_count']}", flush=True)

    print("3. epoch 1 commits (greedy vs restrained)", flush=True)
    wait_phase(room_id, 1, "commit")
    plans = {}
    for agent, amount, strategy, text in [
        (FEEDER_A, "50000000000000000000000", "greedy",
         "Take the maximum the cap allows while the pool is young."),
        (FEEDER_B, "2000000000000000000000", "restrained",
         "Take a small sip so the pool survives the season."),
    ]:
        record, rhash = reasoning_record(room_id, 1, agent, strategy, text, amount)
        salt = f"salt-{agent.split('.')[0]}-e1"
        commitment = hashlib.sha256(f"{amount}|{rhash}|{salt}".encode()).hexdigest()
        path = f"/home/hatch/workspace/the-commons/demo-logs/room0-epoch1-{agent.split('.')[0]}.json"
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
        plans[agent] = (amount, rhash, salt, path)
        call(agent, "commit", {"room_id": room_id, "commitment": commitment})

    print("4. settle epoch 0 (empty) while waiting", flush=True)
    call(SPONSOR, "settle_epoch", {"room_id": room_id, "epoch": 0})

    print("5. epoch 1 reveals", flush=True)
    wait_phase(room_id, 1, "reveal")
    for agent, (amount, rhash, salt, path) in plans.items():
        call(agent, "reveal", {
            "room_id": room_id, "amount": amount, "reasoning_hash": rhash,
            "log_ref": path.split("/")[-1], "salt": salt,
        })

    print("6. settle epoch 1 and read back", flush=True)
    wait_phase(room_id, 2, "commit")
    call(SPONSOR, "settle_epoch", {"room_id": room_id, "epoch": 1})
    epoch = view("get_epoch", {"room_id": room_id, "epoch": 1})
    room = view("get_room", {"room_id": room_id})
    print(json.dumps(epoch, indent=2), flush=True)
    print(f"  pool_after={room['pool_balance']} total_paid_out={room['total_paid_out']}", flush=True)

    # Expected: snapshot at epoch 1 settle = 0.016 + 0.004 (e0 refill)
    # + 0.004 (e1 refill) = 0.024 NEAR. Cap 10% = 0.0024. Greedy asked
    # 0.05 -> paid 0.0024. Restrained asked 0.002 -> paid 0.002.
    paid = {r["account"]: r["paid"] for r in epoch["reveals"]}
    assert paid[FEEDER_A] == "2400000000000000000000", paid
    assert paid[FEEDER_B] == "2000000000000000000000", paid
    assert epoch["pool_after"] == "19600000000000000000000", epoch
    print("SMOKE TEST PASSED", flush=True)


if __name__ == "__main__":
    main()
