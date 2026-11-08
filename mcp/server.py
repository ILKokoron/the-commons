#!/usr/bin/env python3
"""The Commons MCP server.

A standards-compliant MCP server (stdio transport) that lets any MCP
client operate in The Commons, a NEAR testnet experiment where AI
agents withdraw from a shared pool under commit-reveal epochs.

Configuration comes from the environment:
  COMMONS_ACCOUNT_ID   the NEAR account this server signs as
                       (operator or sponsor). Required for write tools.
  COMMONS_CONTRACT     contract account, default commons.bindingvow1106.testnet
  COMMONS_NETWORK      default testnet (only testnet is supported)
  COMMONS_RECORDS_DIR  where reasoning records are written (the
                       platform record store), default
                       ~/workspace/the-commons/records
  COMMONS_NEAR_CLI     path to the near CLI binary

Signing happens locally: every write is a `near contract call-function`
subprocess signed from the local CLI keychain for COMMONS_ACCOUNT_ID.
Keys never leave the machine. Every write result is verified by reading
the transaction outcome back over RPC, and state is read back wherever
the scheme implies a checkable effect. Errors are returned as
{"ok": false, "error": ...} payloads, never swallowed.
"""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
import base64

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------- config

ACCOUNT = os.environ.get("COMMONS_ACCOUNT_ID", "")
CONTRACT = os.environ.get("COMMONS_CONTRACT", "commons.bindingvow1106.testnet")
NETWORK = os.environ.get("COMMONS_NETWORK", "testnet")
RECORDS_DIR = os.path.expanduser(
    os.environ.get("COMMONS_RECORDS_DIR", "~/workspace/the-commons/records")
)
NEAR_CLI = os.path.expanduser(
    os.environ.get(
        "COMMONS_NEAR_CLI",
        "~/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near",
    )
)
RPCS = ["https://rpc.testnet.fastnear.com", "https://rpc.testnet.near.org"]
YOCTO = 10**24

os.makedirs(RECORDS_DIR, exist_ok=True)

server = MCPServer(
    "the-commons",
    instructions=(
        "Operate in The Commons, a NEAR testnet commons experiment. "
        "Start with commons_status to see who you are and what rooms "
        "exist. Sponsors create rooms with commons_create_room. "
        "Operators register in a room (commons_register_agent for a "
        "feeder seat, commons_register_security for a security seat), "
        "then each epoch: commons_commit during the commit phase and "
        "commons_reveal during the reveal phase. Amounts are NEAR "
        "decimal strings like \"0.005\" or integer yocto strings."
    ),
)

# ---------------------------------------------------------------- helpers


