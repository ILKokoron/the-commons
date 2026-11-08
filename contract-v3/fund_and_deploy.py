#!/usr/bin/env python3
"""Create and deploy The Commons v3 at commonsv3.bindingvow1106.testnet,
verifying state after every step.

Funding plan (single source, per the approved v3 run):
- FIRST, a human or browser task sends testnet NEAR to
  bindingvow1106.testnet from the NEAR Docs faucet
  (https://docs.near.org/getting-started/faucet, account id field
  only, no login). This script refuses to move anything until the
  parent holds at least 4 NEAR; it never touches the v1 contract
  account (room 2 is live on it) and never drains the thin operator
  accounts.
- bindingvow1106.testnet then creates the subaccount with 3.3 NEAR.
  The cargo-near wasm is 234,754 bytes (about 2.35 NEAR of code
  storage), so 3.3 NEAR leaves the new account roughly 0.9 NEAR of
  operating headroom above its storage cost.
- Deploy with init (owner = parent, room_fee = 0.01 NEAR) and
  verify get_config by read-back, including the three v3 economics
  parameters.
"""
import base64
import json
import subprocess
import time
import urllib.request

NEAR = "/home/hatch/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near"
NEW = "commonsv3.bindingvow1106.testnet"
PARENT = "bindingvow1106.testnet"
WASM = "/home/hatch/workspace/the-commons/contract-v3/target/near/the_commons_v3.wasm"
RPCS = ["https://rpc.testnet.fastnear.com", "https://rpc.testnet.near.org"]
Y = 10**24


def run(cmd, desc):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    text = out.stdout + out.stderr
    if out.returncode != 0 or "Error:" in text:
        raise RuntimeError(f"{desc} failed:\n{text[-1500:]}")
    txid = ""
    for line in text.splitlines():
        if "Transaction ID" in line:
            txid = line.split(":")[-1].strip()
    print(f"  ok {desc} tx={txid}", flush=True)
    return txid


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
    raise RuntimeError(f"rpc {method} failed: {last}")


def balance(account):
    data = rpc("query", {"request_type": "view_account", "finality": "final",
                         "account_id": account})
    if "error" in data:
        return None
    return int(data["result"]["amount"])


def view(fn, args):
    data = rpc("query", {
        "request_type": "call_function", "finality": "final",
        "account_id": NEW, "method_name": fn,
        "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
    })
    raw = bytes(data["result"]["result"])
    return json.loads(raw) if raw else None


txids = {}

# 0. Funding gate: the faucet must have landed first.
bal = balance(PARENT)
print(f"parent balance {bal / Y:.4f} NEAR", flush=True)
if bal < 4 * Y:
    raise SystemExit(
        "STOP: bindingvow1106.testnet holds less than 4 NEAR. Run the "
        "NEAR Docs faucet first (https://docs.near.org/getting-started/"
        "faucet, account id bindingvow1106.testnet, Request Tokens), "
        "verify the balance by RPC, then re-run this script. No funds "
        "were moved."
    )
if balance(NEW) is not None:
    raise SystemExit(f"STOP: {NEW} already exists; refusing to recreate it.")

# 1. Create the subaccount, funded by the parent alone.
txids["create_account"] = run(
    [NEAR, "account", "create-account", "fund-myself", NEW, "3.3 NEAR",
     "autogenerate-new-keypair", "save-to-keychain", "sign-as", PARENT,
     "network-config", "testnet", "sign-with-keychain", "send"],
    f"create {NEW} with 3.3 NEAR",
)
bal = balance(NEW)
print(f"new account balance {bal / Y:.4f} NEAR", flush=True)
assert bal is not None and bal > int(3.29 * Y), bal

# 2. Deploy and init.
txids["deploy"] = run(
    [NEAR, "contract", "deploy", NEW, "use-file", WASM,
     "with-init-call", "new",
     "json-args", json.dumps({"owner": PARENT, "room_fee": str(Y // 100)}),
     "prepaid-gas", "30 TeraGas", "attached-deposit", "0 NEAR",
     "network-config", "testnet", "sign-with-keychain", "send"],
    "deploy + init commons v3",
)

# 3. Verify state by read-back (a tx id alone proves nothing).
cfg = view("get_config", {})
print(f"get_config: {cfg}", flush=True)
assert cfg["owner"] == PARENT, cfg
assert cfg["room_fee"] == str(Y // 100), cfg
assert cfg["rooms_created"] == 0, cfg
assert cfg["gas_per_epoch"] == "63300000000000000000", cfg
assert cfg["topup_eps"] == "1000000000000000000000", cfg
assert cfg["retainer_per_security"] == "2000000000000000000000", cfg
bal = balance(NEW)
print(f"new account balance after deploy {bal / Y:.4f} NEAR", flush=True)
print("TXIDS " + json.dumps(txids), flush=True)
print("DEPLOY VERIFIED", flush=True)
