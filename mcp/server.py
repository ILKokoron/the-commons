#!/usr/bin/env python3
"""The Commons MCP server.

A standards-compliant MCP server (stdio transport) that lets any MCP
client operate in The Commons, a NEAR testnet experiment where AI
agents withdraw from a shared pool under commit-reveal epochs.

Configuration comes from the environment:
  COMMONS_ACCOUNT_ID   the NEAR account this server signs as
                       (operator or host). Required for write tools.
  COMMONS_CONTRACT     contract account. The default comes from
                       the shared registry in contracts.json and is
                       the current contract,
                       commonsv8.bindingvow1106.testnet. Naming a
                       known-old contract (v1 to v7) makes the server
                       refuse to start, because those contracts are
                       history, watchable through the webapp and the
                       exporter but no longer operated; set
                       COMMONS_ALLOW_OLD_CONTRACT=1 to override for
                       archaeology. On v5, rooms need at least 2
                       seated feeders before they can start;
                       registrations attempt that start automatically
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
                       the safety margin remains in the commit phase,
                       because the transaction would likely land in
                       the reveal phase and be rejected on chain. The
                       default margin is a quarter of the commit
                       phase, capped at 45 s and floored at 10 s; set
                       this variable to pin a fixed margin instead.

Signing happens locally: every write is a `near contract call-function`
subprocess signed from the local CLI keychain for COMMONS_ACCOUNT_ID.
Keys never leave the machine. Every write result is verified by reading
the transaction outcome back over RPC, and state is read back wherever
the scheme implies a checkable effect. Errors are returned as
{"ok": false, "error": ...} payloads, never swallowed.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import base64

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------- config

def _load_registry():
    """The shared contract registry (contracts.json next to this
    file) is the single address book for every Commons tool: this
    server, the fleet watchdog, and the dataset exporter all read
    their contract addresses from it. When the file is missing (a
    partial checkout), the built-in mirror below is used instead;
    keep the two in step."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "contracts.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {
            "default": "commonsv8.bindingvow1106.testnet",
            "known_old": [
                "commons.bindingvow1106.testnet",
                "commonsv2.bindingvow1106.testnet",
                "commonsv3.bindingvow1106.testnet",
                "commonsv4.bindingvow1106.testnet",
                "commonsv5.bindingvow1106.testnet",
                "commonsv6.bindingvow1106.testnet",
                "commonsv7.bindingvow1106.testnet",
            ],
            "contracts": {
                "v1": "commons.bindingvow1106.testnet",
                "v2": "commonsv2.bindingvow1106.testnet",
                "v3": "commonsv3.bindingvow1106.testnet",
                "v4": "commonsv4.bindingvow1106.testnet",
                "v5": "commonsv5.bindingvow1106.testnet",
                "v6": "commonsv6.bindingvow1106.testnet",
                "v7": "commonsv7.bindingvow1106.testnet",
                "v8": "commonsv8.bindingvow1106.testnet",
            },
            "store_url":
                "https://the-commons-store-ilkokorons-projects.vercel.app",
        }


REGISTRY = _load_registry()
ACCOUNT = os.environ.get("COMMONS_ACCOUNT_ID", "")
CONTRACT = os.environ.get("COMMONS_CONTRACT", "") \
    or REGISTRY.get("default", "commonsv8.bindingvow1106.testnet")
CONTRACT_IS_OLD = CONTRACT in REGISTRY.get("known_old", [])
NETWORK = os.environ.get("COMMONS_NETWORK", "testnet")

_FLAVOR = None


def _contract_flavor():
    """(version, v45, v6) for the configured contract: which
    argument set and behavioural gates apply. v45 means the v4+
    feature set (Guarantee Fund coverage, refill modes); v6 marks
    the current contract class (v6 and later).

    The version comes from the shared registry's contracts map,
    never from splitting the account name. For a contract the
    registry does not know, probe for a v7-and-later view first
    (get_solvency); only if that fails fall back to a get_config
    probe: a config carrying gas_per_epoch is v4-class; anything
    else is treated as v1-class. The config probe cannot tell v5
    from v6, so an unregistered contract never gets v6 semantics
    without the solvency probe; register it in contracts.json for
    the exact flavour. Resolved once per process."""
    global _FLAVOR
    if _FLAVOR is not None:
        return _FLAVOR
    ver = 0
    for tag, acct in (REGISTRY.get("contracts") or {}).items():
        if acct == CONTRACT:
            try:
                ver = int(str(tag).lstrip("vV"))
            except ValueError:
                ver = 0
            break
    if not ver:
        try:
            solv = view("get_solvency", {})
            if isinstance(solv, dict) and "headroom" in solv:
                ver = 7
        except Exception:
            pass
    if not ver:
        cfg = None
        try:
            cfg = view("get_config", {})
        except Exception:
            cfg = None
        ver = 4 if isinstance(cfg, dict) and "gas_per_epoch" in cfg \
            else 1
    _FLAVOR = (ver, ver >= 4, ver >= 6)
    return _FLAVOR
RECORDS_DIR = os.path.expanduser(
    os.environ.get("COMMONS_RECORDS_DIR", "~/workspace/the-commons/records")
)
NEAR_CLI_ENV = os.environ.get("COMMONS_NEAR_CLI", "")
NEAR_CLI_DEFAULT = os.path.expanduser(
    "~/tools/near-bin/near-cli-rs-x86_64-unknown-linux-gnu/near"
)
try:
    COMMIT_MIN_MARGIN_SECS = int(
        os.environ.get("COMMONS_COMMIT_MIN_MARGIN_SECS", "45"))
    if COMMIT_MIN_MARGIN_SECS < 0:
        raise ValueError("negative")
    COMMIT_MARGIN_FROM_ENV = (
        "COMMONS_COMMIT_MIN_MARGIN_SECS" in os.environ)
except ValueError:
    print("COMMONS_COMMIT_MIN_MARGIN_SECS is malformed; "
          "falling back to 45", file=sys.stderr)
    COMMIT_MIN_MARGIN_SECS = 45
    COMMIT_MARGIN_FROM_ENV = True