def _rpc(method, params, timeout=25):
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    last = None
    for attempt in range(6):
        rpc = RPCS[attempt % len(RPCS)]
        try:
            req = urllib.request.Request(
                rpc,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            if "error" in data:
                raise RuntimeError(f"RPC error: {data['error']}")
            return data["result"]
        except Exception as e:  # transient RPC failures are normal
            last = e
            time.sleep(1.2)
    raise RuntimeError(f"RPC {method} failed after retries: {last}")


def view(fn, args):
    result = _rpc(
        "query",
        {
            "request_type": "call_function",
            "finality": "final",
            "account_id": CONTRACT,
            "method_name": fn,
            "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
        },
    )
    raw = bytes(result["result"])
    return json.loads(raw) if raw else None


def account_view(account):
    return _rpc(
        "query",
        {"request_type": "view_account", "finality": "final", "account_id": account},
    )


def to_yocto(amount):
    """Accept a NEAR decimal string ("0.005") or an integer yocto string."""
    if isinstance(amount, (int,)):
        return int(amount)
    s = str(amount).strip()
    if "." in s:
        whole, _, frac = s.partition(".")
        frac = (frac + "0" * 24)[:24]
        return int(whole or "0") * YOCTO + int(frac or "0")
    return int(s)


def near_str(yocto):
    """Format a yocto integer as an exact NEAR decimal string."""
    yocto = int(yocto)
    whole, frac = divmod(yocto, YOCTO)
    if frac == 0:
        return str(whole)
    return f"{whole}.{str(frac).zfill(24).rstrip('0')}"


def _tx_outcome(tx_hash, signer):
    """Read a transaction back and classify its execution outcome."""
    result = _rpc("tx", [tx_hash, signer])
    outcomes = [result.get("transaction_outcome", {})] + result.get(
        "receipts_outcome", []
    )
    failures = []
    success_value = None
    for oc in outcomes:
        status = (oc.get("outcome", {}) or {}).get("status", {}) or {}
        if "Failure" in status:
            failures.append(status["Failure"])
        if "SuccessValue" in status and status["SuccessValue"]:
            try:
                success_value = json.loads(
                    base64.b64decode(status["SuccessValue"]).decode()
                )
            except Exception:
                success_value = base64.b64decode(status["SuccessValue"]).decode(
                    "utf-8", "replace"
                )
    if failures:
        return {"executed": False, "failure": failures[0], "return_value": success_value}
    return {"executed": True, "failure": None, "return_value": success_value}


def near_call(fn, args, deposit_yocto=0):
    """Run one signed contract call as COMMONS_ACCOUNT_ID and verify it."""
    if not ACCOUNT:
        return {"ok": False, "error": "COMMONS_ACCOUNT_ID is not configured; "
                "this server is read-only until an account is set."}
    deposit = f"{near_str(deposit_yocto)} NEAR"
    cmd = [
        NEAR_CLI, "contract", "call-function", "as-transaction", CONTRACT, fn,
        "json-args", json.dumps(args), "prepaid-gas", "30 TeraGas",
        "attached-deposit", deposit, "sign-as", ACCOUNT,
        "network-config", NETWORK, "sign-with-keychain", "send",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as e:
        return {"ok": False, "error": f"near CLI failed to run: {e}"}
    text = out.stdout + out.stderr
    tx_id = ""
    for line in text.splitlines():
        if "Transaction ID" in line:
            tx_id = line.split(":")[-1].strip()
    if not tx_id:
        return {"ok": False, "error": "near CLI returned no transaction id",
                "cli_output_tail": text[-1200:]}
    # The CLI prints a Transaction ID even for failed executions, so the
    # outcome is always read back from the chain before reporting.
    last = None
    for _ in range(10):
        try:
            outcome = _tx_outcome(tx_id, ACCOUNT)
            break
        except Exception as e:
            last = e
            time.sleep(2)
    else:
        return {"ok": False, "tx_id": tx_id,
                "error": f"could not read back transaction outcome: {last}"}
    if not outcome["executed"]:
        return {"ok": False, "tx_id": tx_id,
                "error": f"transaction executed but failed on chain: "
                         f"{json.dumps(outcome['failure'])[:600]}"}
    return {"ok": True, "tx_id": tx_id, "return_value": outcome["return_value"]}


def err(msg):
    return {"ok": False, "error": msg}


# ------------------------------------------------------- pending reveals

def _state_path():
    safe = (ACCOUNT or "readonly").replace("/", "_")
    return os.path.join(RECORDS_DIR, f".mcp-pending-{safe}.json")


def _load_pending():
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pending(state):
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _pending_key(room_id, epoch):
    return f"{room_id}:{epoch}"


# ---------------------------------------------------------------- tools: read


@server.tool()
def commons_status() -> dict:
    """Who this server signs as, its balance, the contract config, and
    every room the configured account is registered in, with its role."""
    out = {
        "ok": True,
        "account": ACCOUNT or None,
        "contract": CONTRACT,
        "network": NETWORK,
        "records_dir": RECORDS_DIR,
    }
    try:
        out["config"] = view("get_config", {})
    except Exception as e:
        return err(f"get_config failed: {e}")
    if ACCOUNT:
        try:
            av = account_view(ACCOUNT)
            out["balance_near"] = near_str(int(av["amount"]))
            out["balance_yocto"] = av["amount"]
        except Exception as e:
            out["balance_error"] = str(e)
        registrations = []
        for rid in range(int(out["config"]["rooms_created"])):
            try:
                agent = view("get_agent", {"room_id": rid, "account": ACCOUNT})
            except Exception:
                agent = None
            if agent:
                room = view("get_room", {"room_id": rid})
                registrations.append({
                    "room_id": rid,
                    "room_name": room["name"] if room else None,
                    "role": agent["role"],
                    "active": agent["active"],
                    "stake_near": near_str(int(agent["stake"])),
                    "total_withdrawn_near": near_str(int(agent["total_withdrawn"])),
                })
        out["registrations"] = registrations
    return out


@server.tool()
def commons_list_rooms(from_index: int = 0, limit: int = 50) -> dict:
    """List rooms (seasons) with pool, reserve, occupancy and status fields."""
    try:
        rooms = view("list_rooms", {"from_index": from_index, "limit": limit})
        return {"ok": True, "rooms": rooms}
    except Exception as e:
        return err(f"list_rooms failed: {e}")


@server.tool()
def commons_get_room(room_id: int) -> dict:
    """Full room state: pool, reserve, counts, stake, cap, season length."""
    try:
        room = view("get_room", {"room_id": room_id})
        if room is None:
            return err(f"room {room_id} does not exist")
        return {"ok": True, "room": room}
    except Exception as e:
        return err(f"get_room failed: {e}")


@server.tool()
def commons_current_epoch(room_id: int) -> dict:
    """Current epoch number, phase (commit or reveal), seconds into the
    epoch, and whether the season is over."""
    try:
        info = view("current_epoch", {"room_id": room_id})
        return {"ok": True, "info": info}
    except Exception as e:
        return err(f"current_epoch failed: {e}")


@server.tool()
def commons_get_epoch(room_id: int, epoch: int) -> dict:
    """The permanent record of a settled epoch: every reveal, what was
    requested and paid, the refill moved, and the pool afterwards."""
    try:
        rec = view("get_epoch", {"room_id": room_id, "epoch": epoch})
        if rec is None:
            return err(f"epoch {epoch} of room {room_id} is not settled yet")
        return {"ok": True, "epoch": rec}
    except Exception as e:
        return err(f"get_epoch failed: {e}")


# ---------------------------------------------------------------- tools: write


@server.tool()
def commons_create_room(
    name: str,
    stake: str,
    capacity: int,
    seed: str,
    refill_per_epoch: str,
    cap_bps: int,
    epoch_secs: int,
    season_epochs: int,
    data_exclusivity_days: int,
) -> dict:
    """Sponsor action: open a new room (season). Amounts (stake, seed,
    refill_per_epoch) are NEAR decimal strings like "0.005" or integer
    yocto strings. Attaches exactly room_fee + seed, read from the
    contract config. Returns the new room id and a read-back of it."""
    try:
        cfg = view("get_config", {})
        fee = int(cfg["room_fee"])
        seed_y = to_yocto(seed)
        args = {
            "name": name,
            "stake": str(to_yocto(stake)),
            "capacity": capacity,
            "seed": str(seed_y),
            "refill_per_epoch": str(to_yocto(refill_per_epoch)),
            "cap_bps": cap_bps,
            "epoch_secs": epoch_secs,
            "season_epochs": season_epochs,
            "data_exclusivity_days": data_exclusivity_days,
        }
    except Exception as e:
        return err(f"bad arguments or config read failed: {e}")
    res = near_call("create_room", args, deposit_yocto=fee + seed_y)
    if not res["ok"]:
        return res
    try:
        cfg2 = view("get_config", {})
        new_id = int(cfg2["rooms_created"]) - 1
        room = view("get_room", {"room_id": new_id})
        res["room_id"] = new_id
        res["room"] = room
        if not room or room["sponsor"] != ACCOUNT:
            res["ok"] = False
            res["error"] = "read-back mismatch: new room not found under sponsor"
    except Exception as e:
        res["readback_error"] = str(e)
    return res


def _register(room_id, fn, role):
    try:
        room = view("get_room", {"room_id": room_id})
        if room is None:
            return err(f"room {room_id} does not exist")
        existing = view("get_agent", {"room_id": room_id, "account": ACCOUNT})
        if existing:
            return err(f"{ACCOUNT} is already registered in room {room_id} "
                       f"as {existing['role']}")
        stake = int(room["stake"])
    except Exception as e:
        return err(f"pre-check failed: {e}")
    res = near_call(fn, {"room_id": room_id}, deposit_yocto=stake)
    if not res["ok"]:
        return res
    try:
        agent = view("get_agent", {"room_id": room_id, "account": ACCOUNT})
        res["agent"] = agent
        if not agent or agent["role"] != role:
            res["ok"] = False
            res["error"] = f"read-back mismatch: expected role {role}"
    except Exception as e:
        res["readback_error"] = str(e)
    return res


@server.tool()
def commons_register_agent(room_id: int) -> dict:
    """Take a feeder seat in a room, attaching exactly the room stake
    as a bond. One account holds at most one seat per room."""
    return _register(room_id, "register_agent", "feeder")


@server.tool()
def commons_register_security(room_id: int) -> dict:
    """Take a security seat in a room, attaching exactly the room stake
    as a bond. Security agents file and confirm abuse reports."""
    return _register(room_id, "register_security", "security")


@server.tool()
def commons_commit(
    room_id: int,
    amount: str,
    reasoning: str,
    strategy: str = "custom",
) -> dict:
    """Commit a withdrawal decision for the current epoch (commit phase
    only). Builds the canonical reasoning record (SCHEME section 6),
    writes it into the records store, derives log_ref from the record
    file, computes the commitment, and calls commit. The pending reveal
    data is kept in local MCP state so commons_reveal needs no secrets
    re-entered. Amount is a NEAR decimal string or integer yocto."""
    try:
        info = view("current_epoch", {"room_id": room_id})
        room = view("get_room", {"room_id": room_id})
        if room is None:
            return err(f"room {room_id} does not exist")
        if info["season_over"]:
            return err(f"room {room_id} season is over; no commits accepted")
        if info["phase"] != "commit":
            return err(f"epoch {info['epoch']} is in {info['phase']} phase, "
                       "not commit phase")
        epoch = int(info["epoch"])
        amount_y = to_yocto(amount)
        others = []
        last_settled = int(room["next_epoch_to_settle"]) - 1
        if last_settled >= 0:
            prev = view("get_epoch", {"room_id": room_id, "epoch": last_settled})
            if prev:
                others = [{"agent": r["account"], "paid": r["paid"]}
                          for r in prev["reveals"]]
        record = {
            "room_id": room_id,
            "epoch": epoch,
            "agent": ACCOUNT,
            "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pool_observed": room["pool_balance"],
            "reserve_observed": room["refill_reserve"],
            "others_last_epoch": others,
            "strategy": strategy,
            "reasoning": reasoning,
            "intended_amount": str(amount_y),
        }
        canonical = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        reasoning_hash = hashlib.sha256(canonical.encode()).hexdigest()
        fname = f"room{room_id}-epoch{epoch}-{ACCOUNT}.json"
        fpath = os.path.join(RECORDS_DIR, fname)
        with open(fpath, "w") as f:
            f.write(canonical)
        log_ref = fname
        salt = secrets.token_hex(16)
        commitment = hashlib.sha256(
            f"{amount_y}|{reasoning_hash}|{salt}".encode()
        ).hexdigest()
    except Exception as e:
        return err(f"commit preparation failed: {e}")
    res = near_call("commit", {"room_id": room_id, "commitment": commitment})
    if not res["ok"]:
        return res
    state = _load_pending()
    state[_pending_key(room_id, epoch)] = {
        "room_id": room_id, "epoch": epoch, "amount": str(amount_y),
        "reasoning_hash": reasoning_hash, "log_ref": log_ref, "salt": salt,
        "commitment": commitment, "revealed": False,
    }
    _save_pending(state)
    res.update({
        "epoch": epoch, "commitment": commitment,
        "reasoning_hash": reasoning_hash, "log_ref": log_ref,
        "record_file": fpath,
    })
    return res


@server.tool()
def commons_reveal(room_id: int) -> dict:
    """Reveal the pending commitment made earlier by commons_commit for
    the current epoch (reveal phase only). Uses the amount, reasoning
    hash, log_ref and salt stored by commons_commit; no arguments are
    needed beyond the room."""
    state = _load_pending()
    try:
        info = view("current_epoch", {"room_id": room_id})
    except Exception as e:
        return err(f"current_epoch failed: {e}")
    if info["season_over"]:
        return err(f"room {room_id} season is over; reveal window has passed")
    if info["phase"] != "reveal":
        return err(f"epoch {info['epoch']} is in {info['phase']} phase, "
                   "not reveal phase")
    key = _pending_key(room_id, int(info["epoch"]))
    pending = state.get(key)
    if pending is None:
        return err(f"no pending commit stored for room {room_id} "
                   f"epoch {info['epoch']} by {ACCOUNT}")
    if pending.get("revealed"):
        return err("this commitment was already revealed")
    res = near_call("reveal", {
        "room_id": room_id,
        "amount": pending["amount"],
        "reasoning_hash": pending["reasoning_hash"],
        "log_ref": pending["log_ref"],
        "salt": pending["salt"],
    })
    if not res["ok"]:
        return res
    pending["revealed"] = True
    state[key] = pending
    _save_pending(state)
    res.update({"epoch": pending["epoch"], "amount": pending["amount"],
                "log_ref": pending["log_ref"]})
    return res


@server.tool()
def commons_settle(room_id: int, epoch: int) -> dict:
    """Settle an epoch whose time has ended (epochs settle in order).
    Anyone may call this; agents are expected to act as keepers for
    their own rooms. Verified by reading the epoch record back."""
    res = near_call("settle_epoch", {"room_id": room_id, "epoch": epoch})
    if not res["ok"]:
        return res
    try:
        rec = view("get_epoch", {"room_id": room_id, "epoch": epoch})
        res["epoch_record"] = rec
        if rec is None:
            res["ok"] = False
            res["error"] = "read-back mismatch: epoch record missing after settle"
    except Exception as e:
        res["readback_error"] = str(e)
    return res


@server.tool()
def commons_exit_room(room_id: int) -> dict:
    """Leave a room and take the stake back. Leaving before the season
    ends costs the flat 10 percent exit tax into the security pot;
    leaving at or after season end is free."""
    res = near_call("exit_room", {"room_id": room_id})
    if not res["ok"]:
        return res
    try:
        res["agent_after"] = view(
            "get_agent", {"room_id": room_id, "account": ACCOUNT})
        room = view("get_room", {"room_id": room_id})
        res["room_after"] = room
    except Exception as e:
        res["readback_error"] = str(e)
    return res


@server.tool()
def commons_report_abuse(room_id: int, target: str, evidence_hash: str) -> dict:
    """Security only: file an abuse report against an active feeder in
    the same room. evidence_hash anchors an off-chain evidence bundle."""
    res = near_call("report_abuse", {
        "room_id": room_id, "target": target, "evidence_hash": evidence_hash})
    if not res["ok"]:
        return res
    if isinstance(res.get("return_value"), int):
        res["report_id"] = res["return_value"]
        try:
            res["report"] = view("get_report", {"report_id": res["report_id"]})
        except Exception as e:
            res["readback_error"] = str(e)
    return res


@server.tool()
def commons_confirm_report(report_id: int) -> dict:
    """Security only: confirm an abuse report. A report confirms when
    confirmations reach a majority of the room's security set; the
    reporter and the target cannot confirm."""
    res = near_call("confirm_report", {"report_id": report_id})
    if not res["ok"]:
        return res
    try:
        res["report"] = view("get_report", {"report_id": report_id})
    except Exception as e:
        res["readback_error"] = str(e)
    return res


@server.tool()
def commons_finalize_room(room_id: int) -> dict:
    """After season end: finalize the room. Splits the security pot
    among qualifying security agents (or folds it into the pool) and
    marks the room ended, which unlocks the sponsor residue."""
    res = near_call("finalize_room", {"room_id": room_id})
    if not res["ok"]:
        return res
    try:
        room = view("get_room", {"room_id": room_id})
        res["room"] = room
        if room and not room["ended"]:
            res["ok"] = False
            res["error"] = "read-back mismatch: room not ended after finalize"
    except Exception as e:
        res["readback_error"] = str(e)
    return res


@server.tool()
def commons_withdraw_residue(room_id: int) -> dict:
    """Sponsor only, after finalize: take back whatever pool and
    reserve remain in the room."""
    res = near_call("withdraw_residue", {"room_id": room_id})
    if not res["ok"]:
        return res
    try:
        res["room_after"] = view("get_room", {"room_id": room_id})
    except Exception as e:
        res["readback_error"] = str(e)
    return res


if __name__ == "__main__":
    if NETWORK != "testnet":
        print("COMMONS_NETWORK must be testnet", file=sys.stderr)
        sys.exit(1)
    server.run(transport="stdio")
