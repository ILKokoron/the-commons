#!/usr/bin/env python3
"""The Commons MCP server.

A standards-compliant MCP server (stdio transport) that lets any MCP
client operate in The Commons, a NEAR testnet experiment where AI
agents withdraw from a shared pool under commit-reveal epochs.

Configuration comes from the environment:
  COMMONS_ACCOUNT_ID   the NEAR account this server signs as
                       (operator or sponsor). Required for write tools.
  COMMONS_CONTRACT     contract account, default
                       commons.bindingvow1106.testnet (the v1 contract).
                       Set it to commonsv2.bindingvow1106.testnet to
                       operate against contract v2, which adds room
                       regime presets, the sponsor start trigger and
                       profiles (see commons_create_room's regime
                       argument, commons_start_room and the profile
                       tools). Set it to
                       commonsv5.bindingvow1106.testnet for contract
                       v5, whose rooms also need at least 2 seated
                       feeders before they can start; registrations
                       against v5 attempt that start automatically
                       once the quorum is seated, and
                       commons_create_room accepts the v4/v5 coverage
                       and refill arguments there.
  COMMONS_NETWORK      default testnet (only testnet is supported)
  COMMONS_RECORDS_DIR  where the local fallback copies of reasoning
                       records are written, default
                       ~/workspace/the-commons/records
  COMMONS_STORE_URL    base URL of the platform record store. When set
                       together with COMMONS_STORE_TOKEN, commons_commit
                       publishes each reasoning record to the store at
                       commit time and the reveal's log_ref is the
                       store content id (the record's sha256). Without
                       it, records stay local only and cannot be
                       certified by the platform.
  COMMONS_STORE_TOKEN  write token for the platform record store
  COMMONS_NEAR_CLI     path to the near CLI binary. Resolution order:
                       this variable when set, then `near` found on
                       PATH, then the built-in default path when that
                       file exists. When no binary can be resolved,
                       write tools fail with an error that names
                       COMMONS_NEAR_CLI as the fix.
  COMMONS_COMMIT_MIN_MARGIN_SECS
                       commons_commit refuses to send when fewer than
                       this many seconds remain in the commit phase
                       (default 45), because the transaction would
                       likely land in the reveal phase and be rejected
                       on chain.

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
import shutil
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
NEAR_CLI_ENV = os.environ.get("COMMONS_NEAR_CLI", "")
NEAR_CLI_DEFAULT = os.path.expanduser(
    "~/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near"
)
COMMIT_MIN_MARGIN_SECS = int(
    os.environ.get("COMMONS_COMMIT_MIN_MARGIN_SECS", "45"))


def _resolve_near_cli():
    """Resolve the near CLI binary once, in a fixed order:
    COMMONS_NEAR_CLI when set, then `near` on PATH, then the
    built-in default path when that file exists. PATH comes before
    the default because the default is one machine's install layout;
    a normal npm or cargo install lands on PATH and must win.
    Returns (path, source); path is None when nothing usable was
    found."""
    if NEAR_CLI_ENV:
        return os.path.expanduser(NEAR_CLI_ENV), "COMMONS_NEAR_CLI"
    on_path = shutil.which("near")
    if on_path:
        return on_path, "PATH lookup"
    if os.path.isfile(NEAR_CLI_DEFAULT):
        return NEAR_CLI_DEFAULT, "built-in default path"
    return None, "not found"


NEAR_CLI, NEAR_CLI_SOURCE = _resolve_near_cli()
STORE_URL = os.environ.get("COMMONS_STORE_URL", "").rstrip("/")
STORE_TOKEN = os.environ.get("COMMONS_STORE_TOKEN", "")
# Rotation for view reads. The third entry is the official archival
# endpoint; it was verified live against a full 10-reveal epoch read
# (exact bytes, correct totals) before being added. The Lava testnet
# endpoint was evaluated at the same time and rejected: it answers
# HTTP 410, discontinued.
RPCS = [
    "https://rpc.testnet.fastnear.com",
    "https://rpc.testnet.near.org",
    "https://archival-rpc.testnet.near.org",
]
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
        "commons_reveal during the reveal phase. On v2 and later "
        "contracts a new room sits in a recruiting state until it "
        "starts, which needs at least 3 registered security agents "
        "(the sponsor starts it on v2 and v3, any account on v4, and "
        "on v5 at least 2 feeders must also be seated, after which "
        "the registration that completes the quorum starts the room "
        "automatically), and accounts can carry a display profile "
        "(commons_set_profile, commons_get_profile). Amounts are NEAR "
        "decimal strings like \"0.005\" or integer yocto strings."
    ),
)

# ---------------------------------------------------------------- helpers


# The FastNEAR mirror truncates larger responses (around 9 KB, the size
# of a full 10-reveal epoch record) on the Python HTTP path, failing
# with IncompleteRead across retries. The dataset exporter proved that
# the same reads succeed through curl, so RPC reads go through a curl
# subprocess. If curl is somehow unavailable at runtime, reads fall
# back to the urllib path instead of crashing.
CURL = shutil.which("curl")


def _rpc_post_curl(url, body, timeout):
    proc = subprocess.run(
        [CURL, "-s", "--max-time", str(timeout), "-X", "POST", url,
         "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True, timeout=timeout + 15)
    return proc.stdout


def _rpc_post_urllib(url, body, timeout):
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _rpc(method, params, timeout=25):
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    body = json.dumps(payload)
    backoff = [1.2, 2.0, 3.2, 5.0, 8.0, 12.0, 12.0]
    last = None
    for attempt in range(8):
        rpc = RPCS[attempt % len(RPCS)]
        try:
            if CURL:
                raw = _rpc_post_curl(rpc, body, timeout)
            else:
                raw = _rpc_post_urllib(rpc, body.encode(), timeout)
            data = json.loads(raw)
            if "error" in data:
                raise RuntimeError(f"RPC error: {data['error']}")
            return data["result"]
        except Exception as e:  # transient RPC failures are normal
            last = e
            if attempt < len(backoff):
                time.sleep(backoff[attempt])
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


def view_strict(fn, args):
    """Like view(), but a contract-side failure (the RPC result
    carrying an "error" instead of return bytes) raises with the
    contract's own text. Used by the v2-only profile reads, where a
    missing method on a v1 contract must surface as an honest
    MethodNotFound error rather than looking like an empty result."""
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
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(str(result["error"]))
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


def _add_near_fields(obj, fields):
    """Add exact NEAR decimal companions ("<field>_near") next to
    yocto amounts in a result dict, in place. Additive only: existing
    keys are never changed or overwritten, and values that do not
    parse as integers are left alone. Yocto strings are easy to
    misread by a factor of 1000 or more; the companion makes the
    denomination explicit at the point of reading."""
    if not isinstance(obj, dict):
        return obj
    for f in fields:
        key = f + "_near"
        if f in obj and key not in obj:
            try:
                obj[key] = near_str(int(obj[f]))
            except (TypeError, ValueError):
                pass
    return obj


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
    if not NEAR_CLI:
        return {"ok": False, "near_cli": None,
                "error": "no near CLI binary found (looked at "
                         "COMMONS_NEAR_CLI, the built-in default path, "
                         "and `near` on PATH). Install the NEAR CLI or "
                         "set COMMONS_NEAR_CLI to its path."}
    if not os.path.isfile(NEAR_CLI):
        return {"ok": False, "near_cli": NEAR_CLI,
                "error": f"near CLI binary resolved via {NEAR_CLI_SOURCE} "
                         f"does not exist: {NEAR_CLI}. Set "
                         "COMMONS_NEAR_CLI to the real binary path."}
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
        return {"ok": False, "near_cli": NEAR_CLI,
                "error": f"near CLI failed to run ({NEAR_CLI}, resolved "
                         f"via {NEAR_CLI_SOURCE}): {e}"}
    text = out.stdout + out.stderr
    tx_id = ""
    for line in text.splitlines():
        if "Transaction ID" in line:
            tx_id = line.split(":")[-1].strip()
    if not tx_id:
        return {"ok": False, "near_cli": NEAR_CLI,
                "error": "near CLI returned no transaction id",
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
        return {"ok": False, "tx_id": tx_id, "near_cli": NEAR_CLI,
                "error": f"could not read back transaction outcome: {last}"}
    if not outcome["executed"]:
        return {"ok": False, "tx_id": tx_id, "near_cli": NEAR_CLI,
                "error": f"transaction executed but failed on chain: "
                         f"{json.dumps(outcome['failure'])[:600]}"}
    return {"ok": True, "tx_id": tx_id, "near_cli": NEAR_CLI,
            "return_value": outcome["return_value"]}


def err(msg):
    return {"ok": False, "error": msg}


def _store_upload(canonical, expected_hash):
    """Publish a reasoning record to the platform record store.

    The store addresses records by the sha256 of their canonical bytes,
    which is exactly the reasoning hash anchored on chain, so a
    successful upload makes the record certifiable by anyone holding
    read access to the store. Returns (ok, content_id, error)."""
    try:
        req = urllib.request.Request(
            STORE_URL + "/api/records",
            data=canonical.encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {STORE_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read())
        cid = out.get("id") or out.get("log_ref")
        if not out.get("ok") or cid != expected_hash:
            return False, None, f"store returned unexpected id {cid!r}"
        return True, cid, None
    except Exception as e:
        return False, None, str(e)


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
        "record_store_configured": bool(STORE_URL and STORE_TOKEN),
        "record_store_url": STORE_URL or None,
        "near_cli": NEAR_CLI,
        "near_cli_source": NEAR_CLI_SOURCE,
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
    """Full room state: pool, reserve, counts, stake, cap, season length.
    The room record is returned exactly as the contract reports it, so
    on the v2 contract the regime ("wild" or "paced") and started
    fields are included; v1 rooms simply have no such fields."""
    try:
        room = view("get_room", {"room_id": room_id})
        if room is None:
            return err(f"room {room_id} does not exist")
        _add_near_fields(room, ["pool_balance", "refill_reserve", "stake",
                                "refill_per_epoch", "total_paid_out",
                                "security_pot"])
        return {"ok": True, "room": room}
    except Exception as e:
        return err(f"get_room failed: {e}")


@server.tool()
def commons_current_epoch(room_id: int) -> dict:
    """Current epoch number, phase (commit or reveal), seconds into the
    epoch, and whether the season is over. On the v2+ contracts a room
    that has not been started yet reports phase "recruiting" and
    started false, and started rooms carry started true. Two
    presentation corrections are applied on top of the raw contract
    answer, because the raw answer misleads operators scanning rooms:
    a cancelled room reports phase "cancelled" (the contract keeps
    saying "recruiting" for it), and once the season is over the
    epoch is clamped to the season length, with the raw time-derived
    counter preserved in epoch_raw (the contract counts past the end
    of the season indefinitely)."""
    try:
        info = view("current_epoch", {"room_id": room_id})
        room = view("get_room", {"room_id": room_id})
        if room is not None:
            if room.get("cancelled"):
                info["phase"] = "cancelled"
                info["cancelled"] = True
            season_epochs = int(room.get("season_epochs", 0))
            if info.get("season_over") and season_epochs \
                    and int(info.get("epoch", 0)) > season_epochs:
                info["epoch_raw"] = info["epoch"]
                info["epoch"] = season_epochs
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
        _add_near_fields(rec, ["total_paid", "total_requested",
                               "refill_moved", "pool_after"])
        for r in rec.get("reveals", []):
            _add_near_fields(r, ["amount", "paid"])
        return {"ok": True, "epoch": rec}
    except Exception as e:
        return err(f"get_epoch failed: {e}")


@server.tool()
def commons_get_profile(account_id: str = "") -> dict:
    """Contract v2 only: read an account's display profile
    (display_name, bio, social). Defaults to the configured account
    when account_id is omitted. Returns profile null when the account
    has never set one. Profiles are display metadata only; they carry
    no amounts and touch no room, stake or payout logic. On a v1
    contract this fails with the contract's method-not-found error,
    because v1 has no profile store."""
    acct = account_id or ACCOUNT
    if not acct:
        return err("no account_id given and COMMONS_ACCOUNT_ID is not "
                   "configured; pass account_id explicitly")
    try:
        profile = view_strict("get_profile", {"account": acct})
        return {"ok": True, "account": acct, "profile": profile}
    except Exception as e:
        return err(f"get_profile failed: {e}")


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
    regime: str = "",
    coverage: str = "",
    refill_mode: str = "",
    decay_bps: int = 9800,
    refill_floor: str = "0",
) -> dict:
    """Sponsor action: open a new room (season). Amounts (stake, seed,
    refill_per_epoch) are NEAR decimal strings like "0.005" or integer
    yocto strings. Attaches exactly room_fee + seed, read from the
    contract config. Returns the new room id and a read-back of it.

    On a contract whose get_config exposes gas_per_epoch (the
    current contract), create_room also escrows a Guarantee Fund:
    GF = capacity x season_epochs x gas_per_epoch x 3 / 2, computed
    with the same integer formula the contract uses. The deposit
    becomes room_fee + seed + GF and the result carries the GF as
    "guarantee_fund" (yocto string). On a contract without
    gas_per_epoch in its config nothing changes: the deposit stays
    room_fee + seed and no GF field is added.

    The optional regime argument is contract v2 only: "Wild" settles
    exactly like v1, "Paced" additionally caps each epoch's total
    payout at the post-refill pool snapshot divided by the epochs
    remaining in the season. When regime is provided it is validated
    and included in the create_room call; when it is omitted the call
    is sent exactly as before, with no regime argument at all, which
    is what the v1 contract expects (v1 rejects unknown arguments).
    Passing a regime while COMMONS_CONTRACT points at the v1 contract
    will therefore fail on chain.

    Contracts v4 and v5 take four more arguments, sent automatically
    when COMMONS_CONTRACT points at one of them: coverage ("Covered"
    escrows the Guarantee Fund above; "Bare" escrows none and the
    deposit is fee + seed only; default "Covered"), refill_mode
    ("Flat", or "Decaying" where each epoch's refill is the previous
    one times decay_bps / 10000, floored at refill_floor; default
    "Flat", decay_bps default 9800, refill_floor a NEAR amount
    default "0"). Regime defaults to "Wild" on v4 and v5, where the
    argument is required. Passing coverage or refill_mode against an
    older contract is an error, not silently ignored."""
    reg = (regime or "").strip()
    if reg and reg not in ("Wild", "Paced"):
        return err(f'invalid regime {regime!r}: must be "Wild" or '
                   '"Paced" (contract v2), or omitted entirely for a '
                   "v1 contract")
    v45 = CONTRACT.split(".")[0] in ("commonsv4", "commonsv5")
    cov = (coverage or "").strip()
    rm = (refill_mode or "").strip()
    if not v45 and (cov or rm):
        return err("coverage and refill_mode are contract v4/v5 "
                   "arguments; the configured contract is "
                   f"{CONTRACT}")
    if v45:
        cov = cov or "Covered"
        if cov not in ("Bare", "Covered"):
            return err(f'invalid coverage {coverage!r}: must be '
                       '"Bare" or "Covered"')
        rm = rm or "Flat"
        if rm not in ("Flat", "Decaying"):
            return err(f'invalid refill_mode {refill_mode!r}: must '
                       'be "Flat" or "Decaying"')
        reg = reg or "Wild"
    try:
        cfg = view("get_config", {})
        fee = int(cfg["room_fee"])
        seed_y = to_yocto(seed)
        gf = 0
        if cfg.get("gas_per_epoch") is not None and cov != "Bare":
            gf = (capacity * season_epochs * int(cfg["gas_per_epoch"]) * 3) // 2
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
        if reg:
            args["regime"] = reg
        if v45:
            args["coverage"] = cov
            args["refill_mode"] = rm
            args["decay_bps"] = int(decay_bps)
            args["refill_floor"] = str(to_yocto(refill_floor))
    except Exception as e:
        return err(f"bad arguments or config read failed: {e}")
    res = near_call("create_room", args, deposit_yocto=fee + seed_y + gf)
    if not res["ok"]:
        return res
    if gf:
        res["guarantee_fund"] = str(gf)
    if v45:
        res["coverage"] = cov
        res["refill_mode"] = rm
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


@server.tool()
def commons_start_room(room_id: int) -> dict:
    """Start a room that is still in its recruiting state. Anchors
    epoch 0 at the moment of the call; before this, registration is
    open but commit, reveal and settle are rejected by the contract.
    Who may call it depends on the contract: v2 and v3 are sponsor
    only and need at least 3 registered security agents, so abuse
    reports can actually reach their confirm threshold. v4 makes the
    call permissionless (any account) under the same security gate.
    v5 adds the player quorum: at least 2 feeder agents must also be
    seated, so a room with a lone agent keeps waiting for players;
    the registration tools fire this call automatically when a
    registration completes the quorum. A rejected call fails on
    chain and the contract's panic text is returned in the error,
    unchanged. Verified by reading the room back (started must be
    true). On a v1 contract this fails with a method-not-found
    style error, because v1 starts seasons at creation."""
    res = near_call("start_room", {"room_id": room_id})
    if not res["ok"]:
        return res
    try:
        room = view("get_room", {"room_id": room_id})
        res["room"] = room
        if not room or not room.get("started"):
            res["ok"] = False
            res["error"] = "read-back mismatch: room not started after start_room"
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
    if res["ok"]:
        _maybe_auto_start(res, room_id)
    return res


def _maybe_auto_start(res, room_id):
    """The v5 room model: an empty room waits for players and starts
    itself once the quorum is seated (at least 2 feeders and 3
    security). The contract enforces the quorum inside start_room;
    the agent whose registration completes it fires the call, so
    from the players' side the room starts on its own. Only
    attempted where start_room is permissionless (v4, v5) or by the
    room's own sponsor (v2 and later); v1 starts rooms at creation,
    so this is a no-op there. The outcome lands on the register
    result as room_state / auto_start and never fails the
    registration itself."""
    try:
        if CONTRACT.split(".")[0] == "commons":
            return
        room = view("get_room", {"room_id": room_id})
        if not room or room.get("started") or room.get("ended") \
                or room.get("cancelled"):
            return
        feeders = int(room.get("feeders_count", 0))
        security = int(room.get("security_count", 0))
        res["room_state"] = (
            f"recruiting: {feeders} feeder(s), {security} security "
            f"(starts at 2 feeders + 3 security)")
        if feeders < 2 or security < 3:
            return
        permissionless = CONTRACT.split(".")[0] in ("commonsv4", "commonsv5")
        if not permissionless and ACCOUNT != room.get("sponsor"):
            return
        start = near_call("start_room", {"room_id": room_id})
        res["auto_start"] = {"attempted": True, "ok": bool(start.get("ok"))}
        if start.get("ok"):
            after = view("get_room", {"room_id": room_id})
            res["auto_start"]["started"] = bool(after and after.get("started"))
            if after and after.get("started"):
                res["room_state"] = \
                    "started: this registration completed the quorum"
        else:
            res["auto_start"]["error"] = start.get("error")
    except Exception as e:
        res["auto_start"] = {"attempted": False, "error": str(e)}


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
    always writes a local copy, and, when the platform record store is
    configured (COMMONS_STORE_URL and COMMONS_STORE_TOKEN), publishes
    the record to the store: the store content id (the record's own
    sha256, equal to the reasoning hash) becomes the log_ref revealed
    on chain. Without a store, log_ref falls back to the local filename
    and the result warns loudly that the record is local only and will
    not be certifiable by the platform. Computes the commitment and
    calls commit. The pending reveal data is persisted locally BEFORE
    the commit transaction is broadcast, so a crash cannot strand a
    paid commitment; the result also returns the amount (yocto and
    NEAR) and the salt, so the caller can reveal later with explicit
    arguments even if that local state is lost. Refuses to send when
    less than COMMONS_COMMIT_MIN_MARGIN_SECS remains in the commit
    phase. Amount is a NEAR decimal string or integer yocto."""
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
        half = max(1, int(room["epoch_secs"]) // 2)
        remaining = half - int(info.get("seconds_into_epoch", 0))
        if remaining < COMMIT_MIN_MARGIN_SECS:
            return err(f"only {remaining}s left in the commit phase of "
                       f"epoch {epoch} (safety margin "
                       f"{COMMIT_MIN_MARGIN_SECS}s); a commit sent now "
                       "would likely land in the reveal phase and be "
                       "rejected on chain, burning gas. Wait for the "
                       f"commit phase of epoch {epoch + 1}.")
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
        record_status = {
            "record_store": "local-only",
            "record_local_only": True,
            "record_warning": (
                "COMMONS_STORE_URL and COMMONS_STORE_TOKEN are not both "
                "set, so this record is LOCAL ONLY on this machine and "
                "will not be certifiable by the platform. Configure the "
                "platform record store to publish reasoning records."
            ),
        }
        if STORE_URL and STORE_TOKEN:
            up_ok, up_id, up_err = _store_upload(canonical, reasoning_hash)
            if up_ok:
                log_ref = up_id
                record_status = {"record_store": "platform-store",
                                 "record_local_only": False}
            else:
                record_status["record_warning"] = (
                    f"Platform record store upload failed ({up_err}), "
                    "so this record is LOCAL ONLY on this machine and "
                    "will not be certifiable by the platform until it "
                    "is published to the store."
                )
        salt = secrets.token_hex(16)
        commitment = hashlib.sha256(
            f"{amount_y}|{reasoning_hash}|{salt}".encode()
        ).hexdigest()
    except Exception as e:
        return err(f"commit preparation failed: {e}")
    # Persist the pending reveal data BEFORE broadcasting the commit
    # transaction (atomic write in _save_pending). A crash anywhere
    # after this point, including inside the broadcast itself, can no
    # longer strand a paid commitment: the reveal data already exists
    # on disk. If the transaction is read back as executed-but-failed,
    # or never produced a transaction id at all, the entry is rolled
    # back so a dead commitment is never mistaken for a live one. If
    # the outcome cannot be read back, the entry stays: the commit may
    # have landed, and the reveal data must survive.
    state = _load_pending()
    pkey = _pending_key(room_id, epoch)
    had_prev = pkey in state
    prev_entry = state.get(pkey)
    state[pkey] = {
        "room_id": room_id, "epoch": epoch, "amount": str(amount_y),
        "reasoning_hash": reasoning_hash, "log_ref": log_ref, "salt": salt,
        "commitment": commitment, "revealed": False,
    }
    _save_pending(state)
    res = near_call("commit", {"room_id": room_id, "commitment": commitment})
    res.update(record_status)
    if not res["ok"]:
        dead = ("tx_id" not in res
                or "transaction executed but failed on chain"
                in res.get("error", ""))
        if dead:
            state = _load_pending()
            if had_prev:
                state[pkey] = prev_entry
            else:
                state.pop(pkey, None)
            _save_pending(state)
        return res
    res.update({
        "epoch": epoch, "commitment": commitment,
        "reasoning_hash": reasoning_hash, "log_ref": log_ref,
        "record_file": fpath,
        "amount_yocto": str(amount_y), "amount_near": near_str(amount_y),
        "salt": salt,
    })
    return res


@server.tool()
def commons_reveal(
    room_id: int,
    amount: str = "",
    reasoning_hash: str = "",
    salt: str = "",
    log_ref: str = "",
) -> dict:
    """Reveal a commitment made earlier by commons_commit for the
    current epoch (reveal phase only).

    Two ways to supply the reveal data. Default: pass only room_id;
    the server uses the amount, reasoning hash, log_ref and salt that
    commons_commit persisted locally before broadcasting the commit.
    Recovery: pass amount, reasoning_hash and salt together (exactly
    the values commons_commit returned), for when the local pending
    file is gone, for example after a crash or a machine move.
    log_ref is optional in the recovery flow: it falls back to the
    pending entry's log_ref when one exists, then to the reasoning
    hash itself, which is the record's content id in the platform
    store. When a pending entry does exist, the explicit values must
    reproduce its commitment or the call refuses, rather than burning
    gas on a reveal the contract would reject. Amount is a NEAR
    decimal string or integer yocto."""
    try:
        info = view("current_epoch", {"room_id": room_id})
    except Exception as e:
        return err(f"current_epoch failed: {e}")
    if info["season_over"]:
        return err(f"room {room_id} season is over; reveal window has passed")
    if info["phase"] != "reveal":
        return err(f"epoch {info['epoch']} is in {info['phase']} phase, "
                   "not reveal phase")
    epoch = int(info["epoch"])
    state = _load_pending()
    key = _pending_key(room_id, epoch)
    pending = state.get(key)
    if pending is not None and pending.get("revealed"):
        return err("this commitment was already revealed")
    given = [bool(amount), bool(reasoning_hash), bool(salt)]
    if any(given) and not all(given):
        return err("explicit reveal needs amount, reasoning_hash and "
                   "salt together; pass all three, or none to use the "
                   "stored pending commit")
    if all(given):
        try:
            amount_y = to_yocto(amount)
        except Exception:
            return err(f"unparseable amount: {amount!r}")
        commitment = hashlib.sha256(
            f"{amount_y}|{reasoning_hash}|{salt}".encode()).hexdigest()
        if pending is not None and pending.get("commitment") != commitment:
            return err("explicit reveal data does not reproduce the "
                       "commitment stored for this epoch; refusing to "
                       "burn gas on a reveal the contract would reject")
        entry = {
            "room_id": room_id, "epoch": epoch, "amount": str(amount_y),
            "reasoning_hash": reasoning_hash,
            "log_ref": log_ref or (pending or {}).get("log_ref")
                       or reasoning_hash,
            "salt": salt, "commitment": commitment, "revealed": False,
        }
    else:
        if pending is None:
            return err(f"no pending commit stored for room {room_id} "
                       f"epoch {epoch} by {ACCOUNT}; pass amount, "
                       "reasoning_hash and salt explicitly to reveal "
                       "without the pending file")
        entry = pending
    res = near_call("reveal", {
        "room_id": room_id,
        "amount": entry["amount"],
        "reasoning_hash": entry["reasoning_hash"],
        "log_ref": entry["log_ref"],
        "salt": entry["salt"],
    })
    if not res["ok"]:
        return res
    entry["revealed"] = True
    state[key] = entry
    _save_pending(state)
    res.update({"epoch": epoch, "amount": entry["amount"],
                "amount_yocto": entry["amount"],
                "amount_near": near_str(int(entry["amount"])),
                "log_ref": entry["log_ref"]})
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


@server.tool()
def commons_set_profile(display_name: str, bio: str, social: str) -> dict:
    """Contract v2 only: set (or overwrite) the display profile of the
    configured account: a display name (max 64 chars), a bio (max 160
    chars) and a social handle (max 64 chars). Profiles are display
    metadata keyed by account; interfaces read them with
    commons_get_profile, and they touch no room, stake or payout
    logic. Length limits are the contract's, and breaking one fails
    on chain with the contract's panic text. Verified by reading the
    profile back. On a v1 contract this fails with a
    method-not-found style error, because v1 has no profile store."""
    res = near_call("set_profile", {
        "display_name": display_name, "bio": bio, "social": social})
    if not res["ok"]:
        return res
    try:
        profile = view_strict("get_profile", {"account": ACCOUNT})
        res["profile"] = profile
        if (not profile
                or profile.get("display_name") != display_name
                or profile.get("bio") != bio
                or profile.get("social") != social):
            res["ok"] = False
            res["error"] = "read-back mismatch: profile not stored as sent"
    except Exception as e:
        res["readback_error"] = str(e)
    return res


if __name__ == "__main__":
    if NETWORK != "testnet":
        print("COMMONS_NETWORK must be testnet", file=sys.stderr)
        sys.exit(1)
    server.run(transport="stdio")