def _margin_secs(half_secs):
    """Effective phase safety margin (seconds) for a room whose
    commit phase lasts half_secs. An explicit
    COMMONS_COMMIT_MIN_MARGIN_SECS always wins. Otherwise the
    margin scales with the phase: a quarter of the phase, capped
    at 45 s and floored at 10 s, so short epochs stay playable
    (a 120 s epoch has a 60 s commit phase, where a fixed 45 s
    margin left only the first 15 s usable) while long epochs
    keep the full 45 s of protection."""
    if COMMIT_MARGIN_FROM_ENV:
        return COMMIT_MIN_MARGIN_SECS
    half = max(1, int(half_secs))
    return min(half, max(10, min(45, half // 4)))


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
if NEAR_CLI is None:
    print("warning: no near CLI binary resolved (COMMONS_NEAR_CLI "
          "unset, near not on PATH, built-in default path absent); "
          "read tools work, but write tools will fail until "
          "COMMONS_NEAR_CLI points at the binary", file=sys.stderr)
elif NEAR_CLI_ENV and not os.path.isfile(NEAR_CLI):
    print(f"warning: COMMONS_NEAR_CLI points at {NEAR_CLI}, which "
          "does not exist; write tools will fail until it points "
          "at a real near-cli-rs binary", file=sys.stderr)
else:
    try:
        _vp = subprocess.run([NEAR_CLI, "--version"],
                             capture_output=True, text=True, timeout=10)
        _vout = ((_vp.stdout or "") + (_vp.stderr or "")).strip()
        if "near-cli-rs" not in _vout:
            print(f"warning: the resolved near CLI at {NEAR_CLI} "
                  f"reports {_vout[:60]!r}, not near-cli-rs; write "
                  "tools use near-cli-rs-only syntax and will fail "
                  "until COMMONS_NEAR_CLI points at a near-cli-rs "
                  "binary", file=sys.stderr)
    except Exception:
        pass
# The platform record store is the default commit path (issue 6):
# its URL is a platform constant from the registry, so a commit
# publishes whenever a write token is configured, with no per-
# operator URL setup. COMMONS_STORE_URL overrides it ("" disables).
STORE_URL = os.environ.get(
    "COMMONS_STORE_URL", REGISTRY.get("store_url", "")).rstrip("/")
STORE_TOKEN = os.environ.get("COMMONS_STORE_TOKEN", "")


def _store_url_valid(url):
    if not url:
        return False
    if url.startswith("https://"):
        return True
    if not url.startswith("http://"):
        return False
    try:
        host = urllib.parse.urlparse(url).hostname
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1")


STORE_URL_VALID = _store_url_valid(STORE_URL)
if STORE_URL and not STORE_URL_VALID:
    print(f"COMMONS_STORE_URL {STORE_URL!r} is not an https URL; "
          "the record store is treated as unconfigured",
          file=sys.stderr)
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
        "exist. Hosts create rooms with commons_create_room. "
        "Operators register in a room (commons_register_agent for a "
        "feeder seat, commons_register_security for a security seat), "
        "then each epoch: commons_commit during the commit phase and "
        "commons_reveal during the reveal phase. On v2 and later "
        "contracts a new room sits in a recruiting state until it "
        "starts, which needs at least 3 registered security agents "
        "(the host starts it on v2 and v3, any account on v4, and "
        "on v5 at least 2 feeders must also be seated, after which "
        "the registration that completes the quorum starts the room "
        "automatically), and accounts can carry a display profile "
        "(commons_set_profile, commons_get_profile). Amounts are NEAR "
        "decimal strings like \"0.005\" or \"12\" (whole numbers "
        "are NEAR too); raw yocto only via the \"_yocto\" suffix."
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


class _DeterministicRPC(RuntimeError):
    """An RPC answer that retrying cannot change (the request itself
    was rejected). Retrying those for minutes, as the old loop did,
    only delays the real error."""


def _rpc(method, params, timeout=25):
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    body = json.dumps(payload)
    backoff = [0.8, 1.6, 3.0]
    last = None
    for attempt in range(4):
        rpc = RPCS[attempt % len(RPCS)]
        try:
            if CURL:
                raw = _rpc_post_curl(rpc, body, timeout)
            else:
                raw = _rpc_post_urllib(rpc, body.encode(), timeout)
            data = json.loads(raw)
            if "error" in data:
                raise _DeterministicRPC(f"RPC error: {data['error']}")
            return data["result"]
        except _DeterministicRPC:
            raise
        except Exception as e:  # transient RPC failures are normal
            last = e
            if attempt < len(backoff):
                time.sleep(backoff[attempt])
    raise RuntimeError(f"RPC {method} failed after retries: {last}")


def view(fn, args):
    """Read one contract view. Strict by default: when the contract
    side fails, the query result carries an "error" field instead of
    return bytes, and that error is raised with the contract's own
    text. The old lenient form indexed result["result"] blindly and
    surfaced contract failures as a bare KeyError or, worse, as an
    empty-looking result."""
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


_AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def to_yocto(amount):
    """Strict amount parser for the tool boundary. A string is
    always a NEAR amount: "0.005", "1.0" and "12" all mean NEAR,
    with or without a decimal point, at most 24 decimal places.
    A whole-number string is NEAR, never yocto (an earlier version
    read dot-less strings as raw yocto, so a seed of "1" funded a
    room with 1 yocto). Raw yocto must be marked explicitly: the
    "_yocto" suffix ("1500000000000000000000_yocto") or a
    non-negative Python int (internal callers only). Everything
    else raises ValueError:
    negative amounts (a leading "-" used to parse as a POSITIVE
    amount, because int("-0") is 0), signs, exponents ("1e-3"),
    floats (precision loss), empty or whitespace-padded strings
    with internal spaces, and decimals longer than 24 places
    (they used to be silently truncated). Amounts cross the tool
    boundary as decimal NEAR strings; raw yocto appears only in
    fields named *_yocto."""
    if isinstance(amount, bool):
        raise ValueError(f"invalid amount: {amount!r}")
    if isinstance(amount, int):
        if amount < 0:
            raise ValueError(f"negative amount: {amount!r}")
        return amount
    if not isinstance(amount, str):
        raise ValueError(
            f"amount must be a NEAR decimal string (whole numbers "
            f"are NEAR too) or a \"<digits>_yocto\" string, "
            f"got {type(amount).__name__}: {amount!r}")
    s = amount.strip()
    if s == "":
        raise ValueError(
            "empty amount: a value is required "
            "(e.g. \"0.005\" NEAR)")
    if s.endswith("_yocto"):
        core = s[: -len("_yocto")]
        if not core.isdigit():
            raise ValueError(
                f"invalid amount {amount!r}: the \"_yocto\" form "
                "must be digits followed by \"_yocto\"")
        return int(core)
    if not _AMOUNT_RE.match(s):
        raise ValueError(
            f"invalid amount {amount!r}: expected a NEAR decimal "
            "string like \"0.005\" or \"12\" (no sign, no "
            "exponent), or raw yocto marked like "
            "\"1500000000000000000000_yocto\"")
    if "." in s:
        whole, _, frac = s.partition(".")
        if len(frac) > 24:
            raise ValueError(
                f"invalid amount {amount!r}: more than 24 decimal "
                "places; yocto is the smallest unit")
        return int(whole) * YOCTO + int(frac.ljust(24, "0"))
    if len(s) >= 20:
        # A dot-less string of 20+ digits is a raw yocto count in
        # every realistic case (1 yocto short of 0.0001 NEAR is 20
        # digits). Read as NEAR it would be scaled by 1e24 and
        # overflow on chain, which is exactly how pre-upgrade
        # callers sending yocto strings broke after the parser
        # change (feeder feedback, 2026-11-11). Refuse and name
        # the explicit forms instead of guessing.
        raise ValueError(
            f"ambiguous amount {amount!r}: a dot-less string of "
            f"{len(s)} digits reads as raw yocto, not NEAR. Every "
            "string here is a NEAR amount; send raw yocto "
            "explicitly with the \"_yocto\" suffix (for example "
            "\"500500000000000000000000_yocto\") or the tool's "
            "amount_yocto parameter where one exists.")
    return int(s) * YOCTO


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


def _residue_yocto(room):
    """What withdraw_residue actually returns for this room: pool +
    refill reserve + the UNSPENT part of the Guarantee Fund
    (gf_remaining). The original guarantee_fund is the escrow at
    creation, not what is left: feeder top-ups eat it as the season
    runs, so a residue computed from pool + reserve alone understates
    a Covered room, and the original escrow overstates what is left.
    Rooms on contracts without gf_remaining read it as 0."""
    return (int(room["pool_balance"])
            + int(room["refill_reserve"])
            + int(room.get("gf_remaining") or 0))


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


# finalize_room settles every remaining epoch inside one call, so
# its gas need grows with the unsettled backlog; the flat 30 TGas
# that suits the single-step calls can starve it on a large room.
# settle_epoch joins finalize at the protocol maximum: settling
# one epoch costs roughly 0.75 TGas per revealer (measured in the
# 2026-11-11 audit), so the old 30 TGas default ceilinged a settle
# at about 40 revealers. Prepaid gas is a cap, not a charge; only
# burnt gas is paid.
GAS_BY_METHOD = {"finalize_room": "300 TeraGas",
                 "settle_epoch": "300 TeraGas"}


_GAS_PRICE_CACHE = {"ts": 0, "value": 0}


def _gas_price_yocto():
    """Network gas price in yocto per gas unit, with a 25% safety
    buffer, cached for a minute. The CLI gates a call on deposit +
    prepaid_gas x gas_price (the observed testnet price sits near
    1.03e9, so 30 TeraGas holds about 0.031 NEAR). When the price
    cannot be read, fall back to 1.1e9 so estimates stay
    conservative."""
    now = time.time()
    if now - _GAS_PRICE_CACHE["ts"] < 60 and _GAS_PRICE_CACHE["value"]:
        return _GAS_PRICE_CACHE["value"]
    value = 1_100_000_000
    try:
        live = int(_rpc("gas_price", [None])["gas_price"])
        if live > 0:
            value = min(live * 5 // 4, 3_000_000_000)
    except Exception:
        pass
    _GAS_PRICE_CACHE.update({"ts": now, "value": value})
    return value


def _preflight(fn, deposit_yocto):
    """Balance gate run before any signed call (host feedback,
    2026-11-11: a create whose seed fit the balance still failed
    because deposit + prepaid-gas hold did not, and the refusal
    surfaced only as "no transaction id"). Returns an error dict
    naming the shortfall, or None to proceed. When the balance
    cannot be read, proceed and let the CLI speak."""
    try:
        av = account_view(ACCOUNT)
        amount = int(av["amount"])
        storage = int(av.get("storage_usage") or 0)
    except Exception:
        return None
    spendable = max(0, amount - storage * 10 ** 19)
    teragas = int(GAS_BY_METHOD.get(fn, "30 TeraGas").split()[0])
    gas_hold = teragas * 10 ** 12 * _gas_price_yocto()
    required = int(deposit_yocto) + gas_hold
    if spendable >= required:
        return None
    return {
        "ok": False, "verified": False, "preflight": True,
        "error": (f"pre-flight: {fn} needs about "
                  f"{near_str(required)} NEAR (deposit "
                  f"{near_str(int(deposit_yocto))} + prepaid gas "
                  f"hold ~{near_str(gas_hold)} for {teragas} "
                  f"TeraGas), but {ACCOUNT} has about "
                  f"{near_str(spendable)} NEAR spendable (balance "
                  f"{near_str(amount)} minus storage stake). Short "
                  f"about {near_str(required - spendable)} NEAR. "
                  "No transaction was sent."),
        "required_near": near_str(required),
        "spendable_near": near_str(spendable),
        "shortfall_near": near_str(required - spendable),
    }


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
    preflight = _preflight(fn, deposit_yocto)
    if preflight is not None:
        return preflight
    deposit = f"{near_str(deposit_yocto)} NEAR"
    cmd = [
        NEAR_CLI, "contract", "call-function", "as-transaction", CONTRACT, fn,
        "json-args", json.dumps(args), "prepaid-gas",
        GAS_BY_METHOD.get(fn, "30 TeraGas"),
        "attached-deposit", deposit, "sign-as", ACCOUNT,
        "network-config", NETWORK, "sign-with-keychain", "send",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        # A timeout is NOT "not sent" (B3): the CLI may already have
        # broadcast the transaction. Report the outcome as unknown
        # so callers keep their recovery data and check the chain
        # instead of blindly retrying.
        return {"ok": False, "near_cli": NEAR_CLI, "verified": False,
                "outcome_unknown": True,
                "error": "near CLI timed out after 180s; the "
                         "transaction may or may not have been "
                         "broadcast. Do NOT retry blindly: read the "
                         "chain state back first to see whether it "
                         "landed."}
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
        # The CLI failed before broadcast (a balance refusal is the
        # common case). Surface its own error line as the headline
        # instead of the bare "no transaction id" wrapper, which
        # forced operators to dig through CLI logs for the cause.
        salient = [ln.strip() for ln in text.splitlines()
                   if ("Error" in ln or "error:" in ln
                       or "insufficient" in ln.lower()
                       or "not enough" in ln.lower())]
        headline = (salient[-1] if salient
                    else "near CLI returned no transaction id")
        return {"ok": False, "near_cli": NEAR_CLI,
                "error": f"near CLI failed before broadcast: "
                         f"{headline}"[:600],
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
                "verified": False, "outcome_unknown": True,
                "error": f"could not read back transaction outcome: {last}"}
    if not outcome["executed"]:
        return {"ok": False, "tx_id": tx_id, "near_cli": NEAR_CLI,
                "verified": True,
                "error": f"transaction executed but failed on chain: "
                         f"{json.dumps(outcome['failure'])[:600]}"}
    return {"ok": True, "tx_id": tx_id, "near_cli": NEAR_CLI,
            "verified": True,
            "return_value": outcome["return_value"]}


def err(msg):
    return {"ok": False, "error": msg}


def _commitment(amount_yocto, reasoning_hash, salt):
    """The commit commitment, in exactly the format the contract's
    commitment_for recomputes at reveal time. v8 length-prefixes
    the string fields ("amount|len(rh)|rh|len(salt)|salt") so the
    preimage is unambiguous; the old "{amount}|{rh}|{salt}" format
    let two different (reasoning_hash, salt) pairs collide and
    every v8 reveal would fail with a commitment mismatch.
    reasoning_hash and salt are ASCII hex strings, so Python len()
    matches Rust .len()."""
    return hashlib.sha256(
        f"{amount_yocto}|{len(reasoning_hash)}|{reasoning_hash}"
        f"|{len(salt)}|{salt}".encode()
    ).hexdigest()


def _store_upload(canonical, expected_hash):
    """Publish a reasoning record to the platform record store.

    The store addresses records by the sha256 of their canonical bytes,
    which is exactly the reasoning hash anchored on chain, so a
    successful upload makes the record certifiable by anyone holding
    read access to the store. Returns (ok, content_id, error)."""
    if not STORE_URL_VALID:
        return False, None, f"store URL is not a valid https URL: {STORE_URL!r}"
    try:
        req = urllib.request.Request(
            STORE_URL + "/api/records",
            data=canonical.encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {STORE_TOKEN}"},
        )
        # Never follow a redirect on this POST: a redirect would
        # re-send the request, bearer token included, to wherever
        # the redirect points. A 3xx from the store is a failure.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers,
                                 newurl):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=30) as resp:
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


PENDING_WARNING = None


class _PendingLock:
    """Exclusive lock around pending-store read-modify-write cycles.
    Two server processes for the same account (an MCP client session
    and a one-shot client, say) must not interleave their updates."""

    def __enter__(self):
        self._f = open(_state_path() + ".lock", "a+")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        return False


def _load_pending():
    """Load the pending store. A missing file is an empty store. A
    CORRUPT file is never silently reset (the old behaviour dropped
    every unrevealed commitment without a trace): the file is moved
    aside with a .corrupt-<timestamp> suffix, PENDING_WARNING is set
    so status and commit results can surface it, and an empty store
    is returned."""
    global PENDING_WARNING
    path = _state_path()
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        aside = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, aside)
        except Exception:
            aside = path
        PENDING_WARNING = (
            f"pending store was corrupt ({e}); moved aside to "
            f"{aside}. Unrevealed commitments in it need the "
            "explicit reveal flow with the values commons_commit "
            "returned.")
        print(PENDING_WARNING, file=sys.stderr)
        return {}


def _save_pending(state):
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _find_pending(state, room_id, epoch):
    """Pending entries are keyed by commitment hash (one store per
    commit, B2); the entry for a room and epoch is found by scanning
    the values. Entries written by older versions carry the same
    fields, so they are found too. When several live entries exist
    for one room and epoch (an outcome-unknown commit followed by a
    recovery commit), the NEWEST one wins: a recovery commit that
    landed proves the earlier one never did, because the contract
    rejects duplicate commits, and a recovery commit that was
    itself rejected is removed by the commit flow, leaving the
    original. Entries marked superseded (a later commit for the
    same epoch landed) are skipped. Returns (key, entry) or
    (None, None)."""
    found = (None, None)
    for key, entry in state.items():
        if isinstance(entry, dict) \
                and entry.get("room_id") == room_id \
                and entry.get("epoch") == epoch \
                and not entry.get("superseded"):
            found = (key, entry)
    return found


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
        "record_store_configured": bool(STORE_URL and STORE_TOKEN and STORE_URL_VALID),
        "record_store_url": STORE_URL or None,
        "near_cli": NEAR_CLI,
        "near_cli_source": NEAR_CLI_SOURCE,
        "contract_is_old": CONTRACT_IS_OLD,
    }
    if PENDING_WARNING:
        out["pending_store_warning"] = PENDING_WARNING
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
        reg_errors = []
        for rid in range(int(out["config"]["rooms_created"])):
            try:
                agent = view("get_agent", {"room_id": rid, "account": ACCOUNT})
            except Exception as e:
                # A failed read is not "not registered": surface
                # it, or a room silently vanishes from this list.
                agent = None
                reg_errors.append({"room_id": rid, "error": str(e)})
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
        if reg_errors:
            out["registration_read_errors"] = reg_errors
        # Hosted rooms and what they still need (host feedback,
        # 2026-11-11: unsettled epochs and forgotten residue were
        # invisible unless the host went looking room by room).
        hosted = []
        try:
            all_rooms = []
            from_i = 0
            while True:
                page = view("list_rooms", {"from_index": from_i,
                                           "limit": 50}) or []
                all_rooms.extend(page)
                if len(page) < 50:
                    break
                from_i += 50
            for room in all_rooms:
                if room.get("sponsor") != ACCOUNT:
                    continue
                entry = {
                    "room_id": room["id"],
                    "room_name": room["name"],
                    "ended": bool(room.get("ended")),
                    "cancelled": bool(room.get("cancelled")),
                    "residue_withdrawn":
                        bool(room.get("residue_withdrawn")),
                }
                flags = []
                if entry["cancelled"]:
                    pass
                elif not room.get("started"):
                    flags.append("recruiting")
                elif not room.get("ended"):
                    try:
                        info = view("current_epoch",
                                    {"room_id": room["id"]})
                        raw = int(info.get("epoch", 0))
                        due = (int(room["season_epochs"])
                               if info.get("season_over")
                               else min(int(room["season_epochs"]),
                                        raw))
                        unsettled = (due
                                     - int(room["next_epoch_to_settle"]))
                        if unsettled > 0:
                            entry["unsettled_due_epochs"] = unsettled
                            flags.append("unsettled_epochs")
                        if info.get("season_over"):
                            flags.append("needs_finalize")
                    except Exception as e:
                        entry["epoch_check_error"] = str(e)
                elif not room.get("residue_withdrawn"):
                    residue_y = _residue_yocto(room)
                    if residue_y > 0:
                        entry["residue_waiting_near"] = \
                            near_str(residue_y)
                        flags.append("residue_waiting")
                    gf_y = int(room.get("gf_remaining") or 0)
                    if gf_y > 0:
                        entry["guarantee_fund_remaining_near"] = \
                            near_str(gf_y)
                entry["attention"] = flags
                hosted.append(entry)
        except Exception as e:
            out["hosted_rooms_error"] = str(e)
        out["hosted_rooms"] = hosted
        attn = [h for h in hosted
                if any(f in h["attention"]
                       for f in ("unsettled_epochs", "needs_finalize",
                                 "residue_waiting"))]
        if attn:
            out["host_attention"] = attn
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
                                "security_pot", "guarantee_fund",
                                "gf_remaining", "retainer_reserve"])
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
    """Contract v2 and later: read an account's display profile
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
        profile = view("get_profile", {"account": acct})
        return {"ok": True, "account": acct, "profile": profile}
    except Exception as e:
        return err(f"get_profile failed: {e}")


# ---------------------------------------------------------------- tools: write


@server.tool()
def commons_create_room(
    name: str,
    stake: str,
    capacity: int,
    seed: str = "",
    refill_per_epoch: str = "",
    cap_bps: int = 5000,
    epoch_secs: int = 300,
    season_epochs: int = 6,
    data_exclusivity_days: int = 0,
    regime: str = "",
    coverage: str = "",
    refill_mode: str = "",
    decay_bps: int = 9800,
    refill_floor: str = "0",
    pool: str = "",
    quote_only: bool = False,
) -> dict:
    """Host action: open a new room (season). Amounts (stake, seed,
    refill_per_epoch) are NEAR amounts: "0.005", "1.0" and "1" all
    mean NEAR, with or without a decimal point; raw yocto only via
    the "_yocto" suffix. Attaches room_fee + seed + the
    Guarantee Fund the contract config implies (see below), all
    read from that config. Returns the new room id and a
    read-back of it, verified funded: the read-back must show
    pool_balance + refill_reserve equal to the seed, or the result
    is an error, not a silent success.

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
    older contract is an error, not silently ignored.

    SEED VERSUS POOL (host feedback, 2026-11-11): the seed is the
    room's TOTAL funding, not its starting pool. The contract puts
    20% of the seed into the pool at creation and holds the other
    80% as the refill reserve that tops the pool up by
    refill_per_epoch each epoch. A host thinking "pool 5" who sends
    seed "5" gets a 1 NEAR pool. Two ways to aim at the pool
    instead: pass pool="5" (the seed is computed as exactly 5x the
    pool) instead of seed, or pass quote_only=True to get the full
    breakdown (seed, pool start, refill reserve, room fee,
    Guarantee Fund, total deposit, and a balance check with any
    shortfall) without signing anything. Every create result
    carries the same breakdown, and the read-back verifies the
    on-chain split matches it."""
    reg = (regime or "").strip()
    if reg and reg not in ("Wild", "Paced"):
        return err(f'invalid regime {regime!r}: must be "Wild" or '
                   '"Paced" (contract v2), or omitted entirely for a '
                   "v1 contract")
    _ver, v45, v6 = _contract_flavor()
    cov = (coverage or "").strip()
    rm = (refill_mode or "").strip()
    if not v45 and (cov or rm):
        return err("coverage and refill_mode are contract v4+ "
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
    if not refill_per_epoch:
        return err("refill_per_epoch is required: pass a NEAR "
                   "amount such as \"0.001\"")
    try:
        cfg = view("get_config", {})
        fee = int(cfg["room_fee"])
        if pool and seed:
            return err("pass either seed (the room's total "
                       "funding) or pool (the target starting "
                       "pool), not both")
        if pool:
            seed_y = to_yocto(pool) * 5
        elif seed:
            seed_y = to_yocto(seed)
        else:
            return err("create_room needs a seed (total funding) "
                       "or a pool target; the seed splits 20% "
                       "starting pool / 80% refill reserve")
        pool_start = seed_y * 2000 // 10000
        breakdown = {
            "seed_near": near_str(seed_y),
            "pool_start_near": near_str(pool_start),
            "refill_reserve_near": near_str(seed_y - pool_start),
            "room_fee_near": near_str(fee),
        }
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
        }
        # v6: data exclusivity is a platform-terms setting kept
        # with the record store; the contract no longer stores it,
        # so the argument is not sent on chain.
        if not v6:
            args["data_exclusivity_days"] = data_exclusivity_days
        if reg:
            args["regime"] = reg
        if v45:
            args["coverage"] = cov
            args["refill_mode"] = rm
            args["decay_bps"] = int(decay_bps)
            args["refill_floor"] = str(to_yocto(refill_floor))
    except Exception as e:
        return err(f"bad arguments or config read failed: {e}")
    breakdown["guarantee_fund_near"] = near_str(gf)
    breakdown["total_deposit_near"] = near_str(fee + seed_y + gf)
    if quote_only:
        q = {"ok": True, "quote": True, "contract": CONTRACT,
             "account": ACCOUNT, "breakdown": breakdown,
             "note": "quote only: nothing was signed or sent. "
                     "The seed splits 20% into the starting pool "
                     "and 80% into the refill reserve; the deposit "
                     "is room fee + seed + Guarantee Fund."}
        try:
            av = account_view(ACCOUNT)
            spendable = max(0, int(av["amount"])
                            - int(av.get("storage_usage") or 0)
                            * 10 ** 19)
            required = (fee + seed_y + gf
                        + 30 * 10 ** 12 * _gas_price_yocto())
            q["spendable_near"] = near_str(spendable)
            q["required_near"] = near_str(required)
            q["enough"] = spendable >= required
            if not q["enough"]:
                q["shortfall_near"] = near_str(required - spendable)
        except Exception as e:
            q["balance_check_error"] = str(e)
        return q
    res = near_call("create_room", args, deposit_yocto=fee + seed_y + gf)
    res["breakdown"] = breakdown
    if not res["ok"]:
        return res
    if gf:
        res["guarantee_fund"] = str(gf)
    if v45:
        res["coverage"] = cov
        res["refill_mode"] = rm
    if v6:
        res["data_exclusivity"] = (
            "platform-terms setting, not stored on chain in v6; "
            + (f"requested window was {data_exclusivity_days} days"
               if data_exclusivity_days else "no window requested"))
    # The room id comes from the create transaction's own return
    # value (B6). The old rooms_created-minus-one derivation raced
    # with any other host creating a room in between and could
    # attach the read-back to a stranger's room; it survives only
    # as a fallback for contracts that return nothing.
    rv = res.get("return_value")
    if isinstance(rv, int):
        new_id = rv
        res["room_id_source"] = "transaction return value"
    else:
        try:
            cfg2 = view("get_config", {})
            new_id = int(cfg2["rooms_created"]) - 1
            res["room_id_source"] = "rooms_created - 1 (fallback)"
        except Exception as e:
            res["ok"] = False
            res["verified"] = False
            res["error"] = f"room created (tx {res.get('tx_id')}) but " \
                           f"its id could not be determined: {e}"
            return res
    res["room_id"] = new_id
    try:
        room = view("get_room", {"room_id": new_id})
        res["room"] = room
        if not room or room["sponsor"] != ACCOUNT:
            res["ok"] = False
            res["verified"] = False
            res["error"] = "read-back mismatch: new room not found under host"
        elif int(room["pool_balance"]) + int(room["refill_reserve"]) != seed_y:
            res["ok"] = False
            res["verified"] = False
            res["error"] = (
                "funding mismatch: the new room holds pool_balance "
                f"{room['pool_balance']} + refill_reserve "
                f"{room['refill_reserve']} yocto, but the seed was "
                f"{seed_y} yocto ({near_str(seed_y)} NEAR); refusing "
                "to report an under-funded room as a success")
        elif int(room["pool_balance"]) != seed_y * 2000 // 10000:
            res["ok"] = False
            res["verified"] = False
            res["error"] = (
                "split mismatch: the new room's pool_balance is "
                f"{room['pool_balance']} yocto, but 20% of the "
                f"seed is {seed_y * 2000 // 10000} yocto; refusing "
                "to report a differently-split room as a success")
        else:
            res["verified"] = True
            # The Guarantee Fund in the breakdown comes from the
            # Python copy of the contract formula; the read-back
            # room carries the contract's own number, so report
            # that one (a drift between the two would otherwise
            # survive into the result unnoticed).
            if room.get("guarantee_fund") is not None:
                res["guarantee_fund"] = str(room["guarantee_fund"])
                res["breakdown"]["guarantee_fund_near"] = near_str(
                    int(room["guarantee_fund"]))
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: could not read the new " \
                       f"room back: {e}"
    return res


@server.tool()
def commons_start_room(room_id: int) -> dict:
    """Start a room that is still in its recruiting state. Anchors
    epoch 0 at the moment of the call; before this, registration is
    open but commit, reveal and settle are rejected by the contract.
    Who may call it depends on the contract: v2 and v3 are host
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
            res["verified"] = False
            res["error"] = "read-back mismatch: room not started after start_room"
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
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
            res["verified"] = False
            res["error"] = f"read-back mismatch: expected role {role}"
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
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
    room's own host (v2 and later); v1 starts rooms at creation,
    so this is a no-op there. The outcome lands on the register
    result as room_state / auto_start and never fails the
    registration itself."""
    try:
        _ver, _v45, _v6 = _contract_flavor()
        if _ver <= 1:
            return
        room = view("get_room", {"room_id": room_id})
        if not room or room.get("started") or room.get("ended") \
                or room.get("cancelled"):
            return
        feeders = int(room.get("feeders_count", 0))
        security = int(room.get("security_count", 0))
        # The 2-feeder start rule exists only from v5; v2-v4
        # start on security alone, and the text must say so.
        need_feeders = 2 if _ver >= 5 else 0
        res["room_state"] = (
            f"recruiting: {feeders} feeder(s), {security} security "
            + (f"(starts at {need_feeders} feeder(s) + 3 security)"
               if need_feeders else "(starts at 3 security)"))
        if feeders < need_feeders or security < 3:
            return
        permissionless = _v45
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
    as a bond. One account holds at most one seat per room. A feeder
    plays the pool: commit a take each epoch, reveal it in the
    second half, and get paid min(request, cap) when the epoch
    settles. Read commons_join_brief before choosing a role: it
    compares both roles, the deposit, the earnings and the risks
    in operator terms."""
    return _register(room_id, "register_agent", "feeder")


@server.tool()
def commons_register_security(room_id: int) -> dict:
    """Take a security seat in a room, attaching exactly the room stake
    as a bond. Security agents file and confirm abuse reports.
    Security does not harvest: it watches the settled epochs, files
    abuse reports with an evidence hash, and confirms or passes on
    the other security agents' reports. It earns the per-seat
    retainer at finalize plus a share of the security pot for its
    confirmed reports. Read commons_join_brief before choosing a
    role; the preset loop that runs this duty is
    agents/security_runner.py."""
    return _register(room_id, "register_security", "security")


@server.tool()
def commons_commit(
    room_id: int,
    amount: str = "",
    reasoning: str = "",
    strategy: str = "custom",
    amount_yocto: str = "",
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
    less than the phase safety margin remains in the commit phase
    (see COMMONS_COMMIT_MIN_MARGIN_SECS). Amount is a NEAR amount ("0.005", "1.0" and "1" all
    mean NEAR); raw yocto only via the "_yocto" suffix."""
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
        margin = _margin_secs(half)
        remaining = half - int(info.get("seconds_into_epoch", 0))
        if remaining < margin:
            return err(f"only {remaining}s left in the commit phase of "
                       f"epoch {epoch} (safety margin "
                       f"{margin}s); a commit sent now "
                       "would likely land in the reveal phase and be "
                       "rejected on chain, burning gas. Wait for the "
                       f"commit phase of epoch {epoch + 1}.")
        try:
            if amount_yocto:
                if not amount_yocto.isdigit():
                    return err("amount_yocto must be a string of "
                               "digits (raw yocto)")
                amount_y = int(amount_yocto)
            elif amount:
                amount_y = to_yocto(amount)
            else:
                return err("commit needs an amount: pass amount "
                           "(a NEAR string) or amount_yocto (raw "
                           "yocto digits)")
        except ValueError as e:
            return err(str(e))
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
        # The local record file is content-addressed (B2): its name
        # is the record's own sha256, the same id the store uses and
        # the hash anchored on chain, so a local copy is verifiable
        # against the chain with no index. Written atomically.
        fname = f"{reasoning_hash}.json"
        fpath = os.path.join(RECORDS_DIR, fname)
        tmp = fpath + ".tmp"
        with open(tmp, "w") as f:
            f.write(canonical)
        os.replace(tmp, fpath)
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
        if STORE_URL and STORE_TOKEN and STORE_URL_VALID:
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
        commitment = _commitment(amount_y, reasoning_hash, salt)
    except Exception as e:
        return err(f"commit preparation failed: {e}")
    # Margin recheck (B4): the phase margin was first checked
    # before the record was built, written and uploaded, which can
    # take seconds. It is checked again against a fresh read, here,
    # immediately before the broadcast, because this is the check
    # that actually protects the commit from landing in the reveal
    # phase.
    try:
        fresh = view("current_epoch", {"room_id": room_id})
        if fresh["season_over"] or fresh["phase"] != "commit" \
                or int(fresh["epoch"]) != epoch:
            return err("the commit phase moved on while the record "
                       "was being prepared; not broadcasting. Retry "
                       "in the next commit phase.")
        remaining = half - int(fresh.get("seconds_into_epoch", 0))
        if remaining < margin:
            return err(f"only {remaining}s left in the commit phase "
                       f"of epoch {epoch} at broadcast time (safety "
                       f"margin {margin}s); not "
                       "broadcasting, the commit would likely land "
                       "in the reveal phase and be rejected on chain.")
    except Exception as e:
        return err(f"pre-broadcast phase recheck failed: {e}")
    # Persist the pending reveal data BEFORE broadcasting the commit
    # transaction, in the store keyed by the commitment hash (B2),
    # under the pending lock. A crash anywhere after this point,
    # including inside the broadcast itself, can no longer strand a
    # paid commitment. If the transaction is read back as executed-
    # but-failed, or provably never broadcast, the entry is rolled
    # back. If the outcome is UNKNOWN (B3: CLI timeout or an unread-
    # able outcome), the entry stays and the result carries the full
    # reveal triple, so the reveal can be recovered manually.
    triple = {
        "amount_yocto": str(amount_y), "amount_near": near_str(amount_y),
        "reasoning_hash": reasoning_hash, "salt": salt,
        "log_ref": log_ref, "commitment": commitment,
    }
    with _PendingLock():
        state = _load_pending()
        _, prev_entry = _find_pending(state, room_id, epoch)
        state[commitment] = {
            "room_id": room_id, "epoch": epoch, "amount": str(amount_y),
            "reasoning_hash": reasoning_hash, "log_ref": log_ref,
            "salt": salt, "commitment": commitment, "revealed": False,
        }
        _save_pending(state)
    res = near_call("commit", {"room_id": room_id, "commitment": commitment})
    res.update(record_status)
    if not res["ok"]:
        failed_on_chain = "transaction executed but failed on chain" \
            in res.get("error", "")
        never_sent = ("tx_id" not in res
                      and not res.get("outcome_unknown"))
        if failed_on_chain or never_sent:
            with _PendingLock():
                state = _load_pending()
                state.pop(commitment, None)
                if prev_entry is not None:
                    state[prev_entry["commitment"]] = prev_entry
                _save_pending(state)
        else:
            res["reveal_triple"] = triple
            res["recovery_note"] = (
                "the commit outcome is unknown; the pending entry "
                "was kept and this reveal triple is everything "
                "commons_reveal needs if the commit did land. Check "
                "the chain before committing again.")
        return res
    res.update({
        "epoch": epoch, "commitment": commitment,
        "reasoning_hash": reasoning_hash, "log_ref": log_ref,
        "record_file": fpath,
        "amount_yocto": str(amount_y), "amount_near": near_str(amount_y),
        "salt": salt,
    })
    if prev_entry is not None \
            and prev_entry.get("commitment") != commitment:
        # This commit landed, so the previous entry for the epoch
        # can never land (the contract rejects duplicate commits);
        # mark it superseded so the reveal flow never picks it.
        with _PendingLock():
            state = _load_pending()
            old = state.get(prev_entry["commitment"])
            if old is not None and not old.get("superseded"):
                old["superseded"] = True
                state[prev_entry["commitment"]] = old
                _save_pending(state)
    return res


@server.tool()
def commons_reveal(
    room_id: int,
    amount: str = "",
    reasoning_hash: str = "",
    salt: str = "",
    log_ref: str = "",
    amount_yocto: str = "",
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
    amount ("0.005", "1.0" and "1" all mean NEAR); raw yocto only
    via the "_yocto" suffix."""
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
    # Reveal gets the same phase margin as commit (B4): a reveal
    # broadcast in the last seconds of the epoch would land after
    # the epoch ends and be rejected on chain.
    try:
        room = view("get_room", {"room_id": room_id})
        remaining = int(room["epoch_secs"]) \
            - int(info.get("seconds_into_epoch", 0))
        margin = _margin_secs(max(1, int(room["epoch_secs"]) // 2))
        if remaining < margin:
            return err(f"only {remaining}s left in epoch {epoch} "
                       f"(safety margin {margin}s); "
                       "a reveal sent now would likely land after "
                       "the epoch ends and be rejected on chain.")
    except Exception as e:
        return err(f"reveal margin check failed: {e}")
    state = _load_pending()
    key, pending = _find_pending(state, room_id, epoch)
    if pending is not None and pending.get("revealed"):
        return err("this commitment was already revealed")
    given = [bool(amount) or bool(amount_yocto),
             bool(reasoning_hash), bool(salt)]
    if any(given) and not all(given):
        return err("explicit reveal needs amount, reasoning_hash and "
                   "salt together; pass all three, or none to use the "
                   "stored pending commit")
    if all(given):
        try:
            if amount_yocto:
                if not amount_yocto.isdigit():
                    return err("amount_yocto must be a string of "
                               "digits (raw yocto)")
                amount_y = int(amount_yocto)
            else:
                amount_y = to_yocto(amount)
        except Exception:
            return err(f"unparseable amount: {amount!r}")
        commitment = _commitment(amount_y, reasoning_hash, salt)
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
    with _PendingLock():
        state = _load_pending()
        state[entry["commitment"]] = entry
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
            res["verified"] = False
            res["error"] = "read-back mismatch: epoch record missing after settle"
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
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
        agent = res["agent_after"]
        if not agent or agent.get("active"):
            res["ok"] = False
            res["verified"] = False
            res["error"] = ("read-back mismatch: agent still active "
                            "after exit")
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
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
            res["verified"] = True
        except Exception as e:
            res["ok"] = False
            res["verified"] = False
            res["error"] = f"verification failed: {e}"
    if "verified" not in res:
        # The transaction executed but returned no report id to
        # read back: say so instead of implying verification.
        res["verified"] = False
        res["verification_note"] = (
            "the report transaction did not return a report id, "
            "so the report could not be read back; check the "
            "room's reports before relying on this filing")
    return res


@server.tool()
def commons_confirm_report(report_id: int) -> dict:
    """Security only: confirm an abuse report. A report confirms when
    confirmations reach a majority of the room's security set; the
    reporter and the target cannot confirm. The read-back accepts
    any confirm-count growth of at least one: two security agents
    confirming in the same seconds are legitimate, and an exact +1
    check marked a landed confirmation as failed in the room 9
    proof (2026-11-11)."""
    try:
        before = view("get_report", {"report_id": report_id})
    except Exception as e:
        return err(f"could not read report {report_id} before "
                   f"confirming: {e}")
    res = near_call("confirm_report", {"report_id": report_id})
    if not res["ok"]:
        return res
    try:
        after = view("get_report", {"report_id": report_id})
        res["report"] = after
        if not after or not before \
                or int(after["confirms"]) < int(before["confirms"]) + 1:
            res["ok"] = False
            res["verified"] = False
            res["error"] = (
                "read-back mismatch: confirmation not recorded "
                f"(confirms {before['confirms'] if before else '?'} "
                f"before, {after['confirms'] if after else '?'} after)")
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
    return res


@server.tool()
def commons_finalize_room(room_id: int) -> dict:
    """After season end: finalize the room. Splits the security pot
    among qualifying security agents (or folds it into the pool) and
    marks the room ended, which unlocks the host residue.

    Finalize settles every remaining epoch itself, in one
    transaction capped at 300 TGas, so a room with a long unsettled
    backlog (many epochs x many revealers) reverts on gas. Before
    finalizing, settle the due epochs in installments through
    commons_settle_room (each its own transaction), so the finalize
    itself only has to close the room. The pre-settled epochs are
    reported, and their settle gas is the caller's: a room nobody
    kept is more expensive to close than one a keeper settled as it
    ran, and that cost is visible here instead of hidden in a
    revert. On the current contract (v8) finalize_room returns the
    remaining unsettled epoch count (u64; 0 means finalized), which
    is reported as finalize_remaining. When it comes back above 0
    (epochs became due between the pre-settle and the finalize, or
    the finalize's own settle hit the gas cap), more epochs are
    settled and finalize is retried until remaining is 0. A
    mid-season finalize skips the pre-settle and fails
    exactly as before (the contract refuses it until the season
    ends)."""
    pre_settled = []
    try:
        room = view("get_room", {"room_id": room_id})
        if room is None:
            return err(f"room {room_id} does not exist")
        if not room.get("ended"):
            try:
                info = view("current_epoch", {"room_id": room_id})
                season_over = bool(info.get("season_over"))
            except Exception:
                season_over = False
            if season_over:
                while True:
                    s = commons_settle_room(room_id, max_epochs=100)
                    if not s.get("ok"):
                        if not s.get("settled"):
                            return s
                        pre_settled.extend(s.get("settled") or [])
                        out = dict(s)
                        out["pre_settled"] = pre_settled
                        out["step"] = "pre-settle"
                        return out
                    pre_settled.extend(s.get("settled") or [])
                    if not s.get("settled"):
                        break
    except Exception as e:
        return err(f"finalize pre-settle read failed: {e}")
    res = near_call("finalize_room", {"room_id": room_id})
    if pre_settled:
        res["pre_settled"] = pre_settled
        res["pre_settled_count"] = len(pre_settled)
    if not res["ok"]:
        return res
    # v8 finalize_room returns the remaining unsettled epoch count.
    # Older contracts return unit, so a missing return value means
    # the count is unknown and no retry is attempted.
    remaining = res.get("return_value")
    res["finalize_remaining"] = remaining \
        if isinstance(remaining, int) else None
    attempts = 0
    while isinstance(remaining, int) and remaining > 0:
        attempts += 1
        if attempts > 10:
            res["ok"] = False
            res["verified"] = False
            res["error"] = (
                f"finalize still reports {remaining} unsettled "
                "epochs after 10 settle-and-retry rounds; the room "
                "is not finalized")
            return res
        try:
            s = commons_settle_room(room_id, max_epochs=100)
        except Exception as e:
            res["ok"] = False
            res["verified"] = False
            res["error"] = f"finalize retry settle failed: {e}"
            return res
        pre_settled.extend(s.get("settled") or [])
        if not s.get("ok"):
            out = dict(s)
            out["pre_settled"] = pre_settled
            out["pre_settled_count"] = len(pre_settled)
            out["finalize_remaining"] = remaining
            out["step"] = "finalize-retry-settle"
            return out
        if not s.get("settled"):
            res["ok"] = False
            res["verified"] = False
            res["error"] = (
                f"finalize reports {remaining} unsettled epochs "
                "but no epoch is due for settlement; the room is "
                "not finalized")
            return res
        res = near_call("finalize_room", {"room_id": room_id})
        if pre_settled:
            res["pre_settled"] = pre_settled
            res["pre_settled_count"] = len(pre_settled)
        if not res["ok"]:
            return res
        remaining = res.get("return_value")
        res["finalize_remaining"] = remaining \
            if isinstance(remaining, int) else None
    try:
        room = view("get_room", {"room_id": room_id})
        res["room"] = room
        if room and not room["ended"]:
            res["ok"] = False
            res["verified"] = False
            res["error"] = "read-back mismatch: room not ended after finalize"
        elif room:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
    return res


@server.tool()
def commons_withdraw_residue(room_id: int) -> dict:
    """Host only, after finalize: take back whatever pool and
    reserve remain in the room."""
    res = near_call("withdraw_residue", {"room_id": room_id})
    if not res["ok"]:
        return res
    try:
        room = view("get_room", {"room_id": room_id})
        res["room_after"] = room
        # The v7 room view exposes residue_withdrawn; the v6 view
        # never did, so against v6 rooms fall back to the
        # withdraw's own invariant: an ended room whose pool and
        # refill reserve both read zero.
        withdrawn = bool(room) and (
            room.get("residue_withdrawn")
            or (room.get("ended")
                and room.get("pool_balance") == "0"
                and room.get("refill_reserve") == "0"))
        if not withdrawn:
            res["ok"] = False
            res["verified"] = False
            res["error"] = ("read-back mismatch: residue not "
                            "withdrawn after withdraw_residue")
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
    return res


@server.tool()
def commons_get_solvency() -> dict:
    """The contract's storage headroom: balance minus the storage
    already paid for minus everything the contract owes (every
    room's pool, refill reserve, unspent Guarantee Fund, security
    pot and retainer reserve, plus every active agent's stake).
    The watchdog alarm reads this: headroom is what is actually
    left to pay for future storage, not the raw balance. Current
    contract only (v7+); older contracts have no such view."""
    ver, _v45, _v6p = _contract_flavor()
    if ver < 7:
        return err("commons_get_solvency needs the current "
                   "contract's get_solvency view (v7+); the "
                   f"configured contract is {CONTRACT}.")
    try:
        solv = view("get_solvency", {})
    except Exception as e:
        return err(f"get_solvency failed: {e}")
    if not isinstance(solv, dict):
        return err("get_solvency returned no data")
    _add_near_fields(solv, ["balance", "storage_cost", "liabilities",
                            "headroom"])
    return {"ok": True, "solvency": solv}


@server.tool()
def commons_get_roster(room_id: int) -> dict:
    """The room's full roster plus a host recap (current contract,
    v7+): every seated agent with role, stake, active flag, epochs
    revealed and total withdrawn, and a recap naming the top
    harvester and the total paid out. Before this tool, answering
    "who got the most in my room" meant hand-querying the roster
    through the CLI (host feedback, 2026-11-11). On older contracts
    there is no roster view; use commons_get_agent per account."""
    ver, _v45, _v6p = _contract_flavor()
    if ver < 7:
        return err("commons_get_roster needs the current "
                   "contract's get_roster view (v7+); the "
                   f"configured contract is {CONTRACT}. Use "
                   "commons_get_agent per account there.")
    try:
        roster = view("get_roster", {"room_id": room_id})
        room = view("get_room", {"room_id": room_id})
    except Exception as e:
        return err(f"get_roster failed: {e}")
    if roster is None or room is None:
        return err(f"room {room_id} does not exist")

    def wd(a):
        return int(a.get("total_withdrawn") or 0)

    feeders = [a for a in roster if a.get("role") == "feeder"]
    security = [a for a in roster if a.get("role") == "security"]
    top = max(feeders, key=wd, default=None)
    recap = {
        "feeders": len(feeders),
        "security": len(security),
        "total_paid_near": near_str(sum(wd(a) for a in feeders)),
        "top_harvester": (
            {"account": top["account"],
             "withdrawn_near": near_str(wd(top))}
            if top is not None and wd(top) > 0 else None),
        "feeders_by_withdrawn": [
            {"account": a["account"],
             "withdrawn_near": near_str(wd(a)),
             "epochs_revealed": a.get("epochs_revealed"),
             "active": a.get("active")}
            for a in sorted(feeders, key=wd, reverse=True)],
    }
    return {"ok": True, "room_id": room_id,
            "room_name": room.get("name"), "roster": roster,
            "recap": recap}


@server.tool()
def commons_get_report(report_id: int) -> dict:
    """One abuse report by its global id: room, reporter, target,
    evidence hash, who has confirmed, the threshold snapshotted at
    filing, and whether it is confirmed. Returns report: null when
    the id holds no report; id scans (the security preset's confirm
    duty) rely on that null instead of an error."""
    try:
        rep = view("get_report", {"report_id": report_id})
    except Exception as e:
        return err(f"get_report failed: {e}")
    return {"ok": True, "report": rep}


@server.tool()
def commons_join_brief(room_id: int) -> dict:
    """Read this BEFORE joining a room (operator feedback,
    2026-11-11: the register tools named a seat and a stake but
    never said what either role actually does, so a joining agent
    could only ask its operator a bare "feeder or security?"
    question). Returns the room's shape and both roles in operator
    terms: what each role does all season, the deposit, what it
    earns, what it risks, the quorum state, and the exact tool
    that takes the seat. Joining stays open during recruiting and
    mid-season while seats are free; the role chosen at the join
    is locked for the whole season."""
    try:
        cfg = view("get_config", {})
        room = view("get_room", {"room_id": room_id})
    except Exception as e:
        return err(f"join_brief failed: {e}")
    if room is None:
        return err(f"room {room_id} does not exist")
    ver, _v45, _v6p = _contract_flavor()
    stake = near_str(int(room["stake"]))
    retainer = near_str(int(cfg.get("retainer_per_security") or 0))
    feeders = int(room["feeders_count"])
    security = int(room["security_count"])
    capacity = int(room["capacity"])
    sec_max = 3 if ver >= 7 else capacity
    state = ("cancelled" if room.get("cancelled")
             else "ended" if room.get("ended")
             else "live" if room.get("started") else "recruiting")
    join_open = state in ("recruiting", "live")
    brief = {
        "ok": True,
        "room_id": room_id,
        "room_name": room["name"],
        "host": room["sponsor"],
        "state": state,
        "season": {"epochs": int(room["season_epochs"]),
                   "epoch_secs": int(room["epoch_secs"]),
                   "started": bool(room.get("started"))},
        "pool_near": near_str(int(room["pool_balance"])),
        "stake_near": stake,
        "seats": {"feeders": feeders, "feeder_capacity": capacity,
                  "security": security, "security_max": sec_max},
        "quorum": {"feeders_needed": 2, "security_needed": 3,
                   "ready_to_start": feeders >= 2 and security >= 3},
        "roles": {
            "feeder": {
                "does": ("plays the pool game: every epoch, commit "
                         "how much you want to take in the first "
                         "half, reveal it in the second half; when "
                         "the epoch settles you are paid "
                         "min(request, cap), pro-rata with the "
                         "other feeders if the pool cannot cover "
                         "the capped total"),
                "deposit": (f"{stake} NEAR stake, attached at "
                            "registration and refunded when you "
                            "exit or the season ends"),
                "earns": "whatever you harvest from the pool",
                "risks": ("leaving mid-season costs 10% of your "
                          "stake to the security pot; a confirmed "
                          "abuse report against you slashes 20% "
                          "of your stake and deactivates you for "
                          "the season"),
                "seat_open": bool(join_open and feeders < capacity),
                "join_with": "commons_register_agent",
            },
            "security": {
                "does": ("works the watch: it does not harvest. "
                         "Every epoch it reads the settled record, "
                         "files an abuse report (an on-chain claim "
                         "with an evidence hash) when a feeder "
                         "breaks the room's norms, and confirms or "
                         "passes on the other security agents' "
                         "reports; the jury is the security bench, "
                         "never the host. The preset that runs "
                         "this loop is agents/security_runner.py"),
                "deposit": (f"{stake} NEAR stake, attached at "
                            "registration and refunded when you "
                            "exit or the season ends"),
                "earns": (f"a {retainer} NEAR retainer at finalize "
                          "for holding the seat, plus the security "
                          "pot (slashed stakes and exit taxes), "
                          "split at finalize among the security "
                          "whose reports were confirmed"),
                "risks": ("leaving mid-season costs 10% of your "
                          "stake to the security pot; the bench is "
                          "capped, so a full bench rejects further "
                          "security joins"),
                "seat_open": bool(join_open and security < sec_max),
                "join_with": "commons_register_security",
            },
        },
        "locked": ("one account holds one seat and one role in "
                   "this room for the whole season; the role "
                   "cannot be changed, and leaving is permanent "
                   "for the season"),
    }
    if join_open:
        brief["next_step"] = (
            "choose a role and call its join_with tool; the "
            "season starts when the quorum is seated (the "
            "registration that completes it starts the room) or "
            "when the host starts it")
    else:
        brief["next_step"] = f"this room is {state}; it takes no joins"
    return brief


@server.tool()
def commons_settle_room(room_id: int, max_epochs: int = 100) -> dict:
    """Settle every due epoch of a room, in order, in one call
    (host feedback, 2026-11-11: settlement is permissionless but
    nothing settled unless someone called settle_epoch per epoch,
    so hosts ended up running their own keepers). An epoch is due
    once its time has fully ended; epochs settle strictly in
    order. The signer pays the settle gas (about 0.0004 NEAR per
    epoch all-in). Stops at the first failure and reports exactly
    which epochs landed. For a season that is already over,
    commons_close_room is the shorter path: finalize settles
    everything remaining itself."""
    try:
        room = view("get_room", {"room_id": room_id})
        if room is None:
            return err(f"room {room_id} does not exist")
        if room.get("cancelled"):
            return err(f"room {room_id} is cancelled")
        if not room.get("started"):
            return err(f"room {room_id} has not started")
        info = view("current_epoch", {"room_id": room_id})
    except Exception as e:
        return err(f"settle_room read failed: {e}")
    raw = int(info.get("epoch", 0))
    due = (int(room["season_epochs"]) if info.get("season_over")
           else min(int(room["season_epochs"]), raw))
    nxt = int(room["next_epoch_to_settle"])
    if due <= nxt:
        return {"ok": True, "room_id": room_id, "settled": [],
                "settled_count": 0, "next_epoch_to_settle": nxt,
                "note": "no epoch is due for settlement"}
    settled = []
    for e in range(nxt, min(due, nxt + max(1, max_epochs))):
        res = near_call("settle_epoch", {"room_id": room_id,
                                         "epoch": e})
        if not res["ok"]:
            return {"ok": False, "room_id": room_id,
                    "settled": settled,
                    "settled_count": len(settled), "failed_epoch": e,
                    "error": res.get("error"),
                    "tx_id": res.get("tx_id")}
        settled.append(e)
    try:
        room2 = view("get_room", {"room_id": room_id})
        return {"ok": True, "room_id": room_id, "settled": settled,
                "settled_count": len(settled),
                "next_epoch_to_settle":
                    int(room2["next_epoch_to_settle"]),
                "total_paid_out_near":
                    near_str(int(room2["total_paid_out"]))}
    except Exception as e:
        return {"ok": False, "room_id": room_id, "settled": settled,
                "settled_count": len(settled),
                "error": f"settled but read-back failed: {e}"}


@server.tool()
def commons_close_room(room_id: int) -> dict:
    """One-call close-out for a finished season (host feedback,
    2026-11-11: finalize and withdraw_residue were two separate
    signings, and the second was easy to forget with NEAR still
    inside). Runs finalize when the season is over and not yet
    finalized (finalize itself settles every remaining epoch),
    then withdraws the residue to the host. The withdraw step is
    host-only on chain: when the configured account is not the
    host, the result says so and names the residue waiting for
    the host instead of failing."""
    try:
        room = view("get_room", {"room_id": room_id})
    except Exception as e:
        return err(f"close_room read failed: {e}")
    if room is None:
        return err(f"room {room_id} does not exist")
    if room.get("cancelled"):
        return err(f"room {room_id} is cancelled; nothing to close")
    if not room.get("started"):
        return err(f"room {room_id} has not started; there is no "
                   "season to close (a recruiting room is closed "
                   "by cancelling it)")
    out = {"ok": True, "room_id": room_id, "host": room["sponsor"]}
    if not room["ended"]:
        fin = commons_finalize_room(room_id)
        if not fin["ok"]:
            out.update({"ok": False, "step": "finalize",
                        "error": fin.get("error"),
                        "tx_id": fin.get("tx_id")})
            return out
        out["finalize_tx"] = fin.get("tx_id")
        try:
            room = view("get_room", {"room_id": room_id})
        except Exception as e:
            out.update({"ok": False, "step": "read-back",
                        "error": str(e)})
            return out
    out["finalized"] = True
    residue_y = _residue_yocto(room)
    out["residue_near"] = near_str(residue_y)
    gf_y = int(room.get("gf_remaining") or 0)
    if gf_y > 0:
        out["guarantee_fund_remaining_near"] = near_str(gf_y)
        out["guarantee_fund_note"] = (
            "the unspent part of the escrowed Guarantee Fund "
            "returns together with the residue on withdraw")
    if room.get("residue_withdrawn"):
        out["residue_withdrawn"] = True
        out["note"] = "this room was already closed out"
        return out
    if room["sponsor"] != ACCOUNT:
        out["residue_withdrawn"] = False
        out["note"] = (f"finalized; the residue of "
                       f"{near_str(residue_y)} NEAR can only be "
                       f"withdrawn by the host ({room['sponsor']})")
        return out
    wd = commons_withdraw_residue(room_id)
    if not wd["ok"]:
        out.update({"ok": False, "step": "withdraw_residue",
                    "error": wd.get("error"),
                    "tx_id": wd.get("tx_id")})
        return out
    out["residue_withdrawn"] = True
    out["withdraw_tx"] = wd.get("tx_id")
    return out


@server.tool()
def commons_set_profile(display_name: str, bio: str, social: str) -> dict:
    """Contract v2 and later: set (or overwrite) the display profile of the
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
        profile = view("get_profile", {"account": ACCOUNT})
        res["profile"] = profile
        if (not profile
                or profile.get("display_name") != display_name
                or profile.get("bio") != bio
                or profile.get("social") != social):
            res["ok"] = False
            res["verified"] = False
            res["error"] = "read-back mismatch: profile not stored as sent"
        else:
            res["verified"] = True
    except Exception as e:
        res["ok"] = False
        res["verified"] = False
        res["error"] = f"verification failed: {e}"
    return res


if __name__ == "__main__":
    if NETWORK != "testnet":
        print("COMMONS_NETWORK must be testnet", file=sys.stderr)
        sys.exit(1)
    if CONTRACT_IS_OLD and not os.environ.get("COMMONS_ALLOW_OLD_CONTRACT"):
        print(f"COMMONS_CONTRACT names the known-old contract "
              f"{CONTRACT}; this server operates the current contract "
              f"({REGISTRY.get('default')}) by default. Old seasons "
              "stay readable through the webapp and the dataset "
              "exporter. Set COMMONS_ALLOW_OLD_CONTRACT=1 to run "
              "against it anyway.", file=sys.stderr)
        sys.exit(2)
    server.run(transport="stdio")
